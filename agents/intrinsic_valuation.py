"""Step 21 — Intrinsic valuation engine.

Computes intrinsic-value estimates from Step 16 standardised
statements, using Step 17 ratios / TTM construction and Step 19
growth CAGRs as context.  It never fetches a price itself and makes
no network requests; any price used is supplied by the caller and
always labelled with its as-of date and source.

Canonical DCF
-------------
The DCF arithmetic is ``tools/valuation.py::calculate_dcf`` — the
existing tool.  This module does NOT build a second DCF: it wraps
``calculate_dcf`` with a 10-year forecast, scenario handling and an
implied-growth inversion, and documents the wrapper as the Step 21
entry point.  ``calculate_dcf`` projects FCF as
``FCF * (1 + g) ** year``, discounts at the WACC, and values the
terminal year with Gordon growth on the final-year FCF.

Design rules (from the project brief, Steps 16-21):

* Never invent a missing figure.  A method whose input is missing is
  returned with ``value=None``, ``present=False`` and a ``reason``.
* Every estimate records its assumptions, inputs, reporting period
  and the as-of date of any price used.  Intrinsic value is an
  estimate with uncertainty, not an exact price — the module never
  presents a buy/sell call.  The margin-of-safety field is a number
  (fair value vs price); Step 23 interprets it.
* Starting FCF is TTM free cash flow by default (the most recent
  12 months), built by Step 17's TTM construction; the annual-FY
  variant is computed alongside and labelled.  FCF is the Step 16
  derived concept (operating cash flow - capital expenditure).
* Scenario growth rates are judgement anchored to Step 19 CAGRs,
  stated as assumptions, not forecasts: bear +3% (near the 5y FCF
  CAGR), base +10% (near the 10y FCF CAGR), optimistic +15%
  (between the 10y revenue and OCF CAGRs).  WACC 11% and terminal
  growth 2.5% follow the DCFs already delivered for this project;
  a WACC sensitivity (10% / 11% / 12%) is shown for every scenario.
* A saved valuation profile (``valuation_profiles`` table, if one
  exists for the ticker) overrides the default base-scenario
  assumptions; the table is currently empty, so defaults apply and
  the override path is exercised only by fixture tests.

What is unavailable, honestly:

* Owner earnings (Buffett: net income + D&A + other non-cash
  charges - average maintenance capex): no depreciation /
  amortisation concept exists in the Step 16 concept set, no such
  key exists anywhere in the US raw data, capex is not split into
  maintenance vs growth, and no other non-cash-charge concept
  exists.  Returned missing with the exact missing inputs.  No
  partial version is offered: dropping the D&A add-back and the
  maintenance-capex distinction would silently turn it into FCF.
  FCF is reported as the closest proxy, labelled as such.
* EV/EBITDA: no EBITDA or D&A concept exists.  Returned missing.
  Unlocking both needs D&A added to the SEC collector
  (tag ``DepreciationDepletionAndAmortization``) plus re-collection
  of the statements — a mapping change alone would give 0% coverage
  on the data already stored, so it is not done here.
* Historical P/E: no historical price series is stored in the
  database (the only snapshot is a single 2026-11-03 row), so no
  5y/10y median P/E can be computed from stored data.  The method
  is implemented for a caller-supplied price history and returns
  missing when none is supplied.

This module never touches the production database.  It reads rows
through a connection the caller supplies (used read-only through
``financial_ratios.load_standardised_values``) and never writes.
"""

import sqlite3
import statistics

from agents import financial_ratios as fr
from tools.valuation import calculate_dcf

# ---------------------------------------------------------------------------
# Default assumptions (judgement, stated on every output)
# ---------------------------------------------------------------------------

FORECAST_YEARS = 10
DEFAULT_WACC = 0.11
DEFAULT_TERMINAL_GROWTH = 0.025
WACC_SENSITIVITY = (0.10, 0.11, 0.12)

# Scenario growth rates, anchored to Step 19 CAGRs (see module docstring).
SCENARIO_GROWTH = {
    "bear": 0.03,
    "base": 0.10,
    "optimistic": 0.15,
}

OWNER_EARNINGS_MISSING = (
    "owner earnings unavailable: no depreciation/amortisation concept "
    "in the Step 16 concept set and no such key exists in the US raw "
    "data; capex is not split into maintenance vs growth capex; no "
    "other non-cash-charge concept exists. FCF is the closest proxy "
    "and is labelled as such, not owner earnings."
)

EV_EBITDA_MISSING = (
    "EV/EBITDA unavailable: no EBITDA or depreciation/amortisation "
    "concept in the Step 16 concept set and no such key exists in the "
    "US raw data"
)

HISTORICAL_PE_MISSING = (
    "historical P/E unavailable: no historical price series is stored "
    "in the database (only a single 2026-11-03 snapshot row exists), "
    "so no 5y/10y median P/E can be computed from stored data"
)


# ---------------------------------------------------------------------------
# Result helpers (same pattern as Steps 17-20)
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
# DCF — canonical wrapper around tools/valuation.py::calculate_dcf
# ---------------------------------------------------------------------------

def dcf_value(starting_fcf, growth_rate, discount_rate=DEFAULT_WACC,
              terminal_growth_rate=DEFAULT_TERMINAL_GROWTH,
              net_debt=0, shares_outstanding=None, years=FORECAST_YEARS,
              period=None, period_type="ttm"):
    """One DCF valuation via the canonical ``calculate_dcf`` tool.

    ``starting_fcf``, ``net_debt`` and ``shares_outstanding`` are in
    consistent units (dollars and shares).  Returns a result dict
    with per-share value plus the tool's full detail.  Errors from
    the tool (e.g. non-positive FCF, discount <= terminal growth)
    are returned as missing with the tool's reason, never raised.
    """
    inputs = {
        "starting_fcf": starting_fcf,
        "growth_rate": growth_rate,
        "discount_rate": discount_rate,
        "terminal_growth_rate": terminal_growth_rate,
        "net_debt": net_debt,
        "shares_outstanding": shares_outstanding,
        "forecast_years": years,
    }
    formula = (
        "10y DCF via tools/valuation.py calculate_dcf: FCF grows at "
        "growth_rate, discounted at discount_rate, terminal value by "
        "Gordon growth at terminal_growth_rate; equity value = EV - "
        "net_debt; per share = equity value / shares_outstanding"
    )
    if starting_fcf is None:
        return _missing("dcf", "missing input(s): starting_fcf",
                        period, period_type, inputs, formula=formula)
    if shares_outstanding is None:
        return _missing("dcf", "missing input(s): shares_outstanding",
                        period, period_type, inputs, formula=formula)
    outcome = calculate_dcf(
        free_cash_flow=starting_fcf,
        growth_rate=growth_rate,
        discount_rate=discount_rate,
        terminal_growth_rate=terminal_growth_rate,
        years=years,
        net_debt=net_debt,
        shares_outstanding=shares_outstanding,
    )
    if "error" in outcome:
        return _missing("dcf", f"calculate_dcf error: {outcome['error']}",
                        period, period_type, inputs, formula=formula)
    return _result(
        "dcf", outcome["intrinsic_value_per_share"], formula, inputs,
        period, period_type,
        extra={
            "growth_rate": growth_rate,
            "discount_rate": discount_rate,
            "terminal_growth_rate": terminal_growth_rate,
            "enterprise_value": outcome["enterprise_value"],
            "equity_value": outcome["equity_value"],
            "present_value_of_cash_flows":
                outcome["present_value_of_cash_flows"],
            "present_terminal_value": outcome["present_terminal_value"],
            "terminal_value": outcome["terminal_value"],
            "projected_cash_flows": outcome["projected_cash_flows"],
        })


def dcf_scenarios(starting_fcf, net_debt=0, shares_outstanding=None,
                  discount_rate=DEFAULT_WACC,
                  terminal_growth_rate=DEFAULT_TERMINAL_GROWTH,
                  years=FORECAST_YEARS, period=None, period_type="ttm"):
    """Bear / base / optimistic DCF values at the scenario growth rates.

    Returns {scenario: result dict}.  Scenario order is guaranteed
    bear < base < optimistic when all three are present (same WACC,
    same starting FCF, growth is the only difference).
    """
    return {
        name: dcf_value(
            starting_fcf, growth, discount_rate, terminal_growth_rate,
            net_debt, shares_outstanding, years, period, period_type)
        for name, growth in SCENARIO_GROWTH.items()
    }


def wacc_sensitivity(starting_fcf, growth_rate, net_debt=0,
                     shares_outstanding=None,
                     terminal_growth_rate=DEFAULT_TERMINAL_GROWTH,
                     years=FORECAST_YEARS, period=None, period_type="ttm"):
    """Per-share DCF value at each WACC in WACC_SENSITIVITY, one growth rate."""
    return {
        wacc: dcf_value(
            starting_fcf, growth_rate, wacc, terminal_growth_rate,
            net_debt, shares_outstanding, years, period, period_type)
        for wacc in WACC_SENSITIVITY
    }


def implied_growth(price, starting_fcf, discount_rate=DEFAULT_WACC,
                   terminal_growth_rate=DEFAULT_TERMINAL_GROWTH,
                   net_debt=0, shares_outstanding=None,
                   years=FORECAST_YEARS):
    """The constant FCF growth rate the market price already assumes.

    Inverts the DCF by bisection on growth in [-0.90, 2.00].  Returns
    the growth rate (decimal) or None when the price is outside the
    range the DCF can produce at those bounds.  The price is the
    caller's responsibility to label (as-of date and source).
    """
    if price is None or starting_fcf is None or shares_outstanding is None:
        return None
    if starting_fcf <= 0 or shares_outstanding <= 0 or price <= 0:
        return None

    def value_at(growth):
        outcome = calculate_dcf(
            free_cash_flow=starting_fcf, growth_rate=growth,
            discount_rate=discount_rate,
            terminal_growth_rate=terminal_growth_rate, years=years,
            net_debt=net_debt, shares_outstanding=shares_outstanding)
        if "error" in outcome:
            return None
        return outcome["intrinsic_value_per_share"]

    low, high = -0.90, 2.00
    value_low, value_high = value_at(low), value_at(high)
    if value_low is None or value_high is None:
        return None
    if price < value_low or price > value_high:
        return None
    for _ in range(100):
        mid = (low + high) / 2
        value_mid = value_at(mid)
        if value_mid is None:
            return None
        if abs(value_mid - price) < 1e-9:
            return mid
        if value_mid < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


# ---------------------------------------------------------------------------
# Other methods — implemented where data allows, missing otherwise
# ---------------------------------------------------------------------------

def owner_earnings_valuation(period=None, period_type="ttm", inputs=None):
    """Owner earnings: unavailable.  See OWNER_EARNINGS_MISSING.

    Kept as a function (not a bare constant) so the valuation summary
    and tests exercise the same missing-with-reason path as every
    other method, and so a future Step 16 D&A extension has one place
    to land.
    """
    return _missing(
        "owner_earnings", OWNER_EARNINGS_MISSING, period, period_type,
        inputs or {},
        formula="net income + depreciation/amortisation + other "
                "non-cash charges - average maintenance capex")


def ev_ebitda_valuation(period=None, period_type="ttm", inputs=None):
    """EV/EBITDA: unavailable.  See EV_EBITDA_MISSING."""
    return _missing(
        "ev_ebitda", EV_EBITDA_MISSING, period, period_type,
        inputs or {}, formula="EV / EBITDA")


def historical_pe_valuation(eps_by_period, price_by_period,
                            period=None, period_type="ttm"):
    """Historical P/E value from a caller-supplied price history.

    ``eps_by_period`` maps reporting period -> diluted EPS;
    ``price_by_period`` maps reporting period -> price (same periods).
    The median historical P/E (price / EPS per period) is applied to
    the latest EPS.  With no price history — the stored-data case —
    the method is missing with HISTORICAL_PE_MISSING.  Periods with
    missing or non-positive EPS are skipped, never treated as zero.
    """
    inputs = {
        "eps_periods": len(eps_by_period or {}),
        "price_periods": len(price_by_period or {}),
    }
    formula = ("median historical P/E (price / diluted EPS per period) "
               "x latest diluted EPS")
    if not price_by_period:
        return _missing("historical_pe", HISTORICAL_PE_MISSING,
                        period, period_type, inputs, formula=formula)
    pes = []
    for p, price in price_by_period.items():
        eps = (eps_by_period or {}).get(p)
        if price is None or eps is None or eps <= 0 or price <= 0:
            continue
        pes.append(price / eps)
    if not pes:
        return _missing(
            "historical_pe",
            "no usable historical P/E periods (missing or non-positive "
            "EPS/price in every supplied period)",
            period, period_type, inputs, formula=formula)
    median_pe = statistics.median(pes)
    latest_period = max(eps_by_period)
    latest_eps = eps_by_period[latest_period]
    if latest_eps is None or latest_eps <= 0:
        return _missing("historical_pe",
                        "latest diluted EPS missing or non-positive",
                        period, period_type, inputs, formula=formula)
    return _result(
        "historical_pe", median_pe * latest_eps, formula,
        {**inputs, "median_pe": median_pe, "latest_eps": latest_eps,
         "pe_periods_used": len(pes)},
        period, period_type,
        extra={"median_pe": median_pe, "latest_eps": latest_eps,
               "latest_period": latest_period})


# ---------------------------------------------------------------------------
# Loading inputs from the database
# ---------------------------------------------------------------------------

def _load_profile_assumptions(conn, ticker):
    """Saved valuation-profile assumptions, or None.

    Reads the ``valuation_profiles`` table through the caller's
    connection (never the module's own production connection).  The
    table is currently empty, so this normally returns None and the
    defaults apply.
    """
    try:
        row = conn.execute(
            "SELECT growth_rate, discount_rate, terminal_growth_rate, "
            "forecast_years FROM valuation_profiles WHERE ticker = ?",
            (ticker.upper(),)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "growth_rate": row[0],
        "discount_rate": row[1],
        "terminal_growth_rate": row[2],
        "forecast_years": row[3],
    }


def valuation_inputs(conn, ticker):
    """Load every Step 21 input for ``ticker`` from standardised data.

    Returns (inputs, warnings).  ``inputs`` holds TTM and annual FCF,
    net debt and shares from the latest balance sheet, the latest
    periods, and the snapshot price (labelled with its as-of date).
    The connection is used read-only; nothing is written.
    """
    values, load_warnings = fr.load_standardised_values(conn, ticker)
    warnings = list(load_warnings)

    annual_periods = fr._periods(values, "income_statement", "annual")
    latest_annual = annual_periods[-1] if annual_periods else None
    latest_quarter = fr._latest_period(values, "income_statement",
                                       "quarterly")

    def _num(stmt, period, period_type, concept):
        row = values.get((stmt, period, period_type))
        if row is None:
            return None
        value_dict = row.get(concept)
        if value_dict is None or not value_dict.get("present"):
            return None
        return value_dict.get("value")

    # Annual FCF (latest fiscal year) and TTM FCF (most recent 12m).
    annual_fcf = _num("cash_flow_statement", latest_annual, "annual",
                      "free_cash_flow") if latest_annual else None
    ttm_fcf = None
    ttm_method = None
    if latest_quarter:
        ttm, error = fr.build_ttm_values(values, latest_quarter)
        if ttm is not None:
            ttm_fcf = ttm["flow"].get("free_cash_flow")
            ttm_method = ttm["method"]
        else:
            warnings.append(f"TTM could not be built: {error}")

    # Latest balance sheet (quarterly if present, else latest annual).
    balance_periods = fr._periods(values, "balance_sheet", "quarterly")
    if balance_periods:
        balance_period, balance_type = balance_periods[-1], "quarterly"
    elif fr._periods(values, "balance_sheet", "annual"):
        balance_period = fr._periods(values, "balance_sheet",
                                     "annual")[-1]
        balance_type = "annual"
    else:
        balance_period, balance_type = None, None
    net_debt = None
    if balance_period:
        debt = _num("balance_sheet", balance_period, balance_type,
                    "total_debt")
        cash = _num("balance_sheet", balance_period, balance_type,
                    "cash_and_equivalents")
        if debt is not None and cash is not None:
            net_debt = debt - cash

    # Shares: latest quarterly diluted count, else latest annual.
    shares = None
    if latest_quarter:
        shares = _num("income_statement", latest_quarter, "quarterly",
                      "shares_outstanding_diluted")
    if shares is None and latest_annual:
        shares = _num("income_statement", latest_annual, "annual",
                      "shares_outstanding_diluted")

    # EPS history (annual) for the historical-P/E method.
    eps_by_period = {}
    for p in annual_periods:
        eps = _num("income_statement", p, "annual", "eps_diluted")
        if eps is not None:
            eps_by_period[p] = eps

    price_input = fr.load_snapshot_price(conn, ticker)
    profile = _load_profile_assumptions(conn, ticker)

    inputs = {
        "ticker": ticker,
        "latest_annual_period": latest_annual,
        "latest_quarter_period": latest_quarter,
        "ttm_free_cash_flow": ttm_fcf,
        "ttm_method": ttm_method,
        "annual_free_cash_flow": annual_fcf,
        "net_debt": net_debt,
        "net_debt_period": balance_period,
        "net_debt_period_type": balance_type,
        "shares_outstanding_diluted": shares,
        "eps_by_period": eps_by_period,
        "price_input": price_input,
        "valuation_profile": profile,
    }
    return inputs, warnings


# ---------------------------------------------------------------------------
# Combined valuation summary
# ---------------------------------------------------------------------------

def valuation_summary(conn, ticker, price_input=None, assumptions=None,
                      starting_fcf_basis="ttm"):
    """Full Step 21 valuation summary for one ticker.

    ``starting_fcf_basis`` is "ttm" (default) or "annual".  ``assumptions``
    overrides the defaults (growth/discount/terminal/years); a saved
    valuation profile overrides them first when one exists.  Returns a
    dict with DCF scenarios, WACC sensitivity, implied growth at the
    supplied price, the unavailable methods with reasons, and a
    conservative/base/optimistic fair-value range.  ``margin_of_safety``
    is a number only — Step 23 interprets it; no buy/sell call is made
    here, ever.
    """
    inputs, warnings = valuation_inputs(conn, ticker)
    if price_input is None:
        price_input = inputs["price_input"]
    price = price_input.get("price") if price_input else None
    price_as_of = price_input.get("as_of_date") if price_input else None

    starting_fcf = (inputs["ttm_free_cash_flow"]
                    if starting_fcf_basis == "ttm"
                    else inputs["annual_free_cash_flow"])
    period = (inputs["latest_quarter_period"]
              if starting_fcf_basis == "ttm"
              else inputs["latest_annual_period"])
    period_type = starting_fcf_basis

    # Assumptions: explicit caller overrides > saved profile > defaults.
    profile = inputs["valuation_profile"]
    base_growth = SCENARIO_GROWTH["base"]
    discount = DEFAULT_WACC
    terminal = DEFAULT_TERMINAL_GROWTH
    years = FORECAST_YEARS
    assumption_source = "defaults (Step 21 scenarios)"
    if profile is not None:
        base_growth = profile["growth_rate"]
        discount = profile["discount_rate"]
        terminal = profile["terminal_growth_rate"]
        years = profile["forecast_years"]
        assumption_source = "saved valuation_profiles row"
    if assumptions:
        base_growth = assumptions.get("growth_rate", base_growth)
        discount = assumptions.get("discount_rate", discount)
        terminal = assumptions.get("terminal_growth_rate", terminal)
        years = assumptions.get("forecast_years", years)
        assumption_source = "caller-supplied assumptions"

    scenarios = dcf_scenarios(
        starting_fcf, inputs["net_debt"],
        inputs["shares_outstanding_diluted"], discount, terminal, years,
        period, period_type)
    # The base scenario uses the (possibly profile) base growth rate;
    # the bear/optimistic scenarios keep the documented anchors.
    if base_growth != SCENARIO_GROWTH["base"]:
        scenarios["base"] = dcf_value(
            starting_fcf, base_growth, discount, terminal,
            inputs["net_debt"], inputs["shares_outstanding_diluted"],
            years, period, period_type)

    sensitivity = wacc_sensitivity(
        starting_fcf, SCENARIO_GROWTH["base"], inputs["net_debt"],
        inputs["shares_outstanding_diluted"], terminal, years,
        period, period_type)
    annual_scenarios = None
    if starting_fcf_basis == "ttm":
        annual_scenarios = dcf_scenarios(
            inputs["annual_free_cash_flow"], inputs["net_debt"],
            inputs["shares_outstanding_diluted"], discount, terminal,
            years, inputs["latest_annual_period"], "annual")

    implied = implied_growth(
        price, starting_fcf, discount, terminal, inputs["net_debt"],
        inputs["shares_outstanding_diluted"], years) \
        if price is not None else None

    bear = scenarios["bear"]["value"]
    base = scenarios["base"]["value"]
    optimistic = scenarios["optimistic"]["value"]
    margin_of_safety = None
    if price is not None and base is not None and price > 0:
        margin_of_safety = (base - price) / price

    return {
        "ticker": ticker,
        "reporting_period": period,
        "period_type": period_type,
        "starting_fcf_basis": starting_fcf_basis,
        "starting_fcf": starting_fcf,
        "net_debt": inputs["net_debt"],
        "net_debt_period": inputs["net_debt_period"],
        "shares_outstanding_diluted":
            inputs["shares_outstanding_diluted"],
        "price": price,
        "price_as_of": price_as_of,
        "price_source": price_input.get("source") if price_input else None,
        "assumptions": {
            "scenario_growth": dict(SCENARIO_GROWTH),
            "base_growth_rate": base_growth,
            "discount_rate_wacc": discount,
            "terminal_growth_rate": terminal,
            "forecast_years": years,
            "assumption_source": assumption_source,
            "starting_fcf_note": (
                "TTM free cash flow (Step 17 construction) unless "
                "starting_fcf_basis='annual'; FCF is the Step 16 "
                "derived concept (OCF - capex) and the closest proxy "
                "for owner earnings, labelled as such"),
        },
        "dcf_scenarios": scenarios,
        "dcf_scenarios_annual_fcf": annual_scenarios,
        "wacc_sensitivity_base_growth": sensitivity,
        "implied_growth_at_price": implied,
        "owner_earnings": owner_earnings_valuation(period, period_type),
        "ev_ebitda": ev_ebitda_valuation(period, period_type),
        "historical_pe": historical_pe_valuation(
            inputs["eps_by_period"], {}, period, period_type),
        "fair_value_range": {
            "conservative": bear,
            "base": base,
            "optimistic": optimistic,
        },
        # A number only.  Step 23 interprets margin of safety; this
        # module never turns it into a buy/sell call.
        "margin_of_safety_vs_base": margin_of_safety,
        "warnings": warnings,
    }
