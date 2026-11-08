"""Step 29 — Automated company research reports.

Composes the Step 16-28b analysis engine into one structured
research report per ticker, as a Python dict and as a Markdown
document.  It re-derives nothing: key figures and ratios come from
Step 17, health from Step 18, growth from Step 19, cash flow from
Step 20, the valuation range from Step 21/22, the tier and
entry-price ladder from Step 23, quality from Step 24, the
deterioration verdict from Step 27 and the peer comparison from
Step 28b.

Rules:
- Every section carries its data source and period.  A section that
  cannot be produced is present with its status and reason — never
  silently omitted, never filled in from headlines.
- No business description is invented: the database stores no
  company description, so the business summary says so and sticks
  to the stored sector / industry / listing labels.
- Prices are caller-supplied or fall back to the latest
  ``financial_snapshots`` row, labelled with its as-of date — the
  same fallback the Step 21 valuation uses.  No network requests
  are made and nothing is written to the database; reports are
  written as Markdown files to a caller-chosen path (default
  ``reports/`` under the project root).
- No recommendation is made anywhere in a generated report.  The
  conclusion summarises the evidence (tier, quality, key numbers)
  and states plainly that the final decision is the reader's.
  Generated report text avoids the words "buy" and "sell"
  entirely — including in the conclusion — so a report can never
  be read as a trade instruction.
"""

import sqlite3
from datetime import date
from pathlib import Path

from agents import business_quality as bq
from agents import company_comparison as cc
from agents import deterioration as det
from agents import financial_cashflow as cf
from agents import financial_growth as fg
from agents import financial_health as fh
from agents import financial_ratios as fr
from agents import margin_of_safety as mos
from agents import market_scanner as ms

REPORT_VERSION = "1.0"

MISSING_NO_STATEMENTS = (
    "no financial statement rows for this ticker in the database"
)
MISSING_NO_DESCRIPTION = (
    "no business description is stored in the database for this "
    "company; the summary below uses only the stored sector, "
    "industry and listing labels"
)
MISSING_NO_PEERS = "no peers were requested for this report"
MISSING_NO_PRICE = "no price available (no caller price and no snapshot row)"

CURRENCY_SYMBOLS = {"USD": "$", "SGD": "S$", "HKD": "HK$", "GBP": "£",
                    "EUR": "€", "JPY": "¥", "CNY": "¥"}

# (concept, statement_type, label, unit) for the key-figures table.
KEY_FIGURES = (
    ("revenue", "income_statement", "Revenue", "currency"),
    ("gross_profit", "income_statement", "Gross profit", "currency"),
    ("operating_income", "income_statement", "Operating income",
     "currency"),
    ("net_income", "income_statement", "Net income", "currency"),
    ("eps_diluted", "income_statement", "Diluted EPS", "currency"),
    ("total_assets", "balance_sheet", "Total assets", "currency"),
    ("total_liabilities", "balance_sheet", "Total liabilities",
     "currency"),
    ("total_equity", "balance_sheet", "Total equity", "currency"),
    ("total_debt", "balance_sheet", "Total debt", "currency"),
    ("cash_and_equivalents", "balance_sheet", "Cash and equivalents",
     "currency"),
    ("operating_cash_flow", "cash_flow_statement",
     "Operating cash flow", "currency"),
    ("capital_expenditure", "cash_flow_statement",
     "Capital expenditure", "currency"),
    ("free_cash_flow", "cash_flow_statement", "Free cash flow",
     "currency"),
    ("dividends_paid", "cash_flow_statement", "Dividends paid",
     "currency"),
    ("share_repurchases", "cash_flow_statement",
     "Share repurchases", "currency"),
)

# Step 17 ratio names shown in the ratios table, with labels.
RATIO_ROWS = (
    ("pe_ratio", "P/E", "x"),
    ("pb_ratio", "P/B", "x"),
    ("ps_ratio", "P/S", "x"),
    ("ev_ebit", "EV/EBIT", "x"),
    ("gross_margin", "Gross margin", "pct"),
    ("operating_margin", "Operating margin", "pct"),
    ("net_margin", "Net margin", "pct"),
    ("roe", "Return on equity", "pct"),
    ("roa", "Return on assets", "pct"),
    ("roic", "Return on invested capital", "pct"),
    ("debt_to_equity", "Debt to equity", "x"),
    ("current_ratio", "Current ratio", "x"),
    ("quick_ratio", "Quick ratio", "x"),
)

GROWTH_CONCEPTS = (
    ("revenue", "Revenue"),
    ("operating_income", "Operating income"),
    ("net_income", "Net income"),
    ("eps_diluted", "Diluted EPS"),
    ("operating_cash_flow", "Operating cash flow"),
    ("free_cash_flow", "Free cash flow"),
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _symbol(currency):
    return CURRENCY_SYMBOLS.get(currency or "USD", "")


def _money(value, currency="USD"):
    """Raw-dollar value as $123,456m (or $1,234 for small values)."""
    if value is None:
        return "n/a"
    symbol = _symbol(currency)
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{symbol}{value / 1_000_000:,.0f}m"
    return f"{symbol}{value:,.2f}"


def _fmt(value, unit, currency="USD"):
    if value is None:
        return "n/a"
    if unit == "pct":
        return f"{value * 100:.2f}%"
    if unit == "x":
        return f"{value:.2f}x"
    if unit == "currency":
        return _money(value, currency)
    if unit == "score":
        return f"{value:.2f}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _section(title, status, data=None, reason=None, source=None,
             period=None):
    return {"title": title, "status": status, "data": data or {},
            "reason": reason, "source": source, "period": period}


def _guarded(label, fn, report_warnings):
    """Run ``fn``; on failure return None and record the warning."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - report, never crash
        report_warnings.append(f"{label} failed: {exc}")
        return None


def _latest_value(values, statement_type, concept, period_type="annual"):
    """Latest present value for a concept, or (None, reason)."""
    periods = sorted(
        p for (s, p, t) in values
        if s == statement_type and t == period_type)
    for period in reversed(periods):
        row = values.get((statement_type, period, period_type)) or {}
        entry = row.get(concept) or {}
        if entry.get("present") and entry.get("value") is not None:
            return entry["value"], period
    return None, None


def _ratio_entry(ratios, name):
    result = (ratios or {}).get(name) or {}
    if result.get("present"):
        return result.get("value"), None
    return None, result.get("reason") or f"ratio {name} missing"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _universe_row(conn, ticker):
    try:
        for row in ms.load_universe(conn):
            if row["ticker"] == ticker:
                return row
    except Exception:  # noqa: BLE001 - header is best effort
        pass
    return {}


def _statement_coverage(conn, ticker):
    counts = ms.statement_row_counts(conn)
    return counts.get(ticker, 0)


def _build_business_summary(universe, statement_count, ticker):
    data = {
        "company_name": universe.get("company_name"),
        "sector": universe.get("sector"),
        "industry": universe.get("industry"),
        "country": universe.get("country"),
        "exchange": universe.get("exchange"),
        "currency": universe.get("currency"),
        "security_type": universe.get("security_type"),
        "sec_cik": universe.get("sec_cik"),
        "statement_rows": statement_count,
        "description": None,
    }
    reason = MISSING_NO_DESCRIPTION
    return _section(
        "Business summary", "partial", data, reason=reason,
        source="market_universe",
        period=None)


def _build_financials(conn, ticker, price_input, currency,
                      report_warnings):
    """Key figures, ratios, growth and cash flow (Steps 17-20)."""
    values, load_warnings = fr.load_standardised_values(conn, ticker)
    report_warnings.extend(load_warnings)
    ratios_result = _guarded(
        "ratio computation",
        lambda: fr.compute_ratios(values, ticker,
                                  price_input=price_input),
        report_warnings)
    ratios = (ratios_result or {}).get("ratios", {})
    period = (ratios_result or {}).get("reporting_period")
    report_warnings.extend((ratios_result or {}).get("warnings", []))

    key_figures = []
    for concept, statement_type, label, unit in KEY_FIGURES:
        value, value_period = _latest_value(
            values, statement_type, concept)
        key_figures.append({
            "label": label, "concept": concept, "value": value,
            "unit": unit, "period": value_period,
            "present": value is not None,
            "reason": None if value is not None
            else f"{label} not reported in the latest period",
        })

    ratio_rows = []
    for name, label, unit in RATIO_ROWS:
        value, reason = _ratio_entry(ratios, name)
        ratio_rows.append({
            "label": label, "metric": name, "value": value,
            "unit": unit, "present": value is not None,
            "reason": reason,
        })

    growth = _guarded("growth analysis",
                      lambda: fg.analyse_growth(conn, ticker),
                      report_warnings)
    growth_rows = []
    for concept, label in GROWTH_CONCEPTS:
        entry = (growth or {}).get("series", {}).get(concept) or {}
        latest_yoy = entry.get("latest_yoy") or {}
        cagr = entry.get("cagr") or {}
        cagr_5 = cagr.get(5) or {}
        cagr_10 = cagr.get(10) or {}
        growth_rows.append({
            "label": label, "concept": concept,
            "latest_yoy": latest_yoy.get("value"),
            "latest_yoy_period": latest_yoy.get("period"),
            "cagr_5y": cagr_5.get("value") if isinstance(cagr_5, dict)
            else None,
            "cagr_10y": cagr_10.get("value")
            if isinstance(cagr_10, dict) else None,
            "cyclicality": (entry.get("cyclicality") or {}).get("verdict"),
            "present": bool(entry),
            "reason": None if entry else f"no growth series for {concept}",
        })

    cashflow = _guarded("cash-flow analysis",
                        lambda: cf.assess_cashflow(conn, ticker),
                        report_warnings)
    cashflow_rows = []
    for section_name, section in ((cashflow or {}).get("sections", {})
                                  .items()):
        for name, item in (section or {}).items():
            if not isinstance(item, dict):
                continue
            cashflow_rows.append({
                "label": name, "section": section_name,
                "value": item.get("value"),
                "present": bool(item.get("present")),
                "formula": item.get("formula"),
                "reason": item.get("reason"),
            })

    present = sum(1 for row in key_figures if row["present"])
    status = ("complete" if present == len(key_figures)
              else "partial" if present else "insufficient_data")
    data = {
        "key_figures": key_figures,
        "ratios_latest": ratio_rows,
        "growth": growth_rows,
        "cash_flow": cashflow_rows,
        "latest_annual_period": period,
        "cashflow_period": (cashflow or {}).get("reporting_period"),
        "growth_warnings": (growth or {}).get("warnings", []),
        "cashflow_warnings": (cashflow or {}).get("warnings", []),
    }
    reason = None
    if status != "complete":
        reason = (f"{len(key_figures) - present} of "
                  f"{len(key_figures)} key figures missing in the "
                  f"latest annual period")
    return _section("Financials", status, data, reason=reason,
                    source="Steps 16-20 (standardised statements, "
                    "ratios, growth, cash flow)",
                    period=period)


def _build_health_and_quality(conn, ticker, report_warnings):
    health = _guarded("health assessment",
                      lambda: fh.assess_health(conn, ticker),
                      report_warnings)
    quality = _guarded("business quality assessment",
                       lambda: bq.assess_business_quality(conn, ticker),
                       report_warnings)
    deterioration = _guarded(
        "deterioration assessment",
        lambda: det.assess_deterioration(conn, ticker),
        report_warnings)

    components = []
    for name, comp in ((quality or {}).get("components", {})).items():
        components.append({
            "component": name, "score": comp.get("score"),
            "value": comp.get("value"),
            "present": bool(comp.get("present")),
            "evidence": comp.get("evidence"),
            "reason": comp.get("reason"),
        })
    data = {
        "health_verdict": (health or {}).get("verdict"),
        "health_reasons": (health or {}).get("verdict_reasons", []),
        "health_warnings": (health or {}).get("warnings", []),
        "quality_rating": (quality or {}).get("rating"),
        "quality_mean_score": (quality or {}).get("mean_score"),
        "quality_reason": (quality or {}).get("rating_reason"),
        "quality_components": components,
        "moat_evidence": (quality or {}).get("moat_evidence"),
        "not_assessable": (quality or {}).get("not_assessable", []),
        "deterioration_verdict": (deterioration or {}).get(
            "overall_verdict"),
        "material_deterioration": (deterioration or {}).get(
            "material_deterioration"),
        "deterioration_reasons": (deterioration or {}).get(
            "overall_reasons", []),
        "deterioration_areas": {
            name: {"verdict": area.get("verdict"),
                   "evidence": area.get("evidence", [])}
            for name, area in
            ((deterioration or {}).get("areas", {})).items()},
        "risk_flags": (deterioration or {}).get("risk_flags", []),
        "deterioration_rule": (deterioration or {}).get("rule"),
    }
    # An assessment that ran but returned an "insufficient data"
    # verdict counts as unavailable, not as a completed section —
    # otherwise a ticker with no usable data (DBS) would look
    # complete here while every other section says insufficient.
    def _usable(assessment, verdict_key):
        if assessment is None:
            return False
        return assessment.get(verdict_key) not in (
            None, "insufficient data", "insufficient_data")

    usable = {
        "health": _usable(health, "verdict"),
        "quality": _usable(quality, "rating"),
        "deterioration": _usable(deterioration, "overall_verdict"),
    }
    if all(usable.values()):
        status = "complete"
    elif any(usable.values()):
        status = "partial"
    else:
        status = "insufficient_data"
    reason = None
    if status != "complete":
        missing = [n for n, ok in usable.items() if not ok]
        reason = "unavailable: " + ", ".join(missing)
    return _section(
        "Financial health and business quality", status, data,
        reason=reason,
        source="Steps 18 (health), 24 (quality), 27 (deterioration)",
        period=(health or {}).get("reporting_period"))


def _build_valuation(conn, ticker, price_input, sector_override,
                     report_warnings):
    # TTM basis first (the Step 21 default); if the ticker has no
    # quarterly rows the TTM starting FCF is missing and no range
    # can be computed, so fall back to the labelled annual basis —
    # the same fallback the Step 25 scanner uses.
    summary = _guarded(
        "margin-of-safety summary",
        lambda: mos.margin_of_safety_summary(
            conn, ticker, price_input=price_input,
            sector_override=sector_override),
        report_warnings)
    fcf_basis = "ttm"
    range_values = (summary or {}).get("fair_value_range") or {}
    if summary is not None and all(
            range_values.get(point) is None
            for point in ("conservative", "base", "optimistic")):
        annual = _guarded(
            "margin-of-safety summary (annual FCF basis)",
            lambda: mos.margin_of_safety_summary(
                conn, ticker, price_input=price_input,
                sector_override=sector_override,
                starting_fcf_basis="annual"),
            report_warnings)
        if annual is not None:
            annual_range = annual.get("fair_value_range") or {}
            if any(annual_range.get(point) is not None
                   for point in ("conservative", "base", "optimistic")):
                summary = annual
                fcf_basis = ("annual (no quarterly rows, so no TTM "
                             "free cash flow exists)")
    if summary is None:
        return _section(
            "Valuation and margin of safety", "insufficient_data",
            {"fair_value_range": None, "tier": None},
            reason="valuation could not be computed",
            source="Steps 21-23")
    tier = summary.get("tier") or {}
    margins = summary.get("margins_of_safety") or {}
    ladder = summary.get("entry_price_ladder") or {}
    data = {
        "fcf_basis": fcf_basis,
        "sector": summary.get("sector"),
        "sector_source": summary.get("sector_source"),
        "why_this_model": summary.get("why_this_model"),
        "fair_value_range": summary.get("fair_value_range"),
        "price": summary.get("price"),
        "price_as_of": summary.get("price_as_of"),
        "price_source": summary.get("price_source"),
        "margins": {
            point: {"margin_graham": entry.get("value"),
                    "margin_vs_price": entry.get("margin_vs_price"),
                    "fair_value": entry.get("fair_value"),
                    "present": bool(entry.get("present")),
                    "reason": entry.get("reason")}
            for point, entry in margins.items()},
        "margin_convention": summary.get("margin_convention"),
        "tier": tier.get("tier"),
        "tier_reasons": tier.get("reasons", []),
        "conditions_to_change": tier.get("conditions_to_change", []),
        "strongest_alert_blocked": tier.get("strongest_alert_blocked"),
        "entry_price_ladder": ladder.get("rungs", []),
        "strongest_alert_threshold_price": summary.get(
            "strongest_alert_threshold_price"),
        "relative_pe_check": summary.get("relative_pe_check"),
        "sensitivity": summary.get("sensitivity"),
        "quality_rating": (summary.get("business_quality") or {}).get(
            "rating"),
        "material_deterioration": (
            summary.get("deterioration") or {}).get("deteriorated"),
    }
    range_values = summary.get("fair_value_range") or {}
    has_range = range_values.get("conservative") is not None
    status = "complete" if has_range and summary.get("price") is not None \
        else "partial" if has_range else "insufficient_data"
    reason = None
    if status == "insufficient_data":
        reason = "no conservative intrinsic value could be computed"
    elif status == "partial":
        reason = MISSING_NO_PRICE
    return _section("Valuation and margin of safety", status, data,
                    reason=reason,
                    source="Steps 21-23 (DCF / sector models, margin "
                    "of safety)",
                    period=summary.get("reporting_period"))


def _build_peer_comparison(conn, ticker, peers, prices, sector_override,
                           report_warnings):
    if not peers:
        return _section(
            "Peer comparison", "insufficient_data",
            {"target": ticker, "peers_requested": [],
             "comparable_peers": [], "not_comparable_peers": {}},
            reason=MISSING_NO_PEERS, source="Step 28b")
    comparison = _guarded(
        "peer comparison",
        lambda: cc.compare_companies(
            conn, ticker, peers, prices=prices or {},
            sector_overrides={ticker: sector_override}
            if sector_override else None),
        report_warnings)
    if comparison is None:
        return _section(
            "Peer comparison", "insufficient_data",
            {"target": ticker, "peers_requested": list(peers)},
            reason="peer comparison could not be computed",
            source="Step 28b")
    metrics = []
    for name, metric in (comparison.get("metrics") or {}).items():
        values = (metric or {}).get("values", {})
        metrics.append({
            "metric": name, "label": metric.get("label"),
            "group": metric.get("group"),
            "direction": metric.get("direction"),
            "unit": metric.get("unit"),
            "ranked_count": metric.get("ranked_count"),
            "values": {
                t: {"value": v.get("value"),
                    "present": bool(v.get("present")),
                    "rank": v.get("rank"),
                    "reason": v.get("reason")}
                for t, v in values.items()},
        })
    data = {
        "target": ticker,
        "peers_requested": comparison.get("peers_requested", []),
        "status": comparison.get("status"),
        "status_reason": comparison.get("status_reason"),
        "comparable_peers": comparison.get("comparable_peers", []),
        "not_comparable_peers": comparison.get(
            "not_comparable_peers", {}),
        "excluded_peers": comparison.get("excluded_peers", {}),
        "ranking_meaningful": comparison.get("ranking_meaningful"),
        "ranking_note": comparison.get("ranking_note"),
        "metrics": metrics,
        "stands_out": comparison.get("stands_out", []),
        "lags": comparison.get("lags", []),
        "summary": cc.comparison_summary(comparison),
    }
    status = ("complete" if comparison.get("ranking_meaningful")
              else "partial" if comparison.get("comparable_peers")
              else "insufficient_data")
    reason = comparison.get("ranking_note")
    return _section("Peer comparison", status, data, reason=reason,
                    source="Step 28b (company comparison)",
                    period=(comparison.get("target_profile") or {}).get(
                        "reporting_period"))


def _build_risks(report):
    """Risks and what would change the view, from earlier sections."""
    health = report["sections"]["health_and_quality"]["data"]
    valuation = report["sections"]["valuation"]["data"]
    risks = []
    for flag in health.get("risk_flags", []):
        risks.append({
            "risk": flag.get("name"), "severity": flag.get("severity"),
            "area": flag.get("area"), "evidence": flag.get("evidence"),
            "trend": flag.get("trend"),
            "source": "Step 27 deterioration",
        })
    # Area evidence is not repeated here: the named risk flags above
    # already carry the same evidence, one entry per flag.
    for item in health.get("not_assessable", []):
        risks.append({
            "risk": f"not assessable from statements: "
                    f"{item.get('item')}",
            "severity": "unknown", "area": "qualitative",
            "evidence": item.get("reason"), "trend": None,
            "source": "Step 24 business quality",
        })
    conditions = list(valuation.get("conditions_to_change", []))
    if valuation.get("tier") == "overvalued":
        conditions.append(
            "a fall in price towards the valuation range would "
            "change the tier; the entry-price ladder in the "
            "valuation section shows the prices at which it would")
    data = {"risks": risks, "what_would_change_the_view": conditions}
    status = "complete" if risks or conditions else "partial"
    return _section(
        "Risks and what would change the view", status, data,
        reason=None if risks or conditions else
        "no risk flags or tier conditions were produced",
        source="Steps 24 and 27, Step 23 tier conditions")


def _build_conclusion(report):
    """One-paragraph evidence summary.  Never a recommendation."""
    header = report
    health = report["sections"]["health_and_quality"]["data"]
    valuation = report["sections"]["valuation"]["data"]
    ticker = header["ticker"]
    name = header.get("company_name") or ticker
    price = valuation.get("price")
    price_text = (
        f"{_money(price, header.get('currency'))} "
        f"(as of {valuation.get('price_as_of')})"
        if price is not None else "no price available")
    range_ = valuation.get("fair_value_range") or {}
    if range_.get("conservative") is not None:
        range_text = (
            f"conservative {_money(range_['conservative'], header.get('currency'))}, "
            f"base {_money(range_['base'], header.get('currency'))}, "
            f"optimistic {_money(range_['optimistic'], header.get('currency'))}")
    else:
        range_text = "no intrinsic-value range could be computed"
    alert_price = valuation.get("strongest_alert_threshold_price")
    alert_text = (
        f"The strongest-alert price is "
        f"{_money(alert_price, header.get('currency'))}."
        if alert_price is not None else
        "No strongest-alert price could be computed.")
    text = (
        f"{name} ({ticker}) is rated "
        f"{health.get('quality_rating') or 'unknown'} on business "
        f"quality and {health.get('health_verdict') or 'unknown'} on "
        f"financial health, with a deterioration verdict of "
        f"{health.get('deterioration_verdict') or 'unknown'} "
        f"(material deterioration: "
        f"{health.get('material_deterioration')}). At a price of "
        f"{price_text} the valuation tier is "
        f"{valuation.get('tier') or 'insufficient data'}; the "
        f"intrinsic-value range is {range_text}. {alert_text} "
        f"This report summarises the evidence from the stored "
        f"financial statements and the valuation engine; it is "
        f"research, not a recommendation, and the final decision "
        f"is the reader's.")
    return _section("Conclusion", "complete",
                    {"text": text}, source="report composition")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(conn, ticker, price_input=None, peers=None,
                    sector_override=None, report_date=None):
    """Generate a structured research report dict for one ticker.

    ``conn`` is a read-only SQLite connection (the caller opens it).
    ``price_input`` is a number or a price-input dict; when None the
    latest ``financial_snapshots`` row is used, labelled with its
    as-of date.  ``peers`` is an optional list of peer tickers for
    the Step 28b comparison.  Never raises for a single ticker: a
    ticker with no data gets a report whose sections say so.
    """
    ticker = ticker.upper()
    report_date = report_date or date.today().isoformat()
    warnings = []
    missing_inputs = []

    universe = _universe_row(conn, ticker)
    statement_count = _statement_coverage(conn, ticker)
    if statement_count == 0:
        missing_inputs.append(MISSING_NO_STATEMENTS)

    price_input = ms.normalise_price_input(price_input)
    if price_input is None:
        try:
            price_input = fr.load_snapshot_price(conn, ticker)
        except Exception:  # noqa: BLE001 - no price, never fatal
            price_input = None
    if price_input is None:
        missing_inputs.append(MISSING_NO_PRICE)

    prices = {}
    if peers and price_input is not None:
        prices[ticker] = price_input

    report = {
        "ticker": ticker,
        "company_name": universe.get("company_name"),
        "exchange": universe.get("exchange"),
        "country": universe.get("country"),
        "currency": universe.get("currency") or "USD",
        "sector": universe.get("sector"),
        "industry": universe.get("industry"),
        "security_type": universe.get("security_type"),
        "sec_cik": universe.get("sec_cik"),
        "report_date": report_date,
        "report_version": REPORT_VERSION,
        "statement_rows": statement_count,
        "price": price_input.get("price") if price_input else None,
        "price_as_of": price_input.get("as_of_date")
        if price_input else None,
        "price_source": price_input.get("source")
        if price_input else None,
        "status": None,
        "status_reason": None,
        "missing_inputs": missing_inputs,
        "warnings": warnings,
        "sections": {},
    }

    sections = report["sections"]
    sections["business_summary"] = _build_business_summary(
        universe, statement_count, ticker)
    if statement_count == 0:
        for name, title, source in (
                ("financials", "Financials", "Steps 16-20"),
                ("health_and_quality",
                 "Financial health and business quality",
                 "Steps 18, 24, 27"),
                ("valuation", "Valuation and margin of safety",
                 "Steps 21-23")):
            sections[name] = _section(
                title, "insufficient_data", {},
                reason=MISSING_NO_STATEMENTS, source=source)
    else:
        sections["financials"] = _build_financials(
            conn, ticker, price_input, report["currency"], warnings)
        sections["health_and_quality"] = _build_health_and_quality(
            conn, ticker, warnings)
        sections["valuation"] = _build_valuation(
            conn, ticker, price_input, sector_override, warnings)
    sections["peer_comparison"] = _build_peer_comparison(
        conn, ticker, peers, prices, sector_override, warnings)
    sections["risks"] = _build_risks(report)
    sections["conclusion"] = _build_conclusion(report)

    # Status is driven by the core data sections (financials,
    # health/quality, valuation).  The business summary is always
    # partial (no description is stored) and the risks/conclusion
    # sections are composed, never data — neither drives the status.
    core = ("financials", "health_and_quality", "valuation")
    core_statuses = [sections[name]["status"] for name in core]
    statuses = [s["status"] for s in sections.values()]
    if all(s == "insufficient_data" for s in core_statuses):
        report["status"] = "insufficient_data"
        report["status_reason"] = MISSING_NO_STATEMENTS
    elif any(s == "insufficient_data" for s in core_statuses):
        report["status"] = "partial"
        report["status_reason"] = (
            "some core sections could not be produced: " + "; ".join(
                f"{name} ({sections[name]['status']})"
                for name in core
                if sections[name]["status"] == "insufficient_data"))
    elif any(s == "insufficient_data" for s in statuses):
        report["status"] = "partial"
        report["status_reason"] = (
            "some sections could not be produced: " + "; ".join(
                f"{name} ({s['status']})"
                for name, s in sections.items()
                if s["status"] == "insufficient_data"))
    elif any(s == "partial" for s in statuses):
        report["status"] = "partial"
        report["status_reason"] = (
            "some sections are partial (missing figures or no "
            "business description stored)")
    else:
        report["status"] = "complete"
    return report


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _md_missing(reason):
    return f"*Not available — {reason}.*\n" if reason else \
        "*Not available.*\n"


def report_to_markdown(report):
    """Render a report dict as a Markdown document."""
    lines = []
    currency = report.get("currency") or "USD"
    name = report.get("company_name") or report["ticker"]
    lines.append(f"# Research report: {name} ({report['ticker']})")
    lines.append("")
    lines.append(f"- **Report date:** {report['report_date']}")
    lines.append(f"- **Report status:** {report['status']}")
    if report.get("status_reason"):
        lines.append(f"- **Status note:** {report['status_reason']}")
    lines.append(f"- **Exchange:** {report.get('exchange') or 'n/a'}")
    lines.append(f"- **Country:** {report.get('country') or 'n/a'}")
    lines.append(f"- **Sector:** {report.get('sector') or 'n/a'}")
    lines.append(f"- **Industry:** {report.get('industry') or 'n/a'}")
    lines.append(
        f"- **Security type:** {report.get('security_type') or 'n/a'}")
    if report.get("sec_cik"):
        lines.append(f"- **SEC CIK:** {report['sec_cik']}")
    price = report.get("price")
    lines.append(
        "- **Price:** "
        + (f"{_money(price, currency)} (as of "
           f"{report.get('price_as_of')}, "
           f"{report.get('price_source') or 'unknown source'})"
           if price is not None else "n/a"))
    lines.append(
        f"- **Statement rows in database:** {report['statement_rows']}")
    lines.append("")
    lines.append(
        "_This report is research, not a recommendation. The final "
        "decision is the reader's._")
    lines.append("")

    sections = report["sections"]

    # Business summary
    sec = sections["business_summary"]
    lines.append("## Business summary")
    lines.append("")
    data = sec["data"]
    lines.append(f"- **Company:** {data.get('company_name') or 'n/a'}")
    lines.append(f"- **Sector:** {data.get('sector') or 'n/a'}")
    lines.append(f"- **Industry:** {data.get('industry') or 'n/a'}")
    lines.append(
        f"- **Statement rows:** {data.get('statement_rows', 0)}")
    lines.append("")
    lines.append(_md_missing(sec["reason"]).strip())
    lines.append("")

    # Financials
    sec = sections["financials"]
    lines.append("## Financials")
    lines.append("")
    if sec["status"] == "insufficient_data":
        lines.append(_md_missing(sec["reason"]).strip())
        lines.append("")
    else:
        data = sec["data"]
        lines.append(
            f"_Source: {sec['source']}. Latest annual period: "
            f"{data.get('latest_annual_period') or 'n/a'}._")
        lines.append("")
        lines.append("### Key figures (latest annual period)")
        lines.append("")
        lines.append("| Figure | Value | Period |")
        lines.append("|---|---|---|")
        for row in data["key_figures"]:
            value = (_money(row["value"], currency)
                     if row["unit"] == "currency" and row["present"]
                     else _fmt(row["value"], row["unit"], currency)
                     if row["present"] else "n/a")
            lines.append(f"| {row['label']} | {value} | "
                         f"{row.get('period') or 'n/a'} |")
        lines.append("")
        lines.append("### Ratios (latest annual period)")
        lines.append("")
        lines.append("| Ratio | Value |")
        lines.append("|---|---|")
        for row in data["ratios_latest"]:
            value = (_fmt(row["value"], row["unit"], currency)
                     if row["present"] else "n/a")
            note = (f" ({row['reason']})"
                    if not row["present"] and row.get("reason") else "")
            lines.append(f"| {row['label']} | {value}{note} |")
        lines.append("")
        lines.append("### Growth")
        lines.append("")
        lines.append(
            "| Series | Latest YoY | CAGR 5y | CAGR 10y | "
            "Cyclicality |")
        lines.append("|---|---|---|---|---|")
        for row in data["growth"]:
            lines.append(
                f"| {row['label']} | "
                f"{_fmt(row['latest_yoy'], 'pct') if row['latest_yoy'] is not None else 'n/a'} | "
                f"{_fmt(row['cagr_5y'], 'pct') if row['cagr_5y'] is not None else 'n/a'} | "
                f"{_fmt(row['cagr_10y'], 'pct') if row['cagr_10y'] is not None else 'n/a'} | "
                f"{row.get('cyclicality') or 'n/a'} |")
        lines.append("")
        lines.append("### Cash flow (latest annual period)")
        lines.append("")
        lines.append("| Measure | Value |")
        lines.append("|---|---|")
        shown = 0
        for row in data["cash_flow"]:
            if not row["present"] or row["value"] is None:
                continue
            value = row["value"]
            text = (_money(value, currency)
                    if abs(value) >= 1_000_000 else f"{value:,.2f}")
            label = row['label'].replace('_', ' ')
            lines.append(f"| {label} | {text} |")
            shown += 1
        if not shown:
            lines.append("| _none_ | n/a |")
        lines.append("")

    # Health and quality
    sec = sections["health_and_quality"]
    lines.append("## Financial health and business quality")
    lines.append("")
    if sec["status"] == "insufficient_data":
        lines.append(_md_missing(sec["reason"]).strip())
        lines.append("")
    else:
        data = sec["data"]
        lines.append(
            f"- **Health verdict:** {data.get('health_verdict') or 'n/a'}")
        for reason in data.get("health_reasons", []):
            lines.append(f"  - {reason}")
        lines.append(
            f"- **Business quality:** "
            f"{data.get('quality_rating') or 'n/a'} "
            f"(mean score {data.get('quality_mean_score')})")
        if data.get("quality_reason"):
            lines.append(f"  - {data['quality_reason']}")
        lines.append("")
        lines.append("### Quality components")
        lines.append("")
        lines.append("| Component | Score (0-2) | Key value |")
        lines.append("|---|---|---|")
        for comp in data.get("quality_components", []):
            value = comp.get("value")
            value_text = ("n/a" if value is None else
                          f"{value:.4f}" if isinstance(value, float)
                          else str(value))
            lines.append(f"| {comp['component']} | {comp['score']} | "
                         f"{value_text} |")
        lines.append("")
        moat = data.get("moat_evidence") or {}
        if moat:
            lines.append(
                f"- **Moat evidence:** {moat.get('assessment') or 'n/a'}")
            lines.append(f"  - {moat.get('evidence') or ''}")
            lines.append(f"  - _Caveat: {moat.get('caveat') or ''}_")
        lines.append(
            f"- **Deterioration verdict:** "
            f"{data.get('deterioration_verdict') or 'n/a'} "
            f"(material deterioration: "
            f"{data.get('material_deterioration')})")
        for reason in data.get("deterioration_reasons", []):
            lines.append(f"  - {reason}")
        lines.append("")
        lines.append("### Risk flags")
        lines.append("")
        flags = data.get("risk_flags", [])
        if not flags:
            lines.append("No risk flags.")
        for flag in flags:
            lines.append(
                f"- **{flag.get('name')}** ({flag.get('severity')}): "
                f"{flag.get('evidence')}")
        lines.append("")
        lines.append("### Not assessable from statements")
        lines.append("")
        for item in data.get("not_assessable", []):
            lines.append(
                f"- **{item.get('item')}:** {item.get('reason')}")
        lines.append("")

    # Valuation
    sec = sections["valuation"]
    lines.append("## Valuation and margin of safety")
    lines.append("")
    if sec["status"] == "insufficient_data":
        lines.append(_md_missing(sec["reason"]).strip())
        lines.append("")
    else:
        data = sec["data"]
        lines.append(f"_Source: {sec['source']}. Sector model: "
                     f"{data.get('sector') or 'n/a'} "
                     f"({data.get('sector_source') or 'n/a'})._")
        if data.get("why_this_model"):
            lines.append(f"_Why this model: {data['why_this_model']}_")
        lines.append("")
        range_ = data.get("fair_value_range") or {}
        lines.append("| Range point | Value per share |")
        lines.append("|---|---|")
        for point in ("conservative", "base", "optimistic"):
            value = range_.get(point)
            lines.append(
                f"| {point} | "
                f"{_money(value, currency) if value is not None else 'n/a'} |")
        lines.append("")
        lines.append(
            f"- **Tier:** {data.get('tier') or 'n/a'}")
        for reason in data.get("tier_reasons", []):
            lines.append(f"  - {reason}")
        lines.append("- **Margin of safety (Graham convention, "
                     "(value - price) / value):**")
        for point in ("conservative", "base", "optimistic"):
            margin = (data.get("margins") or {}).get(point) or {}
            value = margin.get("margin_graham")
            vs_price = margin.get("margin_vs_price")
            lines.append(
                f"  - vs {point}: "
                f"{_fmt(value, 'pct') if value is not None else 'n/a'} "
                f"(Step 21/22 convention: "
                f"{_fmt(vs_price, 'pct') if vs_price is not None else 'n/a'})")
        lines.append("")
        lines.append("### Entry-price ladder")
        lines.append("")
        lines.append("| Discount to conservative | Entry price | "
                     "Implied growth |")
        lines.append("|---|---|---|")
        for rung in data.get("entry_price_ladder", []):
            growth = rung.get("implied_fcf_growth")
            if growth is None:
                growth = rung.get("implied_dividend_growth")
            lines.append(
                f"| {rung.get('discount', 0) * 100:.0f}% | "
                f"{_money(rung.get('entry_price'), currency)} | "
                f"{_fmt(growth, 'pct') if growth is not None else 'n/a'} |")
        alert_price = data.get("strongest_alert_threshold_price")
        if alert_price is not None:
            lines.append("")
            lines.append(
                f"- **Strongest-alert threshold price:** "
                f"{_money(alert_price, currency)}")
        rel_pe = data.get("relative_pe_check") or {}
        if rel_pe.get("present"):
            lines.append(
                f"- **Current P/E:** {rel_pe.get('value'):.2f} "
                f"(latest diluted EPS "
                f"{_money((rel_pe.get('inputs') or {}).get('eps_diluted'), currency)})")
        lines.append("")
        for condition in data.get("conditions_to_change", []):
            lines.append(f"- _What would change the tier: {condition}_")
        lines.append("")

    # Peer comparison
    sec = sections["peer_comparison"]
    lines.append("## Peer comparison")
    lines.append("")
    if sec["status"] == "insufficient_data":
        lines.append(_md_missing(sec["reason"]).strip())
        lines.append("")
    else:
        data = sec["data"]
        lines.append(data.get("summary") or "")
        lines.append("")
        if data.get("not_comparable_peers"):
            lines.append("**Not comparable (no statement data):**")
            for peer, reasons in data["not_comparable_peers"].items():
                lines.append(f"- **{peer}:** {'; '.join(reasons)}")
            lines.append("")
        if data.get("metrics"):
            tickers = [report["ticker"]] + list(
                data.get("comparable_peers", []))
            lines.append("| Metric | " + " | ".join(tickers) + " |")
            lines.append("|" + "---|" * (len(tickers) + 1))
            for metric in data["metrics"]:
                cells = []
                for ticker in tickers:
                    entry = (metric.get("values") or {}).get(ticker) or {}
                    value = entry.get("value")
                    if value is None or not entry.get("present"):
                        cells.append("n/a")
                    else:
                        cells.append(_fmt(value, metric.get("unit"),
                                          currency))
                lines.append(f"| {metric.get('label')} | "
                             + " | ".join(cells) + " |")
            lines.append("")
        for item in data.get("stands_out", []):
            lines.append(f"- **Stands out:** {item}")
        for item in data.get("lags", []):
            lines.append(f"- **Lags:** {item}")
        if data.get("stands_out") or data.get("lags"):
            lines.append("")

    # Risks
    sec = sections["risks"]
    lines.append("## Risks and what would change the view")
    lines.append("")
    risks = sec["data"].get("risks", [])
    if not risks:
        lines.append("No risk flags were produced.")
    for risk in risks:
        lines.append(
            f"- **{risk.get('risk')}** ({risk.get('severity')}): "
            f"{risk.get('evidence') or ''}")
    lines.append("")
    lines.append("**What would change the view:**")
    lines.append("")
    for condition in sec["data"].get("what_would_change_the_view", []):
        lines.append(f"- {condition}")
    lines.append("")

    # Conclusion
    sec = sections["conclusion"]
    lines.append("## Conclusion")
    lines.append("")
    lines.append(sec["data"].get("text", ""))
    lines.append("")
    if report.get("warnings"):
        lines.append("### Report warnings")
        lines.append("")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    if report.get("missing_inputs"):
        lines.append("### Missing inputs")
        lines.append("")
        for item in report["missing_inputs"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def write_report(report, path=None, reports_dir=None):
    """Write the report as Markdown; return the path as a string."""
    markdown = report_to_markdown(report)
    if path is None:
        base = Path(reports_dir) if reports_dir else (
            Path(__file__).resolve().parent.parent / "reports")
        base.mkdir(parents=True, exist_ok=True)
        filename = (f"{report['ticker']}_{report['report_date']}"
                    f"_research_report.md")
        path = base / filename
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return str(path)


def generate_report_file(db_path=None, ticker=None, price_input=None,
                         peers=None, sector_override=None,
                         report_date=None, reports_dir=None,
                         output_path=None):
    """Generate a report from a database file and write it.

    Opens the database read-only (``mode=ro``) so generation can
    never write to it.  Returns ``(report, markdown_path)``.
    """
    if ticker is None:
        raise ValueError("ticker is required")
    db_path = db_path or str(
        Path(__file__).resolve().parent.parent / "database"
        / "assistant.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = generate_report(
            conn, ticker, price_input=price_input, peers=peers,
            sector_override=sector_override, report_date=report_date)
    finally:
        conn.close()
    path = write_report(report, path=output_path,
                        reports_dir=reports_dir)
    return report, path


def run_report(db_path=None, ticker=None, **kwargs):
    """Convenience wrapper for ``generate_report_file``."""
    return generate_report_file(db_path=db_path, ticker=ticker,
                                **kwargs)
