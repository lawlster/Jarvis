"""Step 22 — Sector-specific valuation models.

The general 10-year FCF DCF from Step 21 is the right model for an
operating company and the wrong model for several other sectors.
A bank's "free cash flow" is meaningless — deposits are its raw
material, and debt is inventory, not leverage — so the FCF DCF must
not be applied to banks.  Insurers and REITs have the same problem
in different forms.  This module routes each ticker to the model(s)
that fit its sector, refuses the wrong model with a reason, and
returns the combined fair-value range in the same format as Step 21.

Models implemented
------------------
* Banks — dividend discount (Gordon growth on dividends per share),
  excess-return / residual-income valuation on book value (ROE vs
  cost of equity), and justified P/B ((ROE - g) / (k - g) x BVPS),
  plus a historical-P/B relative check for a caller-supplied price
  history.  All three run on Step 16 concepts that already exist
  (net income, total equity, dividends paid, shares, EPS).
* Insurers — book-value valuation via justified P/B (an insurer is
  valued on its book and its return on it, not on FCF).  Embedded
  value, and the underwriting vs investment income split, are
  reported unavailable: no premiums, underwriting income, investment
  income or embedded-value concept exists in the Step 16 set.
* REITs — distribution DDM (Gordon growth on distributions per
  share, from dividends paid / shares).  FFO / AFFO and NAV are
  reported unavailable: FFO needs D&A (not a Step 16 concept, no
  such key in the US raw data) and NAV needs property values.
* Technology / general operating companies — the Step 21 FCF DCF,
  delegated to ``agents.intrinsic_valuation.valuation_summary``,
  with a note confirming why it is the right model here.  EV/EBITDA
  stays unavailable (no EBITDA / D&A).

Sector resolution
-----------------
``market_universe`` carries a sector for Singapore rows only; every
US and HK row has sector NULL, and the SG "Financials" sector mixes
banks, insurers, exchanges and other financials, so a stored sector
of "Financials" is treated as ambiguous, not as "bank".  Resolution
order: caller-supplied override > market_universe sector/industry >
security_type (``reit`` only) > unknown.  An ambiguous or unknown
sector refuses the sector-specific models and says why; it never
guesses.  The one exception is the general FCF DCF, which is the
default for an operating company whose sector is unknown, labelled
with a warning that the sector was not established.

Design rules (same as Steps 16-21):

* Never invent a missing figure.  A method whose input is missing
  is returned with ``value=None``, ``present=False`` and a ``reason``.
* Every estimate records its assumptions, inputs, reporting period
  and the as-of date of any price used.  No buy/sell call is ever
  made; the margin-of-safety field is a number only (Step 23
  interprets it).
* No network requests; this module reads through a connection the
  caller supplies (used read-only) and never writes.  It never
  touches the production database.
* Where a model cannot run on real data (expected for banks,
  insurers and REITs — no statement line items exist for them in
  the database), it is implemented with fixture data and tests and
  reported as implemented-but-unvalidated-on-real-data, never as
  validated.
"""

import statistics

from agents import financial_ratios as fr
from agents import intrinsic_valuation as iv

# ---------------------------------------------------------------------------
# Default assumptions (judgement, stated on every output)
# ---------------------------------------------------------------------------

DEFAULT_COST_OF_EQUITY = 0.10
DEFAULT_TERMINAL_GROWTH = 0.025
FORECAST_YEARS = 10
MAX_DDM_GROWTH = 0.05
DPS_CAGR_YEARS = 5

# ---------------------------------------------------------------------------
# Sector taxonomy
# ---------------------------------------------------------------------------
# Canonical sectors.  "financial" is deliberately NOT a valuation
# sector: the stored "Financials" sector mixes banks, insurers and
# other financials, so it routes to nothing until the caller says
# bank / insurance.  "unknown" likewise routes to nothing except the
# general DCF default (with a warning) for an operating company.

SECTORS = ("bank", "insurance", "reit", "technology", "general",
           "financial", "unknown")

WHY_MODEL = {
    "bank": (
        "A bank's free cash flow is meaningless: deposits are its raw "
        "material and debt is inventory, not leverage, so the Step 21 "
        "FCF DCF is refused for banks.  A bank is valued on the cash "
        "it returns to owners (dividend discount), on the excess "
        "return its book equity earns over the cost of equity "
        "(residual income), and on its book value relative to that "
        "return (justified P/B)."),
    "insurance": (
        "An insurer is valued on its book value and the return it "
        "earns on it, not on free cash flow: premiums arrive before "
        "claims are paid, so FCF timing says nothing about value.  "
        "The primary method here is justified P/B on book value; "
        "embedded value would be better and is unavailable (no "
        "embedded-value, premium or underwriting concept exists in "
        "the Step 16 set)."),
    "reit": (
        "A REIT must distribute most of its income, so it is valued "
        "on the distributions it pays (distribution DDM) and, where "
        "data allows, on funds from operations (P/FFO) and net asset "
        "value.  FCF is refused: a REIT's capex is mostly property "
        "acquisition, not maintenance of an operating business."),
    "technology": (
        "A technology operating company is valued on the free cash "
        "flow its business generates — the Step 21 10-year FCF DCF "
        "is the right model here.  EV/EBITDA would be a useful "
        "cross-check and is unavailable (no EBITDA / D&A concept)."),
    "general": (
        "An ordinary operating company is valued on the free cash "
        "flow its business generates — the Step 21 10-year FCF DCF "
        "is the right model here."),
    "financial": (
        "The stored sector is 'Financials', which mixes banks, "
        "insurers, exchanges and other financials.  No sector "
        "model is selected until the sub-sector (bank / insurance) "
        "is established — pass sector_override='bank' or "
        "'insurance', or supply an industry that distinguishes them."),
    "unknown": (
        "No sector is stored for this ticker and none was supplied, "
        "so the sector is unknown and no sector-specific model is "
        "selected.  The general FCF DCF is shown as the default "
        "for an operating company, with a warning; if the company "
        "is in fact a bank, insurer or REIT, that default is wrong "
        "and must not be used."),
}

# Which models each canonical sector may use.  "fcf_dcf" is refused
# for bank / insurance / reit / financial / unknown; the refusal
# reasons are in REFUSAL_REASONS.
SECTOR_MODELS = {
    "bank": ("dividend_discount", "excess_return", "justified_pb",
             "historical_pb"),
    "insurance": ("justified_pb", "historical_pb", "embedded_value"),
    "reit": ("distribution_ddm", "ffo", "historical_p_ffo", "nav"),
    "technology": ("fcf_dcf",),
    "general": ("fcf_dcf",),
    "financial": (),
    "unknown": (),
}

REFUSAL_REASONS = {
    ("fcf_dcf", "bank"): (
        "FCF DCF refused for a bank: free cash flow is meaningless "
        "for a bank — deposits are its raw material and debt is "
        "inventory, not leverage.  Use dividend discount, "
        "excess-return or justified P/B instead."),
    ("fcf_dcf", "insurance"): (
        "FCF DCF refused for an insurer: premiums arrive before "
        "claims are paid, so free-cash-flow timing says nothing "
        "about value.  Use justified P/B (embedded value when the "
        "data exists) instead."),
    ("fcf_dcf", "reit"): (
        "FCF DCF refused for a REIT: a REIT's capex is mostly "
        "property acquisition, not maintenance of an operating "
        "business.  Use distribution DDM, P/FFO or NAV instead."),
    ("fcf_dcf", "financial"): (
        "FCF DCF refused: the sector is 'Financials', which mixes "
        "banks, insurers and other financials — the sub-sector is "
        "not established, so no model is selected."),
    ("fcf_dcf", "unknown"): (
        "FCF DCF is shown only as the default for an operating "
        "company; the sector is unknown, so if this company is a "
        "bank, insurer or REIT the default is wrong — establish "
        "the sector before relying on it."),
}

FFO_MISSING = (
    "FFO/AFFO unavailable: FFO is net income + depreciation / "
    "amortisation - gains on property sales, and no depreciation / "
    "amortisation concept exists in the Step 16 concept set (no "
    "such key in the US raw data); no gains-on-sales concept "
    "exists either.  AFFO additionally needs maintenance capex, "
    "which is not split out."
)

NAV_MISSING = (
    "NAV unavailable: net asset value needs property-level values "
    "(investment properties at fair value, less debt) and no "
    "property concept exists in the Step 16 concept set."
)

EMBEDDED_VALUE_MISSING = (
    "embedded value unavailable: no embedded-value, premium, "
    "underwriting-income or investment-income concept exists in "
    "the Step 16 concept set, so neither the value of in-force "
    "business nor the underwriting / investment split can be "
    "computed."
)

HISTORICAL_PB_MISSING = (
    "historical P/B unavailable: no historical price series is "
    "stored in the database (only a single 2026-11-03 snapshot "
    "row exists), so no median historical P/B can be computed "
    "from stored data"
)

HISTORICAL_P_FFO_MISSING = (
    "historical P/FFO unavailable: FFO itself is unavailable "
    "(see FFO reason) and no historical price series is stored"
)


# ---------------------------------------------------------------------------
# Result helpers (same pattern as Steps 17-21)
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


def _cagr(start_value, end_value, years):
    """Compound annual growth rate, or None when undefined."""
    if start_value is None or end_value is None or years <= 0:
        return None
    if start_value <= 0 or end_value < 0:
        return None
    if end_value == 0:
        return -1.0
    return (end_value / start_value) ** (1.0 / years) - 1.0


# ---------------------------------------------------------------------------
# Sector resolution
# ---------------------------------------------------------------------------

def normalise_sector(sector, industry=None, security_type=None):
    """Map stored sector / industry / security_type to a canonical sector.

    ``reit`` security_type wins (it is issue-level evidence).  A
    stored "Financials" sector with no distinguishing industry maps
    to "financial" (ambiguous), never to "bank".  Anything
    unrecognised but non-empty maps to "general"; empty maps to
    "unknown".
    """
    if (security_type or "").strip().lower() == "reit":
        return "reit"
    text = " ".join(
        part for part in (sector, industry) if part).lower()
    if "bank" in text:
        return "bank"
    if "insurance" in text or "reinsurance" in text:
        return "insurance"
    if "reit" in text or "real estate investment trust" in text:
        return "reit"
    if "financial" in text:
        return "financial"
    if any(word in text for word in (
            "information technology", "technology", "software",
            "semiconductor", "hardware", "informationtechnology")):
        return "technology"
    if text.strip():
        return "general"
    return "unknown"


def resolve_sector(conn, ticker, sector_override=None):
    """Resolve a ticker's canonical sector from market_universe.

    Returns a dict with the canonical sector, the raw stored values,
    where the answer came from, and warnings.  The connection is
    used read-only; nothing is written.
    """
    raw_sector = raw_industry = security_type = None
    try:
        row = conn.execute(
            "SELECT sector, industry, security_type FROM "
            "market_universe WHERE ticker = ? LIMIT 1",
            (ticker,)).fetchone()
    except Exception:
        row = None
    if row is not None:
        raw_sector, raw_industry, security_type = row
    if sector_override:
        canonical = normalise_sector(sector_override)
        source = "caller-supplied sector_override"
        warnings = []
    elif raw_sector is None and raw_industry is None \
            and security_type is None:
        canonical = "unknown"
        source = "no market_universe row for ticker"
        warnings = ["ticker not found in market_universe"]
    else:
        canonical = normalise_sector(raw_sector, raw_industry,
                                     security_type)
        source = "market_universe sector/industry/security_type"
        warnings = []
    if canonical in ("financial", "unknown"):
        warnings.append(WHY_MODEL[canonical])
    return {
        "ticker": ticker,
        "sector": canonical,
        "raw_sector": raw_sector,
        "raw_industry": raw_industry,
        "security_type": security_type,
        "source": source,
        "why_this_model": WHY_MODEL[canonical],
        "warnings": warnings,
    }


def models_for_sector(sector):
    """(selected models, refused models with reasons) for a sector."""
    selected = list(SECTOR_MODELS.get(sector, ()))
    refused = {}
    for model in ("fcf_dcf", "dividend_discount", "excess_return",
                  "justified_pb", "historical_pb", "distribution_ddm",
                  "ffo", "historical_p_ffo", "nav", "embedded_value"):
        if model not in selected:
            reason = REFUSAL_REASONS.get((model, sector))
            if reason is None:
                reason = (f"{model} is not an applicable model for "
                          f"sector '{sector}'")
            refused[model] = reason
    return selected, refused


def validate_model(model, sector):
    """(allowed, reason): is ``model`` applicable to ``sector``?"""
    selected, refused = models_for_sector(sector)
    if model in selected:
        return True, None
    return False, refused.get(model, f"{model} not applicable to "
                                     f"sector '{sector}'")


# ---------------------------------------------------------------------------
# Bank models — dividend discount, excess return, justified P/B
# ---------------------------------------------------------------------------

def dividend_discount_value(dps, growth_rate, cost_of_equity,
                            period=None, period_type="annual"):
    """Gordon growth DDM: value = DPS x (1 + g) / (k - g).

    ``dps`` is the latest dividend per share; ``growth_rate`` the
    assumed constant dividend growth; ``cost_of_equity`` the discount
    rate.  Requires k > g and DPS > 0; otherwise missing with the
    reason, never an exception.
    """
    inputs = {"dps": dps, "growth_rate": growth_rate,
              "cost_of_equity": cost_of_equity}
    formula = "Gordon growth DDM: DPS x (1 + g) / (k - g)"
    if dps is None or dps <= 0:
        return _missing("dividend_discount",
                        "missing input(s): latest dividend per share "
                        "(no dividends paid in the latest period)",
                        period, period_type, inputs, formula=formula)
    if cost_of_equity <= growth_rate:
        return _missing(
            "dividend_discount",
            f"cost of equity ({cost_of_equity:.2%}) must exceed "
            f"dividend growth ({growth_rate:.2%}) for Gordon growth",
            period, period_type, inputs, formula=formula)
    value = dps * (1 + growth_rate) / (cost_of_equity - growth_rate)
    return _result("dividend_discount", value, formula, inputs,
                   period, period_type,
                   extra={"growth_rate": growth_rate,
                          "cost_of_equity": cost_of_equity})


def dps_history(values):
    """Annual dividends per share from standardised values.

    DPS = dividends_paid / shares_outstanding_diluted per annual
    period.  Returns {period: dps}.  Periods with missing inputs
    are skipped, never treated as zero.
    """
    out = {}
    for p in fr._periods(values, "cash_flow_statement", "annual"):
        div = fr._num(fr._get(values, "cash_flow_statement", p,
                              "annual", "dividends_paid"))
        shares = fr._num(fr._get(values, "income_statement", p,
                                 "annual", "shares_outstanding_diluted"))
        if div is None or shares is None or shares <= 0:
            continue
        out[p] = div / shares
    return out


def bank_dividend_discount(values, cost_of_equity=DEFAULT_COST_OF_EQUITY,
                           max_growth=MAX_DDM_GROWTH,
                           cagr_years=DPS_CAGR_YEARS):
    """DDM from a dividend history: growth is the DPS CAGR over the
    last ``cagr_years`` years, capped at ``max_growth`` and floored
    at 0.  Returns the DDM result dict with the history attached.
    """
    history = dps_history(values)
    periods = sorted(history)
    inputs = {"dps_periods": len(periods),
              "cost_of_equity": cost_of_equity}
    formula = "Gordon growth DDM: DPS x (1 + g) / (k - g)"
    if not periods:
        return _missing("dividend_discount",
                        "no dividend history: dividends_paid or shares "
                        "missing in every annual period",
                        None, "annual", inputs, formula=formula)
    latest = periods[-1]
    dps = history[latest]
    growth = None
    if len(periods) > 1:
        start = periods[-1 - cagr_years] if len(periods) > cagr_years \
            else periods[0]
        years = len(periods) - 1 - periods.index(start)
        growth = _cagr(history[start], dps, years)
    if growth is None:
        growth = 0.0
    growth = max(0.0, min(growth, max_growth))
    result = dividend_discount_value(dps, growth, cost_of_equity,
                                     latest, "annual")
    result["inputs"].update({
        "dps_history": history,
        "dps_cagr_years": cagr_years,
        "growth_capped_at": max_growth,
    })
    result["dps_history"] = history
    return result


def excess_return_value(book_value_per_share, roe, cost_of_equity,
                        book_growth_rate, years=FORECAST_YEARS,
                        terminal_growth_rate=None, period=None,
                        period_type="annual"):
    """Residual-income / excess-return value per share.

    V = BVPS + sum over ``years`` of residual income
    RI_t = (ROE - k) x BV_{t-1}, discounted at k, plus a Gordon
    terminal value on the final year's residual income.  Book value
    grows at ``book_growth_rate`` each year (retained earnings).
    ``terminal_growth_rate`` defaults to ``book_growth_rate``.
    """
    if terminal_growth_rate is None:
        terminal_growth_rate = book_growth_rate
    inputs = {"book_value_per_share": book_value_per_share,
              "roe": roe, "cost_of_equity": cost_of_equity,
              "book_growth_rate": book_growth_rate,
              "forecast_years": years,
              "terminal_growth_rate": terminal_growth_rate}
    formula = ("residual income: BVPS + PV of (ROE - k) x BV_{t-1} "
               "over forecast years + Gordon terminal value on "
               "final-year residual income")
    if book_value_per_share is None or book_value_per_share <= 0:
        return _missing("excess_return",
                        "missing input(s): book value per share",
                        period, period_type, inputs, formula=formula)
    if roe is None:
        return _missing("excess_return", "missing input(s): ROE",
                        period, period_type, inputs, formula=formula)
    if cost_of_equity <= terminal_growth_rate:
        return _missing(
            "excess_return",
            f"cost of equity ({cost_of_equity:.2%}) must exceed "
            f"terminal growth ({terminal_growth_rate:.2%})",
            period, period_type, inputs, formula=formula)
    bv = book_value_per_share
    pv_ri = 0.0
    residual_incomes = []
    for year in range(1, years + 1):
        ri = (roe - cost_of_equity) * bv
        residual_incomes.append(ri)
        pv_ri += ri / (1 + cost_of_equity) ** year
        bv = bv * (1 + book_growth_rate)
    terminal_ri = residual_incomes[-1] * (1 + terminal_growth_rate)
    terminal_value = terminal_ri / (cost_of_equity
                                    - terminal_growth_rate)
    pv_terminal = terminal_value / (1 + cost_of_equity) ** years
    value = book_value_per_share + pv_ri + pv_terminal
    return _result("excess_return", value, formula, inputs, period,
                   period_type,
                   extra={"present_value_of_residual_income": pv_ri,
                          "present_terminal_value": pv_terminal,
                          "residual_incomes": residual_incomes})


def bank_excess_return(values, cost_of_equity=DEFAULT_COST_OF_EQUITY,
                       years=FORECAST_YEARS,
                       terminal_growth_rate=DEFAULT_TERMINAL_GROWTH):
    """Excess-return value from standardised history.

    BVPS and ROE are from the latest annual period (ROE = net
    income / year-end total equity — labelled; Step 17's ROE uses
    average balances, a small definitional difference stated
    here).  Book growth = ROE x (1 - payout), payout =
    DPS / EPS; terminal growth is the caller's rate (default
    2.5%).  Returns the excess-return result dict.
    """
    annual = fr._periods(values, "balance_sheet", "annual")
    inputs = {"cost_of_equity": cost_of_equity}
    formula = ("residual income: BVPS + PV of (ROE - k) x BV_{t-1} "
               "+ Gordon terminal value")
    if not annual:
        return _missing("excess_return",
                        "no annual balance sheet: total equity missing",
                        None, "annual", inputs, formula=formula)
    latest = annual[-1]

    def _num(stmt, concept):
        return fr._num(fr._get(values, stmt, latest, "annual",
                               concept))

    equity = _num("balance_sheet", "total_equity")
    net_income = _num("income_statement", "net_income")
    shares = _num("income_statement", "shares_outstanding_diluted")
    eps = _num("income_statement", "eps_diluted")
    inputs.update({"total_equity": equity, "net_income": net_income,
                   "shares_outstanding_diluted": shares, "eps": eps})
    if equity is None or net_income is None or shares is None \
            or shares <= 0 or equity <= 0:
        return _missing(
            "excess_return",
            "missing input(s): total_equity, net_income or shares "
            "in the latest annual period",
            latest, "annual", inputs, formula=formula)
    bvps = equity / shares
    roe = net_income / equity
    history = dps_history(values)
    payout = None
    if latest in history and eps is not None and eps > 0:
        payout = history[latest] / eps
    if payout is None:
        retention = 1.0
    else:
        retention = max(0.0, min(1.0, 1.0 - payout))
    book_growth = roe * retention
    result = excess_return_value(
        bvps, roe, cost_of_equity, book_growth, years,
        terminal_growth_rate, latest, "annual")
    result["inputs"].update({
        "book_value_per_share": bvps, "roe": roe,
        "retention_ratio": retention, "payout_ratio": payout,
        "roe_basis": "net income / year-end total equity "
                     "(Step 17 ROE uses average balances)",
    })
    return result


def justified_pb_value(book_value_per_share, roe, cost_of_equity,
                       growth_rate, period=None, period_type="annual"):
    """Justified P/B value: P/B = (ROE - g) / (k - g); value = P/B x BVPS."""
    inputs = {"book_value_per_share": book_value_per_share,
              "roe": roe, "cost_of_equity": cost_of_equity,
              "growth_rate": growth_rate}
    formula = "justified P/B = (ROE - g) / (k - g); value = P/B x BVPS"
    if book_value_per_share is None or book_value_per_share <= 0:
        return _missing("justified_pb",
                        "missing input(s): book value per share",
                        period, period_type, inputs, formula=formula)
    if roe is None:
        return _missing("justified_pb", "missing input(s): ROE",
                        period, period_type, inputs, formula=formula)
    if cost_of_equity <= growth_rate:
        return _missing(
            "justified_pb",
            f"cost of equity ({cost_of_equity:.2%}) must exceed "
            f"growth ({growth_rate:.2%}) for justified P/B",
            period, period_type, inputs, formula=formula)
    pb = (roe - growth_rate) / (cost_of_equity - growth_rate)
    return _result("justified_pb", pb * book_value_per_share,
                   formula, inputs, period, period_type,
                   extra={"justified_pb": pb})


def bank_justified_pb(values, cost_of_equity=DEFAULT_COST_OF_EQUITY,
                      growth_rate=DEFAULT_TERMINAL_GROWTH):
    """Justified-P/B value from standardised history (latest annual)."""
    annual = fr._periods(values, "balance_sheet", "annual")
    inputs = {"cost_of_equity": cost_of_equity,
              "growth_rate": growth_rate}
    formula = "justified P/B = (ROE - g) / (k - g); value = P/B x BVPS"
    if not annual:
        return _missing("justified_pb",
                        "no annual balance sheet: total equity missing",
                        None, "annual", inputs, formula=formula)
    latest = annual[-1]

    def _num(stmt, concept):
        return fr._num(fr._get(values, stmt, latest, "annual",
                               concept))

    equity = _num("balance_sheet", "total_equity")
    net_income = _num("income_statement", "net_income")
    shares = _num("income_statement", "shares_outstanding_diluted")
    inputs.update({"total_equity": equity, "net_income": net_income,
                   "shares_outstanding_diluted": shares})
    if equity is None or net_income is None or shares is None \
            or shares <= 0 or equity <= 0:
        return _missing(
            "justified_pb",
            "missing input(s): total_equity, net_income or shares "
            "in the latest annual period",
            latest, "annual", inputs, formula=formula)
    bvps = equity / shares
    roe = net_income / equity
    result = justified_pb_value(bvps, roe, cost_of_equity,
                                growth_rate, latest, "annual")
    result["inputs"].update({
        "book_value_per_share": bvps, "roe": roe,
        "roe_basis": "net income / year-end total equity "
                     "(Step 17 ROE uses average balances)",
    })
    return result


def historical_pb_valuation(bvps_by_period, price_by_period,
                            period=None, period_type="annual"):
    """Historical P/B value from a caller-supplied price history.

    Median historical P/B (price / BVPS per period) x latest BVPS.
    With no price history — the stored-data case — missing with
    HISTORICAL_PB_MISSING.  Periods with missing or non-positive
    BVPS/price are skipped.
    """
    inputs = {"bvps_periods": len(bvps_by_period or {}),
              "price_periods": len(price_by_period or {})}
    formula = ("median historical P/B (price / BVPS per period) "
               "x latest BVPS")
    if not price_by_period:
        return _missing("historical_pb", HISTORICAL_PB_MISSING,
                        period, period_type, inputs, formula=formula)
    pbs = []
    for p, price in price_by_period.items():
        bvps = (bvps_by_period or {}).get(p)
        if price is None or bvps is None or bvps <= 0 or price <= 0:
            continue
        pbs.append(price / bvps)
    if not pbs:
        return _missing(
            "historical_pb",
            "no usable historical P/B periods (missing or "
            "non-positive BVPS/price in every supplied period)",
            period, period_type, inputs, formula=formula)
    median_pb = statistics.median(pbs)
    latest_period = max(bvps_by_period)
    latest_bvps = bvps_by_period[latest_period]
    if latest_bvps is None or latest_bvps <= 0:
        return _missing("historical_pb",
                        "latest BVPS missing or non-positive",
                        period, period_type, inputs, formula=formula)
    return _result(
        "historical_pb", median_pb * latest_bvps, formula,
        {**inputs, "median_pb": median_pb, "latest_bvps": latest_bvps,
         "pb_periods_used": len(pbs)},
        period, period_type,
        extra={"median_pb": median_pb, "latest_bvps": latest_bvps,
               "latest_period": latest_period})


def bvps_history(values):
    """Annual book value per share from standardised values."""
    out = {}
    for p in fr._periods(values, "balance_sheet", "annual"):
        equity = fr._num(fr._get(values, "balance_sheet", p,
                                 "annual", "total_equity"))
        shares = fr._num(fr._get(values, "income_statement", p,
                                 "annual", "shares_outstanding_diluted"))
        if equity is None or shares is None or shares <= 0 \
                or equity <= 0:
            continue
        out[p] = equity / shares
    return out


# ---------------------------------------------------------------------------
# Insurer models
# ---------------------------------------------------------------------------

def embedded_value_valuation(period=None, period_type="annual",
                             inputs=None):
    """Embedded value: unavailable.  See EMBEDDED_VALUE_MISSING."""
    return _missing("embedded_value", EMBEDDED_VALUE_MISSING,
                    period, period_type, inputs or {},
                    formula="adjusted net asset value + value of "
                            "in-force business")


def insurer_valuation(values, cost_of_equity=DEFAULT_COST_OF_EQUITY,
                      growth_rate=DEFAULT_TERMINAL_GROWTH):
    """Insurer valuation: justified P/B (primary) + unavailable methods.

    Returns {"justified_pb": ..., "historical_pb": ... (missing
    without a price history), "embedded_value": ... (missing),
    "underwriting_vs_investment": missing with reason}.
    """
    return {
        "justified_pb": bank_justified_pb(values, cost_of_equity,
                                          growth_rate),
        "historical_pb": historical_pb_valuation(
            bvps_history(values), {}),
        "embedded_value": embedded_value_valuation(),
        "underwriting_vs_investment": _missing(
            "underwriting_vs_investment", EMBEDDED_VALUE_MISSING,
            None, "annual", {},
            formula="underwriting income vs investment income, "
                    "separately reported"),
    }


# ---------------------------------------------------------------------------
# REIT models
# ---------------------------------------------------------------------------

def ffo_valuation(period=None, period_type="annual", inputs=None):
    """FFO/AFFO: unavailable.  See FFO_MISSING."""
    return _missing("ffo", FFO_MISSING, period, period_type,
                    inputs or {},
                    formula="FFO = net income + depreciation / "
                            "amortisation - gains on property sales; "
                            "AFFO = FFO - maintenance capex")


def nav_valuation(period=None, period_type="annual", inputs=None):
    """NAV: unavailable.  See NAV_MISSING."""
    return _missing("nav", NAV_MISSING, period, period_type,
                    inputs or {},
                    formula="NAV = investment properties at fair "
                            "value + other assets - debt, per share")


def reit_valuation(values, cost_of_equity=DEFAULT_COST_OF_EQUITY,
                   max_growth=MAX_DDM_GROWTH,
                   cagr_years=DPS_CAGR_YEARS):
    """REIT valuation: distribution DDM (runnable) + unavailable methods.

    Distributions per share come from dividends_paid / shares —
    for a REIT the cash dividend is the distribution.  FFO, NAV and
    historical P/FFO are missing with reasons.
    """
    ddm = bank_dividend_discount(values, cost_of_equity, max_growth,
                                 cagr_years)
    ddm = dict(ddm)
    ddm["method"] = "distribution_ddm"
    ddm["formula"] = ("Gordon growth DDM on distributions: "
                      "distribution per share x (1 + g) / (k - g); "
                      "distributions = dividends_paid / shares")
    return {
        "distribution_ddm": ddm,
        "ffo": ffo_valuation(),
        "historical_p_ffo": _missing(
            "historical_p_ffo", HISTORICAL_P_FFO_MISSING,
            None, "annual", {},
            formula="median historical P/FFO x latest FFO per share"),
        "nav": nav_valuation(),
    }


# ---------------------------------------------------------------------------
# Technology / general — Step 21 FCF DCF, confirmed as the right model
# ---------------------------------------------------------------------------

def technology_valuation(conn, ticker, price_input=None,
                         assumptions=None, starting_fcf_basis="ttm"):
    """Step 21 valuation for a technology / general operating company.

    Delegates to ``intrinsic_valuation.valuation_summary`` — no
    second DCF is built — and adds the sector confirmation: the FCF
    DCF is the right model here because the company's value is the
    free cash flow its operating business generates.  EV/EBITDA
    stays unavailable (no EBITDA / D&A concept).
    """
    summary = iv.valuation_summary(conn, ticker, price_input,
                                   assumptions, starting_fcf_basis)
    summary["sector"] = "technology"
    summary["why_this_model"] = WHY_MODEL["technology"]
    summary["ev_ebitda_note"] = (
        "EV/EBITDA unavailable as a cross-check: no EBITDA or "
        "depreciation/amortisation concept in the Step 16 set and "
        "no such key in the US raw data (Step 21 finding).")
    return summary


# ---------------------------------------------------------------------------
# Sector router — one combined summary per ticker
# ---------------------------------------------------------------------------

def sector_valuation_summary(conn, ticker, sector_override=None,
                             price_input=None, assumptions=None,
                             cost_of_equity=DEFAULT_COST_OF_EQUITY,
                             starting_fcf_basis="ttm"):
    """Full Step 22 valuation summary for one ticker.

    Resolves the sector, selects the applicable models, refuses the
    wrong ones with reasons, runs the selected models on the
    ticker's standardised data, and returns a combined fair-value
    range in the Step 21 format (conservative / base / optimistic).
    For banks the range spans the three bank methods (base =
    excess-return, the only one valuing the whole equity); for
    insurers the base is justified P/B; for REITs the base is the
    distribution DDM; for technology / general it is the Step 21
    DCF scenarios.  ``margin_of_safety_vs_base`` is a number only —
    Step 23 interprets it; no buy/sell call is made here, ever.
    """
    sector_info = resolve_sector(conn, ticker, sector_override)
    sector = sector_info["sector"]
    selected, refused = models_for_sector(sector)
    if sector == "unknown":
        # The general FCF DCF is the labelled default for an
        # operating company whose sector is unknown; the refusal
        # entry for it would contradict the default, so it is
        # removed and the warning (attached above) carries the
        # caveat instead.
        selected = ["fcf_dcf"]
        refused.pop("fcf_dcf", None)
    warnings = list(sector_info["warnings"])

    values = {}
    load_warnings = []
    try:
        values, load_warnings = fr.load_standardised_values(conn,
                                                             ticker)
    except Exception as exc:  # noqa: BLE001 - report, never crash
        load_warnings = [f"could not load standardised values: {exc}"]
    warnings.extend(load_warnings)

    methods = {}
    price = None
    price_as_of = None
    if price_input is None:
        price_input = fr.load_snapshot_price(conn, ticker)
    if price_input:
        price = price_input.get("price")
        price_as_of = price_input.get("as_of_date")

    if sector in ("technology", "general", "unknown"):
        # Step 21 DCF is the right model; sector unknown gets the
        # same default with the warning already attached above.
        tech = technology_valuation(conn, ticker, price_input,
                                    assumptions, starting_fcf_basis)
        tech["sector"] = sector
        tech["why_this_model"] = WHY_MODEL[sector]
        methods["fcf_dcf"] = {
            "scenarios": tech["dcf_scenarios"],
            "fair_value_range": tech["fair_value_range"],
            "implied_growth_at_price":
                tech["implied_growth_at_price"],
            "owner_earnings": tech["owner_earnings"],
            "ev_ebitda": tech["ev_ebitda"],
            "historical_pe": tech["historical_pe"],
        }
        fair_value_range = tech["fair_value_range"]
        margin_of_safety = tech["margin_of_safety_vs_base"]
        period = tech["reporting_period"]
        period_type = tech["period_type"]
    elif sector == "bank":
        ddm = bank_dividend_discount(values, cost_of_equity)
        excess = bank_excess_return(values, cost_of_equity)
        justified = bank_justified_pb(values, cost_of_equity)
        hist_pb = historical_pb_valuation(bvps_history(values), {})
        methods = {
            "dividend_discount": ddm,
            "excess_return": excess,
            "justified_pb": justified,
            "historical_pb": hist_pb,
        }
        present = [m["value"] for m in methods.values()
                   if m.get("present")]
        if present:
            fair_value_range = {
                "conservative": min(present),
                "base": excess["value"] if excess.get("present")
                        else sorted(present)[len(present) // 2],
                "optimistic": max(present),
            }
        else:
            fair_value_range = {"conservative": None, "base": None,
                                "optimistic": None}
        base = fair_value_range["base"]
        margin_of_safety = ((base - price) / price
                            if price and base and price > 0 else None)
        period = (excess.get("reporting_period")
                  or ddm.get("reporting_period"))
        period_type = "annual"
    elif sector == "insurance":
        ins = insurer_valuation(values, cost_of_equity)
        methods = ins
        justified = ins["justified_pb"]
        if justified.get("present"):
            fair_value_range = {
                "conservative": justified["value"],
                "base": justified["value"],
                "optimistic": justified["value"],
            }
        else:
            fair_value_range = {"conservative": None, "base": None,
                                "optimistic": None}
        base = fair_value_range["base"]
        margin_of_safety = ((base - price) / price
                            if price and base and price > 0 else None)
        period = justified.get("reporting_period")
        period_type = "annual"
    elif sector == "reit":
        reit = reit_valuation(values, cost_of_equity)
        methods = reit
        ddm = reit["distribution_ddm"]
        if ddm.get("present"):
            fair_value_range = {
                "conservative": ddm["value"],
                "base": ddm["value"],
                "optimistic": ddm["value"],
            }
        else:
            fair_value_range = {"conservative": None, "base": None,
                                "optimistic": None}
        base = fair_value_range["base"]
        margin_of_safety = ((base - price) / price
                            if price and base and price > 0 else None)
        period = ddm.get("reporting_period")
        period_type = "annual"
    else:
        # "financial" (ambiguous) or "unknown": no model selected.
        methods = {}
        fair_value_range = {"conservative": None, "base": None,
                            "optimistic": None}
        margin_of_safety = None
        period = None
        period_type = "annual"
        warnings.append(
            "no valuation model selected: establish the sector "
            "(bank / insurance / reit / technology / general) "
            "before valuing this ticker")

    return {
        "ticker": ticker,
        "sector": sector,
        "sector_source": sector_info["source"],
        "raw_sector": sector_info["raw_sector"],
        "raw_industry": sector_info["raw_industry"],
        "security_type": sector_info["security_type"],
        "why_this_model": sector_info["why_this_model"],
        "models_selected": selected,
        "models_refused": refused,
        "methods": methods,
        "fair_value_range": fair_value_range,
        "margin_of_safety_vs_base": margin_of_safety,
        "price": price,
        "price_as_of": price_as_of,
        "price_source": price_input.get("source") if price_input
                        else None,
        "reporting_period": period,
        "period_type": period_type,
        "assumptions": {
            "cost_of_equity": cost_of_equity,
            "terminal_growth_rate": DEFAULT_TERMINAL_GROWTH,
            "forecast_years": FORECAST_YEARS,
            "max_ddm_growth": MAX_DDM_GROWTH,
            "dps_cagr_years": DPS_CAGR_YEARS,
            "assumption_source": "Step 22 defaults (judgement)",
            "dcf_assumptions": ("Step 21 defaults for technology / "
                                "general: WACC 11%, terminal 2.5%, "
                                "10y, scenarios +3% / +10% / +15%"),
        },
        "warnings": warnings,
    }
