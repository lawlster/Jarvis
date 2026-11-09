"""Step 37 — Quarterly portfolio reporting.

Builds a quarterly report for a caller-supplied portfolio:
period and holdings, quarter performance (return, TWR/MWR,
versus a caller-supplied benchmark), a risk snapshot, income
for the quarter, a valuation snapshot per holding from the
Step 23/24 pipeline where the data exists, notable events in
the quarter, what changed versus the previous quarter's report
where one is supplied, and a limitations section stating every
unavailable metric and why.

The module is pure reporting.  It reuses Step 36
(``agents.portfolio_performance``) for the performance and
risk arithmetic and Step 25 (``agents.market_scanner``) for
the valuation snapshots, and derives nothing new about value.

Inputs are caller-supplied every time: holdings, price
histories, benchmark history, cash flows and dividends.  There
is no holdings store yet — the owner has never entered his
actual holdings — and this step does not invent any.  No price
fetching, no network, no database writes, no trades, no
buy/sell language anywhere in a generated report.
"""

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from agents import portfolio_performance as pp

DEFAULT_REPORT_DIR = "data/quarterly_reports"

_QUARTER_RE = re.compile(
    r"^\s*(?:Q([1-4])\s+(\d{4})|(\d{4})\s*[- ]?Q([1-4])|"
    r"(\d{4})Q([1-4]))\s*$", re.IGNORECASE)

# A report is a record, never a recommendation.
_DISCLAIMER = (
    "This report is a record of past performance and the "
    "evidence available at the time.  It is not investment "
    "advice and it makes no recommendation to transact.  "
    "Intrinsic values are estimates and can be wrong; the "
    "final decision is always the owner's.")


# ---------------------------------------------------------------------------
# Quarter handling
# ---------------------------------------------------------------------------

def parse_quarter(quarter):
    """Parse a quarter label to (start_date, end_date).

    Accepts "Q3 2026", "2026-Q3" and "2026Q3".  The quarter
    runs from the first day of its first month to the last
    day of its third month.
    """
    match = _QUARTER_RE.match(str(quarter))
    if not match:
        raise ValueError(
            f"cannot parse quarter label {quarter!r} — use "
            "'Q3 2026', '2026-Q3' or '2026Q3'")
    if match.group(1):
        quarter_no, year = int(match.group(1)), int(match.group(2))
    elif match.group(3):
        year, quarter_no = int(match.group(3)), int(match.group(4))
    else:
        year, quarter_no = int(match.group(5)), int(match.group(6))
    first_month = 3 * (quarter_no - 1) + 1
    start = date(year, first_month, 1)
    if quarter_no == 4:
        end = date(year, 12, 31)
    else:
        end = date(year, first_month + 3, 1)
        end = date.fromordinal(end.toordinal() - 1)
    return start, end


def _parse_date(value):
    """Parse an ISO date string (or a ``date``) to a ``date``."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _slice_history(history, start, end):
    """Points of a [(date, price), ...] history inside a window."""
    return pp._normalise_history(
        [p for p in (history or [])
         if p and start <= _parse_date(p[0]) <= end])


def _boundary_price(history, when, before=True):
    """Price at a quarter boundary.

    ``before=True``: the last price on or before ``when``
    (the price the quarter opened at).  ``before=False``: the
    last price on or before ``when`` inside the window, i.e.
    the quarter's closing price.  Returns (price, date, note)
    with price None when no usable price exists.
    """
    points = pp._normalise_history(history)
    if before:
        candidates = [p for p in points if p[0] <= when]
        if candidates:
            when_p, price = candidates[-1]
            return price, when_p, None
        if points:
            when_p, price = points[0]
            return price, when_p, (
                "no price on or before the quarter start — "
                "first in-quarter price used as the opening "
                "price")
        return None, None, "no price history at all"
    candidates = [p for p in points if p[0] <= when]
    if candidates:
        when_p, price = candidates[-1]
        return price, when_p, None
    return None, None, "no price on or before the quarter end"


def _missing(reason, **extra):
    result = {"value": None, "status": "missing", "reason": reason}
    result.update(extra)
    return result


def _ok(value, **extra):
    result = {"value": value, "status": "ok"}
    result.update(extra)
    return result


# ---------------------------------------------------------------------------
# Events and news in the quarter
# ---------------------------------------------------------------------------

def _load_json_files(directory, pattern="*.json"):
    """Load every JSON file in a directory; corrupt files skipped."""
    files = []
    directory = Path(directory)
    if not directory.is_dir():
        return files, [f"directory not found: {directory}"]
    problems = []
    for path in sorted(directory.glob(pattern)):
        try:
            files.append(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:  # noqa: BLE001
            problems.append(f"skipped unreadable file "
                            f"{path.name}: {exc}")
    return files, problems


def _quarter_events(holdings, start, end, events_dir=None,
                    news_dir=None):
    """Events and articles in the quarter for the holdings.

    Reads Step 33 event files (caller-supplied directory) and
    Step 31 news run files (caller-supplied directory).  A
    market-wide event (no ticker) is included once, at
    portfolio level.  Returns (items, problems) where each
    item is {"date", "ticker", "kind", "title", "source"}.
    """
    tickers = {(h.get("ticker") or "").upper()
               for h in (holdings or [])}
    items = []
    problems = []
    if events_dir is not None:
        records, problems = _load_json_files(events_dir)
        for record in records:
            try:
                when = _parse_date(record.get("date"))
            except (TypeError, ValueError):
                continue
            if not start <= when <= end:
                continue
            ticker = (record.get("ticker") or "").upper() or None
            if ticker is not None and ticker not in tickers:
                continue
            items.append({
                "date": when.isoformat(),
                "ticker": ticker,
                "kind": f"event: {record.get('event_type') or 'other'}",
                "title": record.get("title") or "(untitled event)",
                "source": record.get("source") or "Step 33 event store",
            })
    if news_dir is not None:
        runs, news_problems = _load_json_files(news_dir)
        problems.extend(news_problems)
        for run in runs:
            for article in run.get("articles") or []:
                raw = (article.get("published_date_iso")
                       or article.get("published_date") or "")
                try:
                    when = _parse_date(raw)
                except (TypeError, ValueError):
                    continue
                if not start <= when <= end:
                    continue
                article_tickers = {
                    str(t).upper()
                    for t in (article.get("tickers") or [])}
                if not article_tickers & tickers:
                    continue
                items.append({
                    "date": when.isoformat(),
                    "ticker": sorted(article_tickers & tickers)[0],
                    "kind": "news",
                    "title": article.get("title") or "(untitled)",
                    "source": article.get("source") or "Step 31 news",
                })
    items.sort(key=lambda item: (item["date"],
                                 item["ticker"] or "",
                                 item["title"]))
    return items, problems


# ---------------------------------------------------------------------------
# Valuation snapshots
# ---------------------------------------------------------------------------

def _valuation_snapshots(holdings, end_prices, valuation_snapshots,
                         db_path):
    """Valuation snapshot per holding (Step 23/24 pipeline).

    Caller-supplied snapshots win.  Otherwise, when a
    ``db_path`` is given, the Step 25 scanner is run read-only
    over the holdings at their quarter-end prices.  A holding
    with no snapshot and no scanner result is
    ``insufficient_data`` with the reason, never invented.
    """
    snapshots = {}
    tickers = [(h.get("ticker") or "").upper()
               for h in (holdings or [])]
    supplied = valuation_snapshots or {}
    scanned = {}
    scan_problems = []
    if db_path is not None and any(
            t not in supplied for t in tickers):
        try:
            from agents import market_scanner as ms
            scan = ms.run_scan(
                db_path=db_path,
                tickers=[t for t in tickers if t not in supplied],
                prices={t: p for t, p in end_prices.items()
                        if p is not None})
            for row in scan.get("results", []):
                scanned[row["ticker"].upper()] = row
        except Exception as exc:  # noqa: BLE001 - report, never crash
            scan_problems.append(
                f"valuation scan failed: {exc}")
    for ticker in tickers:
        if ticker in supplied:
            entry = dict(supplied[ticker])
            entry.setdefault("status", "ok")
            entry.setdefault("source", "caller-supplied snapshot")
            snapshots[ticker] = entry
            continue
        row = scanned.get(ticker)
        if row is None:
            snapshots[ticker] = {
                "status": "insufficient_data",
                "reason": ("no valuation snapshot supplied and "
                           + ("the Step 25 scan did not return "
                              "this ticker" if db_path is not None
                              else "no database path supplied to "
                              "run the Step 25 scanner")),
                "tier": None,
                "quality_rating": None,
                "health_verdict": None,
                "alert_price": None,
            }
            continue
        detail = row.get("detail") or {}
        fair_value = row.get("fair_value_range") or {}
        snapshots[ticker] = {
            "status": ("ok" if row.get("scannable")
                       else "insufficient_data"),
            "reason": ("; ".join(row.get("missing_inputs") or [])
                       or None),
            "tier": row.get("tier"),
            "quality_rating": row.get("quality_rating"),
            "health_verdict": row.get("health_verdict"),
            "price": row.get("price"),
            "price_as_of": row.get("price_as_of"),
            "fair_value_range": {
                "conservative": fair_value.get("conservative"),
                "base": fair_value.get("base"),
                "optimistic": fair_value.get("optimistic"),
            },
            "margin_vs_conservative": row.get(
                "margin_of_safety_vs_conservative"),
            "alert_price": row.get(
                "strongest_alert_threshold_price"),
            "deterioration_overall": (
                (detail.get("deterioration_27") or {}).get(
                    "overall_verdict")),
            "source": "Step 25 scanner over the database",
        }
    return snapshots, scan_problems


# ---------------------------------------------------------------------------
# Quarter-over-quarter comparison
# ---------------------------------------------------------------------------

def _compare_quarters(report, previous):
    """What changed versus the previous quarter's report.

    Compares total value, quarter return, each holding's
    tier and each holding's alert price, and the risk flags.
    Anything the previous report does not carry is reported
    as not comparable, never assumed unchanged.
    """
    if previous is None:
        return _missing("no previous quarter report supplied — "
                        "quarter-over-quarter comparison needs "
                        "one")
    changes = {"value_change": None, "return_change": None,
               "tier_changes": [], "alert_price_changes": [],
               "flags_added": [], "flags_removed": [],
               "notes": []}
    prev_value = (previous.get("holdings_summary") or {}).get(
        "end_value")
    this_value = report["holdings_summary"]["end_value"]
    if prev_value is not None and this_value is not None:
        changes["value_change"] = {
            "previous": prev_value,
            "current": this_value,
            "change": round(this_value - prev_value, 2),
            "change_pct": ((this_value - prev_value) / prev_value
                           if prev_value else None),
        }
    else:
        changes["notes"].append(
            "total value not comparable — a quarter is missing "
            "an end value")
    prev_return = (previous.get("performance") or {}).get(
        "quarter_total_return", {}).get("value")
    this_return = report["performance"][
        "quarter_total_return"]["value"]
    if prev_return is not None and this_return is not None:
        changes["return_change"] = {
            "previous": prev_return, "current": this_return,
            "change": round(this_return - prev_return, 4),
        }
    else:
        changes["notes"].append(
            "quarter return not comparable — a quarter is "
            "missing a quarter return")
    prev_val = previous.get("valuation") or {}
    for ticker, snap in report["valuation"].items():
        prev_snap = prev_val.get(ticker)
        if prev_snap is None:
            changes["notes"].append(
                f"{ticker}: no valuation in the previous "
                "quarter — tier not comparable")
            continue
        if snap.get("tier") != prev_snap.get("tier"):
            changes["tier_changes"].append({
                "ticker": ticker,
                "previous": prev_snap.get("tier"),
                "current": snap.get("tier")})
        if snap.get("alert_price") != prev_snap.get("alert_price"):
            changes["alert_price_changes"].append({
                "ticker": ticker,
                "previous": prev_snap.get("alert_price"),
                "current": snap.get("alert_price")})
    prev_flags = set((previous.get("risk") or {}).get(
        "risk_flags") or [])
    this_flags = set(report["risk"]["risk_flags"])
    changes["flags_added"] = sorted(this_flags - prev_flags)
    changes["flags_removed"] = sorted(prev_flags - this_flags)
    if not (changes["tier_changes"]
            or changes["alert_price_changes"]
            or changes["flags_added"]
            or changes["flags_removed"]
            or changes["value_change"] is not None
            or changes["return_change"] is not None):
        changes["notes"].append("no changes detected")
    return {"value": changes, "status": "ok"}


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_quarterly_report(
        holdings, price_history, quarter=None,
        quarter_start=None, quarter_end=None,
        benchmark_history=None, cash_flows=None,
        currency=None, valuation_snapshots=None, db_path=None,
        events_dir=None, news_dir=None, previous_report=None,
        periods_per_year=None):
    """Generate a quarterly portfolio report (dict).

    ``holdings`` and ``price_history`` are the Step 36 shapes
    (holding: ticker, shares, cost_basis, optional dividends /
    quarter_dividends / sold; price history: ticker ->
    [(date, price), ...]).  The quarter is given either as a
    label (``quarter="Q3 2026"``) or as explicit
    ``quarter_start`` / ``quarter_end`` dates.  ``currency``
    is an input, never assumed.  ``valuation_snapshots`` is an
    optional caller-supplied {ticker: snapshot} mapping;
    otherwise ``db_path`` runs the Step 25 scanner read-only.
    ``events_dir`` / ``news_dir`` are optional directories of
    Step 33 event files / Step 31 news run files.
    ``previous_report`` is an earlier report dict for the
    quarter-over-quarter comparison.
    """
    if quarter is not None:
        start, end = parse_quarter(quarter)
        quarter_label = str(quarter)
    elif quarter_start is not None and quarter_end is not None:
        start, end = _parse_date(quarter_start), _parse_date(quarter_end)
        quarter_label = (f"{start.isoformat()} to "
                         f"{end.isoformat()}")
    else:
        raise ValueError(
            "a quarter label or explicit quarter_start and "
            "quarter_end are required")
    if end < start:
        raise ValueError("quarter_end is before quarter_start")

    holdings = list(holdings or [])
    price_history = price_history or {}
    limitations = []

    # --- Quarter boundaries per holding -------------------------
    positions = []
    start_value = 0.0
    end_value = 0.0
    total_cost = 0.0
    quarter_dividends = 0.0
    end_prices = {}
    for holding in holdings:
        ticker = (holding.get("ticker") or "").upper()
        try:
            shares = float(holding.get("shares") or 0)
            cost_basis = float(holding.get("cost_basis") or 0)
        except (TypeError, ValueError):
            shares, cost_basis = 0.0, 0.0
        history = price_history.get(ticker, [])
        start_price, start_date, start_note = _boundary_price(
            history, start, before=True)
        end_price, end_date, end_note = _boundary_price(
            history, end, before=False)
        if start_note is not None:
            # The opening price fell back to the first
            # in-quarter price.  That is only a usable opening
            # price when the quarter actually has more than
            # one price date; a single price point cannot
            # support a quarter return at all.
            in_quarter_dates = {
                _parse_date(p[0]) for p in history
                if p and start <= _parse_date(p[0]) <= end}
            if len(in_quarter_dates) < 2:
                start_price, start_date = None, None
                start_note = ("only one price point in the "
                              "quarter — no opening price can "
                              "be established")
        end_prices[ticker] = end_price
        q_div = holding.get("quarter_dividends")
        if q_div is None:
            q_div = 0.0
            if holding.get("dividends"):
                limitations.append(
                    f"{ticker}: dividends of "
                    f"{holding.get('dividends')} were supplied "
                    "as a total, not for this quarter — quarter "
                    "income uses 0; supply "
                    "'quarter_dividends' to count them")
        q_div = float(q_div)
        quarter_dividends += q_div
        if start_price is None or end_price is None:
            positions.append({
                "ticker": ticker, "shares": shares,
                "cost_basis": cost_basis,
                "status": "missing",
                "reason": (start_note if start_price is None
                           else end_note),
                "start_price": start_price,
                "end_price": end_price,
                "start_value": None, "end_value": None,
                "quarter_gain": None, "quarter_return": None,
                "quarter_dividends": q_div,
            })
            limitations.append(
                f"{ticker}: no quarter value — "
                f"{start_note if start_price is None else end_note}")
            continue
        if start_note:
            limitations.append(f"{ticker}: {start_note}")
        start_val = shares * start_price
        end_val = shares * end_price
        start_value += start_val
        end_value += end_val
        total_cost += shares * cost_basis
        positions.append({
            "ticker": ticker, "shares": shares,
            "cost_basis": cost_basis, "status": "ok",
            "reason": None,
            "start_price": start_price,
            "start_price_date": start_date.isoformat(),
            "end_price": end_price,
            "end_price_date": end_date.isoformat(),
            "start_value": round(start_val, 2),
            "end_value": round(end_val, 2),
            "quarter_gain": round(end_val - start_val, 2),
            "quarter_return": ((end_val - start_val) / start_val
                               if start_val else None),
            "quarter_dividends": q_div,
        })

    valued = [p for p in positions if p["status"] == "ok"]
    quarter_gain = sum(p["quarter_gain"] for p in valued)
    if start_value > 0:
        quarter_return = _ok(
            round(quarter_gain / start_value, 4),
            period=f"{start.isoformat()} to {end.isoformat()}",
            note="price return for the quarter, dividends "
                 "excluded")
        quarter_total_return = _ok(
            round((quarter_gain + quarter_dividends)
                  / start_value, 4),
            period=f"{start.isoformat()} to {end.isoformat()}",
            note="quarter return including dividends received "
                 "in the quarter")
    else:
        quarter_return = _missing(
            "no quarter start value — no holding has a price "
            "on or before the quarter start")
        quarter_total_return = _missing(
            "no quarter start value — no holding has a price "
            "on or before the quarter start")
        limitations.append(
            "quarter return unavailable: no holding has a "
            "price on or before the quarter start")

    # --- Step 36 summary over the quarter's own history --------
    sliced = {t: _slice_history(h, start, end)
              for t, h in price_history.items()}
    # The value series (and so the TWR, volatility and
    # drawdown) should cover the whole quarter, so prepend
    # each holding's quarter-start boundary price at the
    # quarter start where one exists.
    series_history = {}
    for ticker, history in sliced.items():
        points = list(history)
        start_price, start_date, _ = _boundary_price(
            price_history.get(ticker, []), start, before=True)
        if (start_price is not None and start_date is not None
                and start_date < start):
            points = [(start, start_price)] + points
        series_history[ticker] = points
    sliced_benchmark = (_slice_history(benchmark_history, start, end)
                        if benchmark_history else None)
    quarter_cash_flows = None
    if cash_flows:
        quarter_cash_flows = [
            cf for cf in cash_flows
            if start <= _parse_date(cf[0]) <= end]
        if not quarter_cash_flows:
            limitations.append(
                "cash flows were supplied but none fall in "
                "this quarter — money-weighted return is for "
                "the quarter only")
    summary = pp.portfolio_summary(
        holdings, series_history,
        current_prices={t: p for t, p in end_prices.items()
                        if p is not None},
        benchmark_history=sliced_benchmark,
        cash_flows=quarter_cash_flows,
        periods_per_year=periods_per_year,
        currency=currency)
    twr = summary["time_weighted_return"]
    mwr = summary["money_weighted_return"]
    vol = summary["volatility"]
    drawdown = summary["max_drawdown"]
    portfolio_beta = summary["beta"]
    conc = summary["concentration"]
    risk_flags = list(summary["risk_flags"])
    if twr["status"] != "ok":
        limitations.append(
            f"time-weighted return unavailable: {twr['reason']}")
    if mwr["status"] != "ok":
        limitations.append(
            f"money-weighted return unavailable: {mwr['reason']}")
    if vol["status"] != "ok":
        limitations.append(
            f"volatility unavailable: {vol['reason']}")
    if drawdown["status"] != "ok":
        limitations.append(
            f"maximum drawdown unavailable: "
            f"{drawdown['reason']}")
    if portfolio_beta["status"] != "ok":
        limitations.append(
            f"portfolio beta unavailable: "
            f"{portfolio_beta['reason']}")
    if conc.get("total_value", 0) <= 0:
        limitations.append(
            "concentration unavailable: no valued positions")

    # --- Benchmark comparison -----------------------------------
    if benchmark_history:
        bench_start, _, _ = _boundary_price(
            benchmark_history, start, before=True)
        bench_end, _, _ = _boundary_price(
            benchmark_history, end, before=False)
        if bench_start and bench_end and bench_start > 0:
            benchmark_return = _ok(
                round(bench_end / bench_start - 1.0, 4),
                period=f"{start.isoformat()} to {end.isoformat()}")
            if quarter_total_return["status"] == "ok":
                excess = _ok(
                    round(quarter_total_return["value"]
                          - benchmark_return["value"], 4),
                    note="quarter total return minus benchmark "
                         "return")
            else:
                excess = _missing(
                    "no quarter return to compare against the "
                    "benchmark")
        else:
            benchmark_return = _missing(
                "benchmark history has no usable price on or "
                "before the quarter start or end")
            excess = _missing(
                "benchmark return unavailable, so no excess "
                "return can be computed")
            limitations.append(
                "benchmark return unavailable: benchmark "
                "history has no usable price at a quarter "
                "boundary")
    else:
        benchmark_return = _missing(
            "no benchmark history supplied")
        excess = _missing(
            "no benchmark history supplied, so no excess "
            "return can be computed")
        limitations.append(
            "no benchmark supplied — performance is not "
            "compared against any benchmark")

    # --- Contributors / detractors for the quarter --------------
    gains = sorted(valued, key=lambda p: p["quarter_gain"],
                   reverse=True)
    contributors = [{"ticker": p["ticker"],
                     "quarter_gain": p["quarter_gain"],
                     "quarter_return": p["quarter_return"]}
                    for p in gains if p["quarter_gain"] > 0]
    detractors = [{"ticker": p["ticker"],
                   "quarter_gain": p["quarter_gain"],
                   "quarter_return": p["quarter_return"]}
                  for p in gains if p["quarter_gain"] < 0]

    # --- Valuation snapshots ------------------------------------
    valuation, scan_problems = _valuation_snapshots(
        holdings, end_prices, valuation_snapshots, db_path)
    limitations.extend(scan_problems)
    for ticker, snap in valuation.items():
        if snap.get("status") != "ok":
            limitations.append(
                f"{ticker}: valuation snapshot unavailable — "
                f"{snap.get('reason') or 'insufficient data'}")

    # --- Events and news in the quarter -------------------------
    events, event_problems = _quarter_events(
        holdings, start, end, events_dir, news_dir)
    limitations.extend(event_problems)
    if events_dir is None and news_dir is None:
        limitations.append(
            "no event or news directory supplied — notable "
            "events for the quarter are not listed")

    # --- Income --------------------------------------------------
    income = {
        "dividends_received": _ok(
            round(quarter_dividends, 2),
            note="dividends in this quarter only; supply "
                 "'quarter_dividends' per holding to count them",
        ) if quarter_dividends or any(
            h.get("quarter_dividends") is not None
            for h in holdings) else _missing(
            "no quarter dividends supplied — supply "
            "'quarter_dividends' per holding to count income"),
        "yield_on_cost": (
            _ok(round(quarter_dividends / total_cost, 4),
                note="quarter dividends divided by cost basis")
            if total_cost > 0 and quarter_dividends else
            _missing("no quarter dividends or no cost basis")),
        "yield_on_value": (
            _ok(round(quarter_dividends / end_value, 4),
                note="quarter dividends divided by quarter-end "
                     "value")
            if end_value > 0 and quarter_dividends else
            _missing("no quarter dividends or no quarter-end "
                     "value")),
    }

    report = {
        "quarter": quarter_label,
        "quarter_start": start.isoformat(),
        "quarter_end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": currency,
        "status": ("ok" if valued else "insufficient_data"),
        "disclaimer": _DISCLAIMER,
        "holdings_summary": {
            "positions": positions,
            "positions_valued": len(valued),
            "positions_total": len(positions),
            "total_cost": round(total_cost, 2),
            "start_value": round(start_value, 2),
            "end_value": round(end_value, 2),
            "quarter_gain": round(quarter_gain, 2),
        },
        "performance": {
            "quarter_return": quarter_return,
            "quarter_total_return": quarter_total_return,
            "time_weighted_return": twr,
            "money_weighted_return": mwr,
            "benchmark_return": benchmark_return,
            "excess_return_vs_benchmark": excess,
            "contributors": contributors,
            "detractors": detractors,
        },
        "risk": {
            "volatility": vol,
            "max_drawdown": drawdown,
            "portfolio_beta": portfolio_beta,
            "concentration": conc,
            "risk_flags": risk_flags,
        },
        "income": income,
        "valuation": valuation,
        "events": events,
        "quarter_over_quarter": None,  # filled below
        "limitations": sorted(set(limitations)),
        "summary_lines": [],
    }
    report["quarter_over_quarter"] = _compare_quarters(
        report, previous_report)
    if previous_report is None:
        limitations.append(
            "no previous quarter report supplied — "
            "quarter-over-quarter comparison unavailable")
        report["limitations"] = sorted(set(limitations))
    report["summary_lines"] = summary_lines(report)
    return report


# ---------------------------------------------------------------------------
# Summary lines and Markdown
# ---------------------------------------------------------------------------

def summary_lines(report):
    """One-line-per-section summary of a quarterly report."""
    perf = report["performance"]
    hold = report["holdings_summary"]

    def pct(metric):
        if metric.get("status") == "ok" and metric.get("value") is not None:
            return f"{metric['value']:.2%}"
        return "unavailable"

    lines = [
        f"Quarter {report['quarter']}: {hold['positions_valued']} of "
        f"{hold['positions_total']} positions valued; value "
        f"${hold['end_value']:,.2f} (start "
        f"${hold['start_value']:,.2f}, cost "
        f"${hold['total_cost']:,.2f}).",
        f"Quarter return {pct(perf['quarter_total_return'])} "
        f"including dividends "
        f"({pct(perf['quarter_return'])} price only); "
        f"time-weighted {pct(perf['time_weighted_return'])}, "
        f"money-weighted {pct(perf['money_weighted_return'])}.",
        f"Benchmark {pct(perf['benchmark_return'])}; excess "
        f"return {pct(perf['excess_return_vs_benchmark'])}.",
        f"Risk: volatility "
        f"{pct(report['risk']['volatility'])}, max drawdown "
        f"{pct(report['risk']['max_drawdown'])}, beta "
        f"{_beta_text(report['risk']['portfolio_beta'])}; "
        f"{len(report['risk']['risk_flags'])} risk flag(s).",
        f"Income: ${report['income']['dividends_received'].get('value') or 0:,.2f} "
        f"dividends in the quarter.",
        f"Valuation: "
        + "; ".join(
            f"{t}: {s.get('tier') or 'insufficient data'}"
            for t, s in report["valuation"].items())
        if report["valuation"] else "Valuation: no holdings.",
        f"Events in the quarter: {len(report['events'])}.",
        f"Limitations: {len(report['limitations'])} noted.",
    ]
    return lines


def _pct_text(metric):
    if metric.get("status") == "ok" and metric.get("value") is not None:
        return f"{metric['value']:.2%}"
    return f"unavailable ({metric.get('reason')})"


def _beta_text(metric):
    """Beta is a ratio, not a percentage — format it plainly."""
    if metric.get("status") == "ok" and metric.get("value") is not None:
        return f"{metric['value']:.2f}"
    return f"unavailable ({metric.get('reason')})"


def _money_text(value):
    return f"${value:,.2f}" if value is not None else "unknown"


def render_markdown(report):
    """Render a quarterly report dict as Markdown."""
    hold = report["holdings_summary"]
    perf = report["performance"]
    risk = report["risk"]
    lines = [f"# Quarterly portfolio report — {report['quarter']}",
             "",
             f"_Period: {report['quarter_start']} to "
             f"{report['quarter_end']}. Generated "
             f"{report['generated_at']}._",
             "",
             "Currency: " + (report["currency"] or
                           "not supplied — amounts are in the "
                           "currency of the inputs, never "
                           "assumed") + ".",
             "",
             "## Holdings",
             "",
             f"{hold['positions_valued']} of "
             f"{hold['positions_total']} positions valued. "
             f"Total cost {_money_text(hold['total_cost'])}, "
             f"quarter-start value "
             f"{_money_text(hold['start_value'])}, quarter-end "
             f"value {_money_text(hold['end_value'])}, quarter "
             f"gain {_money_text(hold['quarter_gain'])}.",
             "",
             "| Ticker | Shares | Cost | Start value | End value | "
             "Quarter gain | Quarter return |",
             "|---|---|---|---|---|---|---|",
             ]
    for pos in hold["positions"]:
        lines.append(
            f"| {pos['ticker']} | {pos['shares']:g} | "
            f"{_money_text(pos['shares'] * pos['cost_basis'])} | "
            f"{_money_text(pos['start_value'])} | "
            f"{_money_text(pos['end_value'])} | "
            f"{_money_text(pos['quarter_gain'])} | "
            f"{pos['quarter_return']:.2%}"
            if pos['quarter_return'] is not None else
            f"| {pos['ticker']} | {pos['shares']:g} | "
            f"{_money_text(pos['shares'] * pos['cost_basis'])} | "
            "— | — | — | "
            f"unavailable ({pos.get('reason')}) |")
    lines += [
        "",
        "## Performance",
        "",
        f"- Quarter return (price only): "
        f"{_pct_text(perf['quarter_return'])}",
        f"- Quarter total return (including dividends): "
        f"{_pct_text(perf['quarter_total_return'])}",
        f"- Time-weighted return: "
        f"{_pct_text(perf['time_weighted_return'])}",
        f"- Money-weighted return: "
        f"{_pct_text(perf['money_weighted_return'])}",
        f"- Benchmark return: "
        f"{_pct_text(perf['benchmark_return'])}",
        f"- Excess return vs benchmark: "
        f"{_pct_text(perf['excess_return_vs_benchmark'])}",
        "",
    ]
    if perf["contributors"]:
        lines.append("Contributors: " + "; ".join(
            f"{c['ticker']} "
            f"{_money_text(c['quarter_gain'])}"
            for c in perf["contributors"]) + ".")
    if perf["detractors"]:
        lines.append("Detractors: " + "; ".join(
            f"{d['ticker']} "
            f"{_money_text(d['quarter_gain'])}"
            for d in perf["detractors"]) + ".")
    lines += [
        "",
        "## Risk",
        "",
        f"- Volatility: {_pct_text(risk['volatility'])}",
        f"- Maximum drawdown: {_pct_text(risk['max_drawdown'])}",
        f"- Portfolio beta: {_beta_text(risk['portfolio_beta'])}",
    ]
    conc = risk["concentration"]
    if conc.get("total_value", 0) > 0:
        lines.append(
            f"- Concentration: largest name "
            f"{conc.get('max_single_name_ticker')} at "
            f"{conc.get('max_single_name_weight'):.1%} of value; "
            f"largest sector "
            f"{(conc.get('max_sector') or {}).get('sector')} at "
            f"{(conc.get('max_sector') or {}).get('weight', 0):.1%}; "
            f"largest country "
            f"{(conc.get('max_country') or {}).get('country')} at "
            f"{(conc.get('max_country') or {}).get('weight', 0):.1%}.")
    for flag in risk["risk_flags"]:
        lines.append(f"- Risk flag: {flag}")
    lines += [
        "",
        "## Income",
        "",
        f"- Dividends received in the quarter: "
        f"{_money_text(report['income']['dividends_received'].get('value'))}",
        f"- Yield on cost: "
        f"{_pct_text(report['income']['yield_on_cost'])}",
        f"- Yield on value: "
        f"{_pct_text(report['income']['yield_on_value'])}",
        "",
        "## Valuation snapshot",
        "",
    ]
    for ticker, snap in report["valuation"].items():
        if snap.get("status") == "ok":
            lines.append(
                f"- {ticker}: tier {snap.get('tier')}, quality "
                f"{snap.get('quality_rating')}, health "
                f"{snap.get('health_verdict')}, alert price "
                f"{_money_text(snap.get('alert_price'))}.")
        else:
            lines.append(
                f"- {ticker}: insufficient data — "
                f"{snap.get('reason') or 'no valuation available'}.")
    lines += ["", "## Events in the quarter", ""]
    if report["events"]:
        for event in report["events"]:
            lines.append(
                f"- {event['date']} — "
                f"{event['ticker'] or 'market-wide'} — "
                f"{event['kind']}: {event['title']} "
                f"({event['source']})")
    else:
        lines.append("No events recorded for these holdings in "
                     "this quarter.")
    lines += ["", "## Quarter over quarter", ""]
    qoq = report["quarter_over_quarter"]
    if qoq.get("status") == "ok":
        changes = qoq["value"]
        if changes["value_change"] is not None:
            vc = changes["value_change"]
            change_pct = (f"{vc['change_pct']:+.2%}"
                          if vc['change_pct'] is not None
                          else "n/a")
            lines.append(
                f"- Total value: "
                f"{_money_text(vc['previous'])} → "
                f"{_money_text(vc['current'])} "
                f"({vc['change']:+,.2f}, {change_pct}).")
        if changes["return_change"] is not None:
            rc = changes["return_change"]
            lines.append(
                f"- Quarter return: {rc['previous']:.2%} → "
                f"{rc['current']:.2%} "
                f"({rc['change']:+.2%}).")
        for change in changes["tier_changes"]:
            lines.append(
                f"- {change['ticker']}: tier "
                f"{change['previous']} → {change['current']}.")
        for change in changes["alert_price_changes"]:
            lines.append(
                f"- {change['ticker']}: alert price "
                f"{_money_text(change['previous'])} → "
                f"{_money_text(change['current'])}.")
        for flag in changes["flags_added"]:
            lines.append(f"- Risk flag added: {flag}")
        for flag in changes["flags_removed"]:
            lines.append(f"- Risk flag removed: {flag}")
        for note in changes["notes"]:
            lines.append(f"- {note}")
    else:
        lines.append(f"Unavailable: {qoq.get('reason')}")
    lines += ["", "## Limitations", ""]
    if report["limitations"]:
        for limitation in report["limitations"]:
            lines.append(f"- {limitation}")
    else:
        lines.append("None noted.")
    lines += ["", "---", "", report["disclaimer"], ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _default_report_dir():
    project_root = Path(__file__).resolve().parent.parent
    return project_root / DEFAULT_REPORT_DIR


def save_report(report, directory=None):
    """Save a report as JSON and Markdown, one file per quarter.

    Returns {"json_path", "markdown_path"}.  Never writes to
    the production database.
    """
    directory = Path(directory) if directory is not None \
        else _default_report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = report["quarter"].replace(" ", "-").replace("/", "-")
    json_path = directory / f"{stem}-quarterly-report.json"
    md_path = directory / f"{stem}-quarterly-report.md"
    json_path.write_text(json.dumps(report, indent=2,
                                    default=str))
    md_path.write_text(render_markdown(report))
    return {"json_path": str(json_path),
            "markdown_path": str(md_path)}


def load_report(path):
    """Load a previously saved quarterly report (JSON)."""
    return json.loads(Path(path).read_text())
