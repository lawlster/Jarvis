"""Step 33b — macroeconomic and sector analysis (Phase E).

The roadmap's Step 33 ("macroeconomic and sector analysis") was
built as earnings calendar and event tracking per the Step 33
brief, so this step is numbered 33b (same precedent as Step
28/28b) to keep the later numbering unchanged.

Two parts, both caller-supplied data only — nothing is fetched,
nothing is sent, nothing is scheduled:

1. A macro record store.  A macro data point is one indicator
   value on one date (e.g. "S&P 500 daily change", -4.36, "%",
   "2026-11-05", source "owner research").  Points are stored as
   one JSON file each under a caller-chosen directory (default
   ``data/macro/``), following the Step 31/33 file pattern.  The
   store answers "what is the latest value of each indicator as
   of a date" and "what changed between two dates".

2. Sector analysis.  Given a sector name and a caller-supplied
   list of tickers in that sector (or the stored
   ``market_universe`` rows where sector data exists), it
   aggregates the Step 17-24 outputs across the tickers that
   have statements: median margins, ROE, ROIC, growth, P/E, P/B,
   quality distribution, tier distribution, and the count of
   tickers with data vs without.  A sector-vs-market comparison
   puts the same aggregates side by side.  Differences are
   stated as numbers only — never as a verdict on which set is
   "better", and never as a buy/sell call.

Data rules, stated on every result:

- Macro data is caller-supplied.  No value in the store was
  fetched by this module, and no value is presented as
  fetched or DB-validated.  Every point carries its source.
- Sector relevance of a macro indicator (e.g. rates for
  banks/REITs) is a caller-supplied association, never
  invented by the module: the caller passes the mapping.
- An aggregate over 1 ticker is labelled a single-company
  figure, not a sector figure.  Every aggregate states how
  many tickers contributed; a metric with no contributors is
  missing with a reason, never zero.
- Sector is NULL for every US/HK row in the stored
  ``market_universe``, so selecting a sector from stored
  data returns nothing on the real database — the caller
  passes explicit tickers until sector data is collected.
- The module never opens the production database for writing
  (``run_sector_analysis`` opens it read-only) and makes no
  network requests.
"""

import json
import re
import statistics
import time
from collections import Counter
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path

from agents import company_comparison as cc
from agents import market_scanner as ms

DEFAULT_MACRO_DIR = Path(__file__).resolve().parent.parent / "data" / "macro"
BASE_DIR = Path(__file__).resolve().parent.parent

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_iso():
    return _date.today().isoformat()


def _parse_date(value):
    """Validate a YYYY-MM-DD date; return the string or raise."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    try:
        _date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r}") from exc
    return value


def _indicator_key(indicator):
    """Normalised lookup key for an indicator name."""
    return re.sub(r"\s+", " ", str(indicator).strip()).lower()


def _slug(text):
    return _SLUG_RE.sub("_", text.lower()).strip("_") or "indicator"


# ---------------------------------------------------------------------------
# Macro record store
# ---------------------------------------------------------------------------


def validate_macro_point(point):
    """Validate a macro point dict; return a normalised copy or raise.

    Required: ``indicator`` (non-empty string), ``value``
    (a finite number — bool is not a number here), ``date``
    (YYYY-MM-DD).  Optional: ``unit``, ``source``, ``notes``
    (strings or None).  Extra keys are dropped.
    """
    if not isinstance(point, dict):
        raise ValueError(f"macro point must be a dict, got {point!r}")
    indicator = point.get("indicator")
    if not isinstance(indicator, str) or not indicator.strip():
        raise ValueError(
            f"indicator must be a non-empty string, got {indicator!r}")
    value = point.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"value must be a number, got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"value must be finite, got {value!r}")
    point_date = _parse_date(point.get("date"))
    normalised = {
        "indicator": indicator.strip(),
        "indicator_key": _indicator_key(indicator),
        "value": value,
        "date": point_date,
        "unit": None,
        "source": None,
        "notes": None,
    }
    for field in ("unit", "source", "notes"):
        raw = point.get(field)
        if raw is not None:
            if not isinstance(raw, str):
                raise ValueError(
                    f"{field} must be a string or None, got {raw!r}")
            normalised[field] = raw.strip() or None
    return normalised


def _macro_id(point, macro_dir):
    """Next id for this date+indicator: ``YYYY-MM-DD_<slug>_01``."""
    prefix = f"{point['date']}_{_slug(point['indicator_key'])}_"
    existing = sorted(Path(macro_dir).glob(f"{prefix}*.json")) \
        if Path(macro_dir).exists() else []
    return f"{prefix}{len(existing) + 1:02d}"


def add_macro_point(macro_dir=None, indicator=None, value=None,
                    unit=None, date=None, source=None, notes=None):
    """Add one caller-supplied macro data point; return the saved dict.

    The point is stored as one JSON file under ``macro_dir``
    (default ``data/macro/``).  Raises ValueError on invalid
    input.  Nothing is fetched and nothing is written to the
    database.  ``date`` defaults to today (UTC).
    """
    directory = Path(macro_dir) if macro_dir else DEFAULT_MACRO_DIR
    point = validate_macro_point({
        "indicator": indicator,
        "value": value,
        "date": date if date is not None else _today_iso(),
        "unit": unit,
        "source": source,
        "notes": notes,
    })
    point["macro_id"] = _macro_id(point, directory)
    point["added_at"] = _utcnow_iso()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{point['macro_id']}.json"
    point["file_path"] = str(path)
    path.write_text(json.dumps(point, indent=2), encoding="utf-8")
    return point


def load_macro_point(path):
    """Load one macro point JSON file; return the dict or None.

    A corrupt file returns None (callers skip it) instead of
    raising, so one bad file never hides the rest of the store.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return validate_macro_point(data)
    except ValueError:
        return None


def list_macro_points(macro_dir=None, indicator=None, since=None,
                      until=None):
    """Stored macro points, newest first (corrupt files skipped).

    ``indicator`` matches case-insensitively; ``since``/``until``
    are inclusive YYYY-MM-DD bounds.  Each returned dict is the
    stored point (with ``macro_id`` and ``file_path`` when present).
    """
    directory = Path(macro_dir) if macro_dir else DEFAULT_MACRO_DIR
    if since is not None:
        since = _parse_date(since)
    if until is not None:
        until = _parse_date(until)
    wanted = _indicator_key(indicator) if indicator else None
    points = []
    if not directory.exists():
        return points
    for path in sorted(directory.glob("*.json")):
        raw = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            point = validate_macro_point(raw)
        except ValueError:
            continue
        point["macro_id"] = raw.get("macro_id")
        point["file_path"] = raw.get("file_path", str(path))
        point["added_at"] = raw.get("added_at")
        if wanted and point["indicator_key"] != wanted:
            continue
        if since and point["date"] < since:
            continue
        if until and point["date"] > until:
            continue
        points.append(point)
    points.sort(key=lambda p: (p["date"], p.get("added_at") or ""),
                reverse=True)
    return points


def macro_snapshot(macro_dir=None, as_of=None):
    """Latest point per indicator on or before ``as_of`` (default today).

    Returns {"as_of", "indicators": {indicator_key: point},
    "count", "note"}.  A point dated after ``as_of`` is not in
    the snapshot — the snapshot is a point-in-time view, so a
    later revision never leaks into an earlier date.
    """
    as_of = _parse_date(as_of) if as_of is not None else _today_iso()
    latest = {}
    for point in list_macro_points(macro_dir, until=as_of):
        key = point["indicator_key"]
        if key not in latest:
            # list_macro_points is newest-first, so the first
            # point per indicator is the latest on or before as_of.
            latest[key] = point
    return {
        "as_of": as_of,
        "indicators": latest,
        "count": len(latest),
        "note": ("latest stored point per indicator on or before "
                 f"{as_of}; caller-supplied data only, nothing fetched"),
    }


def what_changed(macro_dir=None, earlier=None, later=None):
    """Compare two macro snapshots and list what moved.

    ``earlier`` and ``later`` are YYYY-MM-DD dates (``later``
    defaults to today).  Returns {"earlier_date", "later_date",
    "changes", "added", "dropped", "unchanged"} where each
    change is {indicator, earlier_value, later_value, change,
    change_pct, direction, unit}.  ``change_pct`` is None when
    the earlier value is 0.  ``direction`` is "up", "down" or
    "unchanged".  An indicator present in only one snapshot is
    listed under "added" or "dropped", never silently ignored.
    """
    if earlier is None:
        raise ValueError("earlier date is required")
    earlier = _parse_date(earlier)
    later = _parse_date(later) if later is not None else _today_iso()
    if later < earlier:
        raise ValueError(
            f"later date {later} is before earlier date {earlier}")
    before = macro_snapshot(macro_dir, as_of=earlier)["indicators"]
    after = macro_snapshot(macro_dir, as_of=later)["indicators"]
    changes = []
    added = []
    dropped = []
    unchanged = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            added.append(after[key])
            continue
        if key not in after:
            dropped.append(before[key])
            continue
        old = before[key]
        new = after[key]
        if old["value"] == new["value"]:
            unchanged.append({
                "indicator": new["indicator"],
                "value": new["value"],
                "unit": new.get("unit"),
                "date": new["date"],
            })
            continue
        change = new["value"] - old["value"]
        changes.append({
            "indicator": new["indicator"],
            "earlier_value": old["value"],
            "later_value": new["value"],
            "change": change,
            "change_pct": (change / old["value"] * 100
                           if old["value"] != 0 else None),
            "direction": "up" if change > 0 else "down",
            "unit": new.get("unit") or old.get("unit"),
            "earlier_date": old["date"],
            "later_date": new["date"],
        })
    return {
        "earlier_date": earlier,
        "later_date": later,
        "changes": changes,
        "added": added,
        "dropped": dropped,
        "unchanged": unchanged,
    }


def relevant_macro_points(sector, macro_dir=None, associations=None,
                          as_of=None):
    """Macro points relevant to a sector, per caller-supplied associations.

    ``associations`` maps a sector name to a list of indicator
    names (case-insensitive match on the sector).  Relevance is
    caller-defined — the module invents no mapping of its own:
    with no associations (or no entry for this sector) the
    result is empty and says so.  Returns {"sector",
    "associations_source", "indicators_requested", "points",
    "missing", "note"} where "points" maps each requested
    indicator to its latest point on or before ``as_of``
    (or None) and "missing" lists the requested indicators
    with no stored point.
    """
    if not isinstance(sector, str) or not sector.strip():
        raise ValueError(f"sector must be a non-empty string, got {sector!r}")
    sector = sector.strip()
    associations = associations or {}
    wanted = None
    for key, indicators in associations.items():
        if str(key).strip().lower() == sector.lower():
            wanted = list(indicators)
            break
    snapshot = macro_snapshot(macro_dir, as_of=as_of)["indicators"]
    points = {}
    missing = []
    for name in (wanted or []):
        key = _indicator_key(name)
        if key in snapshot:
            points[name] = snapshot[key]
        else:
            points[name] = None
            missing.append(name)
    return {
        "sector": sector,
        "associations_source": (
            "caller-supplied" if wanted is not None
            else "none supplied — no relevance mapping for this sector"),
        "indicators_requested": wanted or [],
        "points": points,
        "missing": missing,
        "note": ("relevance is caller-defined; the module invents "
                 "no sector-to-indicator mapping of its own"),
    }


# ---------------------------------------------------------------------------
# Sector analysis
# ---------------------------------------------------------------------------


def _aggregate(values):
    """Median/min/max/count for a list of present values (or missing)."""
    present = [v for v in values if v is not None]
    if not present:
        return {
            "median": None,
            "min": None,
            "max": None,
            "count": 0,
            "single_company": False,
            "present": False,
            "reason": "no ticker in the set has this metric",
        }
    return {
        "median": statistics.median(present),
        "min": min(present),
        "max": max(present),
        "count": len(present),
        "single_company": len(present) == 1,
        "present": True,
        "reason": (None if len(present) > 1 else
                   "single-company figure, not a sector figure: "
                   "only 1 ticker contributes to this metric"),
    }


def analyse_sector(conn, sector=None, tickers=None, prices=None,
                   sector_overrides=None, macro_dir=None,
                   associations=None, as_of=None, label=None):
    """Aggregate Step 17-24 outputs across a sector's tickers.

    ``sector`` is the sector name; ``tickers`` is an explicit
    list of tickers in the sector (at least one of the two is
    required).  With no explicit tickers the selection is the
    stored ``market_universe`` rows for the sector — on the real
    database that is empty, because sector is NULL for every
    US/HK row, and the result says so instead of guessing.
    ``prices`` maps ticker -> price (number or price-input dict);
    tickers without one fall back to their latest snapshot price
    inside the pipeline, labelled with its as-of date.

    Returns the sector label, the selection source, coverage,
    per-metric aggregates (median/min/max/contributing count),
    quality / tier / health distributions, one row per ticker,
    and the macro context for the sector.  Every aggregate
    states how many tickers contributed.  No buy/sell call,
    no "better"/"worse" judgement — the numbers are the output.
    """
    if (sector is None or not str(sector).strip()) and not tickers:
        raise ValueError(
            "a sector name or an explicit ticker list is required")
    sector = str(sector).strip() if sector else None
    label = label or sector or "custom selection"
    started = time.perf_counter()
    warnings = []

    if tickers is not None:
        selection = []
        seen = set()
        for ticker in tickers:
            key = str(ticker).upper()
            if key in seen:
                continue
            seen.add(key)
            selection.append(key)
        selection_source = "explicit tickers"
    else:
        rows = ms.load_universe(conn, sector=sector)
        selection = [str(r["ticker"]).upper() for r in rows]
        selection_source = "stored market_universe sector"
        if not selection:
            warnings.append(
                f"no market_universe rows carry sector {sector!r} — "
                "sector is NULL for every US/HK row in the stored "
                "universe, so selection by stored sector is empty; "
                "pass explicit tickers")

    scan = ms.scan_market(conn, tickers=selection or [],
                          prices=prices,
                          sector_overrides=sector_overrides)
    scan_by_ticker = {str(r["ticker"]).upper(): r
                      for r in scan["results"]}
    counts = ms.statement_row_counts(conn)

    profiles = {}
    for ticker in selection:
        if counts.get(ticker, 0) == 0:
            continue
        try:
            profiles[ticker] = cc.build_company_profile(
                conn, ticker,
                price_input=(prices or {}).get(ticker))
        except Exception as exc:  # noqa: BLE001 - report, never crash
            warnings.append(
                f"profile failed for {ticker}: {exc}")

    aggregates = {}
    for metric in cc.METRICS:
        values = []
        for ticker, profile in profiles.items():
            entry = profile["metrics"].get(metric, {})
            if entry.get("present"):
                values.append(entry.get("value"))
        aggregates[metric] = _aggregate(values)
        aggregates[metric]["label"] = cc.METRICS[metric][0]
        aggregates[metric]["group"] = cc.METRICS[metric][1]

    def _distribution(field):
        counter = Counter()
        for ticker in selection:
            value = scan_by_ticker.get(ticker, {}).get(field)
            counter[value if value is not None else "not assessed"] += 1
        return dict(counter)

    quality_distribution = _distribution("quality_rating")
    tier_distribution = _distribution("tier")
    health_distribution = _distribution("health_verdict")

    per_ticker = []
    for ticker in selection:
        row = scan_by_ticker.get(ticker, {})
        per_ticker.append({
            "ticker": ticker,
            "company_name": row.get("company_name"),
            "country": row.get("country"),
            "exchange": row.get("exchange"),
            "has_statements": counts.get(ticker, 0) > 0,
            "statement_rows": counts.get(ticker, 0),
            "scannable": bool(row.get("scannable")),
            "tier": row.get("tier"),
            "quality_rating": row.get("quality_rating"),
            "health_verdict": row.get("health_verdict"),
            "price": row.get("price"),
            "price_as_of": row.get("price_as_of"),
            "missing_inputs": list(row.get("missing_inputs", [])),
        })
    with_statements = sum(1 for t in per_ticker if t["has_statements"])
    if with_statements == 1:
        warnings.append(
            "only 1 ticker in the set has statements — every "
            "aggregate is a single-company figure, not a sector "
            "figure")
    if with_statements == 0:
        warnings.append(
            "no ticker in the set has statements — no aggregate "
            "could be computed")

    macro_context = relevant_macro_points(
        sector or label, macro_dir=macro_dir,
        associations=associations, as_of=as_of)
    run_seconds = time.perf_counter() - started
    return {
        "sector": sector,
        "label": label,
        "selection_source": selection_source,
        "generated_at": _utcnow_iso(),
        "run_seconds": run_seconds,
        "coverage": {
            "selected": len(selection),
            "with_statements": with_statements,
            "scannable": sum(1 for t in per_ticker if t["scannable"]),
            "with_price": sum(
                1 for t in per_ticker if t["price"] is not None),
            "with_tier": sum(
                1 for t in per_ticker
                if t["tier"] not in (None, "insufficient_data")),
            "tickers": selection,
        },
        "aggregates": aggregates,
        "quality_distribution": quality_distribution,
        "tier_distribution": tier_distribution,
        "health_distribution": health_distribution,
        "per_ticker": per_ticker,
        "macro_context": macro_context,
        "warnings": warnings + list(scan.get("warnings", [])),
    }


def compare_analyses(sector_analysis, market_analysis):
    """Side-by-side aggregates for a sector vs a market set.

    Both arguments are results of :func:`analyse_sector`.
    Returns per-metric {sector_median, market_median,
    difference, difference_pct, sector_count, market_count,
    comparable} plus the two quality/tier distributions.
    "comparable" is False when either side has no contributors
    for the metric.  Differences are descriptive only: no
    judgement is made about which set is better, and no
    buy/sell call is made anywhere.
    """
    metrics = {}
    for name in cc.METRICS:
        sector_agg = sector_analysis["aggregates"].get(name, {})
        market_agg = market_analysis["aggregates"].get(name, {})
        sector_median = sector_agg.get("median")
        market_median = market_agg.get("median")
        comparable = (sector_median is not None
                      and market_median is not None)
        difference = None
        difference_pct = None
        if comparable:
            difference = sector_median - market_median
            if market_median != 0:
                difference_pct = difference / market_median * 100
        metrics[name] = {
            "label": cc.METRICS[name][0],
            "group": cc.METRICS[name][1],
            "sector_median": sector_median,
            "market_median": market_median,
            "difference": difference,
            "difference_pct": difference_pct,
            "sector_count": sector_agg.get("count", 0),
            "market_count": market_agg.get("count", 0),
            "comparable": comparable,
        }
    return {
        "sector_label": sector_analysis.get("label"),
        "market_label": market_analysis.get("label"),
        "metrics": metrics,
        "quality_distribution_sector": sector_analysis.get(
            "quality_distribution", {}),
        "quality_distribution_market": market_analysis.get(
            "quality_distribution", {}),
        "tier_distribution_sector": sector_analysis.get(
            "tier_distribution", {}),
        "tier_distribution_market": market_analysis.get(
            "tier_distribution", {}),
        "note": ("differences are descriptive only — medians of "
                 "different sets of companies; no judgement is made "
                 "about which set is better, and this is research, "
                 "not a recommendation"),
    }


def compare_sector_to_market(conn, sector, market_tickers=None,
                             prices=None, market_prices=None,
                             sector_tickers=None,
                             sector_overrides=None, macro_dir=None,
                             associations=None, as_of=None):
    """Analyse one sector and a market set, then compare them.

    ``sector_tickers`` is an explicit ticker list for the sector
    side; without it the sector side is selected from the stored
    ``market_universe`` rows for the sector (empty on the real
    database, where sector is NULL for every US/HK row — pass
    explicit tickers).  ``market_tickers`` is an explicit ticker
    list for the market set; when None the whole stored universe
    is the market set.  ``prices`` are the sector tickers'
    prices; ``market_prices`` the market set's.  Returns
    {"sector", "market", "comparison"}.
    """
    sector_result = analyse_sector(
        conn, sector=sector, tickers=sector_tickers, prices=prices,
        sector_overrides=sector_overrides, macro_dir=macro_dir,
        associations=associations, as_of=as_of)
    if market_tickers is not None:
        market_result = analyse_sector(
            conn, sector=None, tickers=market_tickers,
            prices=market_prices, sector_overrides=sector_overrides,
            macro_dir=macro_dir, associations=associations,
            as_of=as_of, label="market")
    else:
        # Whole universe as the market set: explicit tickers from
        # the stored universe rows (analyse_sector needs a sector
        # name or an explicit list).
        universe_tickers = [str(r["ticker"]).upper()
                            for r in ms.load_universe(conn)]
        market_result = analyse_sector(
            conn, sector=None, tickers=universe_tickers,
            prices=market_prices, sector_overrides=sector_overrides,
            macro_dir=macro_dir, associations=associations,
            as_of=as_of, label="market")
    return {
        "sector": sector_result,
        "market": market_result,
        "comparison": compare_analyses(sector_result, market_result),
    }


# ---------------------------------------------------------------------------
# Dry-run entry point (no main.py changes)
# ---------------------------------------------------------------------------


def run_sector_analysis(db_path=None, sector=None, tickers=None,
                        **kwargs):
    """Dry-run entry: analyse a sector on a database file.

    Opens the database read-only (``mode=ro``) and makes no
    writes and no network requests.  ``db_path`` defaults to
    ``database/assistant.db`` next to this package's project root.
    """
    import os
    import sqlite3
    if db_path is None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "database",
                               "assistant.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return analyse_sector(conn, sector=sector, tickers=tickers,
                              **kwargs)
    finally:
        conn.close()
