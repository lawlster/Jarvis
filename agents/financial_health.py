"""Step 18 — Financial health and quality analysis.

Builds on Step 16 (``agents/financial_standard.py``) and Step 17
(``agents/financial_ratios.py``): it reuses the standardised values
and the Step 17 ratios, and never re-derives a value by a different
definition.  It makes no network requests and never writes to a
database; callers supply a read connection (tests use a temporary
copy of the production database).

What it produces, for one ticker and one period:

* (a) balance-sheet strength — equity ratio, debt-to-equity, net debt
  position, cash cover of total debt;
* (b) liquidity — current / quick ratios (from Step 17), working
  capital and its 3-year / 5-year trend;
* (c) solvency / debt sustainability — total debt vs operating cash
  flow and vs free cash flow, debt trend over 3 / 5 years, free cash
  flow after dividends; net debt / EBITDA only if EBITDA is
  available (it is not for MSFT — recorded missing, never estimated);
* (d) profitability consistency — net margin, ROE and ROIC over the
  5-year and 10-year windows ending at the period: mean, min, max,
  coefficient of variation, share of profitable years.  Method is
  documented on every metric;
* (e) a distress screen — Altman Z-score components ONLY where the
  inputs exist, plus simple hard distress flags.  A Z-score is
  presented only if ALL five Altman inputs exist; a missing input
  means no Z-score, never a proxy substituted silently.

Overall verdict per period: ``strong`` / ``adequate`` / ``weak`` /
``insufficient data``, always with the reasons listed — never a
single unexplained score.

Verdict rules (documented judgement rules, not universal law):

* ``insufficient data`` — fewer than 3 annual periods of history, or
  the period itself is missing core balance-sheet concepts
  (total_assets, total_equity, current_assets, current_liabilities)
  or the income concepts (revenue, net_income).
* ``weak`` — any hard distress flag: negative net income, negative
  operating cash flow, negative free cash flow, current ratio < 1,
  debt-to-equity > 1, equity ratio < 0.20, or total debt more than
  5x operating cash flow.
* ``strong`` — no hard distress flag AND the 5-year window is fully
  profitable, net-margin coefficient of variation <= 0.20, mean ROE
  >= 15%, and free cash flow positive in the period with
  debt / operating cash flow <= 3.
* ``adequate`` — no hard distress flag, but weaker consistency than
  ``strong`` requires.

Check thresholds used in the sections (also recorded on each check):

* equity ratio >= 0.30, debt-to-equity <= 0.50, current ratio >= 1.0,
  quick ratio >= 1.0, operating cash flow > 0, free cash flow > 0,
  net margin > 0, debt / operating cash flow <= 3.

Price is not used in health scores.  The only price-dependent item
is the Altman market-value-of-equity / total-liabilities component,
computed only when the caller supplies a price input, and labelled
with its as-of date (the only price available is the stale
2026-11-03 snapshot).

Known honest limits (from Steps 16-17, re-stated here):

* EV/EBITDA and interest coverage are unavailable — no D&A or
  interest expense concept exists in the Step 16 concept set, and no
  such key exists in the US raw data.  They are recorded missing,
  never estimated.
* Retained earnings is not a Step 16 concept and no such key exists
  in the US raw data, so the Altman retained-earnings component is
  always missing for MSFT and no Z-score can be presented.
* ``reporting_period`` is authoritative (row ``fiscal_year`` is
  unreliable in 236/242 US rows).  87 US rows mix filing dates and
  there is no restatement detection, so a period is treated as the
  latest-filed values for that period end.
* Data is MSFT only (242 US rows); SG is DBS metadata-only (0
  concepts) and there is no HKEX data, so health assessments only
  exist for MSFT.
"""

import math
import statistics

from agents import financial_ratios as fr

# ---------------------------------------------------------------------------
# Check thresholds (documented judgement rules, see module docstring)
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "equity_ratio_min": 0.30,
    "debt_to_equity_max": 0.50,
    "current_ratio_min": 1.0,
    "quick_ratio_min": 1.0,
    "debt_to_ocf_max": 3.0,
    "debt_to_ocf_hard_max": 5.0,
    "equity_ratio_hard_min": 0.20,
    "debt_to_equity_hard_max": 1.0,
    "roe_strong_min": 0.15,
    "net_margin_cv_strong_max": 0.20,
}

# Altman Z-score (original, for a public manufacturing firm) — used
# only as a component checklist here; the score itself is presented
# only when every component exists.
ALTMAN_WEIGHTS = {
    "working_capital_to_assets": 1.2,
    "retained_earnings_to_assets": 1.4,
    "ebit_to_assets": 3.3,
    "market_value_equity_to_liabilities": 0.6,
    "sales_to_assets": 1.0,
}


# ---------------------------------------------------------------------------
# Small helpers (read the standardised values dict from Step 17)
# ---------------------------------------------------------------------------

def _get(values, statement_type, period, period_type, concept):
    """One standardised value dict, or None if row/concept is absent."""
    row = values.get((statement_type, period, period_type))
    if row is None:
        return None
    return row.get(concept)


def _num(value_dict):
    """Numeric value of a standardised value dict, or None."""
    if value_dict is None or not value_dict.get("present"):
        return None
    return value_dict.get("value")


def _check(name, value, formula, inputs, status, reason=None):
    """One pass/fail/missing check, with its inputs recorded."""
    return {"name": name, "value": value, "present": value is not None,
            "formula": formula, "inputs": inputs, "status": status,
            "reason": reason}


def _pass_fail(value, threshold, direction):
    """'pass'/'fail' for a present value, 'missing' otherwise."""
    if value is None:
        return "missing"
    if direction == "min":
        return "pass" if value >= threshold else "fail"
    return "pass" if value <= threshold else "fail"


def _safe_div(numerator, denominator):
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _pct_change(new, old):
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old)


def _cv(series):
    """Coefficient of variation (sample stdev / mean), or None.

    None when fewer than 2 points, or the mean is not positive (a CV
    of a negative or zero mean is not a stability measure).
    """
    if len(series) < 2:
        return None
    mean = statistics.fmean(series)
    if mean <= 0:
        return None
    return statistics.stdev(series) / mean


# ---------------------------------------------------------------------------
# Profitability consistency over a window of annual periods
# ---------------------------------------------------------------------------

def consistency_metrics(annual_results, periods):
    """Consistency of margins/returns over ``periods`` (annual).

    ``annual_results`` maps reporting period -> Step 17 ratio result
    dict (as returned by ``fr.compute_ratios``).  Returns per-metric
    mean / min / max / coefficient of variation, plus the share of
    profitable years (net_income > 0).  Missing ratios are excluded
    from a metric and the count used is recorded; a metric with fewer
    than 2 usable years has no CV.
    """
    metrics = {}
    for metric in ("net_margin", "roe", "roic"):
        series = []
        for period in periods:
            result = annual_results.get(period)
            if result is None:
                continue
            ratio = result["ratios"].get(metric)
            if ratio is not None and ratio.get("present"):
                series.append(ratio["value"])
        if series:
            metrics[metric] = {
                "years_used": len(series),
                "years_available": len(periods),
                "mean": statistics.fmean(series),
                "min": min(series),
                "max": max(series),
                "cv": _cv(series),
                "method": ("coefficient of variation = sample stdev / "
                           "mean over the annual ratios in the window; "
                           "missing years excluded, count recorded"),
            }
        else:
            metrics[metric] = {
                "years_used": 0, "years_available": len(periods),
                "mean": None, "min": None, "max": None, "cv": None,
                "method": "no usable annual ratios in the window",
            }
    profitable = 0
    checked = 0
    for period in periods:
        result = annual_results.get(period)
        if result is None:
            continue
        ratio = result["ratios"].get("net_margin")
        if ratio is not None and ratio.get("present"):
            checked += 1
            # A year is profitable if net margin is positive; the
            # margin itself is net_income / revenue, so this is
            # equivalent to net_income > 0 with revenue > 0.
            if ratio["value"] > 0:
                profitable += 1
    metrics["profitable_years"] = {
        "profitable": profitable, "checked": checked,
        "share": (profitable / checked) if checked else None,
        "method": "years with net margin > 0 / years with a net margin",
    }
    return metrics


# ---------------------------------------------------------------------------
# Distress screen
# ---------------------------------------------------------------------------

def altman_components(values, period, period_type, price_input=None):
    """Altman Z-score components for one period.

    Returns {"components": {name: {value, present, reason}}, "z_score":
    float or None, "reason": str}.  The Z-score is computed only when
    every component exists.  Retained earnings is not a Step 16
    concept and no such key exists in the US raw data, so for MSFT the
    retained-earnings component is always missing and no Z-score is
    ever presented — that is recorded, not worked around.
    """
    total_assets = _num(_get(values, "balance_sheet", period,
                             period_type, "total_assets"))
    total_liabilities = _num(_get(values, "balance_sheet", period,
                                  period_type, "total_liabilities"))
    current_assets = _num(_get(values, "balance_sheet", period,
                               period_type, "current_assets"))
    current_liabilities = _num(_get(values, "balance_sheet", period,
                                    period_type, "current_liabilities"))
    operating_income = _num(_get(values, "income_statement", period,
                                 period_type, "operating_income"))
    revenue = _num(_get(values, "income_statement", period,
                        period_type, "revenue"))

    components = {}
    wc = None
    if current_assets is not None and current_liabilities is not None:
        wc = current_assets - current_liabilities
    components["working_capital_to_assets"] = {
        "value": _safe_div(wc, total_assets),
        "formula": "working capital / total_assets",
        "inputs": {"working_capital": wc, "total_assets": total_assets},
        "present": wc is not None and total_assets is not None,
        "reason": None if (wc is not None and total_assets is not None)
                  else "missing working capital or total_assets",
    }
    components["retained_earnings_to_assets"] = {
        "value": None,
        "formula": "retained earnings / total_assets",
        "inputs": {"retained_earnings": None,
                   "total_assets": total_assets},
        "present": False,
        "reason": ("retained earnings is not a Step 16 concept and no "
                   "such key exists in the US raw data"),
    }
    components["ebit_to_assets"] = {
        "value": _safe_div(operating_income, total_assets),
        "formula": "operating income (EBIT proxy) / total_assets",
        "inputs": {"operating_income": operating_income,
                   "total_assets": total_assets},
        "present": operating_income is not None
                   and total_assets is not None,
        "reason": None if (operating_income is not None
                           and total_assets is not None)
                  else "missing operating income or total_assets",
    }
    mve = None
    mve_reason = "no price input supplied"
    price_as_of = None
    if price_input is not None:
        price_as_of = price_input.get("as_of_date")
        mcap, _method = fr.market_cap_for(
            price_input,
            _num(_get(values, "income_statement", period, period_type,
                      "shares_outstanding_diluted")))
        mve = mcap
        mve_reason = (None if mcap is not None
                      else "market cap could not be derived from the "
                           "price input")
    components["market_value_equity_to_liabilities"] = {
        "value": _safe_div(mve, total_liabilities),
        "formula": "market value of equity / total_liabilities",
        "inputs": {"market_cap": mve,
                   "total_liabilities": total_liabilities,
                   "price_as_of": price_as_of},
        "present": mve is not None and total_liabilities is not None,
        "reason": mve_reason if (mve is None
                                 or total_liabilities is None) else None,
    }
    components["sales_to_assets"] = {
        "value": _safe_div(revenue, total_assets),
        "formula": "revenue / total_assets",
        "inputs": {"revenue": revenue, "total_assets": total_assets},
        "present": revenue is not None and total_assets is not None,
        "reason": None if (revenue is not None
                           and total_assets is not None)
                  else "missing revenue or total_assets",
    }
    missing = [name for name, comp in components.items()
               if not comp["present"]]
    if missing:
        return {"components": components, "z_score": None,
                "reason": ("Z-score not computed: missing component(s): "
                           + ", ".join(missing)),
                "weights": ALTMAN_WEIGHTS}
    z_score = sum(ALTMAN_WEIGHTS[name] * comp["value"]
                  for name, comp in components.items())
    return {"components": components, "z_score": z_score,
            "reason": None, "weights": ALTMAN_WEIGHTS}


# ---------------------------------------------------------------------------
# One-period health assessment
# ---------------------------------------------------------------------------

def assess_health(conn, ticker, period=None, period_type="annual",
                  price_input=None):
    """Health assessment for one ticker and one period.

    ``conn`` is a read connection (tests use a temporary copy).
    ``period`` defaults to the latest annual period.  ``price_input``
    is used only for the Altman market-value component, labelled
    with its as-of date.  Returns a dict with the five sections, the
    checks, the distress screen, and the overall verdict with reasons.
    Every number carries its inputs and a missing reason where
    applicable.
    """
    values, load_warnings = fr.load_standardised_values(conn, ticker)
    annual_periods = sorted(
        p for (st, p, pt) in values
        if st == "income_statement" and pt == "annual")
    if period is None:
        period = annual_periods[-1] if annual_periods else None
    if period is None:
        return {"ticker": ticker, "reporting_period": None,
                "period_type": period_type, "verdict": "insufficient data",
                "verdict_reasons": ["no annual income rows found"],
                "sections": {}, "distress": {}, "warnings": load_warnings}

    # Ratios for this period (fundamentals only unless a price is given)
    # and for every annual period (for the consistency windows).
    ratio_result = fr.compute_ratios(values, ticker, period=period,
                                     period_type=period_type,
                                     price_input=price_input)
    ratios = ratio_result["ratios"]
    warnings = list(load_warnings) + list(ratio_result["warnings"])

    annual_results = {}
    for p in annual_periods:
        annual_results[p] = fr.compute_ratios(
            values, ticker, period=p, period_type="annual")

    def bal(concept):
        return _num(_get(values, "balance_sheet", period, period_type,
                         concept))

    def flow(concept):
        return _num(_get(values, fr._statement_for(concept), period,
                         period_type, concept))

    def ratio_value(name):
        ratio = ratios.get(name)
        return ratio["value"] if ratio and ratio.get("present") else None

    total_assets = bal("total_assets")
    total_equity = bal("total_equity")
    total_debt = bal("total_debt")
    cash = bal("cash_and_equivalents")
    current_assets = bal("current_assets")
    current_liabilities = bal("current_liabilities")
    inventory = bal("inventory")
    operating_cash_flow = flow("operating_cash_flow")
    free_cash_flow = flow("free_cash_flow")
    dividends_paid = flow("dividends_paid")
    net_income = flow("net_income")

    # -- (a) Balance-sheet strength ------------------------------------
    equity_ratio = _safe_div(total_equity, total_assets)
    net_debt = None
    if total_debt is not None and cash is not None:
        net_debt = total_debt - cash
    cash_cover = _safe_div(cash, total_debt)
    debt_to_equity = ratio_value("debt_to_equity")
    balance_strength = {
        "equity_ratio": _check(
            "equity_ratio", equity_ratio,
            "total_equity / total_assets",
            {"total_equity": total_equity, "total_assets": total_assets},
            _pass_fail(equity_ratio, THRESHOLDS["equity_ratio_min"],
                       "min")),
        "debt_to_equity": _check(
            "debt_to_equity", debt_to_equity,
            "total_debt / total_equity (Step 17 ratio)",
            {"total_debt": total_debt, "total_equity": total_equity},
            _pass_fail(debt_to_equity, THRESHOLDS["debt_to_equity_max"],
                       "max")),
        "net_debt": _check(
            "net_debt", net_debt, "total_debt - cash_and_equivalents",
            {"total_debt": total_debt, "cash_and_equivalents": cash},
            "pass" if net_debt is not None else "missing",
            None if net_debt is not None
            else "missing total_debt or cash_and_equivalents"),
        "net_debt_position": ("net cash" if net_debt is not None
                              and net_debt < 0 else
                              "net debt" if net_debt is not None
                              else None),
        "cash_cover_of_debt": _check(
            "cash_cover_of_debt", cash_cover,
            "cash_and_equivalents / total_debt",
            {"cash_and_equivalents": cash, "total_debt": total_debt},
            "pass" if cash_cover is not None else "missing",
            None if cash_cover is not None
            else "missing cash_and_equivalents or total_debt"),
    }

    # -- (b) Liquidity ---------------------------------------------------
    working_capital = None
    if current_assets is not None and current_liabilities is not None:
        working_capital = current_assets - current_liabilities
    liquidity = {
        "current_ratio": _check(
            "current_ratio", ratio_value("current_ratio"),
            "current_assets / current_liabilities (Step 17 ratio)",
            {"current_assets": current_assets,
             "current_liabilities": current_liabilities},
            _pass_fail(ratio_value("current_ratio"),
                       THRESHOLDS["current_ratio_min"], "min")),
        "quick_ratio": _check(
            "quick_ratio", ratio_value("quick_ratio"),
            "(current_assets - inventory) / current_liabilities "
            "(Step 17 ratio)",
            {"current_assets": current_assets, "inventory": inventory,
             "current_liabilities": current_liabilities},
            _pass_fail(ratio_value("quick_ratio"),
                       THRESHOLDS["quick_ratio_min"], "min")),
        "working_capital": _check(
            "working_capital", working_capital,
            "current_assets - current_liabilities",
            {"current_assets": current_assets,
             "current_liabilities": current_liabilities},
            "pass" if working_capital is not None else "missing",
            None if working_capital is not None
            else "missing current_assets or current_liabilities"),
        "working_capital_trend": _trend(
            values, "working_capital", period, period_type),
    }

    # -- (c) Solvency / debt sustainability ------------------------------
    debt_to_ocf = _safe_div(total_debt, operating_cash_flow)
    debt_to_fcf = _safe_div(total_debt, free_cash_flow)
    fcf_after_dividends = None
    if free_cash_flow is not None and dividends_paid is not None:
        fcf_after_dividends = free_cash_flow - dividends_paid
    solvency = {
        "debt_to_operating_cash_flow": _check(
            "debt_to_operating_cash_flow", debt_to_ocf,
            "total_debt / operating_cash_flow",
            {"total_debt": total_debt,
             "operating_cash_flow": operating_cash_flow},
            _pass_fail(debt_to_ocf, THRESHOLDS["debt_to_ocf_max"],
                       "max")),
        "debt_to_free_cash_flow": _check(
            "debt_to_free_cash_flow", debt_to_fcf,
            "total_debt / free_cash_flow",
            {"total_debt": total_debt,
             "free_cash_flow": free_cash_flow},
            _pass_fail(debt_to_fcf, THRESHOLDS["debt_to_ocf_max"],
                       "max")),
        "free_cash_flow_after_dividends": _check(
            "free_cash_flow_after_dividends", fcf_after_dividends,
            "free_cash_flow - dividends_paid",
            {"free_cash_flow": free_cash_flow,
             "dividends_paid": dividends_paid},
            "pass" if fcf_after_dividends is not None else "missing",
            None if fcf_after_dividends is not None
            else "missing free_cash_flow or dividends_paid"),
        "net_debt_to_ebitda": _check(
            "net_debt_to_ebitda", None, "net_debt / EBITDA",
            {"net_debt": net_debt, "ebitda": None},
            "missing",
            "no depreciation/amortisation or EBITDA concept in the "
            "Step 16 concept set, and no such key exists in the US "
            "raw data — not estimated"),
        "debt_trend": _trend(values, "total_debt", period, period_type),
    }

    # -- (d) Profitability consistency -----------------------------------
    idx = annual_periods.index(period) if period in annual_periods \
        else len(annual_periods) - 1
    window_5 = annual_periods[max(0, idx - 4):idx + 1]
    window_10 = annual_periods[max(0, idx - 9):idx + 1]
    consistency = {
        "window_5y": {
            "periods": window_5,
            "metrics": consistency_metrics(annual_results, window_5),
        },
        "window_10y": {
            "periods": window_10,
            "metrics": consistency_metrics(annual_results, window_10),
        },
        "method": ("annual Step 17 ratios in the window ending at the "
                   "period; CV = sample stdev / mean; missing years "
                   "excluded with the count recorded"),
    }

    # -- (e) Distress screen ---------------------------------------------
    altman = altman_components(values, period, period_type,
                               price_input=price_input)
    flags = []
    if net_income is not None and net_income < 0:
        flags.append("negative net income in the period")
    if operating_cash_flow is not None and operating_cash_flow < 0:
        flags.append("negative operating cash flow in the period")
    if free_cash_flow is not None and free_cash_flow < 0:
        flags.append("negative free cash flow in the period")
    if ratio_value("current_ratio") is not None \
            and ratio_value("current_ratio") < 1:
        flags.append("current ratio below 1")
    if debt_to_equity is not None \
            and debt_to_equity > THRESHOLDS["debt_to_equity_hard_max"]:
        flags.append("debt-to-equity above 1")
    if equity_ratio is not None \
            and equity_ratio < THRESHOLDS["equity_ratio_hard_min"]:
        flags.append("equity ratio below 0.20")
    if debt_to_ocf is not None \
            and debt_to_ocf > THRESHOLDS["debt_to_ocf_hard_max"]:
        flags.append("total debt more than 5x operating cash flow")
    distress = {"altman": altman, "hard_flags": flags,
                "method": ("Altman Z-score presented only if all five "
                           "components exist; otherwise the missing "
                           "components are listed. Hard flags are "
                           "single-period distress indicators, each "
                           "recorded with its threshold.")}

    # -- Overall verdict --------------------------------------------------
    verdict, reasons = _verdict(
        period, annual_periods, total_assets, total_equity,
        current_assets, current_liabilities, flow, flags,
        consistency["window_5y"]["metrics"], free_cash_flow,
        debt_to_ocf)

    return {"ticker": ticker, "reporting_period": period,
            "period_type": period_type,
            "price_as_of": (price_input or {}).get("as_of_date"),
            "verdict": verdict, "verdict_reasons": reasons,
            "sections": {"balance_sheet_strength": balance_strength,
                         "liquidity": liquidity,
                         "solvency": solvency,
                         "profitability_consistency": consistency},
            "distress": distress, "warnings": warnings}


def _trend(values, concept, period, period_type):
    """3-year and 5-year change for a balance/flow concept.

    Balance concepts are point-in-time; flow concepts are annual
    totals, so the same comparison applies.  Returns {change_3y,
    change_5y, periods} with None where the earlier period is absent.
    """
    if concept == "working_capital":
        def get(p):
            ca = _num(_get(values, "balance_sheet", p, period_type,
                           "current_assets"))
            cl = _num(_get(values, "balance_sheet", p, period_type,
                           "current_liabilities"))
            return ca - cl if ca is not None and cl is not None else None
    else:
        stmt = fr._statement_for(concept)

        def get(p):
            return _num(_get(values, stmt, p, period_type, concept))

    # Periods where the concept is actually present.  Working capital
    # is not a stored concept, so its periods are the balance-sheet
    # periods where both current assets and current liabilities exist.
    periods = sorted({p for (st, p, pt) in values
                      if pt == period_type and get(p) is not None})
    if period not in periods:
        return {"change_3y": None, "change_5y": None,
                "reason": f"no {concept} value for {period}",
                "periods_compared": {}}
    idx = periods.index(period)
    now = get(period)
    out = {"change_3y": None, "change_5y": None,
           "periods_compared": {"current": period}}
    for years, key in ((3, "change_3y"), (5, "change_5y")):
        if idx >= years:
            earlier = periods[idx - years]
            out[key] = _pct_change(now, get(earlier))
            out["periods_compared"][f"{years}y_ago"] = earlier
    out["reason"] = (None if out["change_3y"] is not None
                     or out["change_5y"] is not None
                     else "fewer than 3 comparable periods")
    return out


def _verdict(period, annual_periods, total_assets, total_equity,
             current_assets, current_liabilities, flow, flags,
             metrics_5y, free_cash_flow, debt_to_ocf):
    """Overall verdict with reasons (rules in the module docstring)."""
    core_missing = [name for name, value in (
        ("total_assets", total_assets), ("total_equity", total_equity),
        ("current_assets", current_assets),
        ("current_liabilities", current_liabilities),
        ("revenue", flow("revenue")),
        ("net_income", flow("net_income"))) if value is None]
    if len(annual_periods) < 3:
        return ("insufficient data",
                [f"only {len(annual_periods)} annual period(s) of "
                 "history; at least 3 required"])
    if core_missing:
        return ("insufficient data",
                ["core concepts missing for the period: "
                 + ", ".join(core_missing)])
    if flags:
        return ("weak", ["hard distress flag(s): " + "; ".join(flags)])
    reasons = []
    profitable = metrics_5y["profitable_years"]
    margin_cv = metrics_5y["net_margin"]["cv"]
    roe_mean = metrics_5y["roe"]["mean"]
    strong = True
    if profitable["checked"] < 5 or profitable["share"] != 1.0:
        strong = False
        reasons.append(
            "5-year window is not fully profitable "
            f"({profitable['profitable']}/{profitable['checked']} "
            "profitable years)")
    if margin_cv is None or margin_cv > THRESHOLDS["net_margin_cv_strong_max"]:
        strong = False
        reasons.append(
            "5-year net-margin CV is "
            f"{margin_cv if margin_cv is not None else 'unavailable'} "
            "(strong requires <= 0.20)")
    if roe_mean is None or roe_mean < THRESHOLDS["roe_strong_min"]:
        strong = False
        reasons.append(
            "5-year mean ROE is "
            f"{roe_mean if roe_mean is not None else 'unavailable'} "
            "(strong requires >= 15%)")
    if free_cash_flow is None or free_cash_flow <= 0:
        strong = False
        reasons.append("free cash flow is not positive in the period")
    if debt_to_ocf is None or debt_to_ocf > THRESHOLDS["debt_to_ocf_max"]:
        strong = False
        reasons.append(
            "debt / operating cash flow is "
            f"{debt_to_ocf if debt_to_ocf is not None else 'unavailable'} "
            "(strong requires <= 3)")
    if strong:
        return ("strong", [
            "no hard distress flags",
            f"5-year window fully profitable "
            f"({profitable['profitable']}/{profitable['checked']} years)",
            f"5-year net-margin CV {margin_cv:.3f} (<= 0.20)",
            f"5-year mean ROE {roe_mean:.1%} (>= 15%)",
            "free cash flow positive in the period",
            f"debt / operating cash flow {debt_to_ocf:.2f} (<= 3)",
        ])
    reasons.insert(0, "no hard distress flags")
    return ("adequate", reasons)


def assess_all_periods(conn, ticker, price_input=None):
    """Annual health assessments for every annual period.

    Price-dependent items are computed only for the latest annual
    period (the price is as of one date).  Returns a list of
    per-period assessments, latest first for the report.
    """
    values, _warnings = fr.load_standardised_values(conn, ticker)
    annual_periods = sorted(
        p for (st, p, pt) in values
        if st == "income_statement" and pt == "annual")
    results = []
    for i, period in enumerate(annual_periods):
        price = price_input if i == len(annual_periods) - 1 else None
        results.append(assess_health(conn, ticker, period=period,
                                     period_type="annual",
                                     price_input=price))
    return results
