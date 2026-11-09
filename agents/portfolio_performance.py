"""Step 36 — Portfolio performance and risk analysis.

Pure analysis over caller-supplied inputs: holdings, price
histories and (optionally) a benchmark history and dividends.
It never fetches prices, never touches the network, never
writes to the production database and never places a trade.

Inputs
------
* ``holdings`` — a list of dicts, one per position:
  ``ticker`` (required), ``shares`` (remaining shares held,
  required), ``cost_basis`` (price paid per share, required),
  ``purchase_date`` (ISO date, optional), ``sector`` /
  ``country`` (optional, for concentration), ``dividends``
  (total dividends received on the position, optional, 0 by
  default), ``sold`` (optional list of ``{"shares", "price",
  "date"}`` — shares already sold, for realised gains).
* ``price_history`` — ``{ticker: [(date, price), ...]}`` (dates
  as ISO strings, sorted or not — the module sorts them).
  The latest price in a ticker's history is its current
  price; a caller-supplied ``current_prices`` dict overrides
  it.  A live price feed is a Step 38 concern; this module
  takes history as given and labels its period on every
  output.
* ``benchmark_history`` — optional ``[(date, value), ...]``
  (e.g. S&P 500) used only for beta.
* ``currency`` — never assumed; ``None`` produces a warning,
  following Step 28 (the owner has not confirmed whether the
  $1,000 basket is SGD or USD).

Every metric is a dict with ``value``, ``status`` (``"ok"`` /
``"missing"``), ``reason`` when missing, and the inputs and
period it rests on.  A metric that cannot be computed from
the history supplied is missing with a reason — never
estimated from a single price point.  In particular:

* return needs a cost basis and a current price;
* volatility, drawdown and time-weighted return need at
  least two dated points;
* beta needs at least two overlapping dated returns with
  the benchmark;
* money-weighted return (IRR) needs dated cash flows and a
  converging solution.

Concentration reuses Step 28's judgement thresholds
(``SINGLE_NAME_WARN`` 25%, ``SECTOR_WARN`` 40%,
``COUNTRY_WARN`` 60%) but is assessed on current value,
not cost — Step 28's ``concentration_check`` assesses a
proposal's cost; a live portfolio concentrates on what it
is worth now, so the same thresholds are applied to current
values here and the difference is stated on the output.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from datetime import date

from agents.portfolio_construction import (
    COUNTRY_WARN,
    SECTOR_WARN,
    SINGLE_NAME_WARN,
)

# ---------------------------------------------------------------------------
# Defaults (judgement, stated on every output)
# ---------------------------------------------------------------------------

DEFAULT_BENCHMARK_LABEL = "benchmark"

PERFORMANCE_DISCLAIMER = (
    "Performance and risk analysis only — no trade has been "
    "placed and nothing here is a recommendation.  A trade is "
    "placed only after the owner's approval for that specific "
    "exact order.  Past performance is not a guide to future "
    "returns; the final decision is always the owner's.")

MISSING_HISTORY_REASON = (
    "no usable price history supplied for this ticker — "
    "performance needs at least two dated prices (a live "
    "price feed is a Step 38 concern)")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _money(value):
    """Round money to cents; None stays None."""
    if value is None:
        return None
    return round(float(value), 2)


def _parse_date(value):
    """Parse an ISO date string (or a ``date``) to a ``date``."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _missing(reason, **extra):
    """A missing metric, with its reason and context."""
    result = {"value": None, "status": "missing", "reason": reason}
    result.update(extra)
    return result


def _ok(value, **extra):
    """A computed metric, with its context."""
    result = {"value": value, "status": "ok"}
    result.update(extra)
    return result


def _normalise_history(history):
    """Sort a [(date, price), ...] history and drop bad points.

    Returns ``[(date, price), ...]`` sorted by date, with
    non-positive prices and unparseable dates dropped.  A
    history with fewer than two distinct dates cannot support
    any return, volatility or drawdown computation.
    """
    points = []
    for item in history or []:
        try:
            when, price = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0:
            continue
        try:
            when = _parse_date(when)
        except ValueError:
            continue
        points.append((when, price))
    points.sort(key=lambda p: p[0])
    # Keep the last price per date (a re-stated close replaces).
    deduped = {}
    for when, price in points:
        deduped[when] = price
    return sorted(deduped.items())


def _period_returns(history):
    """Simple returns between consecutive dated prices.

    Returns ``[(date, return), ...]`` — one return per step,
    dated at the later price's date.
    """
    points = _normalise_history(history)
    returns = []
    for (prev_date, prev_price), (when, price) in zip(points, points[1:]):
        if prev_price <= 0:
            continue
        returns.append((when, price / prev_price - 1.0))
    return returns


def _current_price(ticker, price_history, current_prices=None):
    """Current price for a ticker: override, else latest history."""
    if current_prices and ticker in current_prices:
        price = current_prices[ticker]
        return (float(price), "caller-supplied current price")
    points = _normalise_history((price_history or {}).get(ticker, []))
    if not points:
        return (None, None)
    return (points[-1][1], "latest price in supplied history "
            f"({points[-1][0].isoformat()})")


# ---------------------------------------------------------------------------
# Position performance
# ---------------------------------------------------------------------------

def position_performance(holding, price_history, current_prices=None):
    """Return, value and income for one holding.

    ``holding`` needs ``ticker``, ``shares`` and ``cost_basis``.
    ``sold`` (optional) is a list of ``{"shares", "price",
    "date"}`` for shares already sold — their gain is realised,
    the remaining shares' gain is unrealised.  Dividends are
    taken from ``holding["dividends"]`` (total received, 0 by
    default).  Returns a dict with per-position figures plus
    ``status`` / ``reason`` when the position cannot be valued
    (no current price, no cost basis).
    """
    ticker = (holding.get("ticker") or "").upper()
    try:
        shares = float(holding.get("shares") or 0)
        cost_basis = float(holding.get("cost_basis") or 0)
    except (TypeError, ValueError):
        shares, cost_basis = 0.0, 0.0
    dividends = float(holding.get("dividends") or 0)

    sold = holding.get("sold") or []
    sold_shares = 0.0
    realised_gain = 0.0
    for sale in sold:
        try:
            n = float(sale.get("shares") or 0)
            price = float(sale.get("price") or 0)
        except (TypeError, ValueError):
            continue
        sold_shares += n
        realised_gain += n * (price - cost_basis)

    total_shares = shares + sold_shares
    total_cost = total_shares * cost_basis

    price, price_source = _current_price(
        ticker, price_history, current_prices)
    if price is None:
        return {
            "ticker": ticker,
            "status": "missing",
            "reason": MISSING_HISTORY_REASON,
            "shares": shares,
            "cost_basis": _money(cost_basis),
            "total_cost": _money(total_cost),
            "current_value": None,
            "unrealised_gain": None,
            "realised_gain": _money(realised_gain),
            "total_gain": None,
            "total_return": None,
            "dividends": _money(dividends),
            "yield_on_cost": None,
            "yield_on_value": None,
            "price": None,
            "price_source": None,
        }
    current_value = shares * price
    unrealised_gain = current_value - shares * cost_basis
    total_gain = unrealised_gain + realised_gain
    denominator = total_cost if total_cost > 0 else None
    return {
        "ticker": ticker,
        "status": "ok",
        "shares": shares,
        "sold_shares": sold_shares,
        "cost_basis": _money(cost_basis),
        "total_cost": _money(total_cost),
        "current_value": _money(current_value),
        "unrealised_gain": _money(unrealised_gain),
        "realised_gain": _money(realised_gain),
        "total_gain": _money(total_gain),
        "total_return": (total_gain / denominator
                         if denominator else None),
        "dividends": _money(dividends),
        "yield_on_cost": (dividends / total_cost
                          if total_cost > 0 else None),
        "yield_on_value": (dividends / current_value
                           if current_value > 0 else None),
        "price": price,
        "price_source": price_source,
        "purchase_date": holding.get("purchase_date"),
        "sector": holding.get("sector"),
        "country": holding.get("country"),
    }


# ---------------------------------------------------------------------------
# Portfolio value series, time-weighted return, drawdown
# ---------------------------------------------------------------------------

def portfolio_value_series(holdings, price_history):
    """Dated portfolio values from per-ticker price histories.

    The series starts at the first date on which every holding
    has a price (a ticker with no history is excluded with a
    reason, never priced at zero).  On later dates a ticker's
    price is carried forward from its latest price on or before
    that date.  Returns ``{"values": [(date, value), ...],
    "excluded": [...], "status": ...}`` — ``"missing"`` when
    fewer than two dated values result.
    """
    included = []
    excluded = []
    for holding in holdings or []:
        ticker = (holding.get("ticker") or "").upper()
        points = _normalise_history((price_history or {}).get(ticker, []))
        if not points:
            excluded.append({
                "ticker": ticker,
                "reason": MISSING_HISTORY_REASON,
            })
            continue
        try:
            shares = float(holding.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        included.append((ticker, shares, points))
    if not included:
        return {"values": [], "excluded": excluded,
                "status": "missing",
                "reason": "no holding has a usable price history"}
    # First date on which every included ticker has a price.
    start = max(points[0][0] for _, _, points in included)
    dates = sorted({when for _, _, points in included
                    for when, _ in points if when >= start})
    values = []
    for when in dates:
        total = 0.0
        for _, shares, points in included:
            price = None
            for d, p in points:
                if d <= when:
                    price = p
                else:
                    break
            if price is None:
                total = None
                break
            total += shares * price
        if total is not None:
            values.append((when, round(total, 2)))
    if len(values) < 2:
        return {"values": values, "excluded": excluded,
                "status": "missing",
                "reason": "fewer than two dated portfolio values "
                          "can be built from the supplied histories"}
    return {"values": values, "excluded": excluded, "status": "ok",
            "period": {"from": values[0][0].isoformat(),
                       "to": values[-1][0].isoformat(),
                       "points": len(values)}}


def time_weighted_return(values):
    """Time-weighted return from a dated value series.

    Geometrically links the sub-period returns — the return
    of the portfolio itself, independent of when money moved
    in or out (cash flows are NOT in this series; dividends
    are excluded, stated here).  ``values`` is ``[(date,
    value), ...]``.  Needs at least two points and a positive
    starting value.
    """
    if not values or len(values) < 2:
        return _missing(
            "time-weighted return needs at least two dated "
            "portfolio values", period=None)
    if values[0][1] <= 0:
        return _missing(
            "time-weighted return needs a positive starting "
            "portfolio value", period=None)
    linked = 1.0
    for (_, prev), (_, value) in zip(values, values[1:]):
        if prev <= 0:
            return _missing(
                "time-weighted return needs positive portfolio "
                "values at every step", period=None)
        linked *= value / prev
    return _ok(linked - 1.0,
               period={"from": values[0][0].isoformat(),
                       "to": values[-1][0].isoformat(),
                       "points": len(values)},
               note="price return only — dividends excluded")


def _npv(rate, cash_flows):
    """Net present value of dated cash flows at ``rate``.

    ``cash_flows`` is ``[(date, amount), ...]`` with the first
    date as the base; amounts are signed (negative = money
    in, positive = money out, from the portfolio's point of
    view the first flow is normally negative — the purchase).
    Times are measured in years (actual days / 365.25).
    """
    base = cash_flows[0][0]
    total = 0.0
    for when, amount in cash_flows:
        years = (when - base).days / 365.25
        total += amount / ((1.0 + rate) ** years)
    return total


def money_weighted_return(cash_flows):
    """Money-weighted return (IRR) from dated cash flows.

    ``cash_flows`` is ``[(date, amount), ...]`` — negative for
    money put in, positive for money taken out or the final
    portfolio value.  Solved by bisection on NPV = 0 over
    (-0.9999, 10.0); no sign change in that range means no
    solution and the metric is missing with a reason, never a
    guessed rate.  Needs at least two flows on different
    dates and both a negative and a positive flow.
    """
    flows = []
    for item in cash_flows or []:
        try:
            when = _parse_date(item[0])
            amount = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        flows.append((when, amount))
    flows.sort(key=lambda f: f[0])
    if len(flows) < 2:
        return _missing(
            "money-weighted return needs at least two dated "
            "cash flows")
    if len({when for when, _ in flows}) < 2:
        return _missing(
            "money-weighted return needs cash flows on at "
            "least two different dates")
    signs = {1 if amount > 0 else -1 if amount < 0 else 0
             for _, amount in flows}
    if not ({1, -1} <= signs):
        return _missing(
            "money-weighted return needs both money in and "
            "money out (a purchase and a sale or final value)")
    low, high = -0.9999, 10.0
    npv_low, npv_high = _npv(low, flows), _npv(high, flows)
    if npv_low * npv_high > 0:
        return _missing(
            "money-weighted return has no solution in "
            "(-99.99%, +1000%): NPV does not change sign")
    for _ in range(200):
        mid = (low + high) / 2.0
        npv_mid = _npv(mid, flows)
        if abs(npv_mid) < 1e-9:
            break
        if npv_low * npv_mid <= 0:
            high = mid
        else:
            low, npv_low = mid, npv_mid
    else:
        mid = (low + high) / 2.0
    return _ok((low + high) / 2.0,
               period={"from": flows[0][0].isoformat(),
                       "to": flows[-1][0].isoformat(),
                       "flows": len(flows)},
               note="annualised IRR by bisection on NPV = 0")


# ---------------------------------------------------------------------------
# Risk: volatility, drawdown, beta
# ---------------------------------------------------------------------------

def volatility(returns, periods_per_year=None):
    """Volatility (sample standard deviation) of period returns.

    ``returns`` is a list of floats (or ``(date, return)``
    pairs — the dates are dropped).  Needs at least two
    returns.  Without ``periods_per_year`` the result is the
    per-period volatility and annualisation is missing with
    a reason; with it (252 for daily, 12 for monthly) the
    annualised figure is ``stdev * sqrt(periods_per_year)``.
    """
    values = [r[1] if isinstance(r, (tuple, list)) else r
              for r in (returns or [])]
    values = [float(v) for v in values]
    if len(values) < 2:
        return _missing(
            "volatility needs at least two period returns",
            periods=len(values))
    per_period = statistics.stdev(values)
    result = _ok(per_period, periods=len(values),
                 note="sample standard deviation of period returns")
    if periods_per_year:
        result["annualised"] = per_period * math.sqrt(
            float(periods_per_year))
        result["periods_per_year"] = periods_per_year
    else:
        result["annualised"] = None
        result["annualised_reason"] = (
            "periods per year not supplied — pass 252 for daily "
            "or 12 for monthly prices to annualise")
    return result


def max_drawdown(values):
    """Maximum peak-to-trough drawdown of a dated value series.

    ``values`` is ``[(date, value), ...]``.  Returns the
    largest fall from a running peak, as a negative fraction,
    with the peak and trough dates.  Needs at least two
    points and positive values.
    """
    if not values or len(values) < 2:
        return _missing(
            "maximum drawdown needs at least two dated values")
    peak = values[0][1]
    peak_date = values[0][0]
    worst = 0.0
    worst_peak = values[0][0]
    worst_trough = values[0][0]
    for when, value in values:
        if value <= 0:
            return _missing(
                "maximum drawdown needs positive values at "
                "every step")
        if value > peak:
            peak, peak_date = value, when
        drawdown = value / peak - 1.0
        if drawdown < worst:
            worst = drawdown
            worst_peak, worst_trough = peak_date, when
    return _ok(worst,
               peak_date=worst_peak.isoformat()
               if hasattr(worst_peak, "isoformat") else worst_peak,
               trough_date=worst_trough.isoformat()
               if hasattr(worst_trough, "isoformat") else worst_trough,
               period={"from": values[0][0].isoformat()
                       if hasattr(values[0][0], "isoformat")
                       else values[0][0],
                       "to": values[-1][0].isoformat()
                       if hasattr(values[-1][0], "isoformat")
                       else values[-1][0]})


def beta(asset_returns, benchmark_returns):
    """Beta of an asset vs a benchmark, from dated returns.

    Both arguments are ``[(date, return), ...]``; only dates
    present in both series are used.  Beta = cov(asset,
    benchmark) / var(benchmark) (sample covariance and
    variance).  Needs at least two overlapping dates and
    non-zero benchmark variance.  Returns the beta plus the
    overlapping dates and the benchmark variance, so the
    estimate can be judged — a beta from two points is a
    number, not a conclusion.
    """
    # The inputs are price histories: convert to returns.
    asset_rets = _period_returns(asset_returns)
    bench_rets = _period_returns(benchmark_returns)
    asset_map = dict(asset_rets)
    bench_map = dict(bench_rets)
    common = sorted(set(asset_map) & set(bench_map))
    if len(common) < 2:
        return _missing(
            "beta needs at least two dates shared by the "
            "asset and benchmark return series",
            overlapping_dates=len(common))
    a = [asset_map[d] for d in common]
    b = [bench_map[d] for d in common]
    var_b = statistics.pvariance(b)
    if var_b == 0:
        return _missing(
            "beta is undefined: the benchmark returns do not "
            "vary over the overlapping dates",
            overlapping_dates=len(common))
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b)
              for x, y in zip(a, b)) / len(a)
    return _ok(cov / var_b,
               overlapping_dates=len(common),
               period={"from": common[0].isoformat(),
                       "to": common[-1].isoformat()},
               benchmark_variance=var_b,
               note="population covariance/variance on the "
                    "overlapping dates")


# ---------------------------------------------------------------------------
# Concentration (current value, Step 28 thresholds)
# ---------------------------------------------------------------------------

def concentration(holdings, price_history, current_prices=None):
    """Concentration of a live portfolio on current value.

    Mirrors Step 28's ``concentration_check`` thresholds
    (single name > 25%, sector > 40%, country > 60%) but
    weights by current value, not cost — a live portfolio
    concentrates on what it is worth now.  Positions that
    cannot be valued are listed as unvalued with their
    reason, never priced at zero.
    """
    positions = []
    unvalued = []
    for holding in holdings or []:
        perf = position_performance(holding, price_history,
                                    current_prices)
        if perf["status"] != "ok":
            unvalued.append({"ticker": perf["ticker"],
                             "reason": perf["reason"]})
            continue
        positions.append(perf)
    total = sum(p["current_value"] for p in positions)
    if total <= 0 or not positions:
        return {
            "total_value": 0.0,
            "max_single_name_weight": None,
            "max_sector": None,
            "max_country": None,
            "warnings": ["no valued positions: concentration "
                         "cannot be assessed"],
            "unvalued": unvalued,
            "thresholds": {"single_name": SINGLE_NAME_WARN,
                           "sector": SECTOR_WARN,
                           "country": COUNTRY_WARN},
            "basis": "current value (Step 28 assesses proposals "
                     "on cost)",
        }
    by_sector = Counter()
    by_country = Counter()
    unknown_sector = 0.0
    unknown_country = 0.0
    max_single = 0.0
    max_single_ticker = None
    for p in positions:
        weight = p["current_value"] / total
        if weight > max_single:
            max_single, max_single_ticker = weight, p["ticker"]
        sector = p.get("sector") or "unknown"
        country = p.get("country") or "unknown"
        by_sector[sector] += p["current_value"]
        by_country[country] += p["current_value"]
        if sector == "unknown":
            unknown_sector += p["current_value"]
        if country == "unknown":
            unknown_country += p["current_value"]
    sector, sector_value = max(by_sector.items(),
                               key=lambda kv: kv[1])
    country, country_value = max(by_country.items(),
                                 key=lambda kv: kv[1])
    warnings = []
    if max_single > SINGLE_NAME_WARN:
        warnings.append(
            f"concentrated in one name: {max_single_ticker} is "
            f"{max_single:.1%} of the portfolio by value "
            f"(warn above {SINGLE_NAME_WARN:.0%})")
    if sector != "unknown" and sector_value / total > SECTOR_WARN:
        warnings.append(
            f"concentrated in one sector: {sector} is "
            f"{sector_value / total:.1%} of the portfolio by "
            f"value (warn above {SECTOR_WARN:.0%})")
    if country != "unknown" and country_value / total > COUNTRY_WARN:
        warnings.append(
            f"concentrated in one country: {country} is "
            f"{country_value / total:.1%} of the portfolio by "
            f"value (warn above {COUNTRY_WARN:.0%})")
    if unknown_sector > 0:
        warnings.append(
            f"sector unknown for {unknown_sector / total:.1%} of "
            "the portfolio: concentration cannot be fully assessed")
    if unknown_country > 0:
        warnings.append(
            f"country unknown for {unknown_country / total:.1%} of "
            "the portfolio: concentration cannot be fully assessed")
    if unvalued:
        warnings.append(
            f"{len(unvalued)} position(s) could not be valued "
            "and are excluded from concentration")
    return {
        "total_value": _money(total),
        "max_single_name_weight": max_single,
        "max_single_name_ticker": max_single_ticker,
        "max_sector": {"sector": sector,
                       "weight": sector_value / total},
        "max_country": {"country": country,
                        "weight": country_value / total},
        "warnings": warnings,
        "unvalued": unvalued,
        "thresholds": {"single_name": SINGLE_NAME_WARN,
                       "sector": SECTOR_WARN,
                       "country": COUNTRY_WARN},
        "basis": "current value (Step 28 assesses proposals on cost)",
    }


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------

def portfolio_summary(holdings, price_history, current_prices=None,
                      benchmark_history=None, cash_flows=None,
                      periods_per_year=None, currency=None):
    """Full performance and risk summary for a portfolio.

    ``holdings`` and ``price_history`` as in
    :func:`position_performance`; ``benchmark_history`` for
    beta (portfolio beta is the value-weighted mean of the
    per-position betas that can be computed); ``cash_flows``
    for the money-weighted return; ``periods_per_year`` for
    annualised volatility.  Returns a dict with positions,
    totals, returns (time-weighted and money-weighted),
    risk (volatility, drawdown, beta, concentration), income
    and risk flags — each metric carrying its status, reason
    and period where it applies.
    """
    positions = [position_performance(h, price_history,
                                      current_prices)
                 for h in (holdings or [])]
    valued = [p for p in positions if p["status"] == "ok"]
    total_cost = sum(p["total_cost"] for p in valued)
    total_value = sum(p["current_value"] for p in valued)
    total_gain = sum(p["total_gain"] for p in valued)
    realised = sum(p["realised_gain"] for p in valued)
    unrealised = sum(p["unrealised_gain"] for p in valued)
    dividends = sum(p["dividends"] for p in valued)

    series = portfolio_value_series(holdings, price_history)
    if series["status"] == "ok":
        values = series["values"]
        twr = time_weighted_return(values)
        drawdown = max_drawdown(values)
        all_returns = [v / prev - 1.0
                       for (_, prev), (_, v) in zip(values, values[1:])]
        vol = volatility(all_returns,
                         periods_per_year=periods_per_year)
    else:
        values = []
        twr = _missing(series["reason"])
        drawdown = _missing(series["reason"])
        vol = _missing(series["reason"])

    if cash_flows:
        mwr = money_weighted_return(cash_flows)
    else:
        mwr = _missing("no cash flows supplied — money-weighted "
                       "return needs dated cash flows")

    if benchmark_history:
        betas = []
        for p in valued:
            b = beta(price_history.get(p["ticker"], []),
                     benchmark_history)
            if b["status"] == "ok":
                betas.append((p["current_value"], b["value"]))
        if betas and total_value > 0:
            portfolio_beta = _ok(
                sum(w * b for w, b in betas) / total_value,
                note="value-weighted mean of the per-position "
                     "betas that could be computed "
                     f"({len(betas)} of {len(valued)} positions)",
                positions_with_beta=len(betas),
                positions_total=len(valued))
        else:
            portfolio_beta = _missing(
                "no position beta could be computed against "
                "the supplied benchmark history")
    else:
        portfolio_beta = _missing(
            "no benchmark history supplied — beta needs a "
            "benchmark series")

    conc = concentration(holdings, price_history, current_prices)

    gains = sorted(valued, key=lambda p: p["total_gain"],
                   reverse=True)
    contributors = [{"ticker": p["ticker"],
                     "gain": p["total_gain"],
                     "return": p["total_return"]}
                    for p in gains if p["total_gain"] > 0]
    detractors = [{"ticker": p["ticker"],
                   "gain": p["total_gain"],
                   "return": p["total_return"]}
                  for p in gains if p["total_gain"] < 0]

    flags = list(conc["warnings"])
    if drawdown["status"] == "ok" and drawdown["value"] <= -0.20:
        flags.append(
            f"large drawdown: portfolio fell "
            f"{drawdown['value']:.1%} from its peak "
            f"({drawdown['peak_date']} to "
            f"{drawdown['trough_date']})")
    if total_cost > 0 and total_gain / total_cost < 0:
        flags.append("portfolio is below cost overall")
    if currency is None:
        flags.append(
            "currency not supplied — amounts are in the "
            "currency of the inputs, never assumed")

    return {
        "status": "ok" if valued else "insufficient_data",
        "positions": positions,
        "positions_valued": len(valued),
        "positions_total": len(positions),
        "total_cost": _money(total_cost),
        "total_value": _money(total_value),
        "total_gain": _money(total_gain),
        "total_return": (total_gain / total_cost
                         if total_cost > 0 else None),
        "realised_gain": _money(realised),
        "unrealised_gain": _money(unrealised),
        "dividends_received": _money(dividends),
        "yield_on_cost": (dividends / total_cost
                          if total_cost > 0 else None),
        "yield_on_value": (dividends / total_value
                           if total_value > 0 else None),
        "time_weighted_return": twr,
        "money_weighted_return": mwr,
        "volatility": vol,
        "max_drawdown": drawdown,
        "beta": portfolio_beta,
        "concentration": conc,
        "contributors": contributors,
        "detractors": detractors,
        "risk_flags": flags,
        "value_series": {
            "values": [(d.isoformat(), v) for d, v in values],
            "excluded": series.get("excluded", []),
            "status": series["status"],
        },
        "currency": currency,
        "disclaimer": PERFORMANCE_DISCLAIMER,
    }


def summary_lines(summary):
    """One-line-per-item human-readable summary (records only)."""
    lines = []
    total_return = summary.get("total_return")
    if total_return is None:
        lines.append("Portfolio: no valued positions — "
                     "performance cannot be computed.")
        return lines
    lines.append(
        f"Portfolio: {summary['total_value']:.2f} on "
        f"{summary['total_cost']:.2f} cost "
        f"({total_return:+.1%} total return; "
        f"realised {summary['realised_gain']:.2f}, unrealised "
        f"{summary['unrealised_gain']:.2f}).")
    twr = summary.get("time_weighted_return") or {}
    if twr.get("status") == "ok":
        lines.append(f"Time-weighted return: {twr['value']:+.1%}.")
    mwr = summary.get("money_weighted_return") or {}
    if mwr.get("status") == "ok":
        lines.append(f"Money-weighted return (IRR): "
                     f"{mwr['value']:+.1%}.")
    dd = summary.get("max_drawdown") or {}
    if dd.get("status") == "ok":
        lines.append(f"Maximum drawdown: {dd['value']:.1%} "
                     f"({dd['peak_date']} to {dd['trough_date']}).")
    for flag in summary.get("risk_flags") or []:
        lines.append(f"Risk: {flag}")
    return lines


# ---------------------------------------------------------------------------
# Snapshot storage (JSON files, never the production DB)
# ---------------------------------------------------------------------------

def save_snapshot(summary, path):
    """Save a portfolio summary as JSON to ``path``."""
    path = str(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    return path


def load_snapshot(path):
    """Load a portfolio summary saved by :func:`save_snapshot`."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
