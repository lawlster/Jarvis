"""Step 28b — Company comparison and competitor analysis.

Orchestrates the Step 17-24 and Step 27 assessments into a
side-by-side comparison of a target ticker against caller-supplied
peer tickers.  It re-derives nothing: ratios come from Step 17,
health from Step 18, growth from Step 19, cash flow from Step 20,
the DCF range from Step 21, quality from Step 24 and the
deterioration verdict from Step 27.

Rules:
- A peer is ranked on a metric only if that metric exists for it;
  the comparison states how many peers were ranked per metric.
- If the target has no statements, the whole comparison is
  ``insufficient_data`` with reasons — never valued from headlines.
- If fewer than two peers have data, the side-by-side values are
  still shown, but no ranking is presented as meaningful.
- No buy/sell call, no "best stock" verdict — a comparison with
  reasons; the final decision is always the owner's.

Prices are caller-supplied (number or price-input dict, labelled
with as-of date and source); when none is supplied the profile
falls back to the latest ``financial_snapshots`` row, labelled
with its as-of date — the same fallback the Step 21 valuation
uses, so the price ratios and the DCF range share one price.
No network requests are made and nothing is written.
"""

import sqlite3

from agents import business_quality as bq
from agents import deterioration as det
from agents import financial_cashflow as cf
from agents import financial_growth as fg
from agents import financial_health as fh
from agents import financial_ratios as fr
from agents import intrinsic_valuation as iv
from agents import market_scanner as ms

MISSING_NO_STATEMENTS = (
    "no financial statement rows for this ticker in the database"
)
MISSING_NO_PEER_DATA = (
    "fewer than two peers have statement data; a ranking would "
    "not be meaningful"
)

# metric: (label, group, direction, unit)
# direction "higher" means a larger value ranks first; "lower"
# means a smaller value ranks first.
METRICS = {
    # Size
    "revenue": ("Revenue", "size", "higher", "currency"),
    "market_cap": ("Market cap", "size", "higher", "currency"),
    # Profitability (Step 17 ratios, latest annual period)
    "gross_margin": ("Gross margin", "profitability", "higher", "pct"),
    "operating_margin": (
        "Operating margin", "profitability", "higher", "pct"),
    "net_margin": ("Net margin", "profitability", "higher", "pct"),
    "roe": ("Return on equity", "profitability", "higher", "pct"),
    "roa": ("Return on assets", "profitability", "higher", "pct"),
    "roic": ("Return on invested capital", "profitability",
             "higher", "pct"),
    # Growth (Step 19, annual series)
    "revenue_cagr_10y": (
        "Revenue CAGR 10y", "growth", "higher", "pct"),
    "revenue_cagr_5y": (
        "Revenue CAGR 5y", "growth", "higher", "pct"),
    "eps_cagr_10y": ("Diluted EPS CAGR 10y", "growth", "higher", "pct"),
    "ocf_cagr_10y": (
        "Operating cash flow CAGR 10y", "growth", "higher", "pct"),
    "fcf_cagr_10y": (
        "Free cash flow CAGR 10y", "growth", "higher", "pct"),
    "revenue_yoy_latest": (
        "Revenue YoY (latest year)", "growth", "higher", "pct"),
    "net_income_yoy_latest": (
        "Net income YoY (latest year)", "growth", "higher", "pct"),
    "fcf_yoy_latest": (
        "Free cash flow YoY (latest year)", "growth", "higher", "pct"),
    # Financial health (Step 18 verdict + Step 17/18 checks)
    "debt_to_equity": (
        "Debt to equity", "health", "lower", "x"),
    "current_ratio": ("Current ratio", "health", "higher", "x"),
    "debt_to_ocf": (
        "Debt / operating cash flow", "health", "lower", "x"),
    "debt_to_fcf": (
        "Debt / free cash flow", "health", "lower", "x"),
    # Valuation (Step 17 price ratios + Step 21 DCF range)
    "pe_ratio": ("P/E", "valuation", "lower", "x"),
    "pb_ratio": ("P/B", "valuation", "lower", "x"),
    "ps_ratio": ("P/S", "valuation", "lower", "x"),
    "ev_ebit": ("EV/EBIT", "valuation", "lower", "x"),
    "dcf_conservative": (
        "DCF conservative value / share", "valuation", "higher",
        "currency"),
    "dcf_base": (
        "DCF base value / share", "valuation", "higher", "currency"),
    # Quality (Step 24)
    "quality_mean_score": (
        "Business quality mean score (0-2)", "quality", "higher",
        "score"),
}

# Metrics whose value is a fraction (shown as a percentage).
PCT_METRICS = {
    "gross_margin", "operating_margin", "net_margin", "roe", "roa",
    "roic", "revenue_cagr_10y", "revenue_cagr_5y", "eps_cagr_10y",
    "ocf_cagr_10y", "fcf_cagr_10y", "revenue_yoy_latest",
    "net_income_yoy_latest", "fcf_yoy_latest",
}


def _ratio_value(ratios, name):
    """Value of a Step 17 ratio result, or None with its reason."""
    result = (ratios or {}).get(name)
    if not result:
        return None, f"ratio {name} not computed"
    if result.get("present"):
        return result.get("value"), None
    return None, result.get("reason") or f"ratio {name} missing"


def _check_value(section, name):
    """Value of a Step 18 health check, or None with its reason."""
    check = (section or {}).get(name)
    if not check:
        return None, f"health check {name} not computed"
    if check.get("present"):
        return check.get("value"), None
    return None, check.get("reason") or f"health check {name} missing"


def _growth_value(growth, concept, kind):
    """CAGR or latest YoY from a Step 19 growth analysis."""
    series = (growth or {}).get("series", {}).get(concept)
    if not series:
        return None, f"no growth series for {concept}"
    if kind == "latest_yoy":
        latest = series.get("latest_yoy")
        if not latest or latest.get("value") is None:
            return None, f"no latest YoY for {concept}"
        return latest["value"], None
    years = kind  # 5 or 10
    entry = (series.get("cagr") or {}).get(years)
    # Step 19 CAGR entries are dicts {years, value, present, ...}.
    value = entry.get("value") if isinstance(entry, dict) else entry
    if value is None:
        reason = (entry.get("reason") if isinstance(entry, dict)
                  and entry.get("reason")
                  else f"no {years}y CAGR for {concept}")
        return None, reason
    return value, None


def _latest_series_value(growth, concept):
    """Latest annual value of a Step 19 series, or None + reason."""
    series = (growth or {}).get("series", {}).get(concept)
    if not series:
        return None, f"no growth series for {concept}"
    entries = [e for e in series.get("series", [])
               if e.get("present") and e.get("value") is not None]
    if not entries:
        return None, f"no present values in series for {concept}"
    return entries[-1]["value"], None


def _growth_concept_for(metric):
    """(concept, kind) backing a growth metric, or None."""
    mapping = {
        "revenue_cagr_10y": ("revenue", 10),
        "revenue_cagr_5y": ("revenue", 5),
        "eps_cagr_10y": ("eps_diluted", 10),
        "ocf_cagr_10y": ("operating_cash_flow", 10),
        "fcf_cagr_10y": ("free_cash_flow", 10),
        "revenue_yoy_latest": ("revenue", "latest_yoy"),
        "net_income_yoy_latest": ("net_income", "latest_yoy"),
        "fcf_yoy_latest": ("free_cash_flow", "latest_yoy"),
    }
    return mapping.get(metric)


def build_company_profile(conn, ticker, price_input=None,
                          sector_override=None):
    """One ticker's comparison profile from Steps 17-24 and 27.

    Never raises for a single ticker: a failed assessment is
    reported as missing with the error as a missing input.  A
    ticker with no statement rows returns ``has_statements`` False
    and every metric missing.
    """
    ticker = ticker.upper()
    price_input = ms.normalise_price_input(price_input)
    if price_input is None:
        # No caller price: fall back to the latest financial_snapshots
        # row, labelled with its as-of date — the same fallback the
        # Step 21 valuation uses.  Without this, the Step 17 price
        # ratios (P/E, P/B, P/S, EV/EBIT) would be missing while the
        # DCF range still had a price.
        try:
            price_input = fr.load_snapshot_price(conn, ticker)
        except Exception:  # noqa: BLE001 - no price, never fatal
            price_input = None
    profile = {
        "ticker": ticker,
        "has_statements": False,
        "reporting_period": None,
        "period_type": "annual",
        "price": price_input.get("price") if price_input else None,
        "price_as_of": (price_input.get("as_of_date")
                        if price_input else None),
        "price_source": (price_input.get("source")
                         if price_input else None),
        "metrics": {},
        "missing_inputs": [],
        "health_verdict": None,
        "quality_rating": None,
        "quality_mean_score": None,
        "deterioration_verdict": None,
        "material_deterioration": None,
        "warnings": [],
    }
    metric_values = {}
    metric_reasons = {}

    def _set(metric, value, reason=None):
        metric_values[metric] = value
        metric_reasons[metric] = reason

    counts = ms.statement_row_counts(conn)
    if counts.get(ticker, 0) == 0:
        profile["missing_inputs"].append(MISSING_NO_STATEMENTS)
        for metric in METRICS:
            _set(metric, None, MISSING_NO_STATEMENTS)
        profile["metrics"] = {
            m: {"value": None, "present": False,
                "reason": metric_reasons[m]}
            for m in METRICS}
        return profile
    profile["has_statements"] = True

    ratios = {}
    health = None
    growth = None
    cashflow = None
    valuation = None
    quality = None
    deterioration = None
    try:
        values, load_warnings = fr.load_standardised_values(
            conn, ticker)
        profile["warnings"].extend(load_warnings)
        result = fr.compute_ratios(values, ticker,
                                   price_input=price_input)
        ratios = result.get("ratios", {})
        profile["reporting_period"] = result.get("reporting_period")
        profile["warnings"].extend(result.get("warnings", []))
    except Exception as exc:  # noqa: BLE001 - report, never crash
        profile["missing_inputs"].append(
            f"ratio computation failed: {exc}")
    try:
        health = fh.assess_health(conn, ticker,
                                  price_input=price_input)
        profile["health_verdict"] = health.get("verdict")
        profile["warnings"].extend(health.get("warnings", []))
    except Exception as exc:  # noqa: BLE001
        profile["missing_inputs"].append(
            f"health assessment failed: {exc}")
    try:
        growth = fg.analyse_growth(conn, ticker)
        profile["warnings"].extend(growth.get("warnings", []))
    except Exception as exc:  # noqa: BLE001
        profile["missing_inputs"].append(
            f"growth analysis failed: {exc}")
    try:
        cashflow = cf.assess_cashflow(conn, ticker)
        profile["warnings"].extend(cashflow.get("warnings", []))
    except Exception as exc:  # noqa: BLE001
        profile["missing_inputs"].append(
            f"cash flow assessment failed: {exc}")
    try:
        # TTM or annual starting FCF: TTM when the data supports it,
        # otherwise the labelled annual basis (an annual-only history
        # has no TTM to build) — same rule as the Step 25 scanner.
        starting_fcf_basis = "ttm"
        try:
            _inputs, _warnings = iv.valuation_inputs(conn, ticker)
            if _inputs.get("ttm_free_cash_flow") is None:
                starting_fcf_basis = "annual"
        except Exception:  # noqa: BLE001 - basis falls back, never fatal
            starting_fcf_basis = "annual"
        valuation = iv.valuation_summary(
            conn, ticker, price_input=price_input,
            starting_fcf_basis=starting_fcf_basis)
        profile["price"] = valuation.get("price")
        profile["price_as_of"] = valuation.get("price_as_of")
        profile["price_source"] = valuation.get("price_source")
        profile["warnings"].extend(valuation.get("warnings", []))
    except Exception as exc:  # noqa: BLE001
        profile["missing_inputs"].append(
            f"valuation failed: {exc}")
    try:
        quality = bq.assess_business_quality(
            conn, ticker, health=health, growth=growth,
            cashflow=cashflow)
        profile["quality_rating"] = quality.get("rating")
        profile["quality_mean_score"] = quality.get("mean_score")
        profile["warnings"].extend(quality.get("warnings", []))
    except Exception as exc:  # noqa: BLE001
        profile["missing_inputs"].append(
            f"business quality assessment failed: {exc}")
    try:
        deterioration = det.assess_deterioration(
            conn, ticker, health=health, growth=growth,
            cashflow=cashflow)
        profile["deterioration_verdict"] = deterioration.get(
            "overall_verdict")
        profile["material_deterioration"] = deterioration.get(
            "material_deterioration")
        profile["warnings"].extend(deterioration.get("warnings", []))
    except Exception as exc:  # noqa: BLE001
        profile["missing_inputs"].append(
            f"deterioration assessment failed: {exc}")

    # Size ----------------------------------------------------------
    revenue, reason = _latest_series_value(growth, "revenue")
    _set("revenue", revenue, reason)
    market_cap = None
    market_cap_reason = None
    if valuation is not None:
        shares = valuation.get("shares_outstanding_diluted")
        price = valuation.get("price")
        if price is not None and shares:
            market_cap, _method = fr.market_cap_for(
                {"price": price, "market_cap": None}, shares)
        elif price is None:
            market_cap_reason = "no price available"
        else:
            market_cap_reason = "no diluted share count available"
    else:
        market_cap_reason = "valuation not computed"
    _set("market_cap", market_cap, market_cap_reason)

    # Profitability -------------------------------------------------
    for metric in ("gross_margin", "operating_margin", "net_margin",
                   "roe", "roa", "roic"):
        value, reason = _ratio_value(ratios, metric)
        _set(metric, value, reason)

    # Growth --------------------------------------------------------
    for metric in ("revenue_cagr_10y", "revenue_cagr_5y",
                   "eps_cagr_10y", "ocf_cagr_10y", "fcf_cagr_10y",
                   "revenue_yoy_latest", "net_income_yoy_latest",
                   "fcf_yoy_latest"):
        concept, kind = _growth_concept_for(metric)
        value, reason = _growth_value(growth, concept, kind)
        _set(metric, value, reason)

    # Health --------------------------------------------------------
    sections = (health or {}).get("sections", {})
    for metric, section, check in (
            ("debt_to_equity", "balance_sheet_strength",
             "debt_to_equity"),
            ("current_ratio", "liquidity", "current_ratio"),
            ("debt_to_ocf", "solvency",
             "debt_to_operating_cash_flow"),
            ("debt_to_fcf", "solvency", "debt_to_free_cash_flow")):
        value, reason = _check_value(sections.get(section), check)
        _set(metric, value, reason)

    # Valuation -----------------------------------------------------
    for metric in ("pe_ratio", "pb_ratio", "ps_ratio", "ev_ebit"):
        value, reason = _ratio_value(ratios, metric)
        _set(metric, value, reason)
    fair_value_range = (valuation or {}).get("fair_value_range", {})
    _set("dcf_conservative", fair_value_range.get("conservative"),
         None if fair_value_range.get("conservative") is not None
         else "no conservative DCF value could be computed")
    _set("dcf_base", fair_value_range.get("base"),
         None if fair_value_range.get("base") is not None
         else "no base DCF value could be computed")

    # Quality -------------------------------------------------------
    _set("quality_mean_score", profile["quality_mean_score"],
         None if profile["quality_mean_score"] is not None
         else "business quality could not be assessed")

    profile["metrics"] = {
        m: {"value": metric_values[m], "present":
            metric_values[m] is not None,
            "reason": metric_reasons[m]}
        for m in METRICS}
    profile["missing_inputs"].extend([
        f"{metric}: {reason}"
        for metric, reason in metric_reasons.items()
        if metric_values[metric] is None and reason
        and metric in ("revenue", "net_margin", "roe", "roic",
                       "debt_to_equity", "current_ratio")])
    profile["detail"] = {
        "ratios": ratios,
        "health": health,
        "growth": growth,
        "cashflow": cashflow,
        "valuation": valuation,
        "quality": quality,
        "deterioration": deterioration,
    }
    return profile


def _rank_values(entries, direction):
    """Dense ranks for {ticker: value} entries (present values only).

    Rank 1 is best per ``direction``.  Equal values share a rank.
    Returns {ticker: rank}.
    """
    present = [(t, v) for t, v in entries.items() if v is not None]
    reverse = direction == "higher"
    ordered = sorted(present, key=lambda item: item[1],
                     reverse=reverse)
    ranks = {}
    current_rank = 0
    previous_value = None
    for index, (ticker, value) in enumerate(ordered):
        if previous_value is None or value != previous_value:
            current_rank = index + 1
            previous_value = value
        ranks[ticker] = current_rank
    return ranks


def compare_companies(conn, target, peers, prices=None, sector=None,
                      sector_overrides=None, cache=None):
    """Compare ``target`` against ``peers`` side by side.

    ``prices`` maps ticker -> price (number or price-input dict).
    ``sector`` optionally restricts peers to one stored sector
    (from ``market_universe``); peers whose stored sector does not
    match are listed as excluded with the reason.  ``sector_overrides``
    maps ticker -> sector for the valuation path (Step 22 router).
    Returns a comparison dict; never raises.
    """
    target = target.upper()
    peer_list = []
    seen = set()
    for peer in peers or []:
        peer = peer.upper()
        if peer == target or peer in seen:
            continue
        seen.add(peer)
        peer_list.append(peer)
    prices = prices or {}
    sector_overrides = sector_overrides or {}

    universe = {}
    if sector is not None:
        try:
            for row in ms.load_universe(conn):
                universe[row["ticker"]] = row
        except Exception:  # noqa: BLE001 - sector filter is best effort
            universe = {}

    profiles = {}
    cache = cache if cache is not None else {}

    def _profile(ticker):
        key = (ticker, None if prices.get(ticker) is None
               else str(prices.get(ticker)),
               sector_overrides.get(ticker))
        if key not in cache:
            cache[key] = build_company_profile(
                conn, ticker,
                price_input=prices.get(ticker),
                sector_override=sector_overrides.get(ticker))
        return cache[key]

    target_profile = _profile(target)

    peer_profiles = {}
    excluded_peers = {}
    for peer in peer_list:
        if sector is not None:
            row = universe.get(peer)
            stored_sector = (row or {}).get("sector")
            if stored_sector != sector:
                excluded_peers[peer] = (
                    "excluded by sector filter: stored sector is "
                    f"{stored_sector!r}, filter is {sector!r}")
                continue
        peer_profiles[peer] = _profile(peer)

    comparable_peers = [p for p, prof in peer_profiles.items()
                        if prof["has_statements"]]
    not_comparable = {
        p: prof["missing_inputs"]
        for p, prof in peer_profiles.items()
        if not prof["has_statements"]}
    ranking_meaningful = len(comparable_peers) >= 2

    comparison = {
        "target": target,
        "peers_requested": peer_list,
        "sector_filter": sector,
        "status": ("comparable" if target_profile["has_statements"]
                   else "insufficient_data"),
        "target_profile": target_profile,
        "peer_profiles": peer_profiles,
        "comparable_peers": comparable_peers,
        "not_comparable_peers": not_comparable,
        "excluded_peers": excluded_peers,
        "ranking_meaningful": ranking_meaningful,
        "ranking_note": (
            None if ranking_meaningful else MISSING_NO_PEER_DATA),
        "metrics": {},
        "stands_out": [],
        "lags": [],
        "warnings": [],
    }
    if not target_profile["has_statements"]:
        comparison["status_reason"] = (
            "target has no financial statement rows; no comparison "
            "can be made")
        comparison["warnings"].append(MISSING_NO_STATEMENTS)
        return comparison

    ranked_tickers = [target] + comparable_peers
    for metric, (label, group, direction, unit) in METRICS.items():
        entries = {}
        reasons = {}
        for ticker in ranked_tickers:
            entry = (target_profile if ticker == target
                     else peer_profiles[ticker])["metrics"][metric]
            entries[ticker] = entry["value"]
            reasons[ticker] = entry["reason"]
        ranks = (_rank_values(entries, direction)
                 if ranking_meaningful else {})
        comparison["metrics"][metric] = {
            "label": label,
            "group": group,
            "direction": direction,
            "unit": unit,
            "values": {
                ticker: {"value": entries[ticker],
                         "present": entries[ticker] is not None,
                         "reason": reasons[ticker],
                         "rank": ranks.get(ticker)}
                for ticker in ranked_tickers},
            "ranked_count": sum(
                1 for v in entries.values() if v is not None),
        }

    # Where the target stands out / lags: rank 1 or last, with at
    # least three tickers ranked on the metric so the claim means
    # something.
    for metric, table in comparison["metrics"].items():
        if table["ranked_count"] < 3:
            continue
        target_entry = table["values"][target]
        if target_entry["rank"] == 1:
            comparison["stands_out"].append({
                "metric": metric,
                "label": table["label"],
                "target_value": target_entry["value"],
                "target_rank": 1,
                "ranked_count": table["ranked_count"],
                "reason": (
                    f"{target} ranks 1 of {table['ranked_count']} "
                    f"on {table['label']} "
                    f"({target_entry['value']:.4g})"),
            })
        elif target_entry["rank"] == table["ranked_count"]:
            comparison["lags"].append({
                "metric": metric,
                "label": table["label"],
                "target_value": target_entry["value"],
                "target_rank": target_entry["rank"],
                "ranked_count": table["ranked_count"],
                "reason": (
                    f"{target} ranks last "
                    f"({target_entry['rank']} of "
                    f"{table['ranked_count']}) on {table['label']} "
                    f"({target_entry['value']:.4g})"),
            })
    return comparison


def comparison_summary(comparison):
    """One-paragraph text summary of a comparison (no verdict)."""
    target = comparison["target"]
    if comparison["status"] == "insufficient_data":
        return (f"{target}: no comparison possible — "
                + (comparison.get("status_reason") or
                   "insufficient data") + ".")
    peers = comparison["comparable_peers"]
    lines = [
        f"{target} vs {len(peers)} comparable peer(s) "
        f"({', '.join(peers) if peers else 'none'}); "
        f"{len(comparison['not_comparable_peers'])} peer(s) not "
        "comparable (no statement data)."
    ]
    if not comparison["ranking_meaningful"]:
        lines.append("Ranking is not meaningful: fewer than two "
                     "peers have data.")
    if comparison["stands_out"]:
        lines.append("Stands out: " + "; ".join(
            item["reason"] for item in comparison["stands_out"]) + ".")
    if comparison["lags"]:
        lines.append("Lags: " + "; ".join(
            item["reason"] for item in comparison["lags"]) + ".")
    return " ".join(lines)


def run_comparison(db_path=None, target=None, peers=None, prices=None,
                   **kwargs):
    """Compare on a database file (temp copy for verification)."""
    path = db_path or str(ms.BASE_DIR / "database" / "assistant.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return compare_companies(conn, target, peers, prices=prices,
                                 **kwargs)
    finally:
        conn.close()
