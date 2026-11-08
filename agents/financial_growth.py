"""Step 19 — Growth analysis: revenue, earnings, EPS and FCF growth.

Computes year-on-year growth, multi-year CAGRs, growth consistency
and a cyclicality assessment from Step 16 standardised statements.
It reuses ``agents.financial_ratios.load_standardised_values`` (and
therefore Step 16's standardiser) and Step 16's derived
``free_cash_flow`` (operating cash flow - capital expenditure); it
never re-derives a series by a different definition, and it never
needs a price — growth is a fundamentals-only exercise.

Design rules (from the project brief, Steps 16-19):

* Never invent a missing figure.  A growth figure whose input is
  missing is returned with ``value=None``, ``present=False`` and a
  ``reason``.  A window that lacks a required endpoint is missing,
  never interpolated.
* Percentage growth is not meaningful when the base (prior) value is
  zero or negative: going from -$10m to +$5m is not "-150% growth"
  in any sense a reader can use, so YoY growth is missing with that
  reason when the prior value is <= 0.  The levels are still reported
  alongside, so nothing is hidden.
* CAGR is undefined when the start value is <= 0, and undefined when
  the end value is < 0 (a fractional power of a negative ratio has no
  real value).  An end value of exactly 0 is a -100% CAGR and is
  returned as such.  CAGR uses the fiscal-year count between the two
  annual endpoints (``(end / start) ** (1 / years) - 1``).
* Annual rows are used by default.  Quarterly YoY compares a quarter
  with the same quarter a year ago (same month-day, previous year).
  Quarterly flow values are single-quarter values (verified in the
  Step 16 report), so a quarterly YoY is a genuine like-for-like
  comparison, not a cumulative one.
* Cyclicality is assessed from the series itself, not claimed from
  one company alone: growth-rate volatility (sample stdev of annual
  YoY), the number of YoY declines in the last 10 years, and the
  largest peak-to-trough decline in the level series.  The verdict
  thresholds are documented judgement rules in this module,
  uncalibrated against a cyclical peer set — with one issuer in the
  database there is no peer set to calibrate against.
* Every output records its period, the inputs actually used, and
  missing-input reasons.

What is unavailable, honestly:

* Singapore / Hong Kong — Step 16 standardised 0 concepts for DBS
  and none exist for HKEX, so there is no series to grow.
* TTM growth vs the prior TTM is not computed here: Step 17 builds a
  TTM level, but a prior-year TTM needs the same construction a year
  earlier, and the quarterly series has no row at a fiscal year end;
  the quarterly YoY below is the like-for-like alternative.

This module never touches the production database.  It reads a
connection the caller supplies (tests use a temporary copy) and
never writes.  It makes no network requests.
"""

import statistics

from agents import financial_ratios as fr
from agents import financial_standard as fs

# Series analysed.  EPS is Step 17's diluted EPS (standardised
# ``eps_diluted``; Step 17 falls back to net_income / diluted shares
# only when the standardised EPS is absent — here the standardised
# value is used directly, and a missing EPS is missing).
GROWTH_SERIES = (
    "revenue",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "free_cash_flow",
)

# CAGR windows (fiscal years) ending at the latest annual period.
CAGR_WINDOWS = (3, 5, 10)

# Cyclicality verdict thresholds — documented judgement rules, not
# universal law.  See ``assess_cyclicality``.
CYCLICALITY_RULES = {
    "steady_yoy_stdev_below": 0.10,
    "volatile_yoy_stdev_at_or_above": 0.25,
    "volatile_declines_in_10y_at_or_above": 3,
    "volatile_drawdown_at_or_above": 0.20,
}


def _statement_for(concept):
    return fs.CONCEPT_STATEMENT.get(concept, "income_statement")


def _series(values, concept, period_type="annual"):
    """One series as a sorted list of (reporting_period, value|None).

    Only periods where a row of the right statement type exists are
    included; a missing value is an entry with value None, never a
    skipped period, so a gap is visible to every consumer.
    """
    statement = _statement_for(concept)
    periods = sorted(
        p for (st, p, pt) in values
        if st == statement and pt == period_type
    )
    return [
        (period, fr._num(fr._get(values, statement, period, period_type, concept)))
        for period in periods
    ]


def _yoy_value(current, prior):
    """YoY growth, or (None, reason) when it is not meaningful."""
    if current is None or prior is None:
        missing = []
        if current is None:
            missing.append("current value")
        if prior is None:
            missing.append("prior value")
        return None, f"missing input(s): {', '.join(missing)}"
    if prior == 0:
        return None, "prior value is zero; percentage growth is undefined"
    if prior < 0:
        return None, (
            "prior value is negative; percentage growth from a "
            "negative base is not meaningful"
        )
    return current / prior - 1, None


def yoy_growth(series):
    """Year-on-year growth for every period after the first.

    ``series`` is a list of (period, value|None) as returned by
    ``_series``.  Returns a list of dicts, one per period after the
    first, each with period, prior_period, value, present, current /
    prior values, and reason when missing.
    """
    out = []
    for (period, value), (prior_period, prior_value) in zip(
            series[1:], series):
        growth, reason = _yoy_value(value, prior_value)
        out.append({
            "period": period,
            "prior_period": prior_period,
            "value": growth,
            "present": growth is not None,
            "current_value": value,
            "prior_value": prior_value,
            "reason": reason,
        })
    return out


def cagr(series, years):
    """CAGR over ``years`` fiscal years ending at the latest period.

    The start endpoint is the period ``years`` steps back in the
    annual series (so a 5y CAGR ending 2026-06-30 starts at
    2021-06-30).  Returns a dict with value, present, start/end
    periods and values, years, and a reason when undefined.  A
    missing endpoint, a start value <= 0, or an end value < 0 all
    make the CAGR missing — never interpolated, never a nonsense
    number.
    """
    result = {
        "years": years,
        "value": None,
        "present": False,
        "start_period": None,
        "end_period": None,
        "start_value": None,
        "end_value": None,
        "reason": None,
    }
    if len(series) < years + 1:
        result["reason"] = (
            f"insufficient history: {len(series)} annual periods, "
            f"need {years + 1} for a {years}y CAGR"
        )
        return result
    (end_period, end_value) = series[-1]
    (start_period, start_value) = series[-1 - years]
    result["end_period"] = end_period
    result["start_period"] = start_period
    result["end_value"] = end_value
    result["start_value"] = start_value
    if start_value is None or end_value is None:
        missing = []
        if start_value is None:
            missing.append(f"start value ({start_period})")
        if end_value is None:
            missing.append(f"end value ({end_period})")
        result["reason"] = (
            f"missing endpoint(s): {', '.join(missing)}; "
            "CAGR not interpolated"
        )
        return result
    if start_value <= 0:
        result["reason"] = (
            "start value is zero or negative; CAGR is undefined"
        )
        return result
    if end_value < 0:
        result["reason"] = (
            "end value is negative; CAGR is undefined"
        )
        return result
    if end_value == 0:
        result["value"] = -1.0
        result["present"] = True
        return result
    result["value"] = (end_value / start_value) ** (1 / years) - 1
    result["present"] = True
    return result


def consistency_metrics(yoy_list, window_years=None):
    """Growth consistency over a YoY list (optionally the last N).

    Returns share of years growing, mean / median / sample stdev of
    YoY growth, best and worst years, and the count of usable years.
    Years whose YoY is missing are excluded and counted, not treated
    as zero growth.  Fewer than 2 usable years means no stdev.
    """
    usable = [g for g in yoy_list if g["present"]]
    if window_years is not None:
        usable = usable[-window_years:]
    result = {
        "years_used": len(usable),
        "years_missing": len(yoy_list) - len(usable),
        "share_growing": None,
        "mean_growth": None,
        "median_growth": None,
        "stdev_growth": None,
        "best_year": None,
        "worst_year": None,
        "reason": None,
    }
    if not usable:
        result["reason"] = "no usable YoY growth years in window"
        return result
    growths = [g["value"] for g in usable]
    result["share_growing"] = sum(1 for v in growths if v > 0) / len(growths)
    result["mean_growth"] = statistics.fmean(growths)
    result["median_growth"] = statistics.median(growths)
    result["stdev_growth"] = (
        statistics.stdev(growths) if len(growths) >= 2 else None
    )
    best = max(usable, key=lambda g: g["value"])
    worst = min(usable, key=lambda g: g["value"])
    result["best_year"] = {"period": best["period"], "value": best["value"]}
    result["worst_year"] = {"period": worst["period"], "value": worst["value"]}
    return result


def max_drawdown(series):
    """Largest peak-to-trough decline in the level series.

    Returns {"value": fraction, "peak_period", "trough_period"} or
    None values when the series has no two usable positive values.
    A drawdown is only computed over positive values: a level that
    goes negative is a loss, not a drawdown, and is reported by the
    YoY/consistency figures instead.
    """
    peak = None
    peak_period = None
    worst = 0.0
    worst_peak = None
    worst_trough = None
    for period, value in series:
        if value is None or value <= 0:
            continue
        if peak is None or value > peak:
            peak = value
            peak_period = period
        elif peak > 0:
            drawdown = (peak - value) / peak
            if drawdown > worst:
                worst = drawdown
                worst_peak = peak_period
                worst_trough = period
    if worst_peak is None:
        return {"value": None, "peak_period": None, "trough_period": None,
                "reason": "no peak followed by a lower positive value"}
    return {"value": worst, "peak_period": worst_peak,
            "trough_period": worst_trough, "reason": None}


def assess_cyclicality(series, yoy_list):
    """Cyclicality verdict for one series, from the series itself.

    Method: growth-rate volatility (sample stdev of annual YoY over
    the last 10 years when available, else all years), the number of
    YoY declines in the last 10 years, and the largest peak-to-trough
    level drawdown.  Verdicts:

    * "steady" — YoY stdev < 0.10, at most 1 decline in 10y, and
      drawdown < 0.20.
    * "volatile" — YoY stdev >= 0.25, or >= 3 declines in 10y, or
      drawdown >= 0.20.
    * "moderate" — anything in between.
    * "insufficient data" — fewer than 2 usable YoY years.

    The thresholds are judgement rules, uncalibrated against a
    cyclical peer set (no peer set exists in the database).  The
    verdict describes this series' own history; it does not claim
    the business is or is not in a cycle — one company's history
    cannot establish that.
    """
    rules = CYCLICALITY_RULES
    recent = yoy_list[-10:]
    metrics = consistency_metrics(recent)
    drawdown = max_drawdown(series)
    declines = sum(
        1 for g in recent if g["present"] and g["value"] < 0
    )
    result = {
        "verdict": None,
        "yoy_stdev_10y": metrics["stdev_growth"],
        "declines_10y": declines,
        "max_drawdown": drawdown["value"],
        "drawdown_peak_period": drawdown["peak_period"],
        "drawdown_trough_period": drawdown["trough_period"],
        "years_used_10y": metrics["years_used"],
        "method": (
            "growth-rate volatility (sample stdev of annual YoY, last "
            "10y), YoY declines in last 10y, largest peak-to-trough "
            "level drawdown; thresholds: steady if stdev < 0.10 and "
            "<= 1 decline and drawdown < 0.20; volatile if stdev "
            ">= 0.25 or >= 3 declines or drawdown >= 0.20; else "
            "moderate. Judgement rules, uncalibrated — no peer set "
            "in the database."
        ),
        "reason": None,
    }
    if metrics["years_used"] < 2 or metrics["stdev_growth"] is None:
        result["verdict"] = "insufficient data"
        result["reason"] = "fewer than 2 usable YoY growth years"
        return result
    stdev = metrics["stdev_growth"]
    dd = drawdown["value"] if drawdown["value"] is not None else 0.0
    if (stdev < rules["steady_yoy_stdev_below"] and declines <= 1
            and dd < rules["volatile_drawdown_at_or_above"]):
        result["verdict"] = "steady"
    elif (stdev >= rules["volatile_yoy_stdev_at_or_above"]
          or declines >= rules["volatile_declines_in_10y_at_or_above"]
          or dd >= rules["volatile_drawdown_at_or_above"]):
        result["verdict"] = "volatile"
    else:
        result["verdict"] = "moderate"
    return result


def quarterly_yoy(values, concept, period=None):
    """YoY growth for one quarterly period vs the same quarter a year ago.

    ``period`` defaults to the latest quarterly period.  Quarterly
    flow values are single-quarter values, so this is like-for-like.
    Returns a dict with period, prior_period, value, present, current
    / prior values, and reason when missing.
    """
    series = _series(values, concept, period_type="quarterly")
    if not series:
        return {"concept": concept, "period": period, "prior_period": None,
                "value": None, "present": False,
                "current_value": None, "prior_value": None,
                "reason": "no quarterly rows for this series"}
    if period is None:
        period = series[-1][0]
    current = dict(series).get(period)
    if current is None and period not in dict(series):
        return {"concept": concept, "period": period, "prior_period": None,
                "value": None, "present": False,
                "current_value": None, "prior_value": None,
                "reason": f"no quarterly row for {period}"}
    y, m, d = period.split("-")
    prior_period = f"{int(y) - 1}-{m}-{d}"
    prior = dict(series).get(prior_period)
    if prior is None and prior_period not in dict(series):
        return {"concept": concept, "period": period,
                "prior_period": prior_period, "value": None,
                "present": False, "current_value": current,
                "prior_value": None,
                "reason": f"same-quarter-prior-year row {prior_period} "
                          "missing"}
    growth, reason = _yoy_value(current, prior)
    return {"concept": concept, "period": period,
            "prior_period": prior_period, "value": growth,
            "present": growth is not None, "current_value": current,
            "prior_value": prior, "reason": reason}


def analyse_series(values, concept):
    """Full Step 19 analysis for one series: YoY, CAGRs, consistency,
    cyclicality.  ``values`` is the dict from
    ``financial_ratios.load_standardised_values``."""
    series = _series(values, concept, period_type="annual")
    yoy = yoy_growth(series)
    return {
        "concept": concept,
        "statement_type": _statement_for(concept),
        "series": [
            {"period": p, "value": v, "present": v is not None}
            for p, v in series
        ],
        "yoy": yoy,
        "latest_yoy": yoy[-1] if yoy else None,
        "cagr": {years: cagr(series, years) for years in CAGR_WINDOWS},
        "consistency_all": consistency_metrics(yoy),
        "consistency_10y": consistency_metrics(yoy, window_years=10),
        "consistency_5y": consistency_metrics(yoy, window_years=5),
        "cyclicality": assess_cyclicality(series, yoy),
        "quarterly_yoy_latest": quarterly_yoy(values, concept),
    }


def analyse_growth(conn, ticker):
    """Step 19 growth analysis for ``ticker`` across all series.

    Loads standardised values read-only from ``conn`` (a connection
    the caller supplies; tests use a temporary copy).  Returns
    {"ticker", "series": {concept: analyse_series(...)}, "warnings"}.
    A ticker with no standardised values at all returns an empty
    series map with a warning, not an exception.
    """
    values, load_warnings = fr.load_standardised_values(conn, ticker)
    warnings = list(load_warnings)
    if not values:
        warnings.append(f"no financial_statements rows for {ticker}")
        return {"ticker": ticker, "series": {}, "warnings": warnings}
    return {
        "ticker": ticker,
        "series": {c: analyse_series(values, c) for c in GROWTH_SERIES},
        "warnings": warnings,
    }
