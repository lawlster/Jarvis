"""Live watchlist scanning (Step 54).

The stored-data scanner (Step 25) can only judge tickers with
stored financial statements — in practice MSFT alone.  This
module judges the owner's 38-ticker watchlist from *live*
provider pulls instead: for each watchlist ticker it fetches
live financials (Yahoo via :func:`tools.finance.
get_company_financials`, which retries transient failures since
Step 54), computes a 10-year DCF range on the latest annual
free cash flow, and applies the Step 23 margin-of-safety tier
and the Step 26 detection bands to those live-derived values.

Live data is used IN MEMORY for the scan only.  It is NEVER
written into the production database and never overwrites
stored statements.  A per-ticker failure never aborts the run.

Honest limits, stated on every result:

- The DCF is a single-method FCF DCF (latest annual FCF, WACC
  11%, terminal growth 2.5%, growth scenarios 5% / 10% / 15%
  for conservative / base / optimistic) — the same basis the
  capability scan used.  It is a research figure, not a
  fair-value estimate and never a recommendation.
- Business quality (Step 24) and deterioration (Step 27) can
  NOT be assessed from a live pull alone — they need stored
  statements.  Both are recorded as unknown, and the
  ``detection_band`` is therefore a *margin band only*:
  ``strongest_alert`` here means the price is at least 20%
  below the live-derived conservative value, NOT a confirmed
  Step 26 candidate.  A confirmed candidate needs the
  stored-data quality and deterioration gates.  Banded records
  carry ``provisional: True``.
- Banks, insurers and REITs need the Step 22 sector models —
  an FCF DCF is not valid for them, so they come back
  ``insufficient_data`` when the live pull has no usable free
  cash flow (which is the usual case for banks).
- Prices are the live pull's as-of timestamp, not the stored
  2026-11-03 snapshot.
"""

import time

from datetime import datetime, timezone

from agents import margin_of_safety as mos
from agents.watchlist_foundation import (
    list_tickers,
    load_watchlist,
)
from tools.valuation import calculate_dcf

# DCF basis (same as the Step 21 wrapper and the capability
# scan's live DCFs): 10 years, WACC 11%, terminal growth 2.5%.
DEFAULT_WACC = 0.11
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_YEARS = 10
GROWTH_SCENARIOS = {
    "conservative": 0.05,
    "base": 0.10,
    "optimistic": 0.15,
}

# Pause between live fetches so the provider is not hammered;
# 38 tickers at the default take ~40s of pauses plus fetch time.
DEFAULT_RATE_LIMIT_SECONDS = 1.0

# Singapore tickers are stored under their SGX codes; Yahoo
# wants the .SI suffix.  US tickers are used as-is (a dot in
# the owner's spelling becomes a hyphen for Yahoo).
_SGX_TICKERS = {"D05", "O39", "U11"}

BAND_STRONGEST_ALERT = "strongest_alert"
BAND_WATCH_DISCOUNT = "watch_discount"
BAND_WATCH_BELOW_BASE = "watch_below_base"

DCF_BASIS_TEXT = (
    "10-year FCF DCF on the latest annual free cash flow, "
    "WACC 11%, terminal growth 2.5%, growth 5% / 10% / 15% "
    "for conservative / base / optimistic")


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def yahoo_symbol(entry):
    """Yahoo symbol for a watchlist entry (or a bare ticker)."""
    if isinstance(entry, dict):
        symbol = entry.get("universe_ticker") or entry.get("ticker")
    else:
        symbol = str(entry)
    symbol = (symbol or "").strip().upper().replace(".", "-")
    if symbol in _SGX_TICKERS:
        return symbol + ".SI"
    return symbol


def _latest_value(values):
    """Latest value of a provider field.

    Provider fields are either a number (e.g. shares
    outstanding) or a dict keyed by ISO period (e.g. free cash
    flow by fiscal year end) — the latest key wins.  Returns
    (value, period) with period None for a bare number, or
    (None, None) when there is nothing usable.
    """
    if values is None:
        return None, None
    if isinstance(values, dict):
        if not values:
            return None, None
        period = max(values.keys())
        try:
            return float(values[period]), str(period)
        except (TypeError, ValueError):
            return None, None
    try:
        return float(values), None
    except (TypeError, ValueError):
        return None, None


def dcf_range(live, wacc=DEFAULT_WACC,
              terminal_growth=DEFAULT_TERMINAL_GROWTH,
              years=DEFAULT_YEARS):
    """10-year DCF range from a live financials pull.

    Returns (range_dict, notes) where range_dict is
    {"conservative": v, "base": v, "optimistic": v} in the
    pull's currency, or (None, [reasons]) when no range can be
    computed.  Net debt is latest total debt minus latest cash;
    a missing debt or cash component is treated as zero and
    noted.  Never invents a value.
    """
    notes = []
    fcf, fcf_period = _latest_value(live.get("free_cash_flow"))
    if fcf is None:
        return None, [
            "no free cash flow in the live pull (banks, insurers "
            "and REITs need the Step 22 sector models — an FCF "
            "DCF is not valid for them)"]
    if fcf <= 0:
        return None, [
            f"latest annual free cash flow is not positive "
            f"({fcf:,.0f} for {fcf_period or 'the latest period'})"]
    debt, _ = _latest_value(live.get("total_debt"))
    cash, _ = _latest_value(live.get("cash"))
    if debt is None:
        notes.append("no total debt in the live pull — treated as 0")
        debt = 0.0
    if cash is None:
        notes.append("no cash in the live pull — treated as 0")
        cash = 0.0
    net_debt = debt - cash
    shares, _ = _latest_value(live.get("shares_outstanding"))
    if shares is None or shares <= 0:
        return None, ["no shares outstanding in the live pull"]
    fair_value_range = {}
    for name, growth in GROWTH_SCENARIOS.items():
        result = calculate_dcf(
            fcf, growth, wacc, terminal_growth, years,
            net_debt, shares)
        if not isinstance(result, dict) or "error" in result:
            return None, [
                f"DCF failed for the {name} scenario: "
                f"{result.get('error') if isinstance(result, dict) else result}"]
        value = result.get("intrinsic_value_per_share")
        if value is None:
            return None, [
                f"DCF returned no per-share value for the {name} "
                "scenario"]
        fair_value_range[name] = value
    return fair_value_range, notes


def _margin_band(margin_conservative, margin_base):
    """Step 26 detection band from the margins alone.

    Mirrors :func:`agents.undervalued_detector._watch_band`
    plus the strongest-alert threshold
    (:data:`agents.margin_of_safety.STRONGEST_ALERT_MARGIN`):
    ``strongest_alert`` when the margin vs conservative value
    is at least 20%, ``watch_discount`` when the price is below
    conservative value by less than 20%, ``watch_below_base``
    when the price is below base value but above conservative
    value, else None.  This is a margin band only — see the
    module docstring: quality and deterioration are unknown
    from live data, so a band is provisional, never a
    confirmed Step 26 candidate.
    """
    if margin_conservative is None or margin_base is None:
        return None
    if margin_conservative >= mos.STRONGEST_ALERT_MARGIN:
        return BAND_STRONGEST_ALERT
    if 0.0 <= margin_conservative < mos.STRONGEST_ALERT_MARGIN:
        return BAND_WATCH_DISCOUNT
    if margin_conservative < 0.0 and margin_base >= 0:
        return BAND_WATCH_BELOW_BASE
    return None


def _deterioration_unknown():
    """Deterioration verdict for a live scan: unknown.

    Step 27 needs stored statements; a live pull alone cannot
    assess deterioration.  The Step 23 tier therefore blocks
    any strongest alert (see :func:`agents.margin_of_safety.
    classify_tier`), and the record says so.
    """
    return {
        "deteriorated": None,
        "unknown": True,
        "signals": [],
        "cautions": [
            "live scan: deterioration not assessed — no stored "
            "statements for this ticker"],
    }


def scan_ticker_live(ticker, fetcher=None, yahoo_symbol_override=None,
                     wacc=DEFAULT_WACC,
                     terminal_growth=DEFAULT_TERMINAL_GROWTH):
    """Scan one ticker from a live pull.

    ``fetcher`` is a callable taking a Yahoo symbol and
    returning a :func:`tools.finance.get_company_financials`
    dict; the default fetches live (with the Step 54 retry).
    Returns a per-ticker record with ``status`` one of ``ok``,
    ``insufficient_data`` or ``provider_unavailable``.  A
    fetcher exception is recorded as ``provider_unavailable``,
    never raised — one ticker never aborts a run.
    """
    if fetcher is None:
        from tools.finance import get_company_financials
        fetcher = get_company_financials
    symbol = yahoo_symbol_override or yahoo_symbol(ticker)
    record = {
        "ticker": ticker,
        "yahoo_symbol": symbol,
        "status": "insufficient_data",
        "detection_band": None,
        "provisional": False,
        "deterioration_unknown": True,
        "quality_unknown": True,
        "dcf_basis": DCF_BASIS_TEXT,
    }
    try:
        live = fetcher(symbol)
    except Exception as exc:  # noqa: BLE001 - one ticker must
        # never abort a run; record the failure and move on.
        record["status"] = "provider_unavailable"
        record["reason"] = f"live fetch raised: {exc}"
        return record
    if not isinstance(live, dict):
        record["reason"] = "live fetch returned no usable result"
        return record
    record["company_name"] = live.get("company_name")
    record["sector"] = live.get("sector")
    record["currency"] = live.get("currency")
    # Step 58: the reporting currency of the statements, so a
    # stored scan can later be checked for a currency mismatch.
    record["financial_currency"] = live.get("financial_currency")
    freshness = live.get("data_freshness") or {}
    record["price_as_of"] = freshness.get("price_data_as_of_utc")
    record["retrieved_at"] = freshness.get("retrieved_at_utc")
    if live.get("provider_unavailable"):
        record["status"] = "provider_unavailable"
        record["reason"] = (
            live.get("provider_error") or live.get("error")
            or "provider could not be reached")
        return record
    if not live.get("data_available"):
        record["reason"] = (
            "no live financial data for this ticker "
            "(provider returned data_available False — genuine "
            "no-data, not a provider failure)")
        return record
    price = live.get("current_price")
    if price is None:
        record["reason"] = "no live price in the pull"
        return record
    try:
        price = float(price)
    except (TypeError, ValueError):
        record["reason"] = "live price is not a number"
        return record
    if price <= 0:
        record["reason"] = "live price is not positive"
        return record
    record["price"] = price
    fair_value_range, notes = dcf_range(
        live, wacc=wacc, terminal_growth=terminal_growth)
    record["dcf_notes"] = notes
    if fair_value_range is None:
        record["reason"] = "; ".join(notes)
        return record
    record["fair_value_range"] = fair_value_range
    fcf, fcf_period = _latest_value(live.get("free_cash_flow"))
    record["latest_fcf"] = fcf
    record["latest_fcf_period"] = fcf_period
    debt, _ = _latest_value(live.get("total_debt"))
    cash, _ = _latest_value(live.get("cash"))
    record["net_debt"] = (
        (debt or 0.0) - (cash or 0.0))
    shares, _ = _latest_value(live.get("shares_outstanding"))
    record["shares_outstanding"] = shares
    margins = {
        name: mos.margin_of_safety(value, price)
        for name, value in fair_value_range.items()
    }
    record["margins"] = margins
    record["margin_of_safety_vs_conservative"] = margins["conservative"]
    record["margin_of_safety_vs_base"] = margins["base"]
    record["margin_of_safety_vs_optimistic"] = margins["optimistic"]
    record["strongest_alert_price"] = mos.price_at_margin(
        fair_value_range["conservative"], mos.STRONGEST_ALERT_MARGIN)
    tier = mos.classify_tier(
        margins["conservative"], margins["base"],
        margins["optimistic"], _deterioration_unknown(),
        quality=None)
    record["tier"] = tier["tier"]
    record["tier_reasons"] = tier["reasons"]
    record["strongest_alert_blocked"] = tier["strongest_alert_blocked"]
    record["conditions_to_change"] = tier["conditions_to_change"]
    band = _margin_band(margins["conservative"], margins["base"])
    record["detection_band"] = band
    if band is not None:
        # Margin band only: quality and deterioration are
        # unknown from live data, so this is provisional.
        record["provisional"] = True
        record["band_note"] = (
            f"margin band '{band}' from the live-derived values "
            "only — business quality and deterioration are "
            "unknown from a live pull, so this is NOT a "
            "confirmed Step 26 candidate")
    record["status"] = "ok"
    return record


def scan_watchlist_live(store_dir=None, tickers=None, fetcher=None,
                        rate_limit_seconds=DEFAULT_RATE_LIMIT_SECONDS,
                        sleep=None, wacc=DEFAULT_WACC,
                        terminal_growth=DEFAULT_TERMINAL_GROWTH,
                        as_of=None):
    """Scan the watchlist from live pulls.

    ``tickers`` (owner spellings) selects a subset; the whole
    watchlist is scanned by default.  ``fetcher`` is passed to
    :func:`scan_ticker_live`.  ``sleep`` is injectable for
    tests and defaults to :func:`time.sleep`; the rate-limit
    pause runs between fetches, never before the first.  Live
    data is used in memory only — nothing is written to the
    database or to the watchlist store.  Returns {"results",
    "summary", "scanned_at", "method", "limitations"}.
    """
    if sleep is None:
        sleep = time.sleep
    entries = {e["ticker"]: e for e in load_watchlist(store_dir)}
    wanted = list(tickers) if tickers is not None else list_tickers(
        store_dir)
    scanned_at = as_of or _utcnow_iso()
    results = []
    for index, ticker in enumerate(wanted):
        if index > 0 and rate_limit_seconds:
            sleep(rate_limit_seconds)
        entry = entries.get(ticker)
        symbol = yahoo_symbol(entry) if entry is not None \
            else yahoo_symbol(ticker)
        results.append(scan_ticker_live(
            ticker, fetcher=fetcher, yahoo_symbol_override=symbol,
            wacc=wacc, terminal_growth=terminal_growth))
    by_band = {}
    for record in results:
        band = record.get("detection_band")
        if band:
            by_band[band] = by_band.get(band, 0) + 1
    summary = {
        "selected": len(results),
        "scannable": sum(
            1 for r in results if r["status"] == "ok"),
        "candidates": sum(
            1 for r in results if r.get("detection_band")),
        "by_band": by_band,
        "strongest_alerts": by_band.get(BAND_STRONGEST_ALERT, 0),
        "watch": by_band.get(BAND_WATCH_DISCOUNT, 0)
                 + by_band.get(BAND_WATCH_BELOW_BASE, 0),
        "provider_unavailable": sum(
            1 for r in results
            if r["status"] == "provider_unavailable"),
        "insufficient_data": sum(
            1 for r in results
            if r["status"] == "insufficient_data"),
    }
    return {
        "results": results,
        "summary": summary,
        "scanned_at": scanned_at,
        "method": {
            "dcf_basis": DCF_BASIS_TEXT,
            "wacc": wacc,
            "terminal_growth": terminal_growth,
            "years": DEFAULT_YEARS,
            "growth_scenarios": dict(GROWTH_SCENARIOS),
            "starting_fcf": "latest annual free cash flow "
                            "from the live pull",
            "price": "live pull (price_data_as_of_utc)",
            "fetcher": "Yahoo Finance via tools/finance.py "
                       "(retried since Step 54)",
        },
        "limitations": [
            "Live data is used in memory only — it is never "
            "written to the database and never overwrites stored "
            "statements.",
            "Detection bands are margin bands only and "
            "provisional: business quality (Step 24) and "
            "deterioration (Step 27) are unknown from a live "
            "pull, so no band is a confirmed Step 26 candidate.",
            "Banks, insurers and REITs need the Step 22 sector "
            "models — an FCF DCF is not valid for them, so they "
            "usually come back insufficient_data.",
            "A single-method FCF DCF is a research figure, not "
            "a fair-value estimate and never a recommendation; "
            "the final decision is always the owner's.",
        ],
    }


def briefing_line(record):
    """One line for a banded (candidate) record."""
    band = record.get("detection_band") or "no band"
    price = record.get("price")
    price_text = f"${price:,.2f}" if isinstance(price, (int, float)) \
        else "no price"
    fair_value_range = record.get("fair_value_range") or {}
    range_text = (
        f"${fair_value_range.get('conservative', 0):,.2f} / "
        f"${fair_value_range.get('base', 0):,.2f} / "
        f"${fair_value_range.get('optimistic', 0):,.2f}"
        if fair_value_range else "no range")
    margin = record.get("margin_of_safety_vs_conservative")
    margin_text = f"{margin:.1%} vs conservative" \
        if isinstance(margin, (int, float)) else "no margin"
    return (
        f"{record['ticker']}: {band} (provisional) — {price_text} "
        f"(as of {record.get('price_as_of') or 'unknown'}), DCF "
        f"{range_text}, {margin_text}. Worth a closer look, "
        "not a recommendation.")


def summary_lines(result):
    """Text lines for a live-scan result (main.py, reports)."""
    summary = result["summary"]
    lines = [
        f"Live watchlist scan — {result['scanned_at'][:10]} "
        f"({summary['selected']} tickers, "
        f"{summary['scannable']} scannable, "
        f"{summary['candidates']} candidate(s), "
        f"{summary['strongest_alerts']} strongest alert(s), "
        f"{summary['watch']} watch, "
        f"{summary['provider_unavailable']} provider-unavailable, "
        f"{summary['insufficient_data']} insufficient data)",
        f"Basis: {result['method']['dcf_basis']}.",
    ]
    candidates = [r for r in result["results"]
                  if r.get("detection_band")]
    if candidates:
        lines.append("Candidates (margin bands, provisional — "
                     "quality and deterioration unknown from "
                     "live data):")
        lines.extend(f"- {briefing_line(r)}" for r in candidates)
    else:
        lines.append("No candidate found on the live-derived "
                     "values — this is a coverage statement, not "
                     "a claim that no undervalued company exists.")
    unavailable = [r for r in result["results"]
                   if r["status"] == "provider_unavailable"]
    if unavailable:
        lines.append("Provider-unavailable (not retried further "
                     "in this run): " + ", ".join(
                         r["ticker"] for r in unavailable))
    lines.extend(result["limitations"])
    return lines
