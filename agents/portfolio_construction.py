"""Step 28 — Portfolio construction and position sizing.

Builds a proposed portfolio from Step 26 detection candidates.
It reuses the detector's candidate records (ticker, price, band,
quality, margin of safety) and sizes positions — it never fetches
prices, never touches the network, never writes to the production
database and never places a trade.  Every output is a PROPOSAL
for the owner to approve or reject: the exact order is shown
first and a trade is placed only after his approval for that
specific trade.

Sizing methods
--------------
* equal weight — the default.  Every candidate gets the same
  target weight, subject to a per-position cap (water-filling:
  capped positions keep their cap and the freed capital is
  spread over the remaining positions).  Shares are whole
  numbers (floor); rounding leftovers stay as cash.
* Kelly criterion — only when the caller supplies an explicit
  edge per ticker (win probability and payoff ratio).  The edge
  is the owner's judgement, not computed here: no module in
  Jarvis can know the probability that a price moves.  The
  Kelly fraction is capped (default 25% of capital) and a
  fractional-Kelly multiplier (default 0.5, half-Kelly) is
  applied first — full Kelly is a prescription for ruinous
  volatility in a small portfolio, so it is never the default.
* fixed fraction — every position gets the same stated fraction
  of capital.

The $1,000 small-cap basket is a special case: small-cap
asymmetric bets are sized by the amount the owner is willing
to lose, not by a target weight.  A position's cost IS its
maximum loss (a small cap can go to zero), so the basket
records ``amount_at_risk`` per position and the accept-the-loss
discipline explicitly.  Currency is an input, never assumed:
the owner has not confirmed whether the $1,000 is SGD or USD,
so a basket built without a currency carries a warning.

All inputs are caller-supplied (capital, candidates, prices,
holdings).  A candidate without a price is skipped with a
reason, never guessed.
"""

from __future__ import annotations

import json
import math
from collections import Counter

# ---------------------------------------------------------------------------
# Defaults (judgement, stated on every output)
# ---------------------------------------------------------------------------

DEFAULT_MAX_POSITIONS = 10
DEFAULT_PER_POSITION_CAP = 0.20
DEFAULT_KELLY_CAP = 0.25
DEFAULT_KELLY_MULTIPLIER = 0.5
DEFAULT_BASKET_CAPITAL = 1000.0
DEFAULT_BASKET_MAX_POSITIONS = 5

# Concentration warnings (judgement thresholds, stated on output).
SINGLE_NAME_WARN = 0.25
SECTOR_WARN = 0.40
COUNTRY_WARN = 0.60

PROPOSAL_DISCLAIMER = (
    "Proposal only — no trade has been placed.  A trade is placed "
    "only after the owner's approval for that specific exact order. "
    "Candidates are worth a closer look, never buy recommendations; "
    "the final decision is always the owner's.")

BASKET_DISCIPLINE = (
    "Accept-the-loss discipline: each position's cost is the "
    "maximum amount the owner is willing to lose on that bet — "
    "a small-cap asymmetric bet can go to zero.  Size positions "
    "only with money the owner can afford to lose entirely.")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _money(value):
    """Round a money value to cents; None stays None."""
    if value is None:
        return None
    return round(float(value), 2)


def _assumptions(extra=None):
    assumptions = {
        "proposal_only": True,
        "no_trade_placed": True,
        "disclaimer": PROPOSAL_DISCLAIMER,
    }
    if extra:
        assumptions.update(extra)
    return assumptions


def _candidate_price(record):
    """Price from a detection record or plain dict; None if missing."""
    price = record.get("price")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _candidate_weight_key(record):
    """Detector rank for ordering (None sorts last)."""
    rank = record.get("rank")
    if rank is None:
        rank = record.get("scanner_rank")
    return rank if rank is not None else float("inf")


# ---------------------------------------------------------------------------
# Kelly criterion
# ---------------------------------------------------------------------------

def kelly_fraction(win_prob, payoff_ratio, cap=DEFAULT_KELLY_CAP,
                   kelly_multiplier=DEFAULT_KELLY_MULTIPLIER):
    """Kelly fraction of capital for one bet.

    ``win_prob`` is the probability the bet wins (0..1),
    ``payoff_ratio`` is net win / net loss (b > 0): a bet that
    wins $3 for every $1 lost has b = 3.  The Kelly fraction is
    f* = p - (1 - p) / b.  The raw fraction is first scaled by
    ``kelly_multiplier`` (default 0.5 — half-Kelly, the default
    because full Kelly maximises log-growth but at ruinous
    volatility for a small portfolio), then capped at ``cap``
    (default 25% of capital), then floored at 0: a non-positive
    edge means no bet.

    Returns a dict with the fraction, the uncapped raw fraction,
    the edge, and the inputs.  Raises ValueError on invalid
    inputs.  The edge is caller-supplied judgement — Jarvis
    does not estimate win probabilities.
    """
    try:
        p = float(win_prob)
        b = float(payoff_ratio)
        cap = float(cap)
        mult = float(kelly_multiplier)
    except (TypeError, ValueError):
        raise ValueError(
            "kelly_fraction: win_prob, payoff_ratio, cap and "
            "kelly_multiplier must be numbers")
    if not 0.0 <= p <= 1.0:
        raise ValueError(
            f"kelly_fraction: win_prob {p} is outside 0..1")
    if b <= 0:
        raise ValueError(
            f"kelly_fraction: payoff_ratio {b} must be positive")
    if not 0.0 < cap <= 1.0:
        raise ValueError(
            f"kelly_fraction: cap {cap} must be in (0, 1]")
    if not 0.0 < mult <= 1.0:
        raise ValueError(
            "kelly_fraction: kelly_multiplier "
            f"{mult} must be in (0, 1]")
    raw = p - (1.0 - p) / b
    scaled = raw * mult
    fraction = max(0.0, min(scaled, cap))
    # Round to 6 decimals: raw float arithmetic (e.g. 0.4 * 0.5
    # = 0.19999999999999998) would otherwise cost a whole share
    # when the fraction is multiplied by capital and floored.
    raw = round(raw, 6)
    scaled = round(scaled, 6)
    fraction = round(fraction, 6)
    return {
        "fraction": fraction,
        "raw_kelly_fraction": raw,
        "scaled_fraction": scaled,
        "edge": round(p - (1.0 - p) / b, 6),
        "win_prob": p,
        "payoff_ratio": b,
        "cap": cap,
        "kelly_multiplier": mult,
        "bet": fraction > 0,
        "reason": (
            "positive edge: Kelly fraction "
            f"{fraction:.2%} of capital" if fraction > 0 else
            "no edge (Kelly fraction <= 0): no bet"),
    }


def size_kelly(capital, price, win_prob, payoff_ratio,
               cap=DEFAULT_KELLY_CAP,
               kelly_multiplier=DEFAULT_KELLY_MULTIPLIER):
    """Size one position by Kelly: returns amount, shares, fraction."""
    if capital is None or float(capital) <= 0:
        raise ValueError("size_kelly: capital must be positive")
    if price is None or float(price) <= 0:
        raise ValueError("size_kelly: price must be positive")
    kelly = kelly_fraction(win_prob, payoff_ratio, cap=cap,
                           kelly_multiplier=kelly_multiplier)
    amount = _money(float(capital) * kelly["fraction"])
    shares = int(amount // float(price)) if amount > 0 else 0
    return {
        "method": "kelly",
        "capital": float(capital),
        "price": float(price),
        "kelly": kelly,
        "fraction": kelly["fraction"],
        "amount": amount,
        "shares": shares,
        "cost": _money(shares * float(price)),
        "cash_leftover": _money(amount - shares * float(price)),
    }


def size_fixed_fraction(capital, fraction):
    """Size one position as a fixed fraction of capital."""
    if capital is None or float(capital) <= 0:
        raise ValueError(
            "size_fixed_fraction: capital must be positive")
    if fraction is None or not 0.0 < float(fraction) <= 1.0:
        raise ValueError(
            "size_fixed_fraction: fraction "
            f"{fraction} must be in (0, 1]")
    return {
        "method": "fixed_fraction",
        "capital": float(capital),
        "fraction": float(fraction),
        "amount": _money(float(capital) * float(fraction)),
    }


def size_equal_weight(capital, n_positions):
    """Equal-weight amount per position for ``n_positions``."""
    if capital is None or float(capital) <= 0:
        raise ValueError(
            "size_equal_weight: capital must be positive")
    if n_positions is None or int(n_positions) <= 0:
        raise ValueError(
            "size_equal_weight: n_positions must be positive")
    return {
        "method": "equal_weight",
        "capital": float(capital),
        "n_positions": int(n_positions),
        "amount_each": _money(float(capital) / int(n_positions)),
    }


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------

def concentration_check(positions):
    """Sector / country / single-name concentration of a proposal.

    ``positions`` is a list of position dicts with ``cost``,
    ``sector`` and ``country`` (missing sector/country is grouped
    as "unknown" and flagged — concentration cannot be assessed
    for it).  Returns the largest weights and warnings.  Thresholds
    are judgement (single name > 25%, sector > 40%, country > 60%)
    and are stated on the output.
    """
    total = sum(float(p.get("cost") or 0) for p in positions)
    if total <= 0 or not positions:
        return {
            "total_invested": 0.0,
            "max_single_name_weight": None,
            "max_sector": None,
            "max_country": None,
            "warnings": ["no positions: concentration cannot be "
                         "assessed"],
            "thresholds": {"single_name": SINGLE_NAME_WARN,
                           "sector": SECTOR_WARN,
                           "country": COUNTRY_WARN},
        }
    by_sector = Counter()
    by_country = Counter()
    unknown_sector = 0.0
    unknown_country = 0.0
    max_single = 0.0
    max_single_ticker = None
    for position in positions:
        cost = float(position.get("cost") or 0)
        weight = cost / total
        if weight > max_single:
            max_single = weight
            max_single_ticker = position.get("ticker")
        sector = position.get("sector") or "unknown"
        country = position.get("country") or "unknown"
        by_sector[sector] += cost
        by_country[country] += cost
        if sector == "unknown":
            unknown_sector += cost
        if country == "unknown":
            unknown_country += cost
    sector, sector_cost = max(by_sector.items(), key=lambda kv: kv[1])
    country, country_cost = max(by_country.items(),
                                key=lambda kv: kv[1])
    warnings = []
    if max_single > SINGLE_NAME_WARN:
        warnings.append(
            f"concentrated in one name: {max_single_ticker} is "
            f"{max_single:.1%} of the invested portfolio "
            f"(warn above {SINGLE_NAME_WARN:.0%})")
    if sector != "unknown" and sector_cost / total > SECTOR_WARN:
        warnings.append(
            f"concentrated in one sector: {sector} is "
            f"{sector_cost / total:.1%} of the invested portfolio "
            f"(warn above {SECTOR_WARN:.0%})")
    if country != "unknown" and country_cost / total > COUNTRY_WARN:
        warnings.append(
            f"concentrated in one country: {country} is "
            f"{country_cost / total:.1%} of the invested portfolio "
            f"(warn above {COUNTRY_WARN:.0%})")
    if unknown_sector > 0:
        warnings.append(
            f"sector unknown for {unknown_sector / total:.1%} of the "
            "invested portfolio: concentration cannot be fully "
            "assessed")
    if unknown_country > 0:
        warnings.append(
            f"country unknown for {unknown_country / total:.1%} of "
            "the invested portfolio: concentration cannot be fully "
            "assessed")
    return {
        "total_invested": _money(total),
        "max_single_name_weight": max_single,
        "max_single_name_ticker": max_single_ticker,
        "max_sector": {"sector": sector,
                       "weight": sector_cost / total},
        "max_country": {"country": country,
                        "weight": country_cost / total},
        "warnings": warnings,
        "thresholds": {"single_name": SINGLE_NAME_WARN,
                       "sector": SECTOR_WARN,
                       "country": COUNTRY_WARN},
    }


# ---------------------------------------------------------------------------
# Portfolio builder
# ---------------------------------------------------------------------------

def _waterfill_weights(n, cap):
    """Equal weights for ``n`` positions, capped at ``cap``.

    Water-filling: start equal; any weight above the cap is set
    to the cap and the freed capital is spread equally over the
    remaining positions, repeating until no weight exceeds the
    cap.  Returns a list of weights summing to 1 (or 0 when the
    cap makes full investment impossible — e.g. cap 0.20 with
    only 3 positions can invest at most 60%).
    """
    if n <= 0:
        return []
    weights = [1.0 / n] * n
    for _ in range(n + 1):
        over = [i for i, w in enumerate(weights) if w > cap]
        if not over:
            break
        freed = sum(weights[i] - cap for i in over)
        for i in over:
            weights[i] = cap
        under = [i for i, w in enumerate(weights) if w < cap]
        if not under:
            break
        share = freed / len(under)
        for i in under:
            weights[i] += share
    return weights


def build_portfolio(candidates, capital, currency=None,
                    method="equal_weight", max_positions=None,
                    per_position_cap=DEFAULT_PER_POSITION_CAP,
                    edges=None, fixed_fraction=None):
    """Build a proposed portfolio from Step 26 candidate records.

    ``candidates`` is a list of detection records (or plain dicts
    with at least ``ticker`` and ``price``).  ``capital`` is the
    total money available, ``currency`` the currency of that
    capital (never assumed — None produces a warning).  ``method``
    is "equal_weight" (default), "kelly" or "fixed_fraction".
    ``max_positions`` caps the number of positions taken (top
    candidates by detector rank first).  ``per_position_cap``
    caps any single position's weight.  ``edges`` maps ticker ->
    (win_prob, payoff_ratio) and is required for "kelly".
    ``fixed_fraction`` is the fraction per position for
    "fixed_fraction".

    Returns the proposal dict: positions (ticker, shares, cost,
    target weight, actual weight, band, quality, margin), cash
    left over, totals, concentration check, warnings and
    assumptions.  Proposal only — no trade is placed.
    """
    if capital is None or float(capital) <= 0:
        raise ValueError("build_portfolio: capital must be positive")
    capital = float(capital)
    if method not in ("equal_weight", "kelly", "fixed_fraction"):
        raise ValueError(
            f"build_portfolio: unknown method '{method}'")
    if per_position_cap is None or not 0.0 < float(per_position_cap) <= 1.0:
        raise ValueError(
            "build_portfolio: per_position_cap "
            f"{per_position_cap} must be in (0, 1]")
    per_position_cap = float(per_position_cap)
    max_positions = (DEFAULT_MAX_POSITIONS if max_positions is None
                     else int(max_positions))
    if max_positions <= 0:
        raise ValueError(
            "build_portfolio: max_positions must be positive")
    if method == "kelly" and not edges:
        raise ValueError(
            "build_portfolio: method 'kelly' needs an edges dict "
            "mapping ticker -> (win_prob, payoff_ratio); the edge "
            "is the owner's judgement, not computed here")
    if method == "fixed_fraction":
        if fixed_fraction is None or not 0.0 < float(fixed_fraction) <= 1.0:
            raise ValueError(
                "build_portfolio: method 'fixed_fraction' needs "
                "fixed_fraction in (0, 1]")

    warnings = []
    if currency is None:
        warnings.append(
            "currency not specified: capital currency is unknown — "
            "the owner has not confirmed SGD vs USD, so do not "
            "assume one")
    if not candidates:
        return {
            "method": method,
            "capital": capital,
            "currency": currency,
            "positions": [],
            "n_positions": 0,
            "total_invested": 0.0,
            "cash_leftover": _money(capital),
            "invested_fraction": 0.0,
            "skipped": [],
            "warnings": warnings + [
                "no candidates: no allocation proposed — the "
                "detector found nothing to size"],
            "concentration": concentration_check([]),
            "assumptions": _assumptions({
                "method": method,
                "max_positions": max_positions,
                "per_position_cap": per_position_cap}),
        }

    ordered = sorted(candidates, key=_candidate_weight_key)
    usable = []
    skipped = []
    for record in ordered:
        price = _candidate_price(record)
        if price is None:
            skipped.append({
                "ticker": record.get("ticker"),
                "reason": "no usable price: skipped, never guessed"})
            continue
        if method == "kelly" and record.get("ticker") not in edges:
            skipped.append({
                "ticker": record.get("ticker"),
                "reason": "no edge supplied for Kelly sizing: "
                          "skipped — the edge is the owner's "
                          "judgement, not computed here"})
            continue
        usable.append(record)
    usable = usable[:max_positions]

    if not usable:
        return {
            "method": method,
            "capital": capital,
            "currency": currency,
            "positions": [],
            "n_positions": 0,
            "total_invested": 0.0,
            "cash_leftover": _money(capital),
            "invested_fraction": 0.0,
            "skipped": skipped,
            "warnings": warnings + [
                "no usable candidates after price/edge checks: no "
                "allocation proposed"],
            "concentration": concentration_check([]),
            "assumptions": _assumptions({
                "method": method,
                "max_positions": max_positions,
                "per_position_cap": per_position_cap}),
        }

    # Target weights.
    targets = {}
    if method == "equal_weight":
        weights = _waterfill_weights(len(usable), per_position_cap)
        for record, weight in zip(usable, weights):
            targets[record["ticker"]] = weight
    elif method == "fixed_fraction":
        for record in usable:
            targets[record["ticker"]] = min(float(fixed_fraction),
                                            per_position_cap)
    else:  # kelly
        for record in usable:
            win_prob, payoff = edges[record["ticker"]]
            kelly = kelly_fraction(win_prob, payoff)
            targets[record["ticker"]] = min(kelly["fraction"],
                                            per_position_cap)

    positions = []
    total_invested = 0.0
    for record in usable:
        ticker = record["ticker"]
        price = _candidate_price(record)
        target_amount = capital * targets[ticker]
        shares = int(target_amount // price)
        cost = _money(shares * price)
        if shares == 0:
            skipped.append({
                "ticker": ticker,
                "reason": f"target amount {_money(target_amount)} "
                          f"buys 0 shares at ${price:,.2f}: "
                          "position too small for one share"})
            continue
        total_invested += cost
        positions.append({
            "ticker": ticker,
            "company_name": record.get("company_name"),
            "country": record.get("country"),
            "sector": record.get("sector"),
            "detection_band": record.get("detection_band"),
            "tier": record.get("tier"),
            "quality_rating": record.get("quality_rating"),
            "margin_of_safety_vs_conservative": record.get(
                "margin_of_safety_vs_conservative"),
            "price": price,
            "price_as_of": record.get("price_as_of"),
            "price_source": record.get("price_source"),
            "shares": shares,
            "cost": cost,
            "target_weight": targets[ticker],
            "actual_weight": cost / capital,
            "sizing_method": method,
        })
    if positions and total_invested < capital * 0.5:
        warnings.append(
            f"only {total_invested / capital:.0%} of capital is "
            "invested: per-position cap or share rounding leaves "
            "most of the capital as cash — check the cap and the "
            "number of candidates")
    concentration = concentration_check(positions)
    warnings.extend(concentration["warnings"])
    return {
        "method": method,
        "capital": capital,
        "currency": currency,
        "positions": positions,
        "n_positions": len(positions),
        "total_invested": _money(total_invested),
        "cash_leftover": _money(capital - total_invested),
        "invested_fraction": total_invested / capital,
        "skipped": skipped,
        "warnings": warnings,
        "concentration": concentration,
        "assumptions": _assumptions({
            "method": method,
            "max_positions": max_positions,
            "per_position_cap": per_position_cap,
            "kelly_cap": DEFAULT_KELLY_CAP,
            "kelly_multiplier": DEFAULT_KELLY_MULTIPLIER,
            "note": "shares are whole numbers (floor); rounding "
                    "leftovers stay as cash"}),
    }


def build_from_detection(detection, capital, currency=None, **kwargs):
    """Build a portfolio from a Step 26 detection result dict.

    Takes the ``candidates`` list from
    :func:`undervalued_detector.detect_from_scan` (or
    ``detect_undervalued``).  Returns the same proposal as
    :func:`build_portfolio`, plus the detection summary.  With no
    candidates the proposal is empty, with the reason stated.
    """
    detection = detection or {}
    result = build_portfolio(detection.get("candidates", []),
                             capital, currency=currency, **kwargs)
    result["detection_summary"] = detection.get("summary", {})
    result["value_traps_excluded"] = len(
        detection.get("value_traps", []))
    return result


# ---------------------------------------------------------------------------
# $1,000 small-cap basket
# ---------------------------------------------------------------------------

def build_small_cap_basket(candidates, basket_capital=None,
                           currency=None, max_positions=None,
                           max_loss_per_position=None):
    """Size the small-cap asymmetric-bet basket.

    ``candidates`` is a list of detection records (small-cap
    candidates).  ``basket_capital`` defaults to $1,000 — the
    owner's gamble bucket; its currency is NOT assumed and must
    be supplied by the caller (``currency``), otherwise the
    proposal carries a warning.  ``max_loss_per_position`` is the
    amount the owner is willing to lose on one bet: when given,
    each position costs exactly that (a small cap can go to
    zero, so cost = maximum loss) and the number of positions
    is ``basket_capital // max_loss_per_position`` (capped at
    ``max_positions``).  Without it, the basket is split equally
    across up to ``max_positions`` candidates.

    Every position records ``amount_at_risk`` (its full cost)
    and the accept-the-loss discipline.  Proposal only — no
    trade is placed.
    """
    capital = (DEFAULT_BASKET_CAPITAL if basket_capital is None
               else float(basket_capital))
    if capital <= 0:
        raise ValueError(
            "build_small_cap_basket: basket_capital must be positive")
    max_positions = (DEFAULT_BASKET_MAX_POSITIONS
                     if max_positions is None else int(max_positions))
    if max_positions <= 0:
        raise ValueError(
            "build_small_cap_basket: max_positions must be positive")
    if max_loss_per_position is not None:
        max_loss_per_position = float(max_loss_per_position)
        if max_loss_per_position <= 0:
            raise ValueError(
                "build_small_cap_basket: max_loss_per_position must "
                "be positive")
        if max_loss_per_position > capital:
            raise ValueError(
                "build_small_cap_basket: max_loss_per_position "
                f"{max_loss_per_position} exceeds basket capital "
                f"{capital}")

    warnings = [BASKET_DISCIPLINE]
    if currency is None:
        warnings.append(
            "currency not specified: the $1,000 basket currency "
            "(SGD vs USD) has not been confirmed by the owner — "
            "do not assume one")
    ordered = sorted(candidates or [], key=_candidate_weight_key)
    usable = []
    skipped = []
    for record in ordered:
        price = _candidate_price(record)
        if price is None:
            skipped.append({
                "ticker": record.get("ticker"),
                "reason": "no usable price: skipped, never guessed"})
            continue
        usable.append(record)

    if max_loss_per_position is not None:
        n = min(max_positions,
                int(capital // max_loss_per_position))
        amount_each = max_loss_per_position
    else:
        n = min(max_positions, len(usable))
        amount_each = capital / n if n else 0.0
    usable = usable[:n]

    positions = []
    total_invested = 0.0
    for record in usable:
        price = _candidate_price(record)
        shares = int(amount_each // price)
        cost = _money(shares * price)
        if shares == 0:
            skipped.append({
                "ticker": record.get("ticker"),
                "reason": f"basket amount {_money(amount_each)} buys "
                          f"0 shares at ${price:,.2f}: position too "
                          "small for one share"})
            continue
        total_invested += cost
        positions.append({
            "ticker": record.get("ticker"),
            "company_name": record.get("company_name"),
            "country": record.get("country"),
            "sector": record.get("sector"),
            "detection_band": record.get("detection_band"),
            "tier": record.get("tier"),
            "quality_rating": record.get("quality_rating"),
            "price": price,
            "shares": shares,
            "cost": cost,
            "amount_at_risk": cost,
            "weight_of_basket": cost / capital,
        })
    concentration = concentration_check(positions)
    warnings.extend(concentration["warnings"])
    return {
        "basket_capital": capital,
        "currency": currency,
        "method": "accept_the_loss",
        "max_loss_per_position": max_loss_per_position,
        "positions": positions,
        "n_positions": len(positions),
        "total_invested": _money(total_invested),
        "cash_leftover": _money(capital - total_invested),
        "total_amount_at_risk": _money(total_invested),
        "skipped": skipped,
        "warnings": warnings,
        "concentration": concentration,
        "assumptions": _assumptions({
            "max_positions": max_positions,
            "discipline": BASKET_DISCIPLINE,
            "note": "each position's cost is its maximum loss; "
                    "shares are whole numbers (floor)"}),
    }


# ---------------------------------------------------------------------------
# Rebalancing
# ---------------------------------------------------------------------------

def rebalance_proposal(holdings, target_weights, prices,
                       cash_available=0.0):
    """Propose the trades to move holdings to target weights.

    ``holdings`` is a list of {"ticker", "shares"} (caller-supplied
    current holdings).  ``target_weights`` maps ticker -> target
    weight of the portfolio (must sum to <= 1; the remainder is
    cash).  ``prices`` maps ticker -> current price.  ``cash_available``
    is cash on hand to fund buys.  Sells are assumed to fund buys;
    if buys still exceed cash available plus sale proceeds, a
    warning is raised.  Returns the trade list (ticker, action,
    shares, estimated value), current and target values, and cash
    needed.  Proposal only — no trade is placed or executed.
    """
    if cash_available is None or float(cash_available) < 0:
        raise ValueError(
            "rebalance_proposal: cash_available must be >= 0")
    cash_available = float(cash_available)
    total_weight = sum(float(w) for w in target_weights.values())
    if total_weight > 1.0 + 1e-9:
        raise ValueError(
            "rebalance_proposal: target weights sum to "
            f"{total_weight:.2%}, above 100%")
    current = {}
    for holding in holdings or []:
        ticker = holding.get("ticker")
        shares = holding.get("shares")
        if ticker is None or shares is None:
            raise ValueError(
                "rebalance_proposal: holdings need ticker and shares")
        if ticker not in prices or prices[ticker] is None:
            raise ValueError(
                "rebalance_proposal: no price supplied for "
                f"{ticker} — prices are never guessed")
        if float(prices[ticker]) <= 0:
            raise ValueError(
                "rebalance_proposal: price for "
                f"{ticker} must be positive")
        current[ticker] = float(shares)

    portfolio_value = cash_available + sum(
        shares * float(prices[ticker])
        for ticker, shares in current.items())
    trades = []
    buy_cost = 0.0
    sell_proceeds = 0.0
    for ticker in sorted(set(current) | set(target_weights)):
        raw_price = prices.get(ticker)
        if raw_price is None:
            raise ValueError(
                "rebalance_proposal: no price supplied for "
                f"{ticker} — prices are never guessed")
        price = float(raw_price)
        if price <= 0:
            raise ValueError(
                "rebalance_proposal: price for "
                f"{ticker} must be positive")
        target_value = portfolio_value * float(
            target_weights.get(ticker, 0.0))
        current_value = current.get(ticker, 0.0) * price
        delta_value = target_value - current_value
        delta_shares = int(round(delta_value / price))
        if delta_shares == 0:
            continue
        action = "buy" if delta_shares > 0 else "sell"
        value = _money(abs(delta_shares) * price)
        if action == "buy":
            buy_cost += value
        else:
            sell_proceeds += value
        trades.append({
            "ticker": ticker,
            "action": action,
            "shares": abs(delta_shares),
            "signed_shares": delta_shares,
            "price": price,
            "estimated_value": value,
            "current_value": _money(current_value),
            "target_value": _money(target_value),
        })
    cash_needed = buy_cost - sell_proceeds - cash_available
    warnings = []
    if cash_needed > 0:
        warnings.append(
            f"buys exceed cash available plus sale proceeds by "
            f"${cash_needed:,.2f}: fund the shortfall or reduce "
            "buy targets — no trade is placed here")
    if total_weight < 1.0 - 1e-9:
        warnings.append(
            f"target weights sum to {total_weight:.1%}: the "
            f"remaining {(1.0 - total_weight):.1%} stays as cash")
    return {
        "portfolio_value": _money(portfolio_value),
        "cash_available": _money(cash_available),
        "trades": trades,
        "n_trades": len(trades),
        "buy_cost": _money(buy_cost),
        "sell_proceeds": _money(sell_proceeds),
        "cash_needed": _money(max(0.0, cash_needed)),
        "cash_after": _money(cash_available + sell_proceeds - buy_cost),
        "warnings": warnings,
        "assumptions": _assumptions({
            "note": "whole shares (rounded); sells are assumed to "
                    "fund buys; proposal only, no trade placed"}),
    }


# ---------------------------------------------------------------------------
# Presentation and persistence (records only)
# ---------------------------------------------------------------------------

def portfolio_lines(proposal):
    """One line per position, for a daily check / briefing.

    Records only — never sent from here, never a buy/sell call.
    """
    lines = []
    for position in proposal.get("positions", []):
        lines.append(
            f"{position['ticker']}: {position['shares']} shares @ "
            f"${position['price']:,.2f} = "
            f"${position['cost']:,.2f} "
            f"({position['actual_weight']:.1%} of capital)"
            if "actual_weight" in position else
            f"{position['ticker']}: {position['shares']} shares @ "
            f"${position['price']:,.2f} = "
            f"${position['cost']:,.2f} "
            f"({position['weight_of_basket']:.1%} of basket; "
            f"at risk ${position['amount_at_risk']:,.2f})")
    lines.append(
        f"Invested ${proposal.get('total_invested', 0.0):,.2f}; "
        f"cash left ${proposal.get('cash_leftover', 0.0):,.2f}")
    return lines


def portfolio_summary(proposal):
    """Compact summary of a proposal."""
    return {
        "method": proposal.get("method"),
        "capital": proposal.get("capital",
                                proposal.get("basket_capital")),
        "currency": proposal.get("currency"),
        "n_positions": proposal.get("n_positions"),
        "total_invested": proposal.get("total_invested"),
        "cash_leftover": proposal.get("cash_leftover"),
        "invested_fraction": proposal.get(
            "invested_fraction"),
        "warnings": list(proposal.get("warnings", [])),
        "lines": portfolio_lines(proposal),
    }


def save_proposal(proposal, path):
    """Save a proposal to a JSON file (caller-chosen path)."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(proposal, handle, indent=2, default=str)
    return path


def load_proposal(path):
    """Load a proposal previously saved with :func:`save_proposal`."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
