"""Step 58 — Live fallback for valuation/research when stored data is missing.

The stored-data valuation path (Steps 21/22/23) can only value
tickers with stored statements — in practice MSFT alone.  Asked
about NVDA it answered "I do not have enough data to value it"
plus the Step 22 sector-unknown warning, even though the Step 54
live scan already holds NVDA's live DCF ($57.55 / $82.19 /
$117.66 vs $186.64).  This module is the seam between the two:
when the stored path returns insufficient_data, the orchestrator
(and the Step 41 research session) fall back to the Step 54 live
machinery for that one ticker.

Rules, all enforced here and nowhere else:

- One ticker per question, and only for a ticker on the Step 35
  watchlist or explicitly named by the owner in the question —
  never an arbitrary universe scan.
- The stored answer is never replaced: the fallback only fills
  the gap when the stored path has none (MSFT still answers
  from stored statements, unchanged).
- A fresh stored live-scan result (``step54-live-scan.json``) is
  preferred over a new fetch; a live pull happens only when
  there is no stored result, it is stale, or the owner asks for
  a fresh one (``force_live``).
- Exclusions, checked BEFORE any DCF is compared with a price:
  banks / insurers / REITs (and the ambiguous "Financials")
  never get an FCF DCF — the Step 22 refusal reason is returned
  instead; a reporting currency that differs from the price
  currency is a currency mismatch and is refused (the KSPI case:
  statements in Kazakhstani tenge, price in US dollars).  When
  the reporting currency cannot be established from the pull, a
  magnitude guard refuses any margin above 95% vs the
  conservative value as a suspected currency or scale mismatch.
- Every live-derived answer is labelled: live pull or stored
  scan with its run timestamp, price and as-of date, the DCF
  basis, the tier and band, and that the band is provisional
  because business quality and deterioration are unknown from
  a live pull.  A live band is never a confirmed Step 26
  candidate.
- Live data is used in memory only — nothing is written to the
  production database and no stored statement is overwritten.
  A fetch failure returns provider_unavailable or
  insufficient_data with the reason stated, never an invented
  figure.

The reporting currency comes from the provider: Yahoo's
``financialCurrency`` (the currency of the financial
statements) is surfaced as ``financial_currency`` by
``data/providers/yahoo_provider.py`` and passed through by
``tools/finance.py``; ``agents/live_watchlist_scan.py`` records
it on the per-ticker record, so future stored scans carry it.
Stored scans written before Step 58 have no such field, so the
magnitude guard is their only currency protection — the reply
says so.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from agents import live_watchlist_scan as lws
from agents import sector_valuation as sv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A stored live-scan result older than this is treated as no
# stored result: a live pull is made instead.
MAX_STORED_AGE_DAYS = 7

# A margin vs the conservative value above this is not a
# plausible discount for a listed company — it is a suspected
# currency or scale mismatch (KSPI's was 99.8%), so the value
# is refused rather than presented.
EXTREME_MARGIN = 0.95

# Canonical sectors that must never receive an FCF DCF.
BANK_LIKE_SECTORS = ("bank", "insurance", "reit", "financial")

LIVE_SOURCE_LABEL = (
    "agents/live_fallback.py (Step 58, live-derived value — "
    "provisional)")


def _normalise_ticker(ticker):
    return (ticker or "").strip().upper().replace(".", "-")


def _parse_iso(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _fmt_money(value):
    if value is None:
        return "unknown"
    return f"${value:,.2f}"


# ---------------------------------------------------------------------------
# Stored live-scan reuse (the cheap path)
# ---------------------------------------------------------------------------

def load_stored_scan(live_scan_path=None):
    """(data, error) for the stored Step 54 live-scan JSON.

    An explicit path wins; otherwise the file is looked for in
    the repo root, then next to the project directory (the
    Step 55 dashboard searches the same places).  Read-only,
    never raises — a missing or unreadable file returns
    (None, reason).
    """
    candidates = []
    if live_scan_path:
        candidates.append(Path(live_scan_path))
    else:
        candidates.append(PROJECT_ROOT / "step54-live-scan.json")
        candidates.append(
            PROJECT_ROOT.parent / "step54-live-scan.json")
    for path in candidates:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"Could not read {path}: {exc}"
        if isinstance(data, dict) and isinstance(
                data.get("results"), list):
            return data, None
        return None, f"{path} is not a live-scan file."
    return None, "No stored live scan found."


def stored_record_for(ticker, live_scan_path=None,
                      max_age_days=MAX_STORED_AGE_DAYS):
    """(record, reason) — a fresh stored Step 54 record for the ticker.

    Returns (None, reason) when there is no stored scan, no
    result for the ticker, the stored result is not usable, or
    the scan is older than ``max_age_days`` — in each case the
    caller falls through to a live pull.
    """
    wanted = _normalise_ticker(ticker)
    data, error = load_stored_scan(live_scan_path)
    if data is None:
        return None, error or "No stored live scan found."
    scanned_at = _parse_iso(data.get("scanned_at"))
    if scanned_at is None:
        return None, ("The stored live scan has no usable run "
                      "timestamp, so its age cannot be checked.")
    age_days = (datetime.now(timezone.utc) - scanned_at
                ).total_seconds() / 86400.0
    if age_days > max_age_days:
        return None, (
            f"The stored live scan is {age_days:.0f} days old "
            f"(run {data.get('scanned_at')}), older than the "
            f"{max_age_days}-day freshness limit.")
    for record in data.get("results") or []:
        if _normalise_ticker(record.get("ticker")) == wanted:
            if record.get("status") != "ok":
                return None, (
                    f"The stored live scan has no usable result "
                    f"for {ticker} ({record.get('status')}: "
                    f"{record.get('reason') or 'no reason recorded'}).")
            return record, None
    return None, f"The stored live scan has no result for {ticker}."


# ---------------------------------------------------------------------------
# Exclusion checks — run BEFORE any DCF is compared with a price
# ---------------------------------------------------------------------------

def _refusal_for(sector, industry, financial_currency,
                 price_currency):
    """(refusal_reason or None, warnings) for a live-derived value.

    Banks / insurers / REITs (and ambiguous "Financials") never
    get an FCF DCF — the Step 22 refusal reason is returned.
    A reporting currency that differs from the price currency
    is a currency mismatch (the KSPI case).  An unknown sector
    is allowed, with the Step 22 warning carried so the reply
    says the sector was never established.
    """
    warnings = []
    canonical = sv.normalise_sector(sector, industry)
    if canonical in BANK_LIKE_SECTORS:
        return sv.REFUSAL_REASONS[("fcf_dcf", canonical)], warnings
    if canonical == "unknown":
        warnings.append(sv.WHY_MODEL["unknown"])
    if (financial_currency and price_currency
            and str(financial_currency).strip().upper()
            != str(price_currency).strip().upper()):
        return (
            f"Currency mismatch: the financial statements are "
            f"reported in {financial_currency} but the price is "
            f"in {price_currency}, so a per-share DCF in one "
            f"currency cannot be compared with a price in the "
            f"other (the KSPI case — its statements are in "
            f"Kazakhstani tenge while the price is in US "
            f"dollars). No figure is estimated."), warnings
    return None, warnings


def _magnitude_refusal(record):
    """Refusal when the discount is too large to be plausible.

    The reporting currency could not always be established
    (stored scans written before Step 58 carry no such field),
    so a margin above EXTREME_MARGIN vs the conservative value
    is refused as a suspected currency or scale mismatch rather
    than presented as a candidate.
    """
    margin = record.get("margin_of_safety_vs_conservative")
    if isinstance(margin, (int, float)) and margin > EXTREME_MARGIN:
        return (
            f"Suspected currency or scale mismatch: the "
            f"live-derived conservative value is more than "
            f"{1 / (1 - EXTREME_MARGIN):.0f}x the price "
            f"(margin {margin:.1%} vs the conservative value), "
            f"which is not a plausible discount for a listed "
            f"company. No figure is estimated — verify the "
            f"reporting currency before relying on this.")
    return None


# ---------------------------------------------------------------------------
# The fallback
# ---------------------------------------------------------------------------

def _yahoo_symbol_for(ticker, store_dir):
    """Yahoo symbol for a ticker, honouring the watchlist mapping."""
    if store_dir is not None:
        try:
            from agents.watchlist_foundation import load_watchlist
            for entry in load_watchlist(store_dir):
                if _normalise_ticker(
                        entry.get("ticker")) == ticker:
                    return lws.yahoo_symbol(entry)
        except Exception:  # noqa: BLE001 - a broken store must
            # not stop the fallback; the bare ticker is used.
            pass
    return lws.yahoo_symbol(ticker)


def _result(ticker, status, source=None, record=None, reason=None,
            warnings=None, scanned_at=None):
    return {
        "ticker": ticker,
        "status": status,
        "source": source,
        "record": record,
        "reason": reason,
        "warnings": list(warnings or []),
        "scanned_at": scanned_at,
    }


def live_valuation(ticker, store_dir=None, fetcher=None,
                   live_scan_path=None, force_live=False,
                   explicitly_named=False,
                   max_stored_age_days=MAX_STORED_AGE_DAYS,
                   cache=None):
    """One-ticker live valuation for the fallback.

    Returns a dict with ``status`` ("ok" | "insufficient_data" |
    "provider_unavailable"), ``source`` ("stored_scan" |
    "live_pull" | None), ``record`` (the Step 54 per-ticker
    record, or None), ``reason`` and ``warnings``.  The stored
    scan is preferred; a live pull happens only when there is
    no fresh stored result or ``force_live`` is set.  ``fetcher``
    is a callable taking a Yahoo symbol and returning a
    :func:`tools.finance.get_company_financials` dict; None
    means the real Yahoo fetch (with the Step 54 retry).
    ``cache`` (a dict, caller-owned) memoises results by
    ticker within one session so the scan and valuation steps
    of a research session share one pull.  Never raises for a
    fetch failure, and never writes to the database.
    """
    ticker = _normalise_ticker(ticker)
    if cache is not None and ticker in cache:
        return cache[ticker]

    def _finish(result):
        if cache is not None:
            cache[ticker] = result
        return result

    # Gate: only watchlist tickers or explicitly named ones.
    if store_dir is not None and not explicitly_named:
        try:
            from agents.watchlist_foundation import list_tickers
            on_watchlist = ticker in [
                _normalise_ticker(t) for t in list_tickers(store_dir)]
        except Exception:  # noqa: BLE001 - a broken store is
            # treated as "not on the watchlist" for the gate.
            on_watchlist = False
        if not on_watchlist:
            return _finish(_result(
                ticker, "insufficient_data",
                reason=(f"{ticker} is not on the watchlist and "
                        "was not explicitly requested, so no live "
                        "pull is made for it.")))

    # Cheap path: a fresh stored live-scan result.
    if not force_live:
        record, stored_reason = stored_record_for(
            ticker, live_scan_path, max_stored_age_days)
        if record is not None:
            refusal, warnings = _refusal_for(
                record.get("sector"), record.get("industry"),
                record.get("financial_currency"),
                record.get("currency"))
            if refusal is None:
                refusal = _magnitude_refusal(record)
            if refusal is None:
                if record.get("financial_currency") is None:
                    warnings.append(
                        "The stored record predates the "
                        "reporting-currency field, so the "
                        "currency check could not be run on it — "
                        "the value relies on the magnitude check "
                        "only.")
                return _finish(_result(
                    ticker, "ok", source="stored_scan",
                    record=record, warnings=warnings,
                    scanned_at=_stored_scanned_at(record)))
            return _finish(_result(
                ticker, "insufficient_data", reason=refusal,
                warnings=warnings))

    # Live path: one pull, checked before any DCF is used.
    symbol = _yahoo_symbol_for(ticker, store_dir)
    if fetcher is None:
        from tools.finance import get_company_financials
        fetcher = get_company_financials
    try:
        live = fetcher(symbol)
    except Exception as exc:  # noqa: BLE001 - a fetch failure
        # is a result, never a crash.
        return _finish(_result(
            ticker, "provider_unavailable",
            reason=f"live fetch raised: {exc}"))
    if not isinstance(live, dict):
        return _finish(_result(
            ticker, "insufficient_data",
            reason="live fetch returned no usable result"))
    if live.get("provider_unavailable"):
        return _finish(_result(
            ticker, "provider_unavailable",
            reason=(live.get("provider_error") or live.get("error")
                    or "provider could not be reached")))
    if not live.get("data_available"):
        return _finish(_result(
            ticker, "insufficient_data",
            reason=("no live financial data for this ticker "
                    "(provider returned data_available False — "
                    "genuine no-data, not a provider failure)")))
    refusal, warnings = _refusal_for(
        live.get("sector"), live.get("industry"),
        live.get("financial_currency"), live.get("currency"))
    if refusal is not None:
        return _finish(_result(
            ticker, "insufficient_data", reason=refusal,
            warnings=warnings))
    if not live.get("financial_currency"):
        warnings.append(
            "The live pull does not state the reporting "
            "currency of the financial statements, so the "
            "currency check could not be run — the value relies "
            "on the magnitude check only.")
    # Reuse the Step 54 per-ticker scan on the same pull (the
    # fetcher returns the pull already fetched above, so there
    # is no second network call).
    record = lws.scan_ticker_live(
        ticker, fetcher=lambda _symbol: live,
        yahoo_symbol_override=symbol)
    if record.get("status") != "ok":
        return _finish(_result(
            ticker, record.get("status", "insufficient_data"),
            reason=record.get("reason") or "live scan failed",
            warnings=warnings))
    refusal = _magnitude_refusal(record)
    if refusal is not None:
        return _finish(_result(
            ticker, "insufficient_data", reason=refusal,
            warnings=warnings))
    return _finish(_result(
        ticker, "ok", source="live_pull", record=record,
        warnings=warnings,
        scanned_at=(live.get("data_freshness") or {}).get(
            "retrieved_at_utc")))


def _stored_scanned_at(record):
    """Run timestamp for a stored-scan record, if it has one."""
    return record.get("retrieved_at")


# ---------------------------------------------------------------------------
# Formatting — every live-derived answer carries these labels
# ---------------------------------------------------------------------------

def format_live_valuation(result):
    """Reply text for a successful fallback result.

    States the source (stored scan with its run timestamp, or a
    live pull), the price and as-of date, the DCF basis, the tier
    and band, and that the band is provisional — quality and
    deterioration are unknown from a live pull.
    """
    record = result["record"]
    rng = record.get("fair_value_range") or {}
    band = record.get("detection_band")
    if result["source"] == "stored_scan":
        origin = (
            "the stored live scan run at "
            f"{result.get('scanned_at') or 'an unknown time'}")
    else:
        origin = "a live pull just now"
    lines = [
        f"{record.get('ticker')}: tier {record.get('tier')} — "
        f"live-derived value from {origin}, not stored "
        f"statements.",
        "Fair-value range (10-year FCF DCF on the latest annual "
        "free cash flow, WACC 11%, terminal growth 2.5%, growth "
        "5% / 10% / 15%): conservative "
        f"{_fmt_money(rng.get('conservative'))}, base "
        f"{_fmt_money(rng.get('base'))}, optimistic "
        f"{_fmt_money(rng.get('optimistic'))}.",
    ]
    fcf = record.get("latest_fcf")
    if fcf is not None:
        lines.append(
            f"Latest annual free cash flow "
            f"{record.get('currency') or ''} {fcf:,.0f} "
            f"(period {record.get('latest_fcf_period') or 'unknown'}).")
    price_as_of = record.get("price_as_of") or "unknown"
    lines.append(
        f"Price {_fmt_money(record.get('price'))} "
        f"(as of {price_as_of}).")
    lines.append(
        "Strongest-alert price (20% below the conservative "
        f"value): "
        f"{_fmt_money(record.get('strongest_alert_price'))}.")
    if band:
        lines.append(
            f"Margin band {band} (provisional) — business "
            f"quality and deterioration are unknown from a live "
            f"pull, so this is NOT a confirmed Step 26 candidate.")
    else:
        lines.append(
            "No margin band — the price is above the base value.")
    lines.append(
        "Business quality: unknown from a live pull. Material "
        "deterioration: unknown from a live pull.")
    for warning in result.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    lines.append(
        "The tier and any margin band are provisional — business "
        "quality and deterioration are unknown from a live pull, "
        "so this is not a confirmed candidate under the normal "
        "detection rules.")
    lines.append(
        "This is a research figure, not a recommendation — the "
        "final decision is yours.")
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Shapes for the Step 41 research session
# ---------------------------------------------------------------------------

def _range_with_bear(record):
    rng = dict(record.get("fair_value_range") or {})
    if "conservative" in rng:
        rng["bear"] = rng["conservative"]
    return rng


def candidate_from_live_record(record, source):
    """A Step 25 candidate-shaped dict from a live record.

    Carries ``live_fallback: True`` so the session's claim
    builders can source its claims to this module instead of
    the stored-data scanner.
    """
    return {
        "ticker": record.get("ticker"),
        "tier": record.get("tier"),
        "quality_rating": "unknown (live pull)",
        "health_verdict": "unknown (live pull)",
        "price": record.get("price"),
        "price_as_of": (
            str(record.get("price_as_of"))[:10]
            if record.get("price_as_of") else None),
        "fair_value_range": _range_with_bear(record),
        "strongest_alert_threshold_price": record.get(
            "strongest_alert_price"),
        "margin_of_safety_vs_conservative": record.get(
            "margin_of_safety_vs_conservative"),
        "margin_of_safety_vs_base": record.get(
            "margin_of_safety_vs_base"),
        "margin_of_safety_vs_optimistic": record.get(
            "margin_of_safety_vs_optimistic"),
        "missing_inputs": [
            "live-derived value: business quality and "
            "deterioration are unknown from a live pull, so the "
            "band is provisional, not a confirmed Step 26 "
            "candidate"],
        "conditions_to_change": list(
            record.get("conditions_to_change") or []),
        "live_fallback": True,
        "live_source": source,
        "live_scanned_at": record.get("retrieved_at"),
    }


def valuation_summary_from_live_record(record, source):
    """A Step 23 summary-shaped dict from a live record."""
    return {
        "ticker": record.get("ticker"),
        "tier": {
            "tier": record.get("tier"),
            "reasons": list(record.get("tier_reasons") or []),
            "conditions_to_change": list(
                record.get("conditions_to_change") or []),
            "strongest_alert_blocked": record.get(
                "strongest_alert_blocked"),
        },
        "fair_value_range": _range_with_bear(record),
        "price": record.get("price"),
        "price_as_of": (
            str(record.get("price_as_of"))[:10]
            if record.get("price_as_of") else None),
        "margin_of_safety_vs_conservative": record.get(
            "margin_of_safety_vs_conservative"),
        "margin_of_safety_vs_base": record.get(
            "margin_of_safety_vs_base"),
        "margin_of_safety_vs_optimistic": record.get(
            "margin_of_safety_vs_optimistic"),
        "strongest_alert_threshold_price": record.get(
            "strongest_alert_price"),
        "business_quality": {"rating": "unknown (live pull)"},
        "deterioration": {"deteriorated": None, "unknown": True},
        "warnings": [
            "live-derived value: business quality and "
            "deterioration are unknown from a live pull, so the "
            "band is provisional, not a confirmed Step 26 "
            "candidate"],
        "live_fallback": True,
        "live_source": source,
    }
