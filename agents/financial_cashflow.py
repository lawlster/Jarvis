"""Step 20 — Cash flow and owner earnings analysis.

Computes, for one ticker and period, operating cash flow quality,
capital expenditure, free cash flow, owner earnings, stock-based
compensation treatment, working capital requirements and cash
conversion, from Step 16 standardised statements.  It reuses
``agents.financial_ratios.load_standardised_values`` (and therefore
Step 16's standardiser) and Step 16's derived ``free_cash_flow``
(operating cash flow - capital expenditure); it never re-derives a
figure by a different definition.  It reuses Step 19's growth
helpers (``financial_growth.cagr`` / ``yoy_growth``) for the capex
surge analysis.  No price is used anywhere — cash flow is a
fundamentals-only exercise — and it makes no network requests.

Design rules (from the project brief, Steps 16-20):

* Never invent a missing figure.  An output whose input is missing
  is returned with ``value=None``, ``present=False`` and a
  ``reason``.  A zero denominator is missing with the reason
  "denominator is zero", never an exception and never infinity.
* Every output records its formula, the inputs actually used, the
  reporting period and period type, and missing-input reasons.
* Annual rows are used by default.  Quarterly rows are used only
  when the caller asks for ``period_type="quarterly"`` or ``"ttm"``.
  There are no TTM rows in the database; TTM flow values are built
  the Step 17 way (``financial_ratios.build_ttm_values``: annual +
  latest quarter - same quarter a year ago, or a four-quarter sum
  when four consecutive quarterly rows exist) and the method is
  recorded on the result.
* Sign convention, as stored by the Step 16 standardiser: capex,
  dividends paid and share repurchases are stored as POSITIVE cash
  outflows (the SEC collector reports them as positive amounts).
  Free cash flow is therefore operating cash flow MINUS capex, and
  FCF after dividends is FCF MINUS dividends paid.  If a future
  collector stores capex as a negative number, the Step 16
  derivation (and therefore every figure here) would change sign —
  this module trusts the Step 16 derivation and does not re-sign
  anything.

What is unavailable, honestly:

* Owner earnings (Buffett definition: net income + depreciation /
  amortisation + other non-cash charges - average maintenance
  capex) is NOT computed: depreciation / amortisation is not a
  Step 16 concept and no such key exists anywhere in the US raw
  data (verified across all US statement rows), the capex split
  into maintenance vs growth capex is not reported, and no other
  non-cash-charge concept exists.  It is returned missing with
  that reason and the exact missing inputs listed.  Free cash flow
  is the closest available proxy and is reported as such — it is
  NOT owner earnings (it subtracts ALL capex, including growth
  capex, and adds back nothing for D&A), and it is never labelled
  as owner earnings.
* The cash conversion cycle needs inventory, receivables and
  payables; if any component is missing the cycle is missing with
  that reason, never estimated from the other two.
* Singapore / Hong Kong — Step 16 standardised 0 concepts for DBS
  and none exist for HKEX, so there is nothing to analyse.

This module never touches the production database.  It reads a
connection the caller supplies (tests use a temporary copy) and
never writes.
"""

from agents import financial_growth as fg
from agents import financial_ratios as fr
from agents import financial_standard as fs

# Concepts this module reads, by statement type.
FLOW_CONCEPTS = (
    "revenue",
    "cost_of_revenue",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capital_expenditure",
    "free_cash_flow",
    "dividends_paid",
    "share_repurchases",
    "share_based_compensation",
    "investing_cash_flow",
    "financing_cash_flow",
)
BALANCE_CONCEPTS = (
    "current_assets",
    "current_liabilities",
    "accounts_receivable",
    "accounts_payable",
    "inventory",
    "cash_and_equivalents",
    "total_debt",
    "total_equity",
)

# Owner earnings: the Buffett definition and exactly what is missing.
OWNER_EARNINGS_FORMULA = (
    "net income + depreciation/amortisation + other non-cash charges "
    "- average maintenance capex"
)
OWNER_EARNINGS_MISSING_INPUTS = (
    "depreciation_and_amortisation: no Step 16 concept and no such key "
    "in the US raw data (SEC XBRL tag DepreciationDepletionAndAmortization "
    "would unlock it); maintenance capex: capex is not split into "
    "maintenance vs growth capex in the reported data; other non-cash "
    "charges: no concept in the Step 16 set",
)

DAYS_IN_YEAR = 365


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _statement_for(concept):
    return fs.CONCEPT_STATEMENT.get(concept, "income_statement")


def _series(values, concept, period_type="annual"):
    """One series as a sorted list of (reporting_period, value|None)."""
    statement = _statement_for(concept)
    periods = sorted(
        p for (st, p, pt) in values
        if st == statement and pt == period_type
    )
    return [
        (period, fr._num(fr._get(values, statement, period, period_type, concept)))
        for period in periods
    ]


def _result(name, value, formula, inputs, period, period_type, reason=None,
            extra=None):
    result = {
        "name": name,
        "value": value,
        "present": value is not None,
        "formula": formula,
        "inputs": inputs,
        "reporting_period": period,
        "period_type": period_type,
        "reason": reason,
    }
    if extra:
        result.update(extra)
    return result


def _missing(name, reason, period, period_type, inputs=None, formula=""):
    return _result(name, None, formula, inputs or {}, period, period_type,
                   reason=reason)


def _div(name, numerator, denominator, formula, inputs, period, period_type,
         extra=None):
    """Divide with missing/zero handling.  Returns a result dict."""
    if numerator is None or denominator is None:
        missing = [k for k, v in inputs.items() if v is None]
        return _missing(
            name, f"missing input(s): {', '.join(missing)}",
            period, period_type, inputs, formula)
    if denominator == 0:
        return _missing(name, "denominator is zero", period, period_type,
                        inputs, formula)
    return _result(name, numerator / denominator, formula, inputs, period,
                   period_type, extra=extra)


def _diff(name, a, b, formula, inputs, period, period_type, extra=None):
    """Subtract with missing handling (a - b)."""
    if a is None or b is None:
        missing = [k for k, v in inputs.items() if v is None]
        return _missing(
            name, f"missing input(s): {', '.join(missing)}",
            period, period_type, inputs, formula)
    return _result(name, a - b, formula, inputs, period, period_type,
                   extra=extra)


def _period_values(values, period, period_type):
    """Flat {concept: numeric value|None} for one period."""
    out = {}
    for concept in FLOW_CONCEPTS + BALANCE_CONCEPTS:
        statement = _statement_for(concept)
        out[concept] = fr._num(
            fr._get(values, statement, period, period_type, concept))
    return out


def _prior_period(values, period, period_type):
    """The previous period of the same statement/period type, or None."""
    periods = sorted(
        p for (st, p, pt) in values
        if st == "balance_sheet" and pt == period_type
    )
    if period in periods:
        idx = periods.index(period)
        return periods[idx - 1] if idx > 0 else None
    return None


# ---------------------------------------------------------------------------
# Cash conversion cycle
# ---------------------------------------------------------------------------

def cash_conversion_cycle(vals, period, period_type):
    """Days inventory + days sales - days payable, period-end balances.

    DIO = inventory / cost_of_revenue * 365, DSO = receivables /
    revenue * 365, DPO = payables / cost_of_revenue * 365.  Uses
    period-end balances (no average-balance version is offered: a
    2-point average would silently mix a sparse early balance sheet
    with a full one).  Any missing component makes the cycle missing.
    """
    inventory = vals.get("inventory")
    receivables = vals.get("accounts_receivable")
    payables = vals.get("accounts_payable")
    cost = vals.get("cost_of_revenue")
    revenue = vals.get("revenue")
    inputs = {"inventory": inventory, "accounts_receivable": receivables,
              "accounts_payable": payables, "cost_of_revenue": cost,
              "revenue": revenue}
    missing = [k for k, v in inputs.items() if v is None]
    if missing:
        return _missing(
            "cash_conversion_cycle",
            f"missing input(s): {', '.join(missing)}",
            period, period_type, inputs,
            "DIO + DSO - DPO (days, period-end balances)")
    if cost == 0 or revenue == 0:
        return _missing(
            "cash_conversion_cycle", "denominator is zero",
            period, period_type, inputs,
            "DIO + DSO - DPO (days, period-end balances)")
    dio = inventory / cost * DAYS_IN_YEAR
    dso = receivables / revenue * DAYS_IN_YEAR
    dpo = payables / cost * DAYS_IN_YEAR
    return _result(
        "cash_conversion_cycle", dio + dso - dpo,
        "DIO + DSO - DPO (days, period-end balances)", inputs, period,
        period_type,
        extra={"days_inventory_outstanding": dio,
               "days_sales_outstanding": dso,
               "days_payable_outstanding": dpo})


# ---------------------------------------------------------------------------
# One-period assessment
# ---------------------------------------------------------------------------

def assess_cashflow_from_values(vals, prior_vals, values, ticker, period,
                                period_type="annual", ttm_method=None):
    """Assess cash flow from flat concept->value dicts.

    ``vals`` is the current period, ``prior_vals`` the prior period
    of the same period_type (or None), ``values`` the full
    load_standardised_values dict (used for multi-year trends).
    """
    ocf = vals.get("operating_cash_flow")
    capex = vals.get("capital_expenditure")
    fcf = vals.get("free_cash_flow")
    dividends = vals.get("dividends_paid")
    buybacks = vals.get("share_repurchases")
    sbc = vals.get("share_based_compensation")
    net_income = vals.get("net_income")
    operating_income = vals.get("operating_income")
    revenue = vals.get("revenue")

    out = {"ticker": ticker, "reporting_period": period,
           "period_type": period_type, "sections": {}}

    # (a) Operating cash flow quality ----------------------------------
    out["sections"]["operating_cash_flow"] = {
        "operating_cash_flow": _result(
            "operating_cash_flow", ocf, "as reported (Step 16 concept)",
            {"operating_cash_flow": ocf}, period, period_type),
        "ocf_to_net_income": _div(
            "ocf_to_net_income", ocf, net_income,
            "operating_cash_flow / net_income",
            {"operating_cash_flow": ocf, "net_income": net_income},
            period, period_type),
        "ocf_to_operating_income": _div(
            "ocf_to_operating_income", ocf, operating_income,
            "operating_cash_flow / operating_income",
            {"operating_cash_flow": ocf,
             "operating_income": operating_income},
            period, period_type),
    }

    # (b) Capital expenditure -------------------------------------------
    prior_capex = prior_vals.get("capital_expenditure") if prior_vals else None
    prior_ocf = prior_vals.get("operating_cash_flow") if prior_vals else None
    capex_yoy, capex_yoy_reason = fg._yoy_value(capex, prior_capex)
    ocf_yoy, ocf_yoy_reason = fg._yoy_value(ocf, prior_ocf)
    out["sections"]["capital_expenditure"] = {
        "capital_expenditure": _result(
            "capital_expenditure", capex, "as reported (Step 16 concept)",
            {"capital_expenditure": capex}, period, period_type),
        "capex_to_revenue": _div(
            "capex_to_revenue", capex, revenue,
            "capital_expenditure / revenue",
            {"capital_expenditure": capex, "revenue": revenue},
            period, period_type),
        "capex_to_ocf": _div(
            "capex_to_ocf", capex, ocf,
            "capital_expenditure / operating_cash_flow",
            {"capital_expenditure": capex, "operating_cash_flow": ocf},
            period, period_type),
        "capex_yoy": _result(
            "capex_yoy", capex_yoy, "capex / prior-year capex - 1",
            {"capital_expenditure": capex,
             "prior_capital_expenditure": prior_capex},
            period, period_type, reason=capex_yoy_reason),
        "ocf_yoy": _result(
            "ocf_yoy", ocf_yoy, "OCF / prior-year OCF - 1",
            {"operating_cash_flow": ocf,
             "prior_operating_cash_flow": prior_ocf},
            period, period_type, reason=ocf_yoy_reason),
    }

    # (c) Free cash flow -------------------------------------------------
    fcf_after_div = _diff(
        "fcf_after_dividends", fcf, dividends,
        "free_cash_flow - dividends_paid",
        {"free_cash_flow": fcf, "dividends_paid": dividends},
        period, period_type)
    if fcf_after_div["present"] and buybacks is not None:
        fcf_after_all = _diff(
            "fcf_after_dividends_and_buybacks",
            fcf_after_div["value"], buybacks,
            "free_cash_flow - dividends_paid - share_repurchases",
            {"free_cash_flow": fcf, "dividends_paid": dividends,
             "share_repurchases": buybacks},
            period, period_type)
    else:
        missing = [k for k, v in
                   {"free_cash_flow": fcf, "dividends_paid": dividends,
                    "share_repurchases": buybacks}.items() if v is None]
        fcf_after_all = _missing(
            "fcf_after_dividends_and_buybacks",
            f"missing input(s): {', '.join(missing)}",
            period, period_type,
            {"free_cash_flow": fcf, "dividends_paid": dividends,
             "share_repurchases": buybacks},
            "free_cash_flow - dividends_paid - share_repurchases")
    out["sections"]["free_cash_flow"] = {
        "free_cash_flow": _result(
            "free_cash_flow", fcf,
            "operating_cash_flow - capital_expenditure (Step 16 derived)",
            {"operating_cash_flow": ocf, "capital_expenditure": capex},
            period, period_type),
        "fcf_to_net_income": _div(
            "fcf_to_net_income", fcf, net_income,
            "free_cash_flow / net_income",
            {"free_cash_flow": fcf, "net_income": net_income},
            period, period_type),
        "fcf_to_revenue": _div(
            "fcf_to_revenue", fcf, revenue,
            "free_cash_flow / revenue",
            {"free_cash_flow": fcf, "revenue": revenue},
            period, period_type),
        "fcf_after_dividends": fcf_after_div,
        "fcf_after_dividends_and_buybacks": fcf_after_all,
    }

    # (d) Owner earnings — unavailable, with the exact missing inputs ---
    out["sections"]["owner_earnings"] = {
        "owner_earnings": _missing(
            "owner_earnings",
            "unavailable: " + "; ".join(OWNER_EARNINGS_MISSING_INPUTS),
            period, period_type,
            {"net_income": net_income},
            OWNER_EARNINGS_FORMULA),
        "owner_earnings_note": (
            "Free cash flow is the closest available proxy but is NOT "
            "owner earnings: it subtracts ALL capex (including growth "
            "capex) and adds back nothing for depreciation/amortisation. "
            "Adding retained-earnings / D&A / interest-expense concepts "
            "to Step 16 (SEC tags DepreciationDepletionAndAmortization, "
            "RetainedEarningsAccumulatedDeficit) would unlock a real "
            "owner-earnings figure."),
    }

    # (e) Stock-based compensation ---------------------------------------
    if sbc is not None and fcf is not None:
        sbc_adj = _diff(
            "sbc_adjusted_fcf", fcf, sbc,
            "free_cash_flow - share_based_compensation",
            {"free_cash_flow": fcf, "share_based_compensation": sbc},
            period, period_type,
            extra={"note": (
                "SBC is a non-cash expense, but it is a real economic "
                "cost to shareholders through dilution; subtracting it "
                "treats it as such. This is an adjusted FCF, not a "
                "different definition of FCF.")})
    else:
        missing = [k for k, v in
                   {"free_cash_flow": fcf,
                    "share_based_compensation": sbc}.items() if v is None]
        sbc_adj = _missing(
            "sbc_adjusted_fcf",
            f"missing input(s): {', '.join(missing)}",
            period, period_type,
            {"free_cash_flow": fcf, "share_based_compensation": sbc},
            "free_cash_flow - share_based_compensation")
    out["sections"]["stock_based_compensation"] = {
        "share_based_compensation": _result(
            "share_based_compensation", sbc,
            "as reported (Step 16 concept)",
            {"share_based_compensation": sbc}, period, period_type),
        "sbc_to_ocf": _div(
            "sbc_to_ocf", sbc, ocf,
            "share_based_compensation / operating_cash_flow",
            {"share_based_compensation": sbc, "operating_cash_flow": ocf},
            period, period_type),
        "sbc_to_fcf": _div(
            "sbc_to_fcf", sbc, fcf,
            "share_based_compensation / free_cash_flow",
            {"share_based_compensation": sbc, "free_cash_flow": fcf},
            period, period_type),
        "sbc_adjusted_fcf": sbc_adj,
    }

    # (f) Working capital -------------------------------------------------
    ca = vals.get("current_assets")
    cl = vals.get("current_liabilities")
    if ca is not None and cl is not None:
        wc = _result(
            "working_capital", ca - cl,
            "current_assets - current_liabilities",
            {"current_assets": ca, "current_liabilities": cl},
            period, period_type)
    else:
        missing = [k for k, v in
                   {"current_assets": ca, "current_liabilities": cl}.items()
                   if v is None]
        wc = _missing(
            "working_capital",
            f"missing input(s): {', '.join(missing)}",
            period, period_type,
            {"current_assets": ca, "current_liabilities": cl},
            "current_assets - current_liabilities")
    prior_ca = prior_vals.get("current_assets") if prior_vals else None
    prior_cl = prior_vals.get("current_liabilities") if prior_vals else None
    if (prior_ca is not None and prior_cl is not None
            and wc["present"]):
        prior_wc = prior_ca - prior_cl
        wc_change = _result(
            "change_in_working_capital", wc["value"] - prior_wc,
            "working_capital - prior-year working_capital",
            {"working_capital": wc["value"],
             "prior_working_capital": prior_wc},
            period, period_type)
    else:
        wc_change = _missing(
            "change_in_working_capital",
            "prior-period current assets/liabilities missing; "
            "change cannot be computed",
            period, period_type,
            {"current_assets": ca, "current_liabilities": cl},
            "working_capital - prior-year working_capital")
    prior_revenue = None
    if values is not None:
        rev_series = _series(values, "revenue", period_type)
        for i, (p, v) in enumerate(rev_series):
            if p == period and i > 0:
                prior_revenue = rev_series[i - 1][1]
    if (wc_change["present"] and revenue is not None
            and prior_revenue is not None
            and revenue - prior_revenue != 0):
        wc_req = _result(
            "working_capital_requirement", wc_change["value"] / (revenue - prior_revenue),
            "change in working capital / change in revenue",
            {"change_in_working_capital": wc_change["value"],
             "revenue_change": revenue - prior_revenue},
            period, period_type,
            extra={"note": (
                "Share of each extra dollar of revenue absorbed by "
                "working capital. A negative value means working "
                "capital released cash as revenue grew (current "
                "liabilities grew faster than current assets).")})
    elif wc_change["present"]:
        wc_req = _missing(
            "working_capital_requirement",
            "revenue change is zero or prior revenue missing; "
            "ratio is not meaningful",
            period, period_type,
            {"change_in_working_capital": wc_change["value"],
             "revenue": revenue, "prior_revenue": prior_revenue},
            "change in working capital / change in revenue")
    else:
        wc_req = _missing(
            "working_capital_requirement",
            wc_change["reason"] or "working capital change missing",
            period, period_type,
            {"change_in_working_capital": None},
            "change in working capital / change in revenue")
    out["sections"]["working_capital"] = {
        "working_capital": wc,
        "change_in_working_capital": wc_change,
        "working_capital_requirement": wc_req,
        "cash_conversion_cycle": cash_conversion_cycle(
            vals, period, period_type),
    }

    # (g) Cash conversion summary (5y / 10y means) -----------------------
    out["sections"]["cash_conversion_trends"] = _conversion_trends(
        values, period, period_type)
    out["sections"]["capex_surge"] = _capex_surge(values, period, period_type)
    if ttm_method:
        out["ttm_method"] = ttm_method
    return out


def _conversion_trends(values, period, period_type):
    """Mean OCF/NI, FCF/NI and capex/revenue over 5y and 10y windows."""
    out = {}
    if values is None:
        return out
    for window in (5, 10):
        ocf_series = _series(values, "operating_cash_flow", period_type)
        ni_series = _series(values, "net_income", period_type)
        fcf_series = _series(values, "free_cash_flow", period_type)
        capex_series = _series(values, "capital_expenditure", period_type)
        rev_series = _series(values, "revenue", period_type)
        by_period = {}
        for series, name in ((ocf_series, "ocf"), (ni_series, "ni"),
                             (fcf_series, "fcf"), (capex_series, "capex"),
                             (rev_series, "revenue")):
            for p, v in series:
                by_period.setdefault(p, {})[name] = v
        rows = [by_period[p] for p in sorted(by_period)
                if p <= period][-window:]
        def _mean_ratio(num_key, den_key):
            pairs = [(r[num_key], r[den_key]) for r in rows
                     if r.get(num_key) is not None
                     and r.get(den_key) not in (None, 0)]
            if not pairs:
                return None, len(rows), 0
            return (sum(n / d for n, d in pairs) / len(pairs),
                    len(rows), len(pairs))
        ocf_ni, n, used = _mean_ratio("ocf", "ni")
        fcf_ni, _, _ = _mean_ratio("fcf", "ni")
        capex_rev, _, _ = _mean_ratio("capex", "revenue")
        out[f"{window}y"] = {
            "mean_ocf_to_net_income": ocf_ni,
            "mean_fcf_to_net_income": fcf_ni,
            "mean_capex_to_revenue": capex_rev,
            "periods_in_window": n,
            "periods_used": used,
        }
    return out


def _capex_surge(values, period, period_type):
    """Answer: is FCF lagging because OCF is weak or capex is surging?

    Compares 5y and 10y CAGRs of capex vs OCF (Step 19's cagr, same
    rules: undefined on a non-positive start, never interpolated)
    and the change in capex as a share of OCF.  The verdict is a
    documented judgement, not a causal proof.
    """
    out = {}
    if values is None:
        return out
    for window in (5, 10):
        capex_cagr = fg.cagr(_series(values, "capital_expenditure",
                                     period_type), window)
        ocf_cagr = fg.cagr(_series(values, "operating_cash_flow",
                                   period_type), window)
        fcf_cagr = fg.cagr(_series(values, "free_cash_flow",
                                   period_type), window)
        out[f"{window}y"] = {
            "capex_cagr": capex_cagr["value"] if capex_cagr["present"] else None,
            "ocf_cagr": ocf_cagr["value"] if ocf_cagr["present"] else None,
            "fcf_cagr": fcf_cagr["value"] if fcf_cagr["present"] else None,
            "capex_cagr_reason": capex_cagr["reason"],
            "ocf_cagr_reason": ocf_cagr["reason"],
            "fcf_cagr_reason": fcf_cagr["reason"],
        }
    capex_5 = out["5y"]["capex_cagr"]
    ocf_5 = out["5y"]["ocf_cagr"]
    fcf_5 = out["5y"]["fcf_cagr"]
    if capex_5 is not None and ocf_5 is not None and fcf_5 is not None:
        if capex_5 > ocf_5 and fcf_5 < ocf_5:
            verdict = (
                "capex surge: over 5 years capex grew faster than "
                "operating cash flow, so free cash flow grew slower "
                "than operating cash flow — FCF is lagging because "
                "capex is rising, not because OCF is weak")
        elif ocf_5 <= 0:
            verdict = "OCF weakness: operating cash flow did not grow"
        else:
            verdict = (
                "no capex-surge pattern: capex did not outgrow "
                "operating cash flow over 5 years")
    else:
        verdict = "insufficient data to separate a capex surge from OCF weakness"
    out["verdict"] = verdict
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def assess_cashflow(conn, ticker, period=None, period_type="annual"):
    """Assess cash flow for one period.

    ``period`` defaults to the latest period of ``period_type``.
    For ``period_type="ttm"`` the flow values are built the Step 17
    way (``build_ttm_values``) and balance-sheet values come from the
    latest quarterly balance row; the method is recorded on the
    result.  The connection is used read-only; nothing is written.
    """
    values, load_warnings = fr.load_standardised_values(conn, ticker)
    if period_type == "ttm":
        if period is None:
            period = fr._latest_period(values, "income_statement",
                                       "quarterly")
        if period is None:
            return {"ticker": ticker, "reporting_period": None,
                    "period_type": "ttm", "sections": {},
                    "warnings": ["no quarterly income rows found"]}
        ttm, error = fr.build_ttm_values(values, period)
        if ttm is None:
            return {"ticker": ticker, "reporting_period": period,
                    "period_type": "ttm", "sections": {},
                    "warnings": [error]}
        vals = {}
        for concept in FLOW_CONCEPTS:
            vals[concept] = ttm["flow"].get(concept)
        for concept in BALANCE_CONCEPTS:
            vals[concept] = ttm["balance"].get(concept)
        # Prior TTM values are not built (Step 19 finding: a prior-year
        # TTM needs the same construction a year earlier and the
        # quarterly series has no row at a fiscal year end), so YoY
        # and change-in-working-capital figures are missing for TTM.
        result = assess_cashflow_from_values(
            vals, None, None, ticker, period, "ttm",
            ttm_method=ttm["method"])
        result["warnings"] = load_warnings + [
            f"TTM built by {ttm['method']}",
            "prior-period comparisons are missing for TTM: no prior "
            "TTM is constructed (see Step 19 report)",
        ]
        return result

    periods = fr._periods(values, "income_statement", period_type)
    if not periods:
        return {"ticker": ticker, "reporting_period": None,
                "period_type": period_type, "sections": {},
                "warnings": ["no income rows found"]}
    if period is None:
        period = periods[-1]
    vals = _period_values(values, period, period_type)
    prior_period = _prior_period(values, period, period_type)
    prior_vals = (_period_values(values, prior_period, period_type)
                  if prior_period else None)
    result = assess_cashflow_from_values(
        vals, prior_vals, values, ticker, period, period_type)
    result["warnings"] = load_warnings
    if prior_period is None:
        result["warnings"] = result["warnings"] + [
            "no prior period available; YoY and change figures missing"]
    return result


def assess_all_periods(conn, ticker):
    """Annual assessments for every annual period, plus the latest TTM."""
    values, _ = fr.load_standardised_values(conn, ticker)
    annual_periods = fr._periods(values, "income_statement", "annual")
    results = []
    for period in annual_periods:
        results.append(assess_cashflow(
            conn, ticker, period=period, period_type="annual"))
    latest_q = fr._latest_period(values, "income_statement", "quarterly")
    if latest_q:
        results.append(assess_cashflow(
            conn, ticker, period=latest_q, period_type="ttm"))
    return results
