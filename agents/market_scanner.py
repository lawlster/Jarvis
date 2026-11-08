"""Step 25 — global market scanner (Phase D, first step).

Orchestrates the Step 16-24 pipeline over a list of tickers and
produces a ranked candidate list.  It does NOT re-derive any
valuation: per ticker it calls the Step 23 margin-of-safety
summary (which itself runs the Step 21/22 valuation, the Step
18/19/20 deterioration flag and the Step 24 quality gate) plus
the Step 18 health verdict, and records the tier each ticker
falls into.

Data rules, stated on every result:

- Prices are caller-supplied, or fall back to the latest
  ``financial_snapshots`` row for the ticker (labelled with its
  as-of date).  There is no live price feed and no network
  request here.  A ticker with no price is unscannable — its
  price is never guessed.
- A ticker with no financial statement rows in the database is
  reported as ``insufficient_data`` with the exact missing
  inputs.  It is never silently skipped: the coverage section
  counts how many of the selected universe were scannable and
  why the rest were not.
- The scanner is read-only and runs against the caller's
  connection (tests use a temporary copy).  Nothing is written
  to the database.

Ranking: ``strongest_alert`` first, then ``reasonable_price``,
``fairly_valued``, ``overvalued``, ``insufficient_data`` last.
Within a tier, tickers already surfaced recently (the
``already_surfed`` list) are deprioritised so fresh names rotate
to the top, then the largest margin of safety vs the
conservative value first, then the best business-quality
rating.  No buy/sell call is made anywhere — the final decision
is always the owner's.
"""

import sqlite3
import time
from collections import Counter

from agents import financial_health as fh
from agents import financial_ratios as fr
from agents import intrinsic_valuation as iv
from agents import margin_of_safety as mos
from agents import business_quality as bq

TIER_ORDER = {
    "strongest_alert": 0,
    "reasonable_price": 1,
    "fairly_valued": 2,
    "overvalued": 3,
    "insufficient_data": 4,
}

QUALITY_ORDER = {
    "excellent": 0,
    "good": 1,
    "adequate": 2,
    "weak": 3,
    "insufficient data": 4,
}

MISSING_NO_STATEMENTS = (
    "no financial statement rows for this ticker in the database "
    "(no Step 16 standardised values, so no ratios, health, growth, "
    "cash flow, valuation, margin of safety or quality can be computed)")
MISSING_NO_PRICE = (
    "no price supplied and no snapshot price in the database "
    "(a price is needed before any margin of safety can be computed)")

# ---------------------------------------------------------------------------
# Price inputs
# ---------------------------------------------------------------------------


def normalise_price_input(value):
    """Normalise a caller-supplied price to a price-input dict.

    Accepts a plain number (price only, labelled caller-supplied),
    a dict with at least ``price`` (``as_of_date`` / ``source`` /
    ``market_cap`` carried through when present), or None.  Returns
    None for anything without a usable price.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return {"price": price, "as_of_date": None,
                "source": "caller-supplied price"}
    if isinstance(value, dict):
        price = value.get("price")
        if price is None:
            return None
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return {
            "price": price,
            "market_cap": value.get("market_cap"),
            "as_of_date": value.get("as_of_date"),
            "source": value.get("source") or "caller-supplied price",
        }
    return None


def snapshot_prices(conn):
    """Latest ``financial_snapshots`` price per ticker, one query.

    Returns {ticker: price-input dict}.  Tickers with no snapshot
    row are absent, never guessed.
    """
    prices = {}
    try:
        rows = conn.execute(
            "SELECT ticker, snapshot_date, financial_data "
            "FROM financial_snapshots "
            "ORDER BY ticker, snapshot_date DESC").fetchall()
    except sqlite3.OperationalError:
        return prices
    import json
    for ticker, snapshot_date, financial_data in rows:
        if ticker in prices:
            continue  # rows are newest-first per ticker
        try:
            data = json.loads(financial_data)
        except (ValueError, TypeError):
            continue
        price = data.get("current_price")
        if price is None:
            continue
        prices[ticker] = {
            "price": price,
            "market_cap": data.get("market_cap"),
            "as_of_date": (snapshot_date or "")[:10],
            "source": "financial_snapshots (latest row)",
        }
    return prices


# ---------------------------------------------------------------------------
# Universe and statement coverage
# ---------------------------------------------------------------------------


def load_universe(conn, country=None, sector=None, security_type=None,
                  exchange=None, active_only=True):
    """Universe rows from ``market_universe`` on ``conn``.

    Returns a list of dicts.  A missing ``market_universe`` table
    (fixture databases) yields an empty list, not an exception —
    callers that pass an explicit ticker list still work.
    """
    query = (
        "SELECT ticker, company_name, exchange, country, currency, "
        "sector, industry, security_type, market_cap, is_active, "
        "sec_cik FROM market_universe WHERE 1=1")
    parameters = []
    if country:
        query += " AND country = ?"
        parameters.append(country)
    if sector:
        query += " AND sector = ?"
        parameters.append(sector)
    if security_type:
        query += " AND security_type = ?"
        parameters.append(security_type)
    if exchange:
        query += " AND exchange = ?"
        parameters.append(exchange)
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY country, exchange, ticker"
    try:
        rows = conn.execute(query, parameters).fetchall()
    except sqlite3.OperationalError:
        return []
    columns = ("ticker", "company_name", "exchange", "country",
               "currency", "sector", "industry", "security_type",
               "market_cap", "is_active", "sec_cik")
    return [dict(zip(columns, row)) for row in rows]


def statement_row_counts(conn):
    """{ticker: number of financial_statements rows}, one query."""
    try:
        rows = conn.execute(
            "SELECT ticker, COUNT(*) FROM financial_statements "
            "GROUP BY ticker").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {ticker: count for ticker, count in rows}


# ---------------------------------------------------------------------------
# One ticker through the pipeline
# ---------------------------------------------------------------------------


def _insufficient_candidate(ticker, universe_row, missing_inputs,
                            price_input=None, detail=None,
                            health_verdict="insufficient data",
                            health_reasons=None, quality_rating=None,
                            quality_mean_score=None,
                            fair_value_range=None, margins=None,
                            price=None, price_as_of=None,
                            price_source=None):
    """Candidate dict for a ticker that could not be fully scanned."""
    return {
        "ticker": ticker,
        "company_name": (universe_row or {}).get("company_name"),
        "country": (universe_row or {}).get("country"),
        "exchange": (universe_row or {}).get("exchange"),
        "sector": (universe_row or {}).get("sector"),
        "security_type": (universe_row or {}).get("security_type"),
        "price": price,
        "price_as_of": price_as_of,
        "price_source": price_source,
        "scannable": False,
        "tier": "insufficient_data",
        "tier_reasons": list(missing_inputs),
        "conditions_to_change": [
            "validated financial statements and a price are needed "
            "before any tier can be assigned"],
        "margin_of_safety_vs_conservative": (
            (margins or {}).get("conservative")),
        "margin_of_safety_vs_base": (margins or {}).get("base"),
        "margin_of_safety_vs_optimistic": (
            (margins or {}).get("optimistic")),
        "fair_value_range": fair_value_range or {
            "conservative": None, "base": None, "optimistic": None},
        "strongest_alert_threshold_price": None,
        "quality_rating": quality_rating,
        "quality_mean_score": quality_mean_score,
        "health_verdict": health_verdict,
        "health_reasons": list(health_reasons or []),
        "starting_fcf_basis": None,
        "seen_recently": False,
        "rank": None,
        "missing_inputs": list(missing_inputs),
        "warnings": [],
        "detail": detail,
    }


def scan_ticker(conn, ticker, price_input=None, sector_override=None,
                universe_row=None, cache=None):
    """Scan one ticker through the Step 16-24 pipeline.

    ``price_input`` is a caller-supplied price (number or dict);
    when None, the Step 23 summary falls back to the latest
    snapshot price.  ``cache`` is an optional dict shared by a
    run so a ticker is never scanned twice with the same inputs.
    Returns a candidate dict (never raises for a single ticker —
    a pipeline failure is reported as ``insufficient_data`` with
    the error as a missing input).
    """
    price_input = normalise_price_input(price_input)
    # The cache key includes the exchange: the same ticker can be
    # listed in two countries (e.g. AWX in the USA and in Singapore
    # are separate market_universe rows), and each listing is its
    # own candidate.  The analysis itself is issuer-level (prior
    # steps key statements by ticker), so two listings of one
    # ticker share the same valuation — only their listing
    # metadata differs.
    cache_key = (ticker, (universe_row or {}).get("exchange"),
                 None if price_input is None else price_input["price"],
                 sector_override)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    counts = statement_row_counts(conn)
    if counts.get(ticker, 0) == 0:
        result = _insufficient_candidate(
            ticker, universe_row, [MISSING_NO_STATEMENTS])
        if cache is not None:
            cache[cache_key] = result
        return result

    # TTM or annual starting FCF for the DCF path: TTM when the
    # data supports it, otherwise the labelled annual basis (an
    # annual-only history has no TTM to build).
    starting_fcf_basis = "ttm"
    try:
        inputs, _warnings = iv.valuation_inputs(conn, ticker)
        if inputs.get("ttm_free_cash_flow") is None:
            starting_fcf_basis = "annual"
    except Exception:  # noqa: BLE001 - basis falls back, never fatal
        starting_fcf_basis = "annual"

    health_verdict = "insufficient data"
    health_reasons = []
    try:
        health = fh.assess_health(conn, ticker,
                                  price_input=price_input)
        health_verdict = health.get("verdict", "insufficient data")
        health_reasons = list(health.get("verdict_reasons", []))
    except Exception as exc:  # noqa: BLE001 - health must not kill scan
        health_reasons = [f"health assessment failed: {exc}"]

    try:
        summary = mos.margin_of_safety_summary(
            conn, ticker, price_input=price_input,
            sector_override=sector_override,
            starting_fcf_basis=starting_fcf_basis)
    except Exception as exc:  # noqa: BLE001 - report, never crash
        result = _insufficient_candidate(
            ticker, universe_row,
            [f"pipeline error running the Step 23 summary: {exc}"],
            health_verdict=health_verdict,
            health_reasons=health_reasons)
        if cache is not None:
            cache[cache_key] = result
        return result

    tier = summary["tier"]
    quality = summary.get("business_quality") or {}
    price = summary.get("price")
    missing_inputs = []
    if price is None:
        missing_inputs.append(MISSING_NO_PRICE)
    if summary["fair_value_range"].get("conservative") is None:
        missing_inputs.append(
            "no conservative intrinsic value could be computed "
            "from the available statements (see the valuation "
            "methods in the detail record)")
    fair_value_range = summary["fair_value_range"]
    margins = {
        "conservative": summary.get("margin_of_safety_vs_conservative"),
        "base": summary.get("margin_of_safety_vs_base"),
        "optimistic": summary.get("margin_of_safety_vs_optimistic"),
    }
    result = {
        "ticker": ticker,
        "company_name": (universe_row or {}).get("company_name"),
        "country": (universe_row or {}).get("country"),
        "exchange": (universe_row or {}).get("exchange"),
        "sector": summary.get("sector"),
        "security_type": (universe_row or {}).get("security_type"),
        "price": price,
        "price_as_of": summary.get("price_as_of"),
        "price_source": summary.get("price_source"),
        "scannable": price is not None,
        "tier": tier["tier"],
        "tier_reasons": list(tier.get("reasons", [])),
        "conditions_to_change": list(
            tier.get("conditions_to_change", [])),
        "margin_of_safety_vs_conservative": margins["conservative"],
        "margin_of_safety_vs_base": margins["base"],
        "margin_of_safety_vs_optimistic": margins["optimistic"],
        "fair_value_range": fair_value_range,
        "strongest_alert_threshold_price": summary.get(
            "strongest_alert_threshold_price"),
        "quality_rating": bq.quality_rating(
            summary.get("business_quality")),
        "quality_mean_score": quality.get("mean_score"),
        "health_verdict": health_verdict,
        "health_reasons": health_reasons,
        "starting_fcf_basis": starting_fcf_basis,
        "seen_recently": False,
        "rank": None,
        "missing_inputs": missing_inputs,
        "warnings": list(summary.get("warnings", [])),
        "detail": summary,
    }
    if cache is not None:
        cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Ranking (with fresh-name rotation)
# ---------------------------------------------------------------------------


def rank_results(results, already_surfed=None):
    """Rank candidates in place and return the ranked list.

    Order: tier (strongest_alert first, insufficient_data last);
    within a tier, tickers NOT surfaced recently first (rotation);
    then the largest margin of safety vs the conservative value
    (missing margin last); then the best quality rating; then
    ticker.  Sets ``rank`` (1-based) and ``seen_recently`` on each
    result.  Never returns a buy/sell call.
    """
    surfed = {str(t).upper() for t in (already_surfed or ())}

    def sort_key(candidate):
        tier_rank = TIER_ORDER.get(candidate["tier"], 99)
        seen = str(candidate["ticker"]).upper() in surfed
        margin = candidate.get("margin_of_safety_vs_conservative")
        margin_key = -margin if margin is not None else float("inf")
        quality_rank = QUALITY_ORDER.get(
            candidate.get("quality_rating"), 5)
        return (tier_rank, seen, margin_key, quality_rank,
                str(candidate["ticker"]))

    ranked = sorted(results, key=sort_key)
    for index, candidate in enumerate(ranked, start=1):
        candidate["rank"] = index
        candidate["seen_recently"] = (
            str(candidate["ticker"]).upper() in surfed)
    return ranked


# ---------------------------------------------------------------------------
# Whole-market scan
# ---------------------------------------------------------------------------


def scan_market(conn, tickers=None, prices=None, country=None,
                sector=None, security_type=None, exchange=None,
                sector_overrides=None, already_surfed=None):
    """Scan a selection of tickers and return the ranked results.

    ``tickers`` is an explicit ticker list; when None, the
    selection is the ``market_universe`` rows matching the
    country / sector / security_type / exchange filters.
    ``prices`` maps ticker -> price (number or price-input dict);
    tickers without a supplied price fall back to their latest
    snapshot price inside the pipeline.  ``sector_overrides``
    maps ticker -> sector for the Step 22 router.
    ``already_surfed`` is a list of tickers shown recently, for
    rotation (see :func:`rank_results`).

    Returns {"results", "coverage", "run_seconds", "warnings"}.
    No network requests, no database writes.
    """
    started = time.perf_counter()
    warnings = []
    prices = prices or {}
    prices_by_ticker = {str(k).upper(): v for k, v in prices.items()}
    sector_overrides = sector_overrides or {}
    overrides_by_ticker = {str(k).upper(): v
                           for k, v in sector_overrides.items()}

    universe_rows = load_universe(
        conn, country=country, sector=sector,
        security_type=security_type, exchange=exchange)
    universe_by_ticker = {str(r["ticker"]).upper(): r
                          for r in universe_rows}
    if not universe_rows and tickers is None:
        warnings.append(
            "no market_universe rows matched the selection "
            "(empty table or no such table in this database)")

    if tickers is not None:
        selection = []
        seen = set()
        for ticker in tickers:
            key = str(ticker).upper()
            if key in seen:
                continue
            seen.add(key)
            selection.append(universe_by_ticker.get(key) or {
                "ticker": ticker, "company_name": None,
                "exchange": None, "country": None, "currency": None,
                "sector": None, "industry": None,
                "security_type": None, "market_cap": None,
                "is_active": 1, "sec_cik": None})
    else:
        selection = universe_rows

    unknown_prices = [t for t in prices_by_ticker
                      if t not in {str(r["ticker"]).upper()
                                   for r in selection}]
    if unknown_prices:
        warnings.append(
            "prices supplied for tickers not in the selection "
            f"(ignored): {', '.join(sorted(unknown_prices))}")

    cache = {}
    results = []
    for row in selection:
        ticker = row["ticker"]
        key = str(ticker).upper()
        results.append(scan_ticker(
            conn, ticker,
            price_input=prices_by_ticker.get(key),
            sector_override=overrides_by_ticker.get(key),
            universe_row=row, cache=cache))

    ranked = rank_results(results, already_surfed=already_surfed)
    run_seconds = time.perf_counter() - started

    by_tier = Counter(r["tier"] for r in ranked)
    by_country = {}
    for row in ranked:
        bucket = by_country.setdefault(
            row.get("country") or "unknown",
            {"selected": 0, "scannable": 0, "by_tier": Counter()})
        bucket["selected"] += 1
        if row["scannable"]:
            bucket["scannable"] += 1
        bucket["by_tier"][row["tier"]] += 1
    for bucket in by_country.values():
        bucket["by_tier"] = dict(bucket["by_tier"])

    missing_reasons = Counter()
    for row in ranked:
        for reason in row["missing_inputs"]:
            # Count by the short reason (before the first paren).
            missing_reasons[reason.split(" (")[0]] += 1

    coverage = {
        "selected": len(ranked),
        "with_statements": sum(
            1 for r in ranked
            if MISSING_NO_STATEMENTS not in r["missing_inputs"]),
        "with_price": sum(1 for r in ranked if r["price"] is not None),
        "scannable": sum(1 for r in ranked if r["scannable"]),
        "pipeline_run": sum(1 for r in ranked if r["detail"] is not None),
        "by_tier": dict(by_tier),
        "by_country": by_country,
        "missing_reasons": dict(missing_reasons),
        "price_sources": dict(Counter(
            r["price_source"] or "no price" for r in ranked)),
        "run_seconds": run_seconds,
    }
    return {
        "results": ranked,
        "coverage": coverage,
        "run_seconds": run_seconds,
        "warnings": warnings,
    }


def scan_with_snapshot_prices(conn, **kwargs):
    """Scan using the latest snapshot price for every ticker.

    Convenience wrapper for :func:`scan_market` when no
    caller-supplied prices exist: prices come from
    ``financial_snapshots`` (labelled with their as-of dates).
    """
    return scan_market(conn, prices=snapshot_prices(conn), **kwargs)


# ---------------------------------------------------------------------------
# Dry-run entry point (no main.py changes)
# ---------------------------------------------------------------------------


def run_scan(db_path=None, tickers=None, prices=None, **kwargs):
    """Dry-run entry: scan a database file and return the result.

    Opens the database read-only (``mode=ro``) and makes no
    writes and no network requests.  ``db_path`` defaults to
    ``database/assistant.db`` next to this package's project root.
    """
    import os
    if db_path is None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "database",
                               "assistant.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return scan_market(conn, tickers=tickers, prices=prices,
                           **kwargs)
    finally:
        conn.close()
