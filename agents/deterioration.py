"""Step 27 — Financial risk and deterioration detection.

Deepens the deterioration signal that Step 23 uses for its
strongest-alert gate.  It reuses the Step 18 health, Step 19
growth and Step 20 cash-flow assessments — it re-derives nothing
— and turns them into a structured, four-area deterioration
assessment for one ticker and one period:

* (a) trend breaks — revenue, operating income, net income,
  operating cash flow and free cash flow direction over 3y / 5y /
  10y: sustained decline vs a one-off dip.  A dip year is an
  "identified one-off" when the level recovers past the pre-dip
  level within two years (the FY2015 Nokia write-down and FY2018
  tax charge in MSFT's history are the reference cases); a dip in
  the latest year has no following year yet, so it is a warning,
  not a verdict — recovery cannot be observed yet.
* (b) margin erosion — gross / operating / net margin trends over
  5y, with variability (CV) measured, not eyeballed.
* (c) leverage and liquidity drift — debt trajectory, debt /
  operating cash flow, current / quick ratio trends, working
  capital direction.  A working-capital fall with the current
  ratio still above 1 is a WARNING, never deterioration on its
  own (the MSFT watch item: working capital down 59% over 5y,
  current ratio still 1.23).
* (d) cash-flow stress — FCF conversion falling, capex / OCF
  rising, dividends / buybacks exceeding FCF.

Each area gets a verdict — ``deteriorating`` / ``warning`` /
``stable`` / ``improving`` / ``insufficient data`` — with the
evidence listed.  The overall verdict and the single boolean
``material_deterioration`` (the Step 23 gate) follow an explicit
rule, documented below and in code:

* ``insufficient data`` — fewer than 3 annual periods of history,
  or no area could be assessed at all.
* ``deteriorating`` (material deterioration TRUE) — two or more
  areas deteriorating, OR one deteriorating area plus a Step 18
  health verdict of ``weak``, OR latest annual net income < 0,
  OR latest annual operating cash flow < 0.  The last two are hard
  distress, not trends.
* ``warning`` (material deterioration FALSE) — exactly one
  deteriorating area without hard distress, OR two or more warning
  areas.
* ``improving`` — no warning or deteriorating area, and at least
  two areas improving.
* ``stable`` — anything else (at most one warning area).

``material_deterioration`` is True only for ``deteriorating``,
False for warning / stable / improving, and None (unknown) for
``insufficient data`` — an unknown never clears a name for a
strongest alert, exactly as in Step 23.

Risk flags: one named flag per warning or deteriorating area,
with a severity (``high`` for deteriorating, ``medium`` for
warning), the evidence, and the trend — so the scanner, the
detector and a briefing can surface them directly.

Step 23 wiring: ``margin_of_safety.assess_deterioration`` accepts
an optional ``deterioration_27`` argument (this module's result).
When it is supplied and usable (verdict not ``insufficient
data``), the Step 23 flag takes its boolean from Step 27 and its
signals from Step 27's high-severity flags, corroborated against
Step 23's simpler rule from the same inputs (a disagreement is
recorded as a caution, never hidden).  When Step 27 is
unavailable, Step 23 keeps its existing Step 18/19/20 logic
unchanged.  The rule above is calibrated so Step 23's MSFT
result is unchanged: no material deterioration.

This module makes no network requests and never writes to a
database; callers supply a read connection (tests use a temporary
copy of the production database).  No price is used anywhere.
"""

import statistics

from agents import financial_cashflow as cf
from agents import financial_growth as fg
from agents import financial_health as fh
from agents import financial_ratios as fr

AREA_VERDICTS = ("deteriorating", "warning", "stable", "improving",
                 "insufficient_data")
OVERALL_VERDICTS = AREA_VERDICTS

# Thresholds — documented judgement rules, not universal law.  See
# the module docstring and the per-area functions.
THRESHOLDS = {
    # trend breaks
    "margin_cv_warning": 0.30,
    # margin erosion (percentage points, fractions of 1)
    "net_margin_erode_deteriorating_pp": -0.05,
    "net_margin_erode_warning_pp": -0.02,
    "net_margin_improve_pp": 0.02,
    "net_margin_cv_improving_max": 0.10,
    "operating_margin_erode_warning_pp": -0.05,
    # leverage / liquidity
    "working_capital_fall_warning": -0.25,
    "current_ratio_deteriorating_below": 1.0,
    "current_ratio_fall_warning": -0.5,
    "debt_to_ocf_deteriorating_above": 5.0,
    "debt_to_ocf_rise_warning": 1.0,
    "debt_rise_warning_5y": 0.25,
    "debt_rise_deteriorating_5y": 0.50,
    # cash-flow stress
    "fcf_to_ni_warning_ratio": 0.70,
    "capex_to_ocf_warning_level": 0.50,
    "capex_to_ocf_rise_warning": 0.20,
}

MATERIAL_DETERIORATION_RULE = (
    "material deterioration = overall verdict 'deteriorating': two or "
    "more areas deteriorating, OR one deteriorating area plus a Step "
    "18 health verdict of 'weak', OR latest annual net income < 0, "
    "OR latest annual operating cash flow < 0. One deteriorating "
    "area alone, or two or more warning areas, is a 'warning' "
    "overall (material deterioration FALSE). A working-capital fall "
    "with the current ratio still above 1, an FCF fall alone, and a "
    "recovered one-off dip are warnings/cautions, never "
    "deterioration on their own."
)

TREND_CONCEPTS = ("revenue", "operating_income", "net_income",
                  "operating_cash_flow", "free_cash_flow")
CORE_TREND_CONCEPTS = ("revenue", "operating_income", "net_income",
                       "operating_cash_flow")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(value_dict):
    if value_dict is None or not value_dict.get("present"):
        return None
    return value_dict.get("value")


def _get(values, statement_type, period, period_type, concept):
    return fr._get(values, statement_type, period, period_type, concept)


def _cv(series):
    if len(series) < 2:
        return None
    mean = statistics.fmean(series)
    if mean <= 0:
        return None
    return statistics.stdev(series) / mean


def _concept_series(values, concept, period_type="annual",
                    end_period=None):
    """Annual standardised values for one concept, sorted by period,
    truncated at ``end_period`` when given (so a historical
    assessment never uses later years)."""
    statement = fr._statement_for(concept)
    periods = sorted(p for (st, p, pt) in values
                     if st == statement and pt == period_type
                     and (end_period is None or p <= end_period))
    return [(p, _num(_get(values, statement, p, period_type, concept)))
            for p in periods]


def _ratio_series(values, ticker, annual_periods, metric):
    """Annual Step 17 ratios for ``metric`` over ``annual_periods``."""
    series = []
    for period in annual_periods:
        result = fr.compute_ratios(values, ticker, period=period,
                                   period_type="annual")
        ratio = result["ratios"].get(metric)
        if ratio is not None and ratio.get("present"):
            series.append((period, ratio["value"]))
    return series


def _growth_for(values, end_period):
    """Step 19 growth figures per series, truncated at ``end_period``.

    Built from Step 19's own helpers (``_series`` / ``yoy_growth`` /
    ``cagr``) on series truncated at the assessed period, so a
    historical assessment never uses later years' growth.
    """
    out = {}
    for concept in fg.GROWTH_SERIES:
        series = [(p, v) for p, v in fg._series(values, concept)
                  if p <= end_period]
        yoy = fg.yoy_growth(series)
        out[concept] = {
            "series": series,
            "yoy": yoy,
            "latest_yoy": yoy[-1] if yoy else None,
            "cagr": {years: fg.cagr(series, years)
                     for years in fg.CAGR_WINDOWS},
        }
    return out


def _one_off_dips(series, yoy):
    """Dip years that recovered past the pre-dip level within 2 years.

    ``series`` is [(period, value)] and ``yoy`` is Step 19's YoY
    list for the same series (``yoy[j]`` compares ``series[j+1]``
    with ``series[j]``).  A dip at ``yoy[j]`` is a recovered one-off
    when some value in the two years after the dip year is above
    the pre-dip level ``series[j]``.  Returns a list of
    {"dip_period", "dip_yoy", "recovered_by_period"} dicts.
    """
    by_period = dict(series)
    recovered = []
    for j, entry in enumerate(yoy):
        if not entry["present"] or entry["value"] >= 0:
            continue
        pre_dip = series[j][1]
        dip_period = entry["period"]
        if pre_dip is None:
            continue
        for k in (j + 2, j + 3):
            if k < len(series):
                value = series[k][1]
                if value is not None and value > pre_dip:
                    recovered.append({
                        "dip_period": dip_period,
                        "dip_yoy": entry["value"],
                        "recovered_by_period": series[k][0],
                        "pre_dip_value": pre_dip,
                        "recovered_value": value,
                    })
                    break
    return recovered


def _normalise_growth(growth, values, period, latest_period):
    """Growth figures in the shape ``_trend_breaks`` needs.

    ``_growth_for`` (truncated at the assessed period) is the
    canonical source: a historical assessment must never use
    later years' growth.  A caller-supplied Step 19
    ``analyse_growth`` result is accepted only for the latest
    period, and its series (lists of {"period", "value"} dicts)
    are converted to (period, value) tuples.  Returns
    {concept: {"series", "yoy", "latest_yoy", "cagr"}}.
    """
    if growth is None or period != latest_period:
        return _growth_for(values, period)
    out = {}
    for concept in fg.GROWTH_SERIES:
        analysis = (growth.get("series", {}) or {}).get(concept, {})
        series = [(item["period"], item["value"])
                  for item in analysis.get("series", [])]
        yoy = analysis.get("yoy", [])
        out[concept] = {
            "series": series,
            "yoy": yoy,
            "latest_yoy": analysis.get("latest_yoy"),
            "cagr": analysis.get("cagr", {}),
        }
    return out


def _area(verdict, evidence, details=None, flags=()):
    return {"verdict": verdict, "evidence": list(evidence),
            "details": details or {}, "flags": list(flags)}


def _flag(name, severity, area, evidence, trend):
    return {"name": name, "severity": severity, "area": area,
            "evidence": evidence, "trend": trend}


# ---------------------------------------------------------------------------
# (a) Trend breaks
# ---------------------------------------------------------------------------

def _trend_breaks(values, growth, annual_periods):
    """Trend-break area from Step 19 growth figures."""
    evidence = []
    details = {}
    statuses = {}
    one_offs = {}
    for concept in TREND_CONCEPTS:
        analysis = growth.get(concept, {})
        series = analysis.get("series", [])
        yoy = analysis.get("yoy", [])
        cagr = analysis.get("cagr", {})
        latest_yoy = analysis.get("latest_yoy")
        cagr3 = cagr.get(3, {}).get("value") \
            if cagr.get(3, {}).get("present") else None
        cagr5 = cagr.get(5, {}).get("value") \
            if cagr.get(5, {}).get("present") else None
        declines_5y = sum(1 for g in yoy[-5:]
                          if g["present"] and g["value"] < 0)
        declines_3y = sum(1 for g in yoy[-3:]
                          if g["present"] and g["value"] < 0)
        latest = latest_yoy["value"] if latest_yoy \
            and latest_yoy.get("present") else None
        recovered = _one_off_dips(series, yoy)
        one_offs[concept] = recovered
        if recovered:
            evidence.append(
                f"{concept}: {len(recovered)} recovered one-off dip(s) "
                "in history — " + "; ".join(
                    f"{d['dip_period']} {d['dip_yoy']:.1%} "
                    f"(recovered by {d['recovered_by_period']})"
                    for d in recovered))
        details[concept] = {
            "cagr_3y": cagr3, "cagr_5y": cagr5,
            "latest_yoy": latest,
            "declines_5y": declines_5y,
            "declines_3y": declines_3y,
            "recovered_one_off_dips": recovered,
        }
        if cagr5 is not None and cagr3 is not None \
                and cagr5 < 0 and cagr3 < 0:
            statuses[concept] = "sustained decline"
        elif latest is not None and latest < 0 \
                and (cagr3 is not None and cagr3 < 0
                     or declines_3y >= 2):
            statuses[concept] = "recent decline"
        elif latest is not None and latest < 0:
            statuses[concept] = "softening"
        else:
            statuses[concept] = "growing"
        details[concept]["status"] = statuses[concept]
    for concept, status in statuses.items():
        if status != "growing":
            latest_yoy = details[concept]["latest_yoy"]
            cagr5 = details[concept]["cagr_5y"]
            yoy_text = (f"{latest_yoy:.1%}" if latest_yoy is not None
                        else "unavailable")
            cagr_text = (f"{cagr5:.1%}" if cagr5 is not None
                         else "unavailable")
            evidence.append(
                f"{concept}: {status} (latest YoY {yoy_text}, "
                f"5y CAGR {cagr_text})")
    usable = any(
        details.get(c, {}).get("latest_yoy") is not None
        or details.get(c, {}).get("cagr_5y") is not None
        for c in TREND_CONCEPTS)
    sustained_core = [c for c in CORE_TREND_CONCEPTS
                      if statuses.get(c) == "sustained decline"]
    broad_decline = all(
        details.get(c, {}).get("latest_yoy") is not None
        and details[c]["latest_yoy"] < 0
        for c in ("revenue", "net_income", "operating_cash_flow"))
    flags = []
    if not usable:
        return _area(
            "insufficient data",
            ["no usable growth figures for any series in the window"],
            details)
    if sustained_core or broad_decline:
        verdict = "deteriorating"
        flags.append(_flag(
            "sustained decline in core series", "high", "trend_breaks",
            "; ".join(
                f"{c} in sustained decline (5y CAGR "
                f"{details[c]['cagr_5y']:.1%})" for c in sustained_core)
            or "revenue, net income and operating cash flow all fell "
               "in the latest year",
            "falling"))
    elif any(s in ("recent decline", "softening")
             for s in statuses.values()):
        verdict = "warning"
        soft = [c for c, s in statuses.items()
                if s in ("recent decline", "softening")]
        for concept in soft:
            latest_yoy = details[concept]["latest_yoy"]
            cagr5 = details[concept]["cagr_5y"]
            yoy_text = (f"{latest_yoy:.1%}" if latest_yoy is not None
                        else "unavailable")
            cagr_text = (f"{cagr5:.1%}" if cagr5 is not None
                         else "unavailable")
            if concept == "free_cash_flow":
                flags.append(_flag(
                    "free cash flow softening", "medium",
                    "trend_breaks",
                    f"FCF latest YoY {yoy_text}, "
                    f"{details[concept]['declines_5y']} declines in "
                    f"5y, but 5y CAGR still {cagr_text} — a capex "
                    "effect while operating cash flow grows, not a "
                    "business decline on its own",
                    "softening"))
            else:
                flags.append(_flag(
                    f"{concept.replace('_', ' ')} softening", "medium",
                    "trend_breaks",
                    f"{concept} latest YoY {yoy_text}, 5y CAGR "
                    f"{cagr_text}",
                    "softening"))
    elif all(details.get(c, {}).get("cagr_5y") is not None
             and details[c]["cagr_5y"] > 0.05
             for c in CORE_TREND_CONCEPTS) \
            and not any(details.get(c, {}).get("declines_3y")
                        for c in CORE_TREND_CONCEPTS):
        verdict = "improving"
    elif not details:
        verdict = "insufficient data"
    else:
        verdict = "stable"
    if verdict == "stable" and not evidence:
        evidence.append(
            "no sustained decline, recent decline or softening in "
            "any of revenue, operating income, net income, "
            "operating cash flow or free cash flow")
    return _area(verdict, evidence, details, flags)


# ---------------------------------------------------------------------------
# (b) Margin erosion
# ---------------------------------------------------------------------------

def _margin_erosion(values, ticker, annual_periods):
    """Margin-erosion area from Step 17 annual margins."""
    evidence = []
    details = {}
    for metric in ("gross_margin", "operating_margin", "net_margin"):
        series = _ratio_series(values, ticker, annual_periods, metric)
        window = series[-5:]
        vals = [v for _, v in window]
        details[metric] = {
            "latest": vals[-1] if vals else None,
            "five_years_ago": vals[0] if vals else None,
            "change_5y_pp": (vals[-1] - vals[0]) if len(vals) >= 2
                            else None,
            "mean_5y": statistics.fmean(vals) if vals else None,
            "cv_5y": _cv(vals),
            "years": len(vals),
        }
        if vals:
            evidence.append(
                f"{metric}: 5y {details[metric]['mean_5y']:.1%} mean, "
                f"latest {details[metric]['latest']:.1%} "
                f"({details[metric]['change_5y_pp']:+.1%} over 5y, "
                f"CV {details[metric]['cv_5y']:.3f})"
                if details[metric]['cv_5y'] is not None else
                f"{metric}: latest "
                f"{details[metric]['latest']:.1%}")
    net = details.get("net_margin", {})
    operating = details.get("operating_margin", {})
    flags = []
    if net.get("latest") is None:
        return _area("insufficient data",
                     ["no usable net margin values in the 5-year window"],
                     details)
    if net["latest"] < 0 or (
            net.get("change_5y_pp") is not None
            and net["change_5y_pp"]
            <= THRESHOLDS["net_margin_erode_deteriorating_pp"]
            and net["latest"] < net["mean_5y"]):
        verdict = "deteriorating"
        flags.append(_flag(
            "margin erosion", "high", "margin_erosion",
            f"net margin {net['change_5y_pp']:+.1%} over 5y to "
            f"{net['latest']:.1%}, below the 5y mean "
            f"{net['mean_5y']:.1%}",
            "eroding"))
    elif (
            net.get("change_5y_pp") is not None
            and THRESHOLDS["net_margin_erode_deteriorating_pp"]
            < net["change_5y_pp"]
            <= THRESHOLDS["net_margin_erode_warning_pp"]) \
            or (net.get("cv_5y") is not None
                and net["cv_5y"] > THRESHOLDS["margin_cv_warning"]) \
            or (operating.get("change_5y_pp") is not None
                and operating["change_5y_pp"]
                <= THRESHOLDS["operating_margin_erode_warning_pp"]):
        verdict = "warning"
        flags.append(_flag(
            "margin erosion", "medium", "margin_erosion",
            f"net margin {net['change_5y_pp']:+.1%} over 5y "
            f"(CV {net['cv_5y']:.3f})"
            if net.get("change_5y_pp") is not None else
            "net margin variability is high "
            f"(CV {net['cv_5y']:.3f})",
            "eroding"))
    elif (net.get("change_5y_pp") is not None
          and net["change_5y_pp"]
          >= THRESHOLDS["net_margin_improve_pp"]
          and net.get("cv_5y") is not None
          and net["cv_5y"]
          <= THRESHOLDS["net_margin_cv_improving_max"]):
        verdict = "improving"
    else:
        verdict = "stable"
    return _area(verdict, evidence, details, flags)


# ---------------------------------------------------------------------------
# (c) Leverage and liquidity drift
# ---------------------------------------------------------------------------

def _leverage_liquidity(values, ticker, annual_periods, health):
    """Leverage / liquidity drift, from Step 18 sections + series."""
    evidence = []
    details = {}
    sections = (health or {}).get("sections", {})
    liquidity = sections.get("liquidity", {})
    solvency = sections.get("solvency", {})
    wc_trend = liquidity.get("working_capital_trend", {})
    debt_trend = solvency.get("debt_trend", {})
    current = liquidity.get("current_ratio", {})
    debt_ocf = solvency.get("debt_to_operating_cash_flow", {})
    debt_fcf = solvency.get("debt_to_free_cash_flow", {})
    details = {
        "working_capital_change_3y": wc_trend.get("change_3y"),
        "working_capital_change_5y": wc_trend.get("change_5y"),
        "debt_change_3y": debt_trend.get("change_3y"),
        "debt_change_5y": debt_trend.get("change_5y"),
        "current_ratio": current.get("value"),
        "debt_to_operating_cash_flow": debt_ocf.get("value"),
        "debt_to_free_cash_flow": debt_fcf.get("value"),
        "health_verdict": (health or {}).get("verdict"),
    }
    # Series for the 5-year moves (Step 17 ratios + balance values).
    current_series = _ratio_series(values, ticker, annual_periods,
                                   "current_ratio")
    window = current_series[-6:]
    if len(window) >= 2:
        details["current_ratio_5y_ago"] = window[0][1]
        details["current_ratio_change_5y"] = \
            window[-1][1] - window[0][1]
    debt_series = _concept_series(values, "total_debt",
                                  end_period=annual_periods[-1])
    ocf_series = dict(_concept_series(
        values, "operating_cash_flow", end_period=annual_periods[-1]))
    debt_ocf_5y_ago = None
    if len(debt_series) >= 6:
        d_ago, o_ago = debt_series[-6][1], ocf_series.get(
            debt_series[-6][0])
        if d_ago is not None and o_ago:
            debt_ocf_5y_ago = d_ago / o_ago
    details["debt_to_operating_cash_flow_5y_ago"] = debt_ocf_5y_ago
    if details["debt_to_operating_cash_flow"] is not None \
            and debt_ocf_5y_ago is not None:
        details["debt_to_operating_cash_flow_change_5y"] = \
            details["debt_to_operating_cash_flow"] - debt_ocf_5y_ago
    wc = liquidity.get("working_capital", {})
    details["working_capital"] = wc.get("value")
    if details["working_capital_change_5y"] is not None:
        evidence.append(
            f"working capital "
            f"{details['working_capital_change_5y']:+.1%} over 5y")
    if details["debt_change_5y"] is not None:
        evidence.append(
            f"total debt {details['debt_change_5y']:+.1%} over 5y")
    if details["current_ratio"] is not None:
        evidence.append(
            f"current ratio {details['current_ratio']:.2f}"
            + (f" (was "
               f"{details['current_ratio_5y_ago']:.2f} 5y ago)"
               if details.get("current_ratio_5y_ago") is not None
               else ""))
    if details["debt_to_operating_cash_flow"] is not None:
        evidence.append(
            f"debt / operating cash flow "
            f"{details['debt_to_operating_cash_flow']:.2f}x")
    health_verdict = details["health_verdict"]
    flags = []
    if details["current_ratio"] is not None \
            and details["current_ratio"] \
            < THRESHOLDS["current_ratio_deteriorating_below"]:
        verdict = "deteriorating"
        flags.append(_flag(
            "liquidity below 1", "high", "leverage_liquidity",
            f"current ratio {details['current_ratio']:.2f} — current "
            "liabilities exceed current assets",
            "deteriorating"))
    elif details["working_capital"] is not None \
            and details["working_capital"] < 0:
        verdict = "deteriorating"
        flags.append(_flag(
            "negative working capital", "high", "leverage_liquidity",
            f"working capital "
            f"{details['working_capital']:,.0f} is negative",
            "deteriorating"))
    elif details["debt_to_operating_cash_flow"] is not None \
            and details["debt_to_operating_cash_flow"] \
            > THRESHOLDS["debt_to_ocf_deteriorating_above"]:
        verdict = "deteriorating"
        flags.append(_flag(
            "debt unsustainable vs cash flow", "high",
            "leverage_liquidity",
            f"debt / operating cash flow "
            f"{details['debt_to_operating_cash_flow']:.2f}x",
            "deteriorating"))
    elif health_verdict == "weak":
        verdict = "deteriorating"
        flags.append(_flag(
            "health verdict weak", "high", "leverage_liquidity",
            "Step 18 health verdict is 'weak' (a hard distress flag "
            "fired)",
            "deteriorating"))
    elif (details["debt_change_5y"] is not None
          and details["debt_change_5y"]
          > THRESHOLDS["debt_rise_deteriorating_5y"]):
        # Debt up >50% over 5y is a warning here, not
        # deterioration on its own: debt raised to fund growth
        # that shows up in operating cash flow is not the same
        # as debt raised to plug a hole.  The overall rule
        # escalates to deteriorating when a second area is also
        # deteriorating, or when the health verdict is weak.
        verdict = "warning"
        flags.append(_flag(
            "debt rising fast", "medium", "leverage_liquidity",
            f"total debt {details['debt_change_5y']:+.1%} over 5y",
            "rising"))
    elif (details["working_capital_change_5y"] is not None
          and details["working_capital_change_5y"]
          < THRESHOLDS["working_capital_fall_warning"]
          and details["current_ratio"] is not None
          and details["current_ratio"] >= 1.0):
        verdict = "warning"
        flags.append(_flag(
            "working capital falling", "medium", "leverage_liquidity",
            f"working capital "
            f"{details['working_capital_change_5y']:+.1%} over 5y "
            f"(current ratio still "
            f"{details['current_ratio']:.2f}, above 1 — a warning, "
            "not deterioration on its own)",
            "falling"))
    elif (details.get("current_ratio_change_5y") is not None
          and details["current_ratio_change_5y"]
          <= THRESHOLDS["current_ratio_fall_warning"]
          and details["current_ratio"] is not None
          and details["current_ratio"] >= 1.0):
        verdict = "warning"
        flags.append(_flag(
            "current ratio falling", "medium", "leverage_liquidity",
            f"current ratio "
            f"{details['current_ratio_change_5y']:+.2f} over 5y to "
            f"{details['current_ratio']:.2f} (still above 1)",
            "falling"))
    elif (details.get("debt_to_operating_cash_flow_change_5y")
          is not None
          and details["debt_to_operating_cash_flow_change_5y"]
          > THRESHOLDS["debt_to_ocf_rise_warning"]):
        verdict = "warning"
        flags.append(_flag(
            "debt rising vs cash flow", "medium", "leverage_liquidity",
            f"debt / operating cash flow up "
            f"{details['debt_to_operating_cash_flow_change_5y']:+.2f}x "
            "over 5y",
            "rising"))
    elif (details["debt_change_5y"] is not None
          and details["debt_change_5y"]
          > THRESHOLDS["debt_rise_warning_5y"]):
        verdict = "warning"
        flags.append(_flag(
            "debt rising", "medium", "leverage_liquidity",
            f"total debt {details['debt_change_5y']:+.1%} over 5y",
            "rising"))
    elif (details["debt_change_5y"] is not None
          and details["debt_change_5y"] < 0
          and details.get("current_ratio_change_5y") is not None
          and details["current_ratio_change_5y"] >= -0.1
          and details["working_capital_change_5y"] is not None
          and details["working_capital_change_5y"] > 0):
        verdict = "improving"
    elif details["current_ratio"] is None \
            and details["working_capital_change_5y"] is None \
            and details["debt_change_5y"] is None:
        verdict = "insufficient data"
    else:
        verdict = "stable"
    if verdict == "stable" and not evidence:
        evidence.append("no leverage or liquidity drift signal in "
                        "the data available")
    return _area(verdict, evidence, details, flags)


# ---------------------------------------------------------------------------
# (d) Cash-flow stress
# ---------------------------------------------------------------------------

def _cashflow_stress(values, cashflow, annual_periods):
    """Cash-flow stress area from Step 20 sections + series."""
    evidence = []
    details = {}
    sections = (cashflow or {}).get("sections", {})
    fcf_section = sections.get("free_cash_flow", {})
    capex_section = sections.get("capital_expenditure", {})
    ocf_section = sections.get("operating_cash_flow", {})
    conversion = sections.get("cash_conversion_trends", {})
    surge = sections.get("capex_surge", {})
    fcf_ni = fcf_section.get("fcf_to_net_income", {})
    fcf_after_all = fcf_section.get(
        "fcf_after_dividends_and_buybacks", {})
    capex_ocf = capex_section.get("capex_to_ocf", {})
    ocf_latest = ocf_section.get("operating_cash_flow", {}).get("value")
    fcf_latest = fcf_section.get("free_cash_flow", {}).get("value")
    mean_5y = conversion.get("5y", {})
    fcf_ni_5y_mean = mean_5y.get("mean_fcf_to_net_income")
    details = {
        "fcf_to_net_income_latest": fcf_ni.get("value"),
        "fcf_to_net_income_5y_mean": fcf_ni_5y_mean,
        "capex_to_ocf_latest": capex_ocf.get("value"),
        "fcf_after_dividends_and_buybacks":
            fcf_after_all.get("value"),
        "operating_cash_flow_latest": ocf_latest,
        "free_cash_flow_latest": fcf_latest,
        "capex_surge_verdict": surge.get("verdict"),
        "capex_cagr_5y": surge.get("5y", {}).get("capex_cagr"),
        "ocf_cagr_5y": surge.get("5y", {}).get("ocf_cagr"),
        "fcf_cagr_5y": surge.get("5y", {}).get("fcf_cagr"),
    }
    # Capex / OCF five years ago, from the concept series.
    capex_series = dict(_concept_series(
        values, "capital_expenditure", end_period=annual_periods[-1]))
    ocf_series = dict(_concept_series(
        values, "operating_cash_flow", end_period=annual_periods[-1]))
    capex_ocf_5y_ago = None
    if len(annual_periods) >= 6:
        ago = annual_periods[-6]
        c_ago, o_ago = capex_series.get(ago), ocf_series.get(ago)
        if c_ago is not None and o_ago:
            capex_ocf_5y_ago = c_ago / o_ago
    details["capex_to_ocf_5y_ago"] = capex_ocf_5y_ago
    if details["capex_to_ocf_latest"] is not None \
            and capex_ocf_5y_ago is not None:
        details["capex_to_ocf_change_5y"] = \
            details["capex_to_ocf_latest"] - capex_ocf_5y_ago
    if details["fcf_to_net_income_latest"] is not None:
        evidence.append(
            f"FCF / net income latest "
            f"{details['fcf_to_net_income_latest']:.2f}"
            + (f" vs 5y mean {fcf_ni_5y_mean:.2f}"
               if fcf_ni_5y_mean is not None else ""))
    if details["capex_to_ocf_latest"] is not None:
        evidence.append(
            f"capex / operating cash flow latest "
            f"{details['capex_to_ocf_latest']:.1%}"
            + (f" (was {capex_ocf_5y_ago:.1%} 5y ago)"
               if capex_ocf_5y_ago is not None else ""))
    if details["fcf_after_dividends_and_buybacks"] is not None:
        evidence.append(
            f"FCF after dividends and buybacks "
            f"{details['fcf_after_dividends_and_buybacks']:,.0f}")
    if surge.get("verdict"):
        evidence.append(f"capex surge: {surge['verdict']}")
    flags = []
    if ocf_latest is not None and ocf_latest < 0:
        verdict = "deteriorating"
        flags.append(_flag(
            "operating cash flow negative", "high", "cashflow_stress",
            "latest annual operating cash flow is negative",
            "deteriorating"))
    elif fcf_latest is not None and fcf_latest < 0:
        verdict = "deteriorating"
        flags.append(_flag(
            "free cash flow negative", "high", "cashflow_stress",
            "latest annual free cash flow is negative",
            "deteriorating"))
    elif details["fcf_after_dividends_and_buybacks"] is not None \
            and details["fcf_after_dividends_and_buybacks"] < 0:
        verdict = "deteriorating"
        flags.append(_flag(
            "payouts exceed free cash flow", "high", "cashflow_stress",
            f"FCF after dividends and buybacks "
            f"{details['fcf_after_dividends_and_buybacks']:,.0f} — "
            "dividends and buybacks together exceed free cash flow",
            "deteriorating"))
    elif (details["capex_to_ocf_latest"] is not None
          and details["capex_to_ocf_latest"]
          > THRESHOLDS["capex_to_ocf_warning_level"]
          and details.get("capex_to_ocf_change_5y") is not None
          and details["capex_to_ocf_change_5y"]
          > THRESHOLDS["capex_to_ocf_rise_warning"]):
        # Checked before FCF conversion: a capex surge is the
        # causal explanation for falling FCF conversion (Step 20
        # finding), so it is the flag raised when both fire.
        verdict = "warning"
        cagr_text = ""
        if details["capex_cagr_5y"] is not None \
                and details["ocf_cagr_5y"] is not None:
            cagr_text = (
                f"; 5y capex CAGR {details['capex_cagr_5y']:.1%} vs "
                f"OCF {details['ocf_cagr_5y']:.1%}")
        flags.append(_flag(
            "capex surge compressing FCF", "medium", "cashflow_stress",
            f"capex / operating cash flow "
            f"{details['capex_to_ocf_latest']:.1%} (was "
            f"{capex_ocf_5y_ago:.1%} 5y ago)"
            f"{cagr_text} — FCF lags because capex "
            "is surging, not because operating cash flow is weak",
            "rising"))
    elif (details["fcf_to_net_income_latest"] is not None
          and fcf_ni_5y_mean is not None
          and details["fcf_to_net_income_latest"]
          < THRESHOLDS["fcf_to_ni_warning_ratio"] * fcf_ni_5y_mean):
        verdict = "warning"
        flags.append(_flag(
            "FCF conversion falling", "medium", "cashflow_stress",
            f"FCF / net income latest "
            f"{details['fcf_to_net_income_latest']:.2f} vs 5y mean "
            f"{fcf_ni_5y_mean:.2f}",
            "falling"))
    elif (surge.get("verdict") or "").startswith("capex surge"):
        verdict = "warning"
        flags.append(_flag(
            "capex surge compressing FCF", "medium", "cashflow_stress",
            surge["verdict"],
            "rising"))
    elif (details["fcf_to_net_income_latest"] is not None
          and fcf_ni_5y_mean is not None
          and details["fcf_to_net_income_latest"] >= fcf_ni_5y_mean
          and details.get("capex_to_ocf_change_5y") is not None
          and details["capex_to_ocf_change_5y"] <= 0):
        verdict = "improving"
    elif details["fcf_to_net_income_latest"] is None \
            and details["capex_to_ocf_latest"] is None \
            and details["fcf_after_dividends_and_buybacks"] is None:
        verdict = "insufficient data"
    else:
        verdict = "stable"
    return _area(verdict, evidence, details, flags)


# ---------------------------------------------------------------------------
# Overall verdict
# ---------------------------------------------------------------------------

def _overall_verdict(areas, health_verdict, latest_net_income,
                     latest_ocf):
    """Overall verdict from the four area verdicts (rule in docstring)."""
    assessed = [v for v in areas.values()
                if v["verdict"] != "insufficient data"]
    if not assessed:
        return "insufficient data", [
            "no area could be assessed from the data available"]
    deteriorating = [name for name, area in areas.items()
                     if area["verdict"] == "deteriorating"]
    warnings = [name for name, area in areas.items()
                if area["verdict"] == "warning"]
    reasons = []
    if latest_net_income is not None and latest_net_income < 0:
        return "deteriorating", [
            "latest annual net income is negative (hard distress)"]
    if latest_ocf is not None and latest_ocf < 0:
        return "deteriorating", [
            "latest annual operating cash flow is negative "
            "(hard distress)"]
    if len(deteriorating) >= 2:
        return "deteriorating", [
            f"{len(deteriorating)} areas deteriorating: "
            + ", ".join(deteriorating)]
    if len(deteriorating) == 1 and health_verdict == "weak":
        return "deteriorating", [
            f"area '{deteriorating[0]}' deteriorating and Step 18 "
            "health verdict is 'weak'"]
    if len(deteriorating) == 1:
        return "warning", [
            f"one area deteriorating ('{deteriorating[0]}') without "
            "hard distress"]
    if len(warnings) >= 2:
        return "warning", [
            f"{len(warnings)} areas warning: " + ", ".join(warnings)]
    if len(warnings) == 1:
        return "stable", [
            f"one area warning ('{warnings[0]}'), the rest stable "
            "or improving"]
    improving = [name for name, area in areas.items()
                 if area["verdict"] == "improving"]
    if len(improving) >= 2:
        return "improving", [
            f"{len(improving)} areas improving: "
            + ", ".join(improving)]
    return "stable", ["no area deteriorating or warning"]


# ---------------------------------------------------------------------------
# One-ticker assessment
# ---------------------------------------------------------------------------

def assess_deterioration(conn, ticker, period=None,
                         period_type="annual", health=None, growth=None,
                         cashflow=None):
    """Step 27 deterioration assessment for one ticker and period.

    ``conn`` is a read connection (tests use a temporary copy).
    ``period`` defaults to the latest annual period.  ``health`` /
    ``growth`` / ``cashflow`` are the Step 18/19/20 assessment
    dicts (computed here when not supplied); ``growth`` is only
    used when the assessed period is the latest, otherwise the
    growth figures are rebuilt truncated at the period (see
    ``_growth_for``).  Returns the overall verdict, the boolean
    ``material_deterioration`` for the Step 23 gate, the four area
    assessments, the named risk flags, and the inputs Step 23
    needs to corroborate its simpler rule.  Never returns a
    buy/sell call.
    """
    values, load_warnings = fr.load_standardised_values(conn, ticker)
    warnings = list(load_warnings)
    annual_periods = sorted(
        p for (st, p, pt) in values
        if st == "income_statement" and pt == "annual")
    if period is None:
        period = annual_periods[-1] if annual_periods else None
    window_periods = [p for p in annual_periods if p <= period] \
        if period is not None else []
    if period is None or len(window_periods) < 3:
        return {
            "ticker": ticker,
            "reporting_period": period,
            "period_type": period_type,
            "overall_verdict": "insufficient data",
            "material_deterioration": None,
            "overall_reasons": [
                f"only {len(window_periods)} annual period(s) of "
                "history up to the assessed period; at least 3 "
                "required"],
            "areas": {},
            "risk_flags": [],
            "cautions": ["insufficient history for a deterioration "
                         "assessment"],
            "rule": MATERIAL_DETERIORATION_RULE,
            "warnings": warnings,
            "inputs": {},
        }

    if health is None:
        health = fh.assess_health(conn, ticker, period=period,
                                  period_type=period_type)
    if cashflow is None:
        cashflow = cf.assess_cashflow(conn, ticker, period=period,
                                      period_type=period_type)
    growth = _normalise_growth(growth, values, period,
                               annual_periods[-1])

    areas = {
        "trend_breaks": _trend_breaks(values, growth,
                                      window_periods),
        "margin_erosion": _margin_erosion(values, ticker,
                                          window_periods),
        "leverage_liquidity": _leverage_liquidity(
            values, ticker, window_periods, health),
        "cashflow_stress": _cashflow_stress(
            values, cashflow, window_periods),
    }

    latest_net_income = None
    latest_ocf = None
    ni_series = _concept_series(values, "net_income",
                                end_period=period)
    ocf_series = _concept_series(values, "operating_cash_flow",
                                 end_period=period)
    if ni_series:
        latest_net_income = ni_series[-1][1]
    if ocf_series:
        latest_ocf = ocf_series[-1][1]
    health_verdict = (health or {}).get("verdict")

    overall, reasons = _overall_verdict(
        areas, health_verdict, latest_net_income, latest_ocf)
    material = None
    if overall == "deteriorating":
        material = True
    elif overall in ("warning", "stable", "improving"):
        material = False

    risk_flags = []
    for area_name, area in areas.items():
        risk_flags.extend(area.get("flags", []))
    cautions = []
    for area_name, area in areas.items():
        if area["verdict"] == "warning":
            cautions.append(
                f"area '{area_name}' is warning: "
                + "; ".join(area["evidence"][:2]))

    # Health history: did the verdict fall to weak?
    fell_to_weak = False
    try:
        history = fh.assess_all_periods(conn, ticker)
        history = [h for h in history
                   if h.get("reporting_period") <= period]
        if len(history) >= 2:
            fell_to_weak = (history[-1].get("verdict") == "weak"
                            and history[-2].get("verdict") != "weak")
    except Exception:  # noqa: BLE001 - report, never crash
        fell_to_weak = False

    trend = areas["trend_breaks"]["details"]
    inputs = {
        "health_verdict": health_verdict,
        "health_fell_to_weak": fell_to_weak,
        "revenue_yoy_latest_annual":
            trend.get("revenue", {}).get("latest_yoy"),
        "net_income_yoy_latest_annual":
            trend.get("net_income", {}).get("latest_yoy"),
        "fcf_yoy_latest_annual":
            trend.get("free_cash_flow", {}).get("latest_yoy"),
        "operating_cash_flow_latest_annual": latest_ocf,
        "net_income_latest_annual": latest_net_income,
    }

    return {
        "ticker": ticker,
        "reporting_period": period,
        "period_type": period_type,
        "overall_verdict": overall,
        "material_deterioration": material,
        "overall_reasons": reasons,
        "areas": areas,
        "risk_flags": risk_flags,
        "cautions": cautions,
        "rule": MATERIAL_DETERIORATION_RULE,
        "warnings": warnings,
        "inputs": inputs,
    }


def assess_all_periods(conn, ticker):
    """Annual deterioration assessments for every annual period."""
    values, _warnings = fr.load_standardised_values(conn, ticker)
    annual_periods = sorted(
        p for (st, p, pt) in values
        if st == "income_statement" and pt == "annual")
    return [assess_deterioration(conn, ticker, period=p,
                                 period_type="annual")
            for p in annual_periods]
