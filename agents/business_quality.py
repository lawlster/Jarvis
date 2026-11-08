"""Step 24 — Competitive advantage and business quality analysis.

Builds on Steps 16-20: it reuses the Step 16 standardised values,
the Step 17 ratios, the Step 18 health verdict, the Step 19 growth
analysis and the Step 20 cash-flow assessment, and never re-derives
a value by a different definition.  It makes no network requests
and never writes to a database; callers supply a read connection
(tests use a temporary copy of the production database).

What it produces, for one ticker and one period — the qualitative
half of the Buffett/Munger screen, quantified where the data
allows:

* (a) moat signals — persistent high ROIC/ROE against the cost of
  capital over 5y/10y, stable or expanding margins (variability
  measured, not eyeballed), pricing-power evidence (revenue growth
  with stable margins, low revenue drawdown), scale/efficiency
  (cash conversion cycle, asset turnover);
* (b) durability — consistency of profitability (profitable years
  out of 10), earnings quality (OCF vs net income, FCF conversion,
  SBC drag on FCF), balance-sheet strength (Step 18 verdict);
* (c) management / capital allocation — dividend history and
  growth, buyback history (shares-outstanding trend), incremental
  return on reinvested capital, debt trajectory;
* (d) a combined quality rating — ``excellent`` / ``good`` /
  ``adequate`` / ``weak`` / ``insufficient data`` — with every
  component scored 0/1/2 and explained.  The score is transparent
  (inputs shown on every component), not a black box.

Scoring rules (documented judgement rules, not universal law):

* Each component scores 2 (excellent), 1 (adequate), 0 (weak), or
  is missing (input unavailable).  The overall rating is the mean
  of the scored components: >= 1.6 excellent, >= 1.0 good,
  >= 0.5 adequate, < 0.5 weak.  Fewer than 3 scored components, or
  fewer than 3 annual periods of history, means ``insufficient
  data``.
* Returns on capital use the Step 21 WACC (11%) as the
  cost-of-capital proxy, labelled as such; it is a judgement
  default, not a per-company cost of capital.
* Incremental ROIC is computed only when invested capital grew
  over the window: when capital shrinks, (change in NOPAT) /
  (change in capital) is not a return on reinvestment, so it is
  missing with that reason, never a nonsense number.

Explicit limits — what financial statements CANNOT assess, and
this module does not pretend to:

* brand strength, network effects, switching costs, regulatory
  moats, management integrity, customer concentration and
  competitive position against peers.  These are listed on every
  assessment as ``not_assessable`` with the reason.  The numbers
  are evidence about a moat; the moat conclusion itself stays
  qualified ("financial evidence consistent with a durable
  advantage", never "this company has a moat").
* No peer set exists in the database (MSFT only), so every
  comparison is against absolute thresholds or the company's own
  history, never against competitors.

Quality gate for Step 23: a ``strongest_alert`` tier requires
quality adequate or better.  ``margin_of_safety.classify_tier``
accepts an optional ``quality`` argument; a ``weak`` or
``insufficient data`` rating blocks the alert even at a discount.
The gate is implemented there, not here; this module only
produces the rating.

Known honest limits (from Steps 16-20, re-stated here):

* Data is MSFT only (242 US rows); SG is DBS metadata-only
  (0 concepts) and there is no HKEX data, so quality assessments
  only exist for MSFT.
* Owner earnings / EV/EBITDA / interest coverage are unavailable
  — no D&A or interest expense concept exists in the Step 16
  concept set.  They are recorded missing, never estimated.
* ``reporting_period`` is authoritative (row ``fiscal_year`` is
  unreliable in 236/242 US rows).
"""

import statistics

from agents import financial_cashflow as cf
from agents import financial_growth as fg
from agents import financial_health as fh
from agents import financial_ratios as fr

# Cost of capital used as the hurdle for returns on capital.
# Step 21's WACC default, labelled as a judgement default, not a
# per-company cost of capital.
DEFAULT_COST_OF_CAPITAL = 0.11

# Component scoring thresholds (documented judgement rules).
THRESHOLDS = {
    # returns on capital
    "roic_excellent_mean_5y": 0.20,
    "roic_excellent_min_5y": 0.15,
    "roic_adequate_mean_5y": 0.12,
    "roic_weak_mean_5y": 0.10,
    "roe_adequate_mean_5y": 0.12,
    # margins
    "margin_cv_excellent_max": 0.10,
    "margin_cv_adequate_max": 0.20,
    "gross_margin_excellent_min": 0.40,
    "net_margin_adequate_min": 0.05,
    # earnings quality
    "ocf_to_ni_excellent_mean": 1.10,
    "ocf_to_ni_adequate_mean": 1.00,
    "ocf_to_ni_weak_mean": 0.90,
    "fcf_to_ni_excellent_mean": 0.60,
    "fcf_to_ni_adequate_mean": 0.40,
    "fcf_to_ni_weak_mean": 0.30,
    "sbc_to_fcf_caution": 0.25,
    # capital allocation
    "incremental_roic_excellent": 0.15,
    "incremental_roic_adequate": 0.10,
    "incremental_roic_weak": 0.08,
    # efficiency
    "ccc_excellent_max_days": 30,
    "ccc_adequate_max_days": 60,
    "ccc_weak_min_days": 90,
}

# Overall rating bands on the mean component score (0-2).
RATING_BANDS = (
    (1.6, "excellent"),
    (1.0, "good"),
    (0.5, "adequate"),
)

# Things financial statements cannot assess.  Listed on every
# assessment, with the reason, so a reader never mistakes a
# numbers-based rating for a full moat analysis.
NOT_ASSESSABLE = (
    ("brand strength",
     "no brand or pricing-power survey exists in financial "
     "statements; needs research"),
    ("network effects",
     "user counts and network size are not statement line items; "
     "needs research"),
    ("switching costs",
     "customer retention and contract terms are not in financial "
     "statements; needs research"),
    ("regulatory moats",
     "licences, patents and regulation are not statement line "
     "items; needs research"),
    ("management integrity and skill",
     "integrity and capital-allocation judgement cannot be read "
     "off a balance sheet; needs research"),
    ("competitive position vs peers",
     "no peer set exists in the database (MSFT only); a relative "
     "assessment needs peer statements"),
    ("customer concentration",
     "customer-level revenue split is not a standardised concept; "
     "needs research"),
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(value_dict):
    """Numeric value of a standardised value dict, or None."""
    if value_dict is None or not value_dict.get("present"):
        return None
    return value_dict.get("value")


def _get(values, statement_type, period, period_type, concept):
    return fr._get(values, statement_type, period, period_type, concept)


def _cv(series):
    """Coefficient of variation (sample stdev / mean), or None."""
    if len(series) < 2:
        return None
    mean = statistics.fmean(series)
    if mean <= 0:
        return None
    return statistics.stdev(series) / mean


def _score(value, score, evidence, formula, inputs, reason=None):
    """One component score: 2/1/0, or None when the input is missing."""
    return {"score": score, "value": value, "present": value is not None,
            "evidence": evidence, "formula": formula, "inputs": inputs,
            "reason": reason}


def _ratio_series(values, ticker, annual_periods, metric, years):
    """Annual Step 17 ratios for ``metric`` over the last ``years``.

    Returns a list of (period, value) for years with a present
    ratio, plus the periods checked.  Missing years are excluded
    and counted, not treated as zero.
    """
    window = annual_periods[-years:] if years else annual_periods
    series = []
    for period in window:
        result = fr.compute_ratios(values, ticker, period=period,
                                   period_type="annual")
        ratio = result["ratios"].get(metric)
        if ratio is not None and ratio.get("present"):
            series.append((period, ratio["value"]))
    return series, window


def _concept_series(values, concept, period_type="annual"):
    """Annual standardised values for one concept, sorted by period."""
    statement = fr._statement_for(concept)
    periods = sorted(p for (st, p, pt) in values
                     if st == statement and pt == period_type)
    return [(p, _num(_get(values, statement, p, period_type, concept)))
            for p in periods]


# ---------------------------------------------------------------------------
# (a) Moat signals — returns on capital
# ---------------------------------------------------------------------------

def _returns_on_capital(values, ticker, annual_periods,
                        cost_of_capital=DEFAULT_COST_OF_CAPITAL):
    """Persistent high ROIC/ROE against the cost of capital."""
    roic_5y, _ = _ratio_series(values, ticker, annual_periods, "roic", 5)
    roic_10y, _ = _ratio_series(values, ticker, annual_periods, "roic", 10)
    roe_5y, _ = _ratio_series(values, ticker, annual_periods, "roe", 5)
    roic_vals_5y = [v for _, v in roic_5y]
    roic_vals_10y = [v for _, v in roic_10y]
    roe_vals_5y = [v for _, v in roe_5y]
    inputs = {
        "roic_5y_mean": statistics.fmean(roic_vals_5y)
                         if roic_vals_5y else None,
        "roic_5y_min": min(roic_vals_5y) if roic_vals_5y else None,
        "roic_5y_cv": _cv(roic_vals_5y),
        "roic_5y_years": len(roic_vals_5y),
        "roic_10y_mean": statistics.fmean(roic_vals_10y)
                          if roic_vals_10y else None,
        "roic_10y_min": min(roic_vals_10y) if roic_vals_10y else None,
        "roic_10y_years": len(roic_vals_10y),
        "roe_5y_mean": statistics.fmean(roe_vals_5y)
                        if roe_vals_5y else None,
        "cost_of_capital": cost_of_capital,
    }
    formula = ("5y/10y mean and min ROIC and 5y mean ROE from Step 17 "
               "annual ratios; hurdle is the cost of capital "
               "(Step 21 WACC default 11%, labelled judgement)")
    if not roic_vals_5y:
        return _score(None, None,
                      "no usable ROIC values in the 5-year window",
                      formula, inputs,
                      reason="ROIC missing for every year in the "
                             "5-year window")
    mean_5y = inputs["roic_5y_mean"]
    min_5y = inputs["roic_5y_min"]
    if (mean_5y >= THRESHOLDS["roic_excellent_mean_5y"]
            and min_5y >= THRESHOLDS["roic_excellent_min_5y"]):
        return _score(mean_5y, 2,
                      f"5y mean ROIC {mean_5y:.1%} with every year "
                      f">= {min_5y:.1%}, well above the "
                      f"{cost_of_capital:.0%} cost of capital",
                      formula, inputs)
    if (mean_5y >= THRESHOLDS["roic_adequate_mean_5y"]
            and mean_5y > cost_of_capital):
        return _score(mean_5y, 1,
                      f"5y mean ROIC {mean_5y:.1%} above the "
                      f"{cost_of_capital:.0%} cost of capital",
                      formula, inputs)
    if mean_5y < THRESHOLDS["roic_weak_mean_5y"] \
            or mean_5y <= cost_of_capital:
        return _score(mean_5y, 0,
                      f"5y mean ROIC {mean_5y:.1%} at or below the "
                      f"{cost_of_capital:.0%} cost of capital",
                      formula, inputs)
    return _score(mean_5y, 1,
                  f"5y mean ROIC {mean_5y:.1%} modestly above the "
                  f"{cost_of_capital:.0%} cost of capital",
                  formula, inputs)


# ---------------------------------------------------------------------------
# (a) Moat signals — margin stability and pricing power
# ---------------------------------------------------------------------------

def _margin_stability(values, ticker, annual_periods):
    """Stable or expanding margins, variability measured."""
    stats = {}
    for metric in ("gross_margin", "operating_margin", "net_margin"):
        series, _ = _ratio_series(values, ticker, annual_periods, metric, 5)
        vals = [v for _, v in series]
        stats[metric] = {
            "mean": statistics.fmean(vals) if vals else None,
            "cv": _cv(vals),
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "latest": vals[-1] if vals else None,
            "five_years_ago": vals[0] if vals else None,
            "years": len(vals),
        }
    net = stats["net_margin"]
    gross = stats["gross_margin"]
    inputs = {metric: {k: v for k, v in s.items()}
              for metric, s in stats.items()}
    formula = ("5y mean / CV / min / max of Step 17 annual margins; "
               "CV = sample stdev / mean")
    if net["mean"] is None:
        return _score(None, None,
                      "no usable net margin values in the 5-year window",
                      formula, inputs,
                      reason="net margin missing for every year in "
                             "the 5-year window")
    cvs = [s["cv"] for s in stats.values() if s["cv"] is not None]
    all_stable = (len(cvs) == 3
                  and all(cv <= THRESHOLDS["margin_cv_excellent_max"]
                          for cv in cvs))
    direction = None
    if net["latest"] is not None and net["five_years_ago"] is not None:
        direction = net["latest"] - net["five_years_ago"]
    evidence = (f"5y net margin mean {net['mean']:.1%} "
                f"(CV {net['cv']:.3f}), gross margin mean "
                f"{gross['mean']:.1%} (CV {gross['cv']:.3f})"
                if net["cv"] is not None and gross["cv"] is not None
                else f"5y net margin mean {net['mean']:.1%}")
    if direction is not None:
        evidence += (f"; net margin "
                     f"{'expanded' if direction > 0.005 else 'contracted' if direction < -0.005 else 'flat'} "
                     f"by {direction:+.1%} over 5y")
    if (all_stable and gross["mean"] is not None
            and gross["mean"] >= THRESHOLDS["gross_margin_excellent_min"]):
        return _score(net["mean"], 2, evidence, formula, inputs)
    if net["cv"] is not None \
            and net["cv"] <= THRESHOLDS["margin_cv_adequate_max"]:
        return _score(net["mean"], 1, evidence, formula, inputs)
    if (net["mean"] < THRESHOLDS["net_margin_adequate_min"]
            or (net["cv"] is not None and net["cv"] > 0.30)):
        return _score(net["mean"], 0, evidence, formula, inputs)
    return _score(net["mean"], 1, evidence, formula, inputs)


# ---------------------------------------------------------------------------
# (a) Moat signals — efficiency / scale
# ---------------------------------------------------------------------------

def _efficiency(values, annual_periods, period, cashflow=None):
    """Cash conversion cycle and asset turnover."""
    ccc = None
    ccc_inputs = {}
    if cashflow is not None:
        ccc_result = cashflow.get("sections", {}).get(
            "working_capital", {}).get("cash_conversion_cycle", {})
        ccc = ccc_result.get("value")
        ccc_inputs = ccc_result.get("inputs", {})
    turnover = None
    turnover_inputs = {}
    revenue = _num(_get(values, "income_statement", period, "annual",
                        "revenue"))
    assets = _num(_get(values, "balance_sheet", period, "annual",
                       "total_assets"))
    if revenue is not None and assets:
        turnover = revenue / assets
        turnover_inputs = {"revenue": revenue, "total_assets": assets}
    inputs = {"cash_conversion_cycle_days": ccc,
              "cash_conversion_cycle_inputs": ccc_inputs,
              "asset_turnover": turnover,
              "asset_turnover_inputs": turnover_inputs}
    formula = ("cash conversion cycle from Step 20 (DIO + DSO - DPO, "
               "days, period-end balances); asset turnover = revenue / "
               "total_assets")
    if ccc is None:
        return _score(None, None,
                      "cash conversion cycle unavailable",
                      formula, inputs,
                      reason="missing inventory, receivables, payables, "
                             "cost of revenue or revenue")
    if ccc <= THRESHOLDS["ccc_excellent_max_days"]:
        return _score(ccc, 2,
                      f"cash conversion cycle {ccc:.1f} days — paid by "
                      "customers long before paying suppliers",
                      formula, inputs)
    if ccc <= THRESHOLDS["ccc_adequate_max_days"]:
        return _score(ccc, 1,
                      f"cash conversion cycle {ccc:.1f} days",
                      formula, inputs)
    if ccc >= THRESHOLDS["ccc_weak_min_days"]:
        return _score(ccc, 0,
                      f"cash conversion cycle {ccc:.1f} days — cash "
                      "tied up in working capital for a long time",
                      formula, inputs)
    return _score(ccc, 1,
                  f"cash conversion cycle {ccc:.1f} days",
                  formula, inputs)


# ---------------------------------------------------------------------------
# (b) Durability — growth consistency
# ---------------------------------------------------------------------------

def _growth_consistency(growth):
    """Revenue growth steadiness from Step 19."""
    series = (growth or {}).get("series", {}).get("revenue", {})
    cyclicality = series.get("cyclicality", {})
    verdict = cyclicality.get("verdict")
    consistency_10y = series.get("consistency_10y", {})
    inputs = {
        "cyclicality_verdict": verdict,
        "yoy_stdev_10y": cyclicality.get("yoy_stdev_10y"),
        "declines_10y": cyclicality.get("declines_10y"),
        "max_drawdown": cyclicality.get("max_drawdown"),
        "share_growing_10y": consistency_10y.get("share_growing"),
        "years_used_10y": consistency_10y.get("years_used"),
        "revenue_cagr_10y": (series.get("cagr", {})
                             .get(10, {}).get("value")),
    }
    formula = ("Step 19 revenue cyclicality (YoY stdev, declines in "
               "10y, max peak-to-trough drawdown) and 10y share of "
               "growing years")
    if verdict is None or verdict == "insufficient data":
        return _score(None, None,
                      "revenue growth consistency unavailable",
                      formula, inputs,
                      reason="Step 19 revenue cyclicality is "
                             f"'{verdict}'")
    share = inputs["share_growing_10y"]
    drawdown = inputs["max_drawdown"]
    if (verdict == "steady" and share == 1.0
            and (drawdown is None or drawdown < 0.10)):
        return _score(share, 2,
                      f"revenue growth steady: {share:.0%} of the last "
                      f"10 years growing, max drawdown "
                      f"{drawdown:.1%}" if drawdown is not None else
                      "revenue growth steady: every year in the last "
                      "10 growing",
                      formula, inputs)
    if verdict == "volatile":
        return _score(share, 0,
                      f"revenue growth volatile: "
                      f"{inputs['declines_10y']} YoY declines in 10y, "
                      f"max drawdown {drawdown:.1%}"
                      if drawdown is not None else
                      "revenue growth volatile",
                      formula, inputs)
    return _score(share, 1,
                  f"revenue growth moderate: {verdict}, "
                  f"{share:.0%} of the last 10 years growing"
                  if share is not None else
                  f"revenue growth moderate: {verdict}",
                  formula, inputs)


# ---------------------------------------------------------------------------
# (b) Durability — earnings quality
# ---------------------------------------------------------------------------

def _earnings_quality(values, annual_periods):
    """OCF vs net income, FCF conversion, SBC drag, profitability."""
    def _mean_ratio(num_concept, den_concept, years=5):
        num_series = _concept_series(values, num_concept)[-years:]
        den_series = dict(_concept_series(values, den_concept))
        ratios = []
        for period, num in num_series:
            den = den_series.get(period)
            if num is not None and den:
                ratios.append(num / den)
        return (statistics.fmean(ratios) if ratios else None), len(ratios)

    ocf_ni_mean, ocf_ni_years = _mean_ratio("operating_cash_flow",
                                            "net_income")
    fcf_ni_mean, fcf_ni_years = _mean_ratio("free_cash_flow",
                                            "net_income")
    latest = annual_periods[-1] if annual_periods else None
    sbc = _num(_get(values, "cash_flow_statement", latest, "annual",
                    "share_based_compensation")) if latest else None
    fcf_latest = _num(_get(values, "cash_flow_statement", latest,
                           "annual", "free_cash_flow")) if latest else None
    sbc_to_fcf = (sbc / fcf_latest
                  if sbc is not None and fcf_latest else None)
    # Profitable years in the 10-year window (net income > 0).
    ni_series = _concept_series(values, "net_income")[-10:]
    profitable = sum(1 for _, v in ni_series
                     if v is not None and v > 0)
    checked = sum(1 for _, v in ni_series if v is not None)
    inputs = {
        "ocf_to_net_income_5y_mean": ocf_ni_mean,
        "ocf_to_net_income_years": ocf_ni_years,
        "fcf_to_net_income_5y_mean": fcf_ni_mean,
        "fcf_to_net_income_years": fcf_ni_years,
        "fcf_to_net_income_latest": (
            fcf_latest / dict(_concept_series(values, "net_income"))
            .get(latest) if latest and fcf_latest is not None
            and dict(_concept_series(values, "net_income"))
            .get(latest) else None),
        "sbc_to_fcf_latest": sbc_to_fcf,
        "profitable_years": profitable,
        "profitable_years_checked": checked,
    }
    formula = ("5y mean OCF / net income and FCF / net income from "
               "Step 16 concepts; SBC / FCF latest; profitable years "
               "in the 10y window")
    if ocf_ni_mean is None and fcf_ni_mean is None:
        return _score(None, None,
                      "earnings quality unavailable",
                      formula, inputs,
                      reason="no usable OCF / FCF to net income ratios "
                             "in the 5-year window")
    evidence = []
    if ocf_ni_mean is not None:
        evidence.append(f"5y mean OCF / net income {ocf_ni_mean:.2f}")
    if fcf_ni_mean is not None:
        evidence.append(f"5y mean FCF / net income {fcf_ni_mean:.2f}")
    evidence.append(f"{profitable}/{checked} profitable years in 10y")
    if sbc_to_fcf is not None and sbc_to_fcf > THRESHOLDS["sbc_to_fcf_caution"]:
        evidence.append(
            f"caution: SBC is {sbc_to_fcf:.0%} of latest FCF")
    evidence_text = "; ".join(evidence)
    if (ocf_ni_mean is not None
            and ocf_ni_mean >= THRESHOLDS["ocf_to_ni_excellent_mean"]
            and fcf_ni_mean is not None
            and fcf_ni_mean >= THRESHOLDS["fcf_to_ni_excellent_mean"]
            and profitable == checked and checked > 0):
        return _score(ocf_ni_mean, 2, evidence_text, formula, inputs)
    if (ocf_ni_mean is not None
            and ocf_ni_mean < THRESHOLDS["ocf_to_ni_weak_mean"]) \
            or (fcf_ni_mean is not None
                and fcf_ni_mean < THRESHOLDS["fcf_to_ni_weak_mean"]) \
            or (checked > 0 and profitable < checked):
        return _score(ocf_ni_mean, 0, evidence_text, formula, inputs)
    return _score(ocf_ni_mean, 1, evidence_text, formula, inputs)


# ---------------------------------------------------------------------------
# (b) Durability — balance-sheet strength (Step 18 verdict)
# ---------------------------------------------------------------------------

def _balance_sheet_strength(health):
    """Step 18 verdict mapped to a component score."""
    verdict = (health or {}).get("verdict")
    reasons = (health or {}).get("verdict_reasons", [])
    mapping = {"strong": 2, "adequate": 1, "weak": 0}
    formula = "Step 18 health verdict (strong / adequate / weak / insufficient data)"
    if verdict in mapping:
        return _score(mapping[verdict], mapping[verdict],
                      f"Step 18 health verdict '{verdict}'",
                      formula,
                      {"health_verdict": verdict,
                       "health_reasons": reasons})
    return _score(None, None,
                  f"Step 18 health verdict is '{verdict}'",
                  formula,
                  {"health_verdict": verdict,
                   "health_reasons": reasons},
                  reason="health verdict is 'insufficient data': no "
                         "balance-sheet strength score")


# ---------------------------------------------------------------------------
# (c) Capital allocation
# ---------------------------------------------------------------------------

def _incremental_roic(values, annual_periods, years):
    """Change in NOPAT / change in invested capital over ``years``.

    NOPAT = operating income x (1 - effective tax rate); invested
    capital = total debt + total equity - cash.  Missing (never a
    nonsense number) when invested capital did not grow: a
    shrinking capital base makes the ratio meaningless.
    """
    if len(annual_periods) < years + 1:
        return None, (f"insufficient history: {len(annual_periods)} "
                      f"annual periods, need {years + 1} for a {years}y "
                      "incremental ROIC")
    end = annual_periods[-1]
    start = annual_periods[-1 - years]

    def _nopat(period):
        oi = _num(_get(values, "income_statement", period, "annual",
                       "operating_income"))
        tax = _num(_get(values, "income_statement", period, "annual",
                        "income_tax"))
        before_tax = _num(_get(values, "income_statement", period,
                               "annual", "income_before_tax"))
        if None in (oi, tax, before_tax) or before_tax == 0:
            return None
        return oi * (1 - tax / before_tax)

    def _ic(period):
        debt = _num(_get(values, "balance_sheet", period, "annual",
                         "total_debt"))
        equity = _num(_get(values, "balance_sheet", period, "annual",
                           "total_equity"))
        cash = _num(_get(values, "balance_sheet", period, "annual",
                         "cash_and_equivalents"))
        if None in (debt, equity, cash):
            return None
        return debt + equity - cash

    nopat_start, nopat_end = _nopat(start), _nopat(end)
    ic_start, ic_end = _ic(start), _ic(end)
    if None in (nopat_start, nopat_end, ic_start, ic_end):
        return None, "missing NOPAT or invested-capital input"
    d_ic = ic_end - ic_start
    if d_ic <= 0:
        return None, ("invested capital did not grow over the window "
                      f"({ic_start:,.0f} to {ic_end:,.0f}); an "
                      "incremental return on shrinking capital is "
                      "not meaningful")
    return ((nopat_end - nopat_start) / d_ic,
            {"start_period": start, "end_period": end,
             "nopat_start": nopat_start, "nopat_end": nopat_end,
             "invested_capital_start": ic_start,
             "invested_capital_end": ic_end})


def _capital_allocation(values, annual_periods):
    """Dividends, buybacks, reinvestment returns."""
    div_series = _concept_series(values, "dividends_paid")
    sh_series = _concept_series(values, "shares_outstanding_diluted")
    ni_series = dict(_concept_series(values, "net_income"))
    bb_series = dict(_concept_series(values, "share_repurchases"))

    # Dividend history: consecutive years of growth, cuts in 10y.
    # The 10y window is the last 11 dividend years (10 transitions).
    divs = [(p, v) for p, v in div_series if v is not None]
    cuts_10y = 0
    divs_10y = divs[-11:]  # last 11 years = 10 transitions
    for (_, prev_v), (_, v) in zip(divs_10y, divs_10y[1:]):
        if v < prev_v:
            cuts_10y += 1
    # Streak counted from the end: consecutive increases to latest.
    streak = 0
    for (_, prev_v), (_, v) in reversed(list(zip(divs, divs[1:]))):
        if v > prev_v:
            streak += 1
        else:
            break
    latest_div = divs[-1][1] if divs else None

    # Share count trend (buybacks reduce it).
    sh_5y = None
    if len(sh_series) >= 6:
        start_sh = [v for _, v in sh_series[-6:] if v is not None]
        end_sh = sh_series[-1][1]
        if len(start_sh) >= 2 and start_sh[0] and end_sh:
            sh_5y = end_sh / start_sh[0] - 1
    sh_10y = None
    if len(sh_series) >= 11:
        start_sh = [v for _, v in sh_series[-11:] if v is not None]
        end_sh = sh_series[-1][1]
        if len(start_sh) >= 2 and start_sh[0] and end_sh:
            sh_10y = end_sh / start_sh[0] - 1

    # Payout: (dividends + buybacks) / net income, latest and 5y mean.
    payouts = []
    for p, _ in sh_series[-5:]:
        ni = ni_series.get(p)
        div = dict(div_series).get(p)
        bb = bb_series.get(p)
        if ni and div is not None and bb is not None:
            payouts.append((div + bb) / ni)
    payout_latest = payouts[-1] if payouts else None
    payout_5y_mean = (statistics.fmean(payouts) if payouts else None)

    inc_roic_5y, inc_roic_5y_detail = _incremental_roic(
        values, annual_periods, 5)
    inc_roic_10y, inc_roic_10y_detail = _incremental_roic(
        values, annual_periods, 10)

    inputs = {
        "dividend_years": len(divs),
        "dividend_cuts_10y": cuts_10y,
        "dividend_growth_streak_years": streak,
        "latest_dividends_paid": latest_div,
        "shares_change_5y": sh_5y,
        "shares_change_10y": sh_10y,
        "payout_ratio_latest": payout_latest,
        "payout_ratio_5y_mean": payout_5y_mean,
        "incremental_roic_5y": inc_roic_5y,
        "incremental_roic_5y_detail": inc_roic_5y_detail,
        "incremental_roic_10y": inc_roic_10y,
        "incremental_roic_10y_detail": inc_roic_10y_detail,
    }
    formula = ("dividend cuts and growth streak from the dividends_paid "
               "series; share-count change from shares_outstanding_"
               "diluted; payout = (dividends + buybacks) / net income; "
               "incremental ROIC = change in NOPAT / change in invested "
               "capital, only when capital grew")
    if not divs and sh_5y is None and inc_roic_5y is None:
        return _score(None, None,
                      "capital allocation unavailable",
                      formula, inputs,
                      reason="no dividend history, share-count trend or "
                             "incremental ROIC available")
    evidence = []
    if divs:
        evidence.append(
            f"dividends grew in {streak} consecutive year(s) to the "
            f"latest, {cuts_10y} cut(s) in 10y")
    if sh_5y is not None:
        evidence.append(f"shares outstanding {sh_5y:+.1%} over 5y")
    if inc_roic_5y is not None:
        evidence.append(f"incremental ROIC 5y {inc_roic_5y:.1%}")
    if payout_latest is not None:
        evidence.append(f"payout ratio latest {payout_latest:.0%}")
    evidence_text = "; ".join(evidence) if evidence else "limited inputs"
    if (cuts_10y == 0 and streak >= 5 and sh_5y is not None
            and sh_5y < 0 and inc_roic_5y is not None
            and inc_roic_5y >= THRESHOLDS["incremental_roic_excellent"]):
        return _score(inc_roic_5y, 2, evidence_text, formula, inputs)
    if cuts_10y > 0 and streak == 0:
        return _score(inc_roic_5y, 0,
                      evidence_text + "; dividends cut recently",
                      formula, inputs)
    if (inc_roic_5y is not None
            and inc_roic_5y < THRESHOLDS["incremental_roic_weak"]):
        return _score(inc_roic_5y, 0, evidence_text, formula, inputs)
    return _score(inc_roic_5y, 1, evidence_text, formula, inputs)


# ---------------------------------------------------------------------------
# Overall rating
# ---------------------------------------------------------------------------

def _overall_rating(components):
    """Mean of scored components -> rating, with reasons."""
    scored = {name: comp for name, comp in components.items()
              if comp.get("score") is not None}
    if len(scored) < 3:
        return {
            "rating": "insufficient data",
            "mean_score": None,
            "components_scored": len(scored),
            "components_total": len(components),
            "reason": (f"only {len(scored)} of {len(components)} "
                       "components could be scored; at least 3 required"),
        }
    mean = statistics.fmean(comp["score"] for comp in scored.values())
    for threshold, rating in RATING_BANDS:
        if mean >= threshold:
            return {
                "rating": rating,
                "mean_score": mean,
                "components_scored": len(scored),
                "components_total": len(components),
                "reason": (f"mean component score {mean:.2f} over "
                           f"{len(scored)} scored components"),
            }
    return {
        "rating": "weak",
        "mean_score": mean,
        "components_scored": len(scored),
        "components_total": len(components),
        "reason": (f"mean component score {mean:.2f} over "
                   f"{len(scored)} scored components"),
    }


def _moat_evidence(components, rating):
    """Qualified moat statement from the financial evidence only."""
    if rating == "insufficient data":
        return {
            "assessment": "not established",
            "evidence": "insufficient financial data to assess any "
                        "durable advantage",
            "caveat": ("a moat cannot be established from financial "
                       "statements alone; brand, network effects, "
                       "switching costs and regulation need research"),
        }
    returns = components["returns_on_capital"].get("score")
    margins = components["margin_stability"].get("score")
    growth = components["growth_consistency"].get("score")
    if returns == 2 and margins == 2 and growth == 2:
        return {
            "assessment": "strong financial evidence of a durable advantage",
            "evidence": ("returns on capital persistently well above "
                         "the cost of capital, stable high margins, "
                         "and steady revenue growth"),
            "caveat": ("financial evidence only: it is consistent "
                       "with a durable advantage, it does not prove "
                       "one — brand, network effects, switching "
                       "costs and regulation still need research"),
        }
    if returns is not None and returns >= 1 \
            and margins is not None and margins >= 1:
        return {
            "assessment": "moderate financial evidence of a durable advantage",
            "evidence": ("returns on capital above the cost of capital "
                         "with reasonably stable margins"),
            "caveat": ("financial evidence only; the source of the "
                       "advantage (brand, network, switching costs) "
                       "still needs research"),
        }
    return {
        "assessment": "weak financial evidence of a durable advantage",
        "evidence": ("returns on capital and/or margins do not show "
                     "a persistent advantage"),
        "caveat": ("financial evidence only; a moat could still exist "
                   "in forms statements cannot show, and equally the "
                   "numbers do not support one"),
    }


# ---------------------------------------------------------------------------
# One-ticker assessment
# ---------------------------------------------------------------------------

def _growth_to_period(values, growth, period, annual_periods):
    """Step 19 growth analysis truncated to end at ``period``.

    The supplied full-series ``growth`` is used only when the
    assessed period is the latest annual period; for an earlier
    period the revenue series is truncated at that period and the
    YoY / consistency / cyclicality figures are recomputed, so a
    historical assessment never uses later years' growth.  Only
    the revenue series is rebuilt — it is the only series the
    quality assessment uses.
    """
    if growth is not None and period == annual_periods[-1]:
        return growth
    series = [item for item in fg._series(values, "revenue")
              if item[0] <= period]
    yoy = fg.yoy_growth(series)
    return {"ticker": None,
            "series": {"revenue": {
                "concept": "revenue",
                "yoy": yoy,
                "cagr": {years: fg.cagr(series, years)
                         for years in fg.CAGR_WINDOWS},
                "consistency_10y": fg.consistency_metrics(
                    yoy, window_years=10),
                "cyclicality": fg.assess_cyclicality(series, yoy),
            }},
            "warnings": []}


def assess_business_quality(conn, ticker, period=None,
                            period_type="annual",
                            cost_of_capital=DEFAULT_COST_OF_CAPITAL,
                            health=None, growth=None, cashflow=None):
    """Step 24 business-quality assessment for one ticker.

    ``conn`` is a read connection (tests use a temporary copy).
    ``period`` defaults to the latest annual period.  ``health`` /
    ``growth`` / ``cashflow`` are the Step 18/19/20 assessment dicts
    (computed here when not supplied).  Every window (5y/10y)
    ends at the assessed period, never at the latest period, so a
    historical assessment uses only data available at that time.
    Returns the seven components, the overall rating, the qualified
    moat statement, and the list of things statements cannot
    assess.  Never returns a buy/sell call.
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
            "rating": "insufficient data",
            "mean_score": None,
            "rating_reason": (
                f"only {len(window_periods)} annual period(s) of "
                "history up to the assessed period; at least 3 "
                "required"),
            "components": {},
            "components_scored": 0,
            "moat_evidence": _moat_evidence({}, "insufficient data"),
            "not_assessable": [
                {"item": item, "reason": reason}
                for item, reason in NOT_ASSESSABLE],
            "warnings": warnings + [
                "no annual income rows found" if not annual_periods
                else f"only {len(window_periods)} annual period(s) "
                     "up to the assessed period"],
            "cost_of_capital": cost_of_capital,
        }

    if health is None:
        health = fh.assess_health(conn, ticker, period=period,
                                  period_type=period_type)
    if cashflow is None:
        cashflow = cf.assess_cashflow(conn, ticker, period=period,
                                      period_type=period_type)
    growth = _growth_to_period(values, growth, period, annual_periods)

    components = {
        "returns_on_capital": _returns_on_capital(
            values, ticker, window_periods, cost_of_capital),
        "margin_stability": _margin_stability(values, ticker,
                                              window_periods),
        "efficiency": _efficiency(values, window_periods, period,
                                  cashflow=cashflow),
        "growth_consistency": _growth_consistency(growth),
        "earnings_quality": _earnings_quality(values, window_periods),
        "balance_sheet_strength": _balance_sheet_strength(health),
        "capital_allocation": _capital_allocation(values,
                                                  window_periods),
    }
    overall = _overall_rating(components)
    rating = overall["rating"]
    return {
        "ticker": ticker,
        "reporting_period": period,
        "period_type": period_type,
        "rating": rating,
        "mean_score": overall["mean_score"],
        "rating_reason": overall["reason"],
        "components": components,
        "components_scored": overall["components_scored"],
        "components_total": overall["components_total"],
        "moat_evidence": _moat_evidence(components, rating),
        "not_assessable": [
            {"item": item, "reason": reason}
            for item, reason in NOT_ASSESSABLE],
        "warnings": warnings,
        "cost_of_capital": cost_of_capital,
    }


def quality_rating(quality):
    """Rating string from a quality assessment dict (or a plain string)."""
    if quality is None:
        return None
    if isinstance(quality, str):
        return quality
    return quality.get("rating")


def assess_all_periods(conn, ticker,
                       cost_of_capital=DEFAULT_COST_OF_CAPITAL):
    """Annual quality assessments for every annual period."""
    values, _warnings = fr.load_standardised_values(conn, ticker)
    annual_periods = sorted(
        p for (st, p, pt) in values
        if st == "income_statement" and pt == "annual")
    return [assess_business_quality(conn, ticker, period=p,
                                    period_type="annual",
                                    cost_of_capital=cost_of_capital)
            for p in annual_periods]
