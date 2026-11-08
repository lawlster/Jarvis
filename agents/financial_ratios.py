"""Step 17 — Financial ratios and metrics engine.

Computes the Step 17 ratio set from Step 16 standardised statements
(``agents/financial_standard.py``), plus explicit price / market-cap
inputs supplied by the caller.  It never fetches a price itself and
makes no network requests.

Design rules (from the project brief, Steps 16-17):

* Never invent a missing figure.  A ratio whose input is missing is
  returned with ``value=None``, ``present=False`` and a ``reason``.
  A zero denominator is missing with the reason "denominator is zero",
  never an exception and never infinity.
* Every ratio records its formula, the inputs actually used, the
  reporting period, the period type, and the as-of date of any price
  used.  Price-dependent ratios are only computed when the caller
  supplies a price input, and that input is always labelled with its
  date — a stale price is a stale price, not a current one.
* Annual rows are used by default.  Quarterly rows are used only when
  the caller asks for ``period_type="quarterly"`` or ``"ttm"``.
* TTM: there are no TTM rows in the database, and no quarterly row is
  stored at a fiscal year end (the June-30 quarter is only ever the
  annual 10-K row), so a four-quarter sum is not possible from the
  stored rows.  TTM flow concepts are therefore built as
  ``annual (latest fiscal year) + latest quarter - same quarter a year
  ago``; balance-sheet concepts for a TTM use the latest quarterly
  balance row.  The construction method is recorded on every TTM
  ratio.  If four quarterly rows do exist for a period (another
  issuer), the four-quarter sum is used instead.
* ROE / ROA / ROIC use average balances when the prior period-end
  balance exists, otherwise the period-end balance; the basis is
  recorded on the ratio ("average" or "period_end").
* ROIC definition: NOPAT / invested capital, where
  NOPAT = operating_income * (1 - effective tax rate),
  effective tax rate = income_tax / income_before_tax, and
  invested capital = total_debt + total_equity - cash_and_equivalents.
  If the tax rate cannot be computed (income_before_tax or income_tax
  missing), ROIC is missing — the tax rate is never assumed.
* EV definition: market_cap + total_debt - cash_and_equivalents.
  Short-term investments are NOT subtracted (they are not cash, even
  though they are liquid); this is a documented choice, and the
  alternative is noted in the Step 17 report.
* Quick ratio definition: (current_assets - inventory) /
  current_liabilities.  The liquid-assets variant
  (cash + short-term investments + receivables) / current_liabilities
  is not used; it is noted in the report.
* total_debt is the Step 16 derived concept
  (short_term_debt + long_term_debt).  The snapshot table's
  ``total_debt`` ($56,826m for MSFT at 2026-06-30) does NOT match the
  standardised short+long-term debt ($40,294m) — it presumably includes
  leases or other items — so snapshot debt is never used here; only
  standardised components are.

What is unavailable, honestly:

* EV/EBITDA: no depreciation / amortisation or EBITDA concept exists
  in the Step 16 concept set, and no such key exists anywhere in the
  US raw data.  The ratio is returned missing with that reason.
* Interest coverage: no interest expense concept exists in the Step 16
  concept set, and no such key exists in the US raw data.  Returned
  missing with that reason.  Do not estimate it from operating income.

This module never touches the production database.  It reads rows
supplied by the caller (or a connection the caller supplies, used
read-only through ``financial_standard.standardise_row``), and never
writes.  Price inputs come from ``load_snapshot_price`` (latest
``financial_snapshots`` row) or are supplied directly by the caller.
"""

import json
import sqlite3

from agents import financial_standard as fs

# ---------------------------------------------------------------------------
# Ratio definitions: formula text, inputs, category, needs price?
# ---------------------------------------------------------------------------

RATIO_DEFINITIONS = {
    "pe_ratio": {
        "formula": "price / diluted EPS",
        "inputs": ["eps_diluted", "shares_outstanding_diluted", "net_income"],
        "category": "valuation",
        "needs_price": True,
    },
    "pb_ratio": {
        "formula": "price / (total_equity / shares_outstanding_diluted)",
        "inputs": ["total_equity", "shares_outstanding_diluted"],
        "category": "valuation",
        "needs_price": True,
    },
    "ps_ratio": {
        "formula": "market_cap / revenue",
        "inputs": ["revenue"],
        "category": "valuation",
        "needs_price": True,
    },
    "ev_ebitda": {
        "formula": "EV / EBITDA",
        "inputs": ["operating_income"],
        "category": "valuation",
        "needs_price": True,
    },
    "ev_ebit": {
        "formula": "EV / operating_income, where EV = market_cap + "
                   "total_debt - cash_and_equivalents",
        "inputs": ["operating_income", "total_debt", "cash_and_equivalents"],
        "category": "valuation",
        "needs_price": True,
    },
    "gross_margin": {
        "formula": "gross_profit / revenue",
        "inputs": ["gross_profit", "revenue"],
        "category": "profitability",
        "needs_price": False,
    },
    "operating_margin": {
        "formula": "operating_income / revenue",
        "inputs": ["operating_income", "revenue"],
        "category": "profitability",
        "needs_price": False,
    },
    "net_margin": {
        "formula": "net_income / revenue",
        "inputs": ["net_income", "revenue"],
        "category": "profitability",
        "needs_price": False,
    },
    "roe": {
        "formula": "net_income / average total_equity "
                   "(period-end if no prior period)",
        "inputs": ["net_income", "total_equity"],
        "category": "returns",
        "needs_price": False,
    },
    "roa": {
        "formula": "net_income / average total_assets "
                   "(period-end if no prior period)",
        "inputs": ["net_income", "total_assets"],
        "category": "returns",
        "needs_price": False,
    },
    "roic": {
        "formula": "NOPAT / average invested capital, where "
                   "NOPAT = operating_income * (1 - income_tax / "
                   "income_before_tax) and invested capital = "
                   "total_debt + total_equity - cash_and_equivalents "
                   "(period-end values if no prior period)",
        "inputs": ["operating_income", "income_tax", "income_before_tax",
                   "total_debt", "total_equity", "cash_and_equivalents"],
        "category": "returns",
        "needs_price": False,
    },
    "debt_to_equity": {
        "formula": "total_debt / total_equity",
        "inputs": ["total_debt", "total_equity"],
        "category": "leverage",
        "needs_price": False,
    },
    "interest_coverage": {
        "formula": "operating_income / interest_expense",
        "inputs": ["operating_income"],
        "category": "leverage",
        "needs_price": False,
    },
    "current_ratio": {
        "formula": "current_assets / current_liabilities",
        "inputs": ["current_assets", "current_liabilities"],
        "category": "liquidity",
        "needs_price": False,
    },
    "quick_ratio": {
        "formula": "(current_assets - inventory) / current_liabilities",
        "inputs": ["current_assets", "inventory", "current_liabilities"],
        "category": "liquidity",
        "needs_price": False,
    },
}

# Flow concepts (income / cash flow): summed over a TTM window.
FLOW_CONCEPTS = (
    "revenue", "cost_of_revenue", "gross_profit",
    "research_and_development", "selling_general_administrative",
    "operating_income", "income_before_tax", "income_tax", "net_income",
    "operating_cash_flow", "capital_expenditure", "free_cash_flow",
    "investing_cash_flow", "financing_cash_flow", "dividends_paid",
    "share_repurchases", "share_based_compensation",
)

# Ratios that cannot be computed from the Step 16 concept set at all,
# however complete the data is.  Kept as data so the gap is explicit.
UNAVAILABLE_RATIOS = {
    "ev_ebitda": "no depreciation/amortisation or EBITDA concept in "
                 "the Step 16 concept set, and no such key exists in "
                 "the US raw data",
    "interest_coverage": "no interest expense concept in the Step 16 "
                         "concept set, and no such key exists in the "
                         "US raw data",
}


# ---------------------------------------------------------------------------
# Price inputs
# ---------------------------------------------------------------------------

def load_snapshot_price(conn, ticker):
    """Load the latest ``financial_snapshots`` row for ``ticker``.

    Returns a price-input dict ``{price, market_cap, as_of_date,
    source}`` or None when no snapshot exists.  The price is labelled
    with its snapshot date; callers must not present it as current.
    Market cap is taken from the snapshot when present; otherwise it
    is computed as price * diluted shares for the period (the caller
    supplies the share count) — see ``market_cap_for``.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT snapshot_date, financial_data FROM financial_snapshots "
        "WHERE ticker = ? ORDER BY snapshot_date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["financial_data"])
    except (ValueError, TypeError):
        return None
    price = data.get("current_price")
    if price is None:
        return None
    return {
        "price": price,
        "market_cap": data.get("market_cap"),
        "as_of_date": (row["snapshot_date"] or "")[:10],
        "source": "financial_snapshots (latest row)",
    }


def market_cap_for(price_input, shares_outstanding_diluted):
    """Market cap for EV / P/S: snapshot value, else price * shares.

    Returns (market_cap, method) or (None, reason).
    """
    if price_input is None:
        return None, "no price input supplied"
    if price_input.get("market_cap"):
        return price_input["market_cap"], "snapshot market_cap"
    if shares_outstanding_diluted:
        return (price_input["price"] * shares_outstanding_diluted,
                "price * shares_outstanding_diluted")
    return None, "no market_cap in snapshot and no share count"


# ---------------------------------------------------------------------------
# Loading standardised values
# ---------------------------------------------------------------------------

def load_standardised_values(conn, ticker):
    """Standardise every ``financial_statements`` row for ``ticker``.

    Returns {(statement_type, reporting_period, period_type): {concept:
    value-dict}} using ``financial_standard.standardise_row``.  The
    connection is used read-only; nothing is written.  Duplicate keys
    cannot occur (the source table has a unique key on
    ticker/exchange/statement_type/reporting_period/period_type), but
    if two rows ever share a key the first is kept and the clash is
    reported in ``load_warnings``.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM financial_statements WHERE ticker = ? "
        "ORDER BY statement_type, reporting_period, period_type",
        (ticker,),
    ).fetchall()
    values = {}
    warnings = []
    for row in rows:
        record = fs.standardise_row(dict(row))
        key = (record["statement_type"], record["reporting_period"],
               record["period_type"])
        if key in values:
            warnings.append(
                f"duplicate standardised key {key}; kept first row "
                f"(source id {values[key]['_source_id']}), ignored "
                f"source id {record['source_statement_id']}"
            )
            continue
        flat = {c: v for c, v in record["values"].items()}
        flat["_source_id"] = record["source_statement_id"]
        flat["_warnings"] = record["warnings"]
        flat["_currency"] = record["currency"]
        values[key] = flat
    return values, warnings


def _get(values, statement_type, period, period_type, concept):
    """One standardised value dict, or None if the row/concept is absent."""
    row = values.get((statement_type, period, period_type))
    if row is None:
        return None
    return row.get(concept)


def _num(value_dict):
    """The numeric value of a standardised value dict, or None."""
    if value_dict is None or not value_dict.get("present"):
        return None
    return value_dict.get("value")


def _periods(values, statement_type, period_type):
    """Sorted reporting periods available for a statement/period type."""
    return sorted(p for (st, p, pt) in values
                  if st == statement_type and pt == period_type)


def _latest_period(values, statement_type, period_type):
    periods = _periods(values, statement_type, period_type)
    return periods[-1] if periods else None


# ---------------------------------------------------------------------------
# TTM construction
# ---------------------------------------------------------------------------

def build_ttm_values(values, latest_quarter_period):
    """Build TTM flow values ending at ``latest_quarter_period``.

    Method A (preferred): sum the four quarterly rows ending at the
    latest quarter.  Method B (used for MSFT and any issuer whose
    fiscal-year-end quarter is only stored as an annual row): annual
    (latest fiscal year) + latest quarter - same quarter a year ago.
    Balance-sheet concepts are taken from the latest quarterly balance
    row, not summed.  Returns (ttm_values, method_note) where
    ttm_values maps concept -> numeric value (flow concepts summed,
    balance concepts point-in-time, EPS/shares from the latest
    quarter's annual-equivalent handling is NOT done — EPS for a TTM
    is net_income_ttm / shares; see ``compute_ratios``).
    """
    flow = {}
    q_periods = _periods(values, "income_statement", "quarterly")
    if latest_quarter_period not in q_periods:
        return None, (f"no quarterly income row for "
                      f"{latest_quarter_period}; TTM cannot be built")
    idx = q_periods.index(latest_quarter_period)
    if idx >= 3 and _consecutive_quarters(q_periods[idx - 3:idx + 1]):
        window = q_periods[idx - 3:idx + 1]
        method = ("sum of 4 quarterly rows "
                  f"({window[0]} to {window[-1]})")
        for concept in FLOW_CONCEPTS:
            total = 0
            complete = True
            for period in window:
                v = _num(_get(values, _statement_for(concept),
                              period, "quarterly", concept))
                if v is None:
                    complete = False
                    break
                total += v
            flow[concept] = total if complete else None
    else:
        # Method B: annual + latest quarter - same quarter last year.
        annual_periods = _periods(values, "income_statement", "annual")
        if not annual_periods:
            return None, "no annual income row; TTM cannot be built"
        annual = annual_periods[-1]
        # Same quarter a year ago: same month-day, previous year.
        y, m, d = latest_quarter_period.split("-")
        prior_quarter = f"{int(y) - 1}-{m}-{d}"
        if prior_quarter not in q_periods:
            return None, (f"same-quarter-prior-year row {prior_quarter} "
                          "missing; TTM cannot be built")
        method = (f"annual {annual} + quarter {latest_quarter_period} "
                  f"- quarter {prior_quarter} (no quarterly row is "
                  "stored at a fiscal year end)")
        for concept in FLOW_CONCEPTS:
            stmt = _statement_for(concept)
            a = _num(_get(values, stmt, annual, "annual", concept))
            q = _num(_get(values, stmt, latest_quarter_period,
                          "quarterly", concept))
            p = _num(_get(values, stmt, prior_quarter, "quarterly",
                          concept))
            flow[concept] = (a + q - p
                             if None not in (a, q, p) else None)
    # Balance-sheet concepts: latest quarterly balance row.
    balance_periods = _periods(values, "balance_sheet", "quarterly")
    balance = {}
    balance_period = None
    if balance_periods:
        balance_period = max(p for p in balance_periods
                             if p <= latest_quarter_period)
        for concept in ("total_assets", "total_liabilities",
                        "total_equity", "cash_and_equivalents",
                        "total_debt", "current_assets",
                        "current_liabilities", "inventory",
                        "short_term_debt", "long_term_debt"):
            balance[concept] = _num(_get(
                values, "balance_sheet", balance_period, "quarterly",
                concept))
    return {"flow": flow, "balance": balance,
            "balance_period": balance_period,
            "method": method}, None


def _statement_for(concept):
    return fs.CONCEPT_STATEMENT.get(concept, "income_statement")


def _consecutive_quarters(window):
    """True if ``window`` is four consecutive calendar quarters.

    A stored quarterly series can have a gap — MSFT has no quarterly
    row at a fiscal year end, so the four most recent quarterly rows
    ending 2026-09-30 are 2025-09-30, 2025-12-31, 2026-03-31 and
    2026-09-30, which span five quarters.  Summing those would be
    wrong, so a gap forces Method B (annual + latest quarter -
    same quarter a year ago).
    """
    if len(window) != 4:
        return False
    months = []
    for period in window:
        try:
            y, m, _d = period.split("-")
            months.append(int(y) * 12 + int(m))
        except (ValueError, AttributeError):
            return False
    return all(b - a == 3 for a, b in zip(months, months[1:]))


# ---------------------------------------------------------------------------
# Ratio computation
# ---------------------------------------------------------------------------

def _result(ratio, value, formula, inputs, period, period_type,
            price_as_of=None, reason=None, extra=None):
    result = {
        "ratio": ratio,
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


def _missing(ratio, reason, period, period_type, inputs=None,
             price_as_of=None):
    definition = RATIO_DEFINITIONS[ratio]
    return _result(ratio, None, definition["formula"], inputs or {},
                   period, period_type, price_as_of=price_as_of,
                   reason=reason)


def _div(numerator, denominator, ratio, period, period_type, inputs,
         price_as_of=None, extra=None):
    """Divide with missing/zero handling. Returns a ratio result."""
    definition = RATIO_DEFINITIONS[ratio]
    if numerator is None or denominator is None:
        missing_inputs = [k for k, v in inputs.items() if v is None]
        return _missing(
            ratio,
            f"missing input(s): {', '.join(missing_inputs)}",
            period, period_type, inputs, price_as_of)
    if denominator == 0:
        return _missing(ratio, "denominator is zero", period,
                        period_type, inputs, price_as_of)
    return _result(ratio, numerator / denominator,
                   definition["formula"], inputs, period, period_type,
                   price_as_of=price_as_of, extra=extra)


def compute_ratios(values, ticker, period=None, period_type="annual",
                   price_input=None, prior_values=None):
    """Compute the Step 17 ratio set for one period.

    ``values`` is the dict returned by ``load_standardised_values``.
    ``period`` defaults to the latest period of ``period_type``.
    ``price_input`` is a dict from ``load_snapshot_price`` or supplied
    by the caller; price-dependent ratios are missing without it.
    ``prior_values`` (optional) is a second ``values`` dict holding the
    prior period's balance rows, used for average ROE / ROA / ROIC;
    when omitted, the prior period is taken from ``values`` itself
    (the period before ``period`` of the same period_type).
    Returns {"ticker", "reporting_period", "period_type",
    "price_as_of", "ratios": {name: result}, "warnings": [...]}.
    """
    warnings = []
    if period is None:
        period = _latest_period(values, "income_statement", period_type)
    if period is None:
        return {"ticker": ticker, "reporting_period": None,
                "period_type": period_type, "price_as_of": None,
                "ratios": {}, "warnings": ["no income rows found"]}

    price_as_of = price_input.get("as_of_date") if price_input else None

    if period_type == "ttm":
        ttm, error = build_ttm_values(values, period)
        if ttm is None:
            return {"ticker": ticker, "reporting_period": period,
                    "period_type": "ttm", "price_as_of": price_as_of,
                    "ratios": {}, "warnings": [error]}
        warnings.append(f"TTM built by {ttm['method']}")
        flow = ttm["flow"]
        bal = ttm["balance"]
        bal_period = ttm["balance_period"]
        # Prior-period balances for averages: balance row one year
        # before the TTM balance period.
        prior_bal = {}
        if bal_period:
            y, m, d = bal_period.split("-")
            prior_bal_period = f"{int(y) - 1}-{m}-{d}"
            for concept in ("total_assets", "total_equity",
                            "total_debt", "cash_and_equivalents"):
                prior_bal[concept] = _num(_get(
                    values, "balance_sheet", prior_bal_period,
                    "quarterly", concept))
            if all(v is None for v in prior_bal.values()):
                # Fall back to the annual balance row a year earlier.
                annual_bal = _periods(values, "balance_sheet", "annual")
                earlier = [p for p in annual_bal if p < bal_period]
                if earlier:
                    for concept in ("total_assets", "total_equity",
                                    "total_debt", "cash_and_equivalents"):
                        prior_bal[concept] = _num(_get(
                            values, "balance_sheet", earlier[-1],
                            "annual", concept))
        get_flow = flow.get
        get_bal = bal.get
        get_prior_bal = prior_bal.get
        # EPS for a TTM: net_income_ttm / diluted shares (latest
        # quarterly diluted share count is a point-in-time average for
        # that quarter, so this is an approximation — recorded).
        shares_d = _num(_get(values, "income_statement", period,
                             "quarterly", "shares_outstanding_diluted"))
        eps_d = (flow.get("net_income") / shares_d
                 if flow.get("net_income") is not None and shares_d
                 else None)
        if eps_d is not None:
            warnings.append(
                "TTM diluted EPS approximated as net_income_ttm / "
                "latest-quarter diluted shares")
        inputs_flow = {c: flow.get(c) for c in FLOW_CONCEPTS}
    else:
        get_flow = lambda c: _num(_get(
            values, _statement_for(c), period, period_type, c))
        get_bal = lambda c: _num(_get(
            values, "balance_sheet", period, period_type, c))
        # Prior period balances for averages.
        periods = _periods(values, "balance_sheet", period_type)
        prior_period = None
        if period in periods:
            idx = periods.index(period)
            prior_period = periods[idx - 1] if idx > 0 else None
        if prior_period:
            def get_prior_bal(c, _pp=prior_period):
                return _num(_get(values, "balance_sheet", _pp,
                                 period_type, c))
        else:
            def get_prior_bal(c):
                return None
        eps_d = _num(_get(values, "income_statement", period,
                          period_type, "eps_diluted"))
        if eps_d is None:
            # Fall back to net_income / diluted shares, documented.
            net = get_flow("net_income")
            sh = _num(_get(values, "income_statement", period,
                           period_type, "shares_outstanding_diluted"))
            if net is not None and sh:
                eps_d = net / sh
                warnings.append(
                    "diluted EPS not in standardised data; computed "
                    "as net_income / shares_outstanding_diluted")
        inputs_flow = {c: get_flow(c) for c in FLOW_CONCEPTS}

    revenue = get_flow("revenue")
    ratios = {}

    # -- Valuation (need a price) -------------------------------------
    if price_input is None:
        for name in ("pe_ratio", "pb_ratio", "ps_ratio", "ev_ebit",
                     "ev_ebitda"):
            ratios[name] = _missing(
                name, "no price input supplied", period, period_type)
    else:
        price = price_input["price"]
        # P/E
        ratios["pe_ratio"] = _div(
            price, eps_d, "pe_ratio", period, period_type,
            {"price": price, "eps_diluted": eps_d},
            price_as_of=price_as_of)
        # P/B
        equity = get_bal("total_equity")
        shares_d = _num(_get(values, "income_statement", period,
                             period_type, "shares_outstanding_diluted")) \
            if period_type != "ttm" else _num(_get(
                values, "income_statement", period, "quarterly",
                "shares_outstanding_diluted"))
        book_per_share = (equity / shares_d
                          if equity is not None and shares_d else None)
        ratios["pb_ratio"] = _div(
            price, book_per_share, "pb_ratio", period, period_type,
            {"price": price, "total_equity": equity,
             "shares_outstanding_diluted": shares_d,
             "book_value_per_share": book_per_share},
            price_as_of=price_as_of)
        # P/S
        mcap, mcap_method = market_cap_for(price_input, shares_d)
        ratios["ps_ratio"] = _div(
            mcap, revenue, "ps_ratio", period, period_type,
            {"market_cap": mcap, "market_cap_method": mcap_method,
             "revenue": revenue},
            price_as_of=price_as_of)
        # EV / EBIT
        total_debt = get_bal("total_debt")
        cash = get_bal("cash_and_equivalents")
        if mcap is None or total_debt is None or cash is None:
            missing = [k for k, v in
                       {"market_cap": mcap, "total_debt": total_debt,
                        "cash_and_equivalents": cash}.items()
                       if v is None]
            ratios["ev_ebit"] = _missing(
                "ev_ebit", f"missing input(s): {', '.join(missing)}",
                period, period_type,
                {"market_cap": mcap, "total_debt": total_debt,
                 "cash_and_equivalents": cash},
                price_as_of=price_as_of)
        else:
            ev = mcap + total_debt - cash
            ratios["ev_ebit"] = _div(
                ev, get_flow("operating_income"), "ev_ebit", period,
                period_type,
                {"enterprise_value": ev, "market_cap": mcap,
                 "total_debt": total_debt,
                 "cash_and_equivalents": cash,
                 "operating_income": get_flow("operating_income")},
                price_as_of=price_as_of)
        # EV / EBITDA: unavailable — no EBITDA concept exists.
        ratios["ev_ebitda"] = _missing(
            "ev_ebitda", UNAVAILABLE_RATIOS["ev_ebitda"], period,
            period_type, {"operating_income": get_flow("operating_income")},
            price_as_of=price_as_of)

    # -- Profitability --------------------------------------------------
    ratios["gross_margin"] = _div(
        get_flow("gross_profit"), revenue, "gross_margin", period,
        period_type, {"gross_profit": get_flow("gross_profit"),
                      "revenue": revenue})
    ratios["operating_margin"] = _div(
        get_flow("operating_income"), revenue, "operating_margin",
        period, period_type,
        {"operating_income": get_flow("operating_income"),
         "revenue": revenue})
    ratios["net_margin"] = _div(
        get_flow("net_income"), revenue, "net_margin", period,
        period_type, {"net_income": get_flow("net_income"),
                      "revenue": revenue})

    # -- Returns (average balances when a prior period exists) --------
    def _average(current, prior):
        if current is None:
            return None, None
        if prior is None:
            return current, "period_end"
        return (current + prior) / 2, "average"

    equity = get_bal("total_equity")
    assets = get_bal("total_assets")
    net_income = get_flow("net_income")
    equity_avg, equity_basis = _average(equity, get_prior_bal("total_equity"))
    assets_avg, assets_basis = _average(assets, get_prior_bal("total_assets"))
    ratios["roe"] = _div(
        net_income, equity_avg, "roe", period, period_type,
        {"net_income": net_income, "total_equity": equity,
         "average_total_equity": equity_avg},
        extra={"basis": equity_basis})
    ratios["roa"] = _div(
        net_income, assets_avg, "roa", period, period_type,
        {"net_income": net_income, "total_assets": assets,
         "average_total_assets": assets_avg},
        extra={"basis": assets_basis})

    # ROIC
    operating_income = get_flow("operating_income")
    income_tax = get_flow("income_tax")
    income_before_tax = get_flow("income_before_tax")
    total_debt = get_bal("total_debt")
    cash = get_bal("cash_and_equivalents")
    if None in (operating_income, income_tax, income_before_tax,
                total_debt, equity, cash):
        missing = [k for k, v in {
            "operating_income": operating_income,
            "income_tax": income_tax,
            "income_before_tax": income_before_tax,
            "total_debt": total_debt, "total_equity": equity,
            "cash_and_equivalents": cash}.items() if v is None]
        ratios["roic"] = _missing(
            "roic", f"missing input(s): {', '.join(missing)}", period,
            period_type,
            {"operating_income": operating_income,
             "income_tax": income_tax,
             "income_before_tax": income_before_tax,
             "total_debt": total_debt, "total_equity": equity,
             "cash_and_equivalents": cash})
    elif income_before_tax == 0:
        ratios["roic"] = _missing(
            "roic", "denominator is zero (income_before_tax)", period,
            period_type,
            {"operating_income": operating_income,
             "income_tax": income_tax,
             "income_before_tax": income_before_tax})
    else:
        tax_rate = income_tax / income_before_tax
        nopat = operating_income * (1 - tax_rate)
        ic_now = total_debt + equity - cash
        prior_debt = get_prior_bal("total_debt")
        prior_equity = get_prior_bal("total_equity")
        prior_cash = get_prior_bal("cash_and_equivalents")
        if None in (prior_debt, prior_equity, prior_cash):
            ic_avg, ic_basis = ic_now, "period_end"
        else:
            ic_avg = (ic_now + (prior_debt + prior_equity - prior_cash)) / 2
            ic_basis = "average"
        ratios["roic"] = _div(
            nopat, ic_avg, "roic", period, period_type,
            {"nopat": nopat, "effective_tax_rate": tax_rate,
             "operating_income": operating_income,
             "invested_capital": ic_now,
             "average_invested_capital": ic_avg},
            extra={"basis": ic_basis})

    # -- Leverage -------------------------------------------------------
    ratios["debt_to_equity"] = _div(
        total_debt, equity, "debt_to_equity", period, period_type,
        {"total_debt": total_debt, "total_equity": equity})
    ratios["interest_coverage"] = _missing(
        "interest_coverage", UNAVAILABLE_RATIOS["interest_coverage"],
        period, period_type,
        {"operating_income": operating_income})

    # -- Liquidity ------------------------------------------------------
    current_assets = get_bal("current_assets")
    current_liabilities = get_bal("current_liabilities")
    inventory = get_bal("inventory")
    ratios["current_ratio"] = _div(
        current_assets, current_liabilities, "current_ratio", period,
        period_type, {"current_assets": current_assets,
                      "current_liabilities": current_liabilities})
    if current_assets is None or inventory is None \
            or current_liabilities is None:
        missing = [k for k, v in {
            "current_assets": current_assets, "inventory": inventory,
            "current_liabilities": current_liabilities}.items()
            if v is None]
        ratios["quick_ratio"] = _missing(
            "quick_ratio", f"missing input(s): {', '.join(missing)}",
            period, period_type,
            {"current_assets": current_assets, "inventory": inventory,
             "current_liabilities": current_liabilities})
    else:
        ratios["quick_ratio"] = _div(
            current_assets - inventory, current_liabilities,
            "quick_ratio", period, period_type,
            {"current_assets": current_assets, "inventory": inventory,
             "current_liabilities": current_liabilities})

    return {"ticker": ticker, "reporting_period": period,
            "period_type": period_type, "price_as_of": price_as_of,
            "ratios": ratios, "warnings": warnings}


def compute_all_periods(conn, ticker, price_input=None):
    """Annual ratios for every annual period, plus the latest TTM.

    Price-dependent ratios are computed only for the latest annual
    period (the price is as of one date and must not be applied to
    earlier years as if it were the price then).  Earlier periods get
    fundamental ratios only.  Returns a list of per-period results.
    """
    values, load_warnings = load_standardised_values(conn, ticker)
    annual_periods = _periods(values, "income_statement", "annual")
    results = []
    for i, period in enumerate(annual_periods):
        price = price_input if i == len(annual_periods) - 1 else None
        result = compute_ratios(values, ticker, period=period,
                                period_type="annual",
                                price_input=price)
        if i == 0:
            result["warnings"].extend(load_warnings)
        results.append(result)
    latest_q = _latest_period(values, "income_statement", "quarterly")
    if latest_q:
        results.append(compute_ratios(
            values, ticker, period=latest_q, period_type="ttm",
            price_input=price_input))
    return results
