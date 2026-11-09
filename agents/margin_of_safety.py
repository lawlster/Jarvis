"""Step 23 — Margin of safety and relative valuation analysis.

Takes a valuation range (conservative / base / optimistic, from Step 21
or Step 22) and a price, and answers the owner's two questions:

1. How much margin of safety is there at this price?
2. At what price would there be enough?

It implements the owner's standing strongest-alert rule — a name at
least 20% below a conservative intrinsic value with no material
deterioration — plus a "good business at a good (reasonable) price"
tier, and it returns a structured verdict object, never a buy/sell
call.  The final investment decision is always the owner's.

Margin-of-safety convention
---------------------------
Step 23 defines margin of safety the Graham way:

    margin = (value - price) / value

i.e. the discount to value as a share of the value itself.  A price
20% below conservative value is a margin of +20%; a price above
value gives a negative margin.  NOTE: Steps 21 and 22 reported a
different number, ``(value - price) / price`` (discount as a share
of the price — MSFT vs base was -67.8% there).  Both numbers are
shown in the summary, labelled, so the two conventions are never
confused: ``margin_of_safety`` is the Step 23 convention,
``margin_vs_price`` is the Step 21/22 convention.

Entry-price ladder
------------------
For a requested discount the price that would give it is
``value * (1 - discount)``.  The ladder shows the prices at 0%,
20%, 30% and 40% below the conservative value — the 20% rung is
exactly the strongest-alert threshold price.  Where the DCF path
applies, each ladder price also carries the FCF growth rate the
price would imply (Step 21's implied-growth inversion); for a
bank/REIT dividend path the implied dividend growth is shown
instead (inverting Gordon growth); where neither applies the
implied growth is missing with a reason.

Tier logic (judgement rules, documented here and in code)
---------------------------------------------------------
Tiers, in order of how far the price sits below value:

* ``strongest_alert`` — margin vs the conservative value >= 20%
  (price at or below 80% of conservative value) AND no material
  deterioration.  Exactly 20% qualifies (>=, not >).
* ``reasonable_price`` — price at or below the base value but not
  a strongest alert (price inside the conservative–base range, or
  below base), and no material deterioration.  A deteriorating
  business that would otherwise alert falls back here, flagged.
* ``fairly_valued`` — price above base but at or below the
  optimistic value (inside the fair-value range, upper half).
* ``overvalued`` — price above the optimistic value.
* ``insufficient_data`` — conservative value or price missing.

Material deterioration (explicit rule, not guessed)
---------------------------------------------------
Deterioration is derived from the Step 18 health, Step 19 growth
and Step 20 cash-flow assessments, or from an explicit caller
override.  Since Step 27, the flag takes its verdict from the
Step 27 deterioration assessment (which itself builds on Steps
18/19/20) when one is available — see ``assess_deterioration``
and ``_deterioration_from_step27`` — and the rule below is kept
as the corroboration and as the fallback when Step 27 cannot
assess the ticker.  It is TRUE when any of these holds:

* Step 18 health verdict is ``weak`` (a hard distress flag fired),
  or the health verdict fell to ``weak`` from a prior period;
* the latest annual net income is negative;
* latest annual revenue fell more than 10% AND latest annual net
  income fell more than 20% (a broad decline, not one line item);
* latest annual operating cash flow is negative.

It is NOT deterioration on its own: a ``strong`` or ``adequate``
health verdict; a fall in free cash flow alone (MSFT FY2026 FCF
fell 6.5% while OCF rose 34.4% — a capex surge, Step 20 finding,
not business deterioration); a working-capital decline alone
(a Step 18 watch item, recorded as a caution); a single year's
earnings dip on an identified one-off.  Where the health/growth
inputs are missing the flag is UNKNOWN (``deteriorated=None``)
and a strongest alert is blocked — an unknown is never treated
as "no deterioration".

Quality gate (Step 24)
----------------------
A ``strongest_alert`` also requires business quality adequate or
better (Step 24 rating).  A ``weak`` or ``insufficient data``
quality rating blocks the alert even at a discount, exactly like
material deterioration: a cheap price on a weak business is not
a strongest alert.  The gate only applies when a quality rating
is supplied to ``classify_tier``; callers that do not pass one
keep the previous behaviour (the summary always passes one).

Sensitivity
-----------
How the margin moves with the discount rate and the growth
assumption, reusing Step 21's WACC sensitivity (10% / 11% / 12%)
and scenario growth rates for the DCF path, and a cost-of-equity
grid (9% / 10% / 11%) on the dividend discount for the bank/REIT
path.  The point is to show what the answer depends on.

Relative valuation
------------------
A cross-check, not a second valuation: the current P/E against the
P/E each range point would imply at the latest diluted EPS
(price / EPS vs value / EPS).  It is missing when EPS or the range
is missing.  Historical P/E itself is unavailable (Step 21 finding:
no historical price series stored).

This module never fetches a price and makes no network requests;
any price used is supplied by the caller or taken from the stored
snapshot, always labelled with its as-of date and source.  It reads
through a connection the caller supplies (used read-only) and never
writes.  It never touches the production database.
"""

import sqlite3

from agents import business_quality as bq
from agents import deterioration as det
from agents import financial_cashflow as cf
from agents import financial_growth as fg
from agents import financial_health as fh
from agents import financial_ratios as fr
from agents import intrinsic_valuation as iv
from agents import sector_valuation as sv

# ---------------------------------------------------------------------------
# Defaults (judgement, stated on every output)
# ---------------------------------------------------------------------------

STRONGEST_ALERT_MARGIN = 0.20
LADDER_DISCOUNTS = (0.0, 0.20, 0.30, 0.40)
WACC_SENSITIVITY = (0.10, 0.11, 0.12)
COST_OF_EQUITY_SENSITIVITY = (0.09, 0.10, 0.11)

TIERS = ("strongest_alert", "reasonable_price", "fairly_valued",
         "overvalued", "insufficient_data")

# Deterioration thresholds (see module docstring).
REVENUE_DECLINE_LIMIT = -0.10
NET_INCOME_DECLINE_LIMIT = -0.20

DETERIORATION_RULE = (
    "material deterioration = Step 18 health verdict 'weak' (or a fall "
    "to 'weak'), OR latest annual net income negative, OR latest "
    "annual revenue down >10% AND net income down >20%, OR latest "
    "annual operating cash flow negative. FCF falling alone, a "
    "working-capital decline alone, or a one-off earnings dip is NOT "
    "deterioration. Missing health/growth inputs make the flag "
    "UNKNOWN and block a strongest alert."
)


# ---------------------------------------------------------------------------
# Result helpers (same pattern as Steps 17-22)
# ---------------------------------------------------------------------------

def _result(name, value, formula, inputs, period, period_type,
            price_as_of=None, reason=None, extra=None):
    result = {
        "method": name,
        "value": value,
        "present": value is not None,
        "formula": formula,
        "inputs": inputs,
        "reporting_period": period,
        "period_type": period_type,
        "price_as_of": price_as_of,
        "reason": reason,
    }
    if extra:
        result.update(extra)
    return result


def _missing(name, reason, period, period_type, inputs=None,
             price_as_of=None, formula=""):
    return _result(name, None, formula, inputs or {}, period,
                   period_type, price_as_of=price_as_of, reason=reason)


# ---------------------------------------------------------------------------
# Margin of safety arithmetic
# ---------------------------------------------------------------------------

def margin_of_safety(value, price):
    """Graham margin of safety: (value - price) / value.

    Returns None when value or price is missing or non-positive.
    Positive means the price is below value; negative means above.
    """
    if value is None or price is None:
        return None
    if value <= 0 or price <= 0:
        return None
    return (value - price) / value


def margin_vs_price(value, price):
    """Step 21/22 convention: (value - price) / price.

    Shown alongside ``margin_of_safety`` so the two conventions are
    never confused.  Returns None on missing or non-positive inputs.
    """
    if value is None or price is None:
        return None
    if value <= 0 or price <= 0:
        return None
    return (value - price) / price


def price_at_margin(value, margin):
    """The price that gives the requested margin of safety.

    ``price = value * (1 - margin)``.  A 20% margin on a $100
    conservative value is an $80 entry price.  Returns None when
    value is missing or non-positive, or the margin is not a number.
    """
    if value is None or margin is None:
        return None
    if value <= 0:
        return None
    return value * (1.0 - margin)


def margins_for_range(fair_value_range, price, period=None,
                      period_type="annual", price_as_of=None):
    """Margin of safety vs each point of a fair-value range.

    Returns {point: result dict} for conservative / base /
    optimistic, each with both conventions.  Missing range points
    are missing with a reason, never zero.
    """
    out = {}
    for point in ("conservative", "base", "optimistic"):
        value = (fair_value_range or {}).get(point)
        inputs = {"value": value, "price": price}
        if value is None:
            out[point] = _missing(
                f"margin_of_safety_{point}",
                f"fair-value range point '{point}' is missing",
                period, period_type, inputs,
                price_as_of=price_as_of,
                formula="(value - price) / value")
        elif price is None:
            out[point] = _missing(
                f"margin_of_safety_{point}",
                "price is missing",
                period, period_type, inputs,
                price_as_of=price_as_of,
                formula="(value - price) / value")
        else:
            out[point] = _result(
                f"margin_of_safety_{point}",
                margin_of_safety(value, price),
                "(value - price) / value", inputs, period, period_type,
                price_as_of=price_as_of,
                extra={"margin_vs_price": margin_vs_price(value, price),
                       "fair_value": value, "price": price})
    return out


# ---------------------------------------------------------------------------
# Entry-price ladder
# ---------------------------------------------------------------------------

def implied_ddm_growth(price, dps, cost_of_equity):
    """Dividend growth the price already assumes (Gordon inversion).

    value = DPS x (1 + g) / (k - g)  =>  g = (k x price - DPS)
    / (price + DPS).  Returns None when the inputs are missing or
    the implied growth is outside [-0.90, 0.90], or k <= 0.
    """
    if price is None or dps is None or cost_of_equity is None:
        return None
    if price <= 0 or dps <= 0 or cost_of_equity <= 0:
        return None
    growth = (cost_of_equity * price - dps) / (price + dps)
    if growth < -0.90 or growth > 0.90:
        return None
    return growth


def entry_price_ladder(conservative_value, dcf_inputs=None,
                       ddm_inputs=None, period=None, period_type="annual"):
    """Entry prices at 0% / 20% / 30% / 40% below conservative value.

    ``dcf_inputs`` is the Step 21 input set (starting_fcf, net_debt,
    shares_outstanding, discount_rate, terminal_growth_rate) for the
    implied-FCF-growth column; ``ddm_inputs`` is (dps,
    cost_of_equity) for the implied-dividend-growth column.  At most
    one implied-growth column is filled; if neither applies the
    column is missing with a reason.  The 20% rung is exactly the
    strongest-alert threshold price.
    """
    if conservative_value is None or conservative_value <= 0:
        return _missing(
            "entry_price_ladder",
            "conservative value is missing or non-positive, so no "
            "entry prices can be computed",
            period, period_type,
            {"conservative_value": conservative_value},
            formula="entry price = conservative value x (1 - discount)")
    rungs = []
    for discount in LADDER_DISCOUNTS:
        price = price_at_margin(conservative_value, discount)
        implied_fcf_growth = None
        implied_dividend_growth = None
        implied_reason = None
        if dcf_inputs:
            implied_fcf_growth = iv.implied_growth(
                price, dcf_inputs.get("starting_fcf"),
                dcf_inputs.get("discount_rate", iv.DEFAULT_WACC),
                dcf_inputs.get("terminal_growth_rate",
                               iv.DEFAULT_TERMINAL_GROWTH),
                dcf_inputs.get("net_debt", 0),
                dcf_inputs.get("shares_outstanding"),
                dcf_inputs.get("forecast_years", iv.FORECAST_YEARS))
            if implied_fcf_growth is None:
                implied_reason = (
                    "implied FCF growth could not be inverted at "
                    "this ladder price (price outside the DCF range)")
        elif ddm_inputs:
            implied_dividend_growth = implied_ddm_growth(
                price, ddm_inputs.get("dps"),
                ddm_inputs.get("cost_of_equity"))
            if implied_dividend_growth is None:
                implied_reason = (
                    "implied dividend growth could not be inverted "
                    "at this ladder price")
        else:
            implied_reason = (
                "no DCF or dividend-discount inputs supplied, so no "
                "implied growth can be computed for this ladder price")
        rungs.append({
            "discount": discount,
            "entry_price": price,
            "implied_fcf_growth": implied_fcf_growth,
            "implied_dividend_growth": implied_dividend_growth,
            "implied_growth_reason": implied_reason,
            "is_strongest_alert_threshold": discount == 0.20,
        })
    return _result(
        "entry_price_ladder", conservative_value,
        "entry price = conservative value x (1 - discount); "
        "ladder discounts 0% / 20% / 30% / 40%",
        {"conservative_value": conservative_value,
         "discounts": list(LADDER_DISCOUNTS)},
        period, period_type,
        extra={"rungs": rungs})


# ---------------------------------------------------------------------------
# Material deterioration — explicit rule over Steps 18/19/20
# ---------------------------------------------------------------------------

def _latest_annual_yoy(growth, concept):
    """Latest annual YoY for a Step 19 series, or None."""
    series = (growth or {}).get("series", {}).get(concept)
    if not series:
        return None
    latest = series.get("latest_yoy")
    if not latest:
        return None
    return latest.get("value")


def _latest_value(values, concept):
    """Latest annual value of a concept from standardised values."""
    stmt = fr._statement_for(concept)
    periods = fr._periods(values, stmt, "annual")
    if not periods:
        return None
    return fr._num(fr._get(values, stmt, periods[-1], "annual",
                           concept))


def _deterioration_from_step27(deterioration_27):
    """Step 23 flag from a Step 27 assessment (see docstring above).

    The boolean comes from Step 27's ``material_deterioration``.
    Signals are Step 27's high-severity risk flags plus any
    deteriorating area's evidence.  Step 23's simpler rule is
    re-run from Step 27's compact ``inputs`` (health verdict,
    latest YoYs, latest OCF / net income) as a corroboration: if
    the simple rule would flag deterioration while Step 27 does
    not, that disagreement is recorded as a caution.  The flag is
    never unknown when Step 27 assessed the ticker.
    """
    inputs = deterioration_27.get("inputs", {})
    material = deterioration_27.get("material_deterioration")
    signals = []
    for flag in deterioration_27.get("risk_flags", []):
        if flag.get("severity") == "high":
            signals.append(f"{flag['name']}: {flag['evidence']}")
    for area_name, area in (deterioration_27.get("areas", {}) or {}).items():
        if area.get("verdict") == "deteriorating":
            signals.append(
                f"Step 27 area '{area_name}' deteriorating: "
                + "; ".join(area.get("evidence", [])[:2]))
    cautions = list(deterioration_27.get("cautions", []))

    # Corroboration: Step 23's simpler rule from the same inputs.
    simple_signals = []
    if inputs.get("health_verdict") == "weak":
        simple_signals.append(
            "Step 18 health verdict is 'weak' (a hard distress flag "
            "fired)")
    if inputs.get("health_fell_to_weak"):
        simple_signals.append(
            "Step 18 health verdict fell to 'weak' in the latest "
            "period")
    revenue_yoy = inputs.get("revenue_yoy_latest_annual")
    net_income_yoy = inputs.get("net_income_yoy_latest_annual")
    if (revenue_yoy is not None and net_income_yoy is not None
            and revenue_yoy < REVENUE_DECLINE_LIMIT
            and net_income_yoy < NET_INCOME_DECLINE_LIMIT):
        simple_signals.append(
            "broad decline: latest annual revenue YoY "
            f"{revenue_yoy:.1%} and net income YoY "
            f"{net_income_yoy:.1%} (thresholds -10% and -20%)")
    ocf = inputs.get("operating_cash_flow_latest_annual")
    if ocf is not None and ocf < 0:
        simple_signals.append(
            "latest annual operating cash flow is negative")
    net_income = inputs.get("net_income_latest_annual")
    if net_income is not None and net_income < 0:
        simple_signals.append("latest annual net income is negative")
    for signal in simple_signals:
        if signal not in signals:
            signals.append(signal)
    if simple_signals and material is False:
        cautions.append(
            "Step 23's simpler rule would flag deterioration "
            "(" + "; ".join(simple_signals) + ") but Step 27's "
            "deeper assessment does not — the Step 27 verdict "
            "governs the flag; the disagreement is recorded here, "
            "not hidden")
    if material is True and not signals:
        signals.append(
            "Step 27 overall verdict is 'deteriorating': "
            + "; ".join(deterioration_27.get("overall_reasons", [])))
    return {
        "deteriorated": material,
        "unknown": False,
        "signals": signals,
        "cautions": cautions,
        "rule": DETERIORATION_RULE,
        "source": ("Step 27 deterioration assessment "
                   "(Steps 18/19/20 as inputs), corroborated by the "
                   "Step 23 rule"),
        "step27_overall_verdict":
            deterioration_27.get("overall_verdict"),
        "inputs": {
            "health_verdict": inputs.get("health_verdict"),
            "revenue_yoy_latest_annual": revenue_yoy,
            "net_income_yoy_latest_annual": net_income_yoy,
            "fcf_yoy_latest_annual":
                inputs.get("fcf_yoy_latest_annual"),
            "operating_cash_flow_latest_annual": ocf,
            "net_income_latest_annual": net_income,
        },
    }


def assess_deterioration(conn, ticker, health=None, growth=None,
                         cashflow=None, deterioration_override=None,
                         deterioration_27=None):
    """Material-deterioration flag from Steps 18/19/20 (rule in docstring).

    ``health`` / ``growth`` / ``cashflow`` are the Step 18/19/20
    assessment dicts (computed here when not supplied).
    ``deterioration_override`` is an explicit caller input
    (True/False); when given it is used and recorded as the source.
    ``deterioration_27`` is a Step 27 deterioration assessment
    (``agents.deterioration.assess_deterioration``); when supplied
    and usable (overall verdict not ``insufficient data``) the flag
    takes its boolean from Step 27, its signals from Step 27's
    high-severity risk flags, and its cautions from Step 27's
    warnings — corroborated against this module's simpler rule
    from the same inputs, with any disagreement recorded as a
    caution, never hidden.  When Step 27 is unavailable the
    existing Step 18/19/20 logic below runs unchanged.
    Returns {"deteriorated": bool|None, "unknown": bool, "signals":
    [...], "cautions": [...], "rule": ..., "source": ...}.
    ``deteriorated=None`` means unknown — and an unknown blocks a
    strongest alert, it is never treated as "no deterioration".
    """
    signals = []
    cautions = []
    source = "Steps 18/19/20 assessments"
    if deterioration_override is not None:
        return {
            "deteriorated": bool(deterioration_override),
            "unknown": False,
            "signals": [f"caller-supplied deterioration override: "
                        f"{bool(deterioration_override)}"],
            "cautions": [],
            "rule": DETERIORATION_RULE,
            "source": "caller-supplied deterioration_override",
        }

    if deterioration_27 is not None and deterioration_27.get(
            "overall_verdict") not in (None, "insufficient data"):
        return _deterioration_from_step27(deterioration_27)

    if health is None:
        health = fh.assess_health(conn, ticker)
    if growth is None:
        growth = fg.analyse_growth(conn, ticker)
    if cashflow is None:
        cashflow = cf.assess_cashflow(conn, ticker)

    verdict = health.get("verdict")
    if verdict == "weak":
        signals.append(
            "Step 18 health verdict is 'weak' (a hard distress flag "
            "fired)")
    elif verdict == "insufficient data":
        cautions.append(
            "Step 18 health verdict is 'insufficient data': "
            "deterioration cannot be established from health")
    # A fall to weak in the latest period vs the prior one.
    try:
        all_periods = fh.assess_all_periods(conn, ticker)
    except Exception:  # noqa: BLE001 - report, never crash
        all_periods = []
    if len(all_periods) >= 2:
        latest_v = all_periods[-1].get("verdict")
        prior_v = all_periods[-2].get("verdict")
        if latest_v == "weak" and prior_v != "weak":
            signals.append(
                "Step 18 health verdict fell to 'weak' from "
                f"'{prior_v}' in the prior period")

    revenue_yoy = _latest_annual_yoy(growth, "revenue")
    net_income_yoy = _latest_annual_yoy(growth, "net_income")
    fcf_yoy = _latest_annual_yoy(growth, "free_cash_flow")
    if (revenue_yoy is not None and net_income_yoy is not None
            and revenue_yoy < REVENUE_DECLINE_LIMIT
            and net_income_yoy < NET_INCOME_DECLINE_LIMIT):
        signals.append(
            "broad decline: latest annual revenue YoY "
            f"{revenue_yoy:.1%} and net income YoY "
            f"{net_income_yoy:.1%} (thresholds -10% and -20%)")
    if fcf_yoy is not None and fcf_yoy < 0:
        cautions.append(
            f"free cash flow YoY {fcf_yoy:.1%} is negative, but FCF "
            "falling alone is NOT deterioration (Step 20: it can be "
            "a capex surge, not business weakness)")

    # Latest annual levels, from the cash-flow assessment sections.
    sections = cashflow.get("sections", {})
    ocf = None
    net_income = None
    try:
        ocf = sections.get("operating_cash_flow", {}).get(
            "operating_cash_flow", {}).get("value")
    except AttributeError:
        ocf = None
    if ocf is None:
        # Fall back to the standardised values directly.
        try:
            values, _ = fr.load_standardised_values(conn, ticker)
            ocf = _latest_value(values, "operating_cash_flow")
        except Exception:  # noqa: BLE001 - report, never crash
            ocf = None
    if ocf is not None and ocf < 0:
        signals.append("latest annual operating cash flow is negative")
    # Latest annual net income, always checked (not only in the
    # OCF fallback path above).
    try:
        values, _ = fr.load_standardised_values(conn, ticker)
        net_income = _latest_value(values, "net_income")
    except Exception:  # noqa: BLE001 - report, never crash
        net_income = None
    if net_income is not None and net_income < 0:
        signals.append("latest annual net income is negative")

    # Working-capital decline is a watch item, never a signal.
    try:
        wc_trend = health.get("sections", {}).get(
            "liquidity", {}).get("working_capital_trend", {})
        change_5y = wc_trend.get("change_5y")
        if change_5y is not None and change_5y < 0:
            cautions.append(
                f"working capital down {change_5y:.1%} over 5y "
                "(Step 18 watch item: current liabilities growing "
                "faster than current assets, not deterioration "
                "on its own)")
    except AttributeError:
        pass

    has_inputs = (verdict not in (None, "insufficient data")
                  or revenue_yoy is not None
                  or net_income_yoy is not None
                  or ocf is not None)
    unknown = not has_inputs
    deteriorated = None if unknown else bool(signals)
    if unknown:
        cautions.append(
            "no health/growth/cash-flow inputs available: "
            "deterioration is UNKNOWN and a strongest alert is "
            "blocked")
    return {
        "deteriorated": deteriorated,
        "unknown": unknown,
        "signals": signals,
        "cautions": cautions,
        "rule": DETERIORATION_RULE,
        "source": source,
        "inputs": {
            "health_verdict": verdict,
            "revenue_yoy_latest_annual": revenue_yoy,
            "net_income_yoy_latest_annual": net_income_yoy,
            "fcf_yoy_latest_annual": fcf_yoy,
            "operating_cash_flow_latest_annual": ocf,
            "net_income_latest_annual": net_income,
        },
    }


# ---------------------------------------------------------------------------
# Tier logic
# ---------------------------------------------------------------------------

def classify_tier(margin_conservative, margin_base, margin_optimistic,
                  deterioration, quality=None):
    """Tier from the margins and the deterioration flag.

    ``margin_*`` are Step 23 margins (value - price) / value, or
    None when missing.  ``deterioration`` is the dict from
    ``assess_deterioration``.  ``quality`` is the Step 24 quality
    assessment dict (or a plain rating string); when supplied, a
    ``weak`` or ``insufficient data`` rating blocks a strongest
    alert — a strongest alert requires quality adequate or better.
    When ``quality`` is None the gate does not apply (previous
    behaviour; the summary always supplies one).  Returns {"tier",
    "reasons", "strongest_alert_blocked", "quality_rating",
    "conditions_to_change": [...]}.  Never returns a buy/sell call.
    """
    quality_rating = bq.quality_rating(quality)
    reasons = []
    conditions = []
    blocked = False
    deteriorated = (deterioration or {}).get("deteriorated")
    unknown = (deterioration or {}).get("unknown", True)

    if margin_conservative is None:
        return {
            "tier": "insufficient_data",
            "reasons": ["conservative value or price is missing, so "
                        "no margin of safety can be computed"],
            "strongest_alert_blocked": False,
            "quality_rating": quality_rating,
            "conditions_to_change": [
                "a conservative intrinsic value and a price are "
                "needed before any tier can be assigned"],
        }

    alert_qualifies = margin_conservative >= STRONGEST_ALERT_MARGIN
    if alert_qualifies:
        if deteriorated is True:
            blocked = True
            reasons.append(
                f"margin vs conservative value is "
                f"{margin_conservative:.1%} (>= 20%) but a strongest "
                "alert is BLOCKED by material deterioration: "
                + "; ".join((deterioration or {}).get("signals", [])))
        elif unknown:
            blocked = True
            reasons.append(
                f"margin vs conservative value is "
                f"{margin_conservative:.1%} (>= 20%) but a strongest "
                "alert is BLOCKED: deterioration is UNKNOWN "
                "(no health/growth inputs)")
        else:
            if quality_rating in ("weak", "insufficient data"):
                # Quality gate (Step 24): a cheap price on a weak
                # business is not a strongest alert.  Fall through
                # to the margin-based tiers with blocked=True, the
                # same as a deterioration block.
                blocked = True
                reasons.append(
                    f"margin vs conservative value is "
                    f"{margin_conservative:.1%} (>= 20%) but a "
                    "strongest alert is BLOCKED by business quality: "
                    f"Step 24 rating is '{quality_rating}' "
                    "(strongest alert requires quality adequate or "
                    "better)")
            else:
                reasons = reasons + [
                    f"margin vs conservative value is "
                    f"{margin_conservative:.1%} (>= 20% threshold)",
                    "no material deterioration (Step 18/19/20 rule)"]
                if quality_rating is not None:
                    reasons.append(
                        "business quality rating is "
                        f"'{quality_rating}' (adequate or better)")
                return {
                    "tier": "strongest_alert",
                    "reasons": reasons,
                    "strongest_alert_blocked": False,
                    "quality_rating": quality_rating,
                    "conditions_to_change": [
                        "a price rise above 80% of conservative value, "
                        "or new evidence of material deterioration, "
                        "would remove the alert"],
                }

    # Step 52: a missing base or optimistic margin must never
    # crash the tier logic (the old code formatted ``None`` with
    # ``:.1%`` and raised TypeError).  The conservative margin
    # alone can only justify a strongest alert (handled above);
    # without the base value the price cannot be placed inside
    # the fair-value range, and without the optimistic value a
    # price above base cannot be split into fairly_valued vs
    # overvalued — so the honest answer is insufficient_data,
    # with the blocked flag preserved.
    if margin_base is None:
        reasons.append(
            "base value is missing, so the price cannot be placed "
            "inside the fair-value range")
        return {
            "tier": "insufficient_data",
            "reasons": reasons,
            "strongest_alert_blocked": blocked,
            "quality_rating": quality_rating,
            "conditions_to_change": [
                "a base intrinsic value is needed before any tier "
                "can be assigned"],
        }

    if margin_base is not None and margin_base >= 0:
        # Price at or below base value: reasonable, not an alert.
        if blocked:
            tier = "reasonable_price"
            reasons.append(
                "price is at or below base value, so the tier is "
                "'reasonable_price' with the strongest alert blocked")
        else:
            tier = "reasonable_price"
            reasons.append(
                f"price is at or below base value (margin vs base "
                f"{margin_base:.1%}) but the margin vs conservative "
                f"value is {margin_conservative:.1%} (< 20%)")
        conditions.append(
            "a fall to 80% of conservative value (with no material "
            "deterioration) would make it a strongest alert")
        conditions.append(
            "a rise above base value would move it to "
            "'fairly_valued' or 'overvalued'")
        return {"tier": tier, "reasons": reasons,
                "strongest_alert_blocked": blocked,
                "quality_rating": quality_rating,
                "conditions_to_change": conditions}

    if margin_optimistic is not None and margin_optimistic >= 0:
        # Price above base but inside the range.
        if blocked:
            reasons.append(
                "price is above base value, so even with the alert "
                "blocked the tier is 'fairly_valued'")
        else:
            reasons.append(
                f"price is above base value (margin vs base "
                f"{margin_base:.1%}) but inside the fair-value "
                f"range (margin vs optimistic "
                f"{margin_optimistic:.1%})")
        conditions.append(
            "a fall to base value would make it 'reasonable_price'; "
            "a fall to 80% of conservative value (with no material "
            "deterioration) would make it a strongest alert")
        conditions.append(
            "a rise above optimistic value would make it "
            "'overvalued'")
        return {"tier": "fairly_valued", "reasons": reasons,
                "strongest_alert_blocked": blocked,
                "quality_rating": quality_rating,
                "conditions_to_change": conditions}

    # Step 52: same guard as for the base margin — a price above
    # base with no optimistic value cannot be split into
    # fairly_valued vs overvalued.  (At this point margin_base is
    # known not None, and margin_optimistic is the only remaining
    # unknown.)
    if margin_optimistic is None:
        reasons.append(
            "optimistic value is missing, so a price above base "
            "value cannot be placed as fairly_valued or overvalued")
        return {
            "tier": "insufficient_data",
            "reasons": reasons,
            "strongest_alert_blocked": blocked,
            "quality_rating": quality_rating,
            "conditions_to_change": [
                "an optimistic intrinsic value is needed before "
                "any tier can be assigned"],
        }

    # Price above the optimistic value.
    if blocked:
        reasons.append(
            "price is above optimistic value, so the tier is "
            "'overvalued' regardless of the blocked alert")
    else:
        reasons.append(
            f"price is above optimistic value (margin vs optimistic "
            f"{margin_optimistic:.1%})")
    conditions.append(
        "a fall to optimistic value would make it 'fairly_valued'; "
        "a fall to base value would make it 'reasonable_price'; a "
        "fall to 80% of conservative value (with no material "
        "deterioration) would make it a strongest alert")
    return {"tier": "overvalued", "reasons": reasons,
            "strongest_alert_blocked": blocked,
            "quality_rating": quality_rating,
            "conditions_to_change": conditions}


# ---------------------------------------------------------------------------
# Sensitivity — how the margin moves with WACC / growth
# ---------------------------------------------------------------------------

def dcf_margin_sensitivity(starting_fcf, net_debt, shares_outstanding,
                           price, growth_rates=None, waccs=None,
                           terminal_growth_rate=iv.DEFAULT_TERMINAL_GROWTH,
                           years=iv.FORECAST_YEARS, period=None,
                           period_type="ttm", price_as_of=None):
    """Margin of safety across WACC x growth for the DCF path.

    Reuses Step 21's scenario growth rates and WACC sensitivity.
    Returns {"rows": [{wacc, growth_rate, value, margin_of_safety,
    margin_vs_price}], ...} with missing values where the DCF
    cannot run.  Price is the caller's, labelled with its as-of date.
    """
    growth_rates = growth_rates or list(iv.SCENARIO_GROWTH.values())
    waccs = waccs or list(WACC_SENSITIVITY)
    rows = []
    for wacc in waccs:
        for growth in growth_rates:
            result = iv.dcf_value(
                starting_fcf, growth, wacc, terminal_growth_rate,
                net_debt, shares_outstanding, years, period,
                period_type)
            value = result.get("value")
            rows.append({
                "discount_rate_wacc": wacc,
                "growth_rate": growth,
                "value": value,
                "margin_of_safety": margin_of_safety(value, price),
                "margin_vs_price": margin_vs_price(value, price),
                "reason": result.get("reason"),
            })
    return {
        "method": "dcf_margin_sensitivity",
        "formula": "Step 21 DCF value at each WACC x growth; margin "
                   "= (value - price) / value",
        "price": price,
        "price_as_of": price_as_of,
        "rows": rows,
    }


def ddm_margin_sensitivity(dps, price, cost_of_equities=None,
                           growth_rate=sv.DEFAULT_TERMINAL_GROWTH,
                           period=None, period_type="annual",
                           price_as_of=None):
    """Margin of safety across cost of equity for the dividend path.

    Gordon DDM value at each cost of equity in the grid, with the
    margin vs each value.  Used for bank/REIT paths where the DCF
    path does not apply.
    """
    cost_of_equities = cost_of_equities or list(
        COST_OF_EQUITY_SENSITIVITY)
    rows = []
    for coe in cost_of_equities:
        result = sv.dividend_discount_value(dps, growth_rate, coe,
                                            period, period_type)
        value = result.get("value")
        rows.append({
            "cost_of_equity": coe,
            "growth_rate": growth_rate,
            "value": value,
            "margin_of_safety": margin_of_safety(value, price),
            "margin_vs_price": margin_vs_price(value, price),
            "reason": result.get("reason"),
        })
    return {
        "method": "ddm_margin_sensitivity",
        "formula": "Gordon DDM value DPS x (1 + g) / (k - g) at each "
                   "cost of equity; margin = (value - price) / value",
        "price": price,
        "price_as_of": price_as_of,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Relative valuation cross-check
# ---------------------------------------------------------------------------

def relative_pe_check(price, eps_diluted, fair_value_range,
                      period=None, period_type="annual"):
    """Current P/E vs the P/E each range point would imply.

    implied P/E = value / latest diluted EPS.  Missing when EPS or
    the range is missing.  A cross-check only — it is not a second
    valuation, and historical P/E itself is unavailable (Step 21).
    """
    inputs = {"price": price, "eps_diluted": eps_diluted,
              "fair_value_range": fair_value_range}
    formula = "current P/E = price / EPS; implied P/E at a range " \
              "point = value / EPS"
    if price is None or eps_diluted is None or eps_diluted <= 0:
        return _missing("relative_pe_check",
                        "price or latest diluted EPS missing or "
                        "non-positive",
                        period, period_type, inputs, formula=formula)
    if not fair_value_range or all(
            v is None for v in fair_value_range.values()):
        return _missing("relative_pe_check",
                        "fair-value range is missing",
                        period, period_type, inputs, formula=formula)
    current_pe = price / eps_diluted
    implied = {}
    for point in ("conservative", "base", "optimistic"):
        value = (fair_value_range or {}).get(point)
        implied[point] = (value / eps_diluted
                          if value is not None else None)
    return _result("relative_pe_check", current_pe, formula, inputs,
                   period, period_type,
                   extra={"current_pe": current_pe,
                          "implied_pe_by_range_point": implied,
                          "eps_diluted": eps_diluted})


# ---------------------------------------------------------------------------
# Combined summary — one ticker, Step 22 range + Step 23 interpretation
# ---------------------------------------------------------------------------

def margin_of_safety_summary(conn, ticker, price_input=None,
                             sector_override=None,
                             deterioration_override=None,
                             starting_fcf_basis="ttm"):
    """Full Step 23 margin-of-safety summary for one ticker.

    Takes the Step 22 sector valuation summary (which for
    technology / general delegates to the Step 21 DCF range),
    computes margins, the entry-price ladder, the tier, the
    deterioration flag from Steps 18/19/20, the sensitivity and
    the relative P/E cross-check.  Never returns a buy/sell call.
    """
    sector = sv.sector_valuation_summary(
        conn, ticker, sector_override=sector_override,
        price_input=price_input,
        starting_fcf_basis=starting_fcf_basis)
    fair_value_range = sector["fair_value_range"]
    price = sector["price"]
    price_as_of = sector["price_as_of"]
    period = sector["reporting_period"]
    period_type = sector["period_type"]

    margins = margins_for_range(fair_value_range, price, period,
                                period_type, price_as_of)
    margin_conservative = margins["conservative"]["value"]
    margin_base = margins["base"]["value"]
    margin_optimistic = margins["optimistic"]["value"]

    # Step 27 deterioration assessment — the deeper verdict that
    # drives the flag when available (guarded like business
    # quality: a Step 27 failure never breaks the margin summary,
    # and then the Step 18/19/20 logic in assess_deterioration
    # runs instead).
    deterioration_27 = None
    deterioration_27_error = None
    try:
        deterioration_27 = det.assess_deterioration(conn, ticker)
    except Exception as exc:  # noqa: BLE001 - report, never crash
        deterioration_27_error = str(exc)
    deterioration = assess_deterioration(
        conn, ticker, deterioration_override=deterioration_override,
        deterioration_27=deterioration_27)

    # Business quality (Step 24) — the quality gate: a strongest
    # alert requires quality adequate or better.  Guarded so a
    # quality failure never breaks the margin summary; a missing
    # quality assessment is recorded, not treated as good quality.
    quality = None
    quality_error = None
    try:
        quality = bq.assess_business_quality(conn, ticker)
    except Exception as exc:  # noqa: BLE001 - report, never crash
        quality_error = str(exc)
    tier = classify_tier(margin_conservative, margin_base,
                         margin_optimistic, deterioration,
                         quality=quality)

    # Ladder inputs: DCF path (technology / general) or DDM path.
    dcf_inputs = None
    ddm_inputs = None
    sensitivity = None
    if sector["sector"] in ("technology", "general", "unknown"):
        inputs, _warnings = iv.valuation_inputs(conn, ticker)
        starting_fcf = (inputs["ttm_free_cash_flow"]
                        if starting_fcf_basis == "ttm"
                        else inputs["annual_free_cash_flow"])
        dcf_inputs = {
            "starting_fcf": starting_fcf,
            "net_debt": inputs["net_debt"],
            "shares_outstanding":
                inputs["shares_outstanding_diluted"],
            "discount_rate": iv.DEFAULT_WACC,
            "terminal_growth_rate": iv.DEFAULT_TERMINAL_GROWTH,
            "forecast_years": iv.FORECAST_YEARS,
        }
        sensitivity = dcf_margin_sensitivity(
            starting_fcf, inputs["net_debt"],
            inputs["shares_outstanding_diluted"], price,
            period=period, period_type=period_type,
            price_as_of=price_as_of)
    elif sector["sector"] in ("bank", "reit"):
        values, _ = fr.load_standardised_values(conn, ticker)
        history = sv.dps_history(values)
        if history:
            latest = sorted(history)[-1]
            ddm_inputs = {"dps": history[latest],
                          "cost_of_equity": sv.DEFAULT_COST_OF_EQUITY}
            sensitivity = ddm_margin_sensitivity(
                history[latest], price, period=period,
                period_type=period_type, price_as_of=price_as_of)

    ladder = entry_price_ladder(
        fair_value_range.get("conservative"), dcf_inputs=dcf_inputs,
        ddm_inputs=ddm_inputs, period=period, period_type=period_type)

    # Relative P/E cross-check (latest diluted EPS, annual).
    eps = None
    try:
        values, _ = fr.load_standardised_values(conn, ticker)
        annual = fr._periods(values, "income_statement", "annual")
        if annual:
            eps = fr._num(fr._get(values, "income_statement",
                                  annual[-1], "annual", "eps_diluted"))
    except Exception:  # noqa: BLE001 - report, never crash
        eps = None
    relative = relative_pe_check(price, eps, fair_value_range,
                                 period, period_type)

    # Strongest-alert threshold price, for the conditions list.
    conservative = fair_value_range.get("conservative")
    alert_price = price_at_margin(conservative, STRONGEST_ALERT_MARGIN) \
        if conservative else None

    return {
        "ticker": ticker,
        "sector": sector["sector"],
        "sector_source": sector["sector_source"],
        "why_this_model": sector["why_this_model"],
        "reporting_period": period,
        "period_type": period_type,
        "price": price,
        "price_as_of": price_as_of,
        "price_source": sector["price_source"],
        "fair_value_range": fair_value_range,
        "margins_of_safety": margins,
        "margin_of_safety_vs_base": margin_base,
        "margin_of_safety_vs_conservative": margin_conservative,
        "margin_of_safety_vs_optimistic": margin_optimistic,
        "margin_convention": (
            "Step 23 (Graham): margin = (value - price) / value. "
            "Steps 21/22 used (value - price) / price; that number "
            "is shown as margin_vs_price on each margin result."),
        "entry_price_ladder": ladder,
        "strongest_alert_threshold_price": alert_price,
        "strongest_alert_margin": STRONGEST_ALERT_MARGIN,
        "tier": tier,
        "deterioration": deterioration,
        "deterioration_27": deterioration_27,
        "deterioration_27_error": deterioration_27_error,
        "business_quality": quality,
        "business_quality_error": quality_error,
        "sensitivity": sensitivity,
        "relative_pe_check": relative,
        "warnings": list(sector.get("warnings", [])),
    }
