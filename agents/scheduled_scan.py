"""Step 59 — Automatic daily live scan after the US close.

The owner asked for an AI agent that continuously checks for
valued stocks to buy: Jarvis only answers when asked, it never
goes looking on its own.  This module is the automatic part —
once a day, after the US close (05:30 SGT, which is after the
16:00 Eastern close year-round), it runs the Step 54 live
watchlist scan, stores the run, diffs it against the previous
stored run, and raises Step 38 alerts for new strongest-alert
bands.

Design decisions, each honest about its limits:

- The scan reuses :func:`agents.live_watchlist_scan.
  scan_watchlist_live` — no duplicated fetch/DCF/banding logic.
  One full run takes ~20 minutes over 38 tickers.
- Cadence is once a day, not continuous: the DCF inputs
  (annual free cash flow) barely move day to day — only the
  price moves — so scanning every few minutes adds nothing.
- The practical route is a Windows Task Scheduler task on the
  owner's PC that runs ``python -m agents.scheduled_scan run``
  and exits (see ``scripts/setup_daily_scan.ps1``).  Jarvis is
  NOT always-on (Step 53: not ready, 9 blockers; no service
  exists), and the Step 46 in-process scheduler has no real
  delivery and needs Jarvis left running.  The task needs no
  always-on Jarvis.
- Two hard limits of the scheduled route: (1) the PC must be
  on or asleep (not fully shut down) at 05:30 SGT, or the run
  is missed — a missed run is recorded as missed, never
  backfilled with invented data; (2) the first run on the
  owner's PC needs him present for the Yahoo approval cards
  (the Step 54 run stalled ~15 minutes on them here) — after
  he taps always-allow, scheduled runs proceed unattended.  A
  scheduled run that hits an unapproved card fails loudly in
  its log (provider_unavailable tickers listed, exit code 0
  only when the scan itself ran), never silently pretending
  the scan happened.
- New-candidate alerts go through the Step 38 alert engine
  (``agents/watchlist_alerts.py``), so ``/alerts`` and the
  home page list them like any other alert.  The engine's
  reason text for a live-derived band is provisional (Step 59
  change in ``_rules_for_ticker``): a live band is a margin
  band only — quality and deterioration are unknown from a
  live pull, so it is NOT a confirmed Step 26 candidate and
  the reason never claims otherwise.  Every scannable ticker
  is evaluated, banded or not, so a band that disappeared
  clears its alert; a ticker whose live pull failed is not
  evaluated and keeps its alerts.  The quality-change rule is
  suppressed for live evidence (a stored-data rating changing
  to "unknown (live pull)" is a change in evidence, not in
  the business).
- Live data is used in memory for the scan only, never
  written into the production database.  Run files live under
  ``data/scans/`` (one JSON file per run, named by the SGT
  date) and the latest run also refreshes ``step54-live-scan.
  json`` in the repo root, which is the file the dashboard
  already reads — so the dashboard shows the latest scheduled
  scan without any other change.
- A lock file (``data/scans/.scan.lock``) prevents two scans
  running at once.  A lock older than 3 hours is treated as
  stale (a crashed run) and taken over, with the fact noted
  in the run record — otherwise one crash would block every
  later daily run forever.
- No trade is ever placed or proposed here: a new band is a
  name worth a closer look, and the final decision is always
  the owner's.  The word "buy" never appears in a run or
  alert line.
"""

import argparse
import json
import os
import sys
import time

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents import live_fallback as lfb
from agents import live_watchlist_scan as lws
from agents import watchlist_alerts as wa
from agents.watchlist_foundation import list_tickers

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The daily run time, Singapore time.  The US close is 16:00
# Eastern — 05:00 SGT in EST (Nov–Mar), 04:00 SGT in EDT
# (Mar–Nov) — so 05:30 SGT is after the close year-round.
SCHEDULED_TIME_SGT = "05:30"
SGT_OFFSET = timedelta(hours=8)

DEFAULT_SCANS_DIR = PROJECT_ROOT / "data" / "scans"
DEFAULT_LIVE_SCAN_PATH = PROJECT_ROOT / "step54-live-scan.json"

LOCK_FILENAME = ".scan.lock"
# A lock older than this is a crashed run, not a live one.
LOCK_STALE_SECONDS = 3 * 3600

RUN_FILENAME_RE = None  # built lazily, to keep the import light


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sgt_date(utc_value):
    """SGT calendar date (YYYY-MM-DD) for a UTC timestamp.

    Singapore is UTC+8 year-round (no daylight saving), so a
    fixed offset is exact — no tzdata dependency, which matters
    on the owner's Windows PC.
    """
    parsed = _parse_iso(utc_value)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + SGT_OFFSET).date().isoformat()


def _scans_dir(scans_dir):
    return Path(scans_dir) if scans_dir else DEFAULT_SCANS_DIR


def _run_filename(sgt_date_str):
    return f"{sgt_date_str}.json"


# ---------------------------------------------------------------------------
# Run storage
# ---------------------------------------------------------------------------

def list_runs(scans_dir=None):
    """Run files in the scans dir, oldest first.

    Only YYYY-MM-DD.json files count as runs; the lock file,
    the daily log and anything else are ignored.  A file that
    cannot be read is still listed (its date is known) — the
    caller decides what to do with it.
    """
    import re

    directory = _scans_dir(scans_dir)
    if not directory.exists():
        return []
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and pattern.match(p.name))


def latest_run(scans_dir=None):
    """(run_data, path) for the newest parseable run, or (None, None).

    A run that cannot be parsed is skipped — a corrupt latest
    file must not hide the previous good run, and must not
    crash the dashboard.  Nothing is fabricated for a date
    with no run file: no run means no run.
    """
    for path in reversed(list_runs(scans_dir)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(
                data.get("results"), list):
            return data, path
    return None, None


def previous_scan(scans_dir=None, live_scan_path=None):
    """The scan to diff against: latest scheduled run, else the
    stored Step 54 live scan, else None.

    The first scheduled run therefore diffs against the
    2026-11-09 Step 54 run rather than calling every band
    "new" — the bands were already found once, by hand.
    """
    run, _path = latest_run(scans_dir)
    if run is not None:
        return run
    data, _error = lfb.load_stored_scan(live_scan_path)
    return data


# ---------------------------------------------------------------------------
# Lock — one scan at a time
# ---------------------------------------------------------------------------

class ScanAlreadyRunning(Exception):
    """Another scan holds the lock (a second run must not start)."""


def acquire_lock(scans_dir=None):
    """Create the lock file, or raise :class:`ScanAlreadyRunning`.

    Returns (lock_path, stale_removed).  A lock older than
    :data:`LOCK_STALE_SECONDS` is a crashed run: it is removed
    and the fact is reported so the run record can say so.
    The lock file holds the pid and start time for diagnosis.
    """
    directory = _scans_dir(scans_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_FILENAME
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0
        if age <= LOCK_STALE_SECONDS:
            raise ScanAlreadyRunning(
                f"another scan is already running (lock {lock_path}, "
                f"age {age:.0f}s) — refusing to start a second one")
        try:
            lock_path.unlink()
        except OSError:
            pass
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        stale_removed = True
    else:
        stale_removed = False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"pid": os.getpid(),
                        "started_at": _utcnow_iso()}))
    return lock_path, stale_removed


def release_lock(lock_path):
    """Remove the lock file, never raising."""
    try:
        Path(lock_path).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Diff — what changed since the previous run
# ---------------------------------------------------------------------------

def _band_map(scan):
    """ticker -> (detection_band, status) for a scan result."""
    mapping = {}
    for record in (scan or {}).get("results", []) or []:
        ticker = record.get("ticker")
        if ticker:
            mapping[ticker] = (
                record.get("detection_band"),
                record.get("status"))
    return mapping


def _diff_entry(record, previous_band):
    """A diff entry for one ticker: band, price, range, margin."""
    fair_value_range = record.get("fair_value_range") or {}
    return {
        "ticker": record.get("ticker"),
        "band": record.get("detection_band"),
        "previous_band": previous_band,
        "price": record.get("price"),
        "price_as_of": record.get("price_as_of"),
        "fair_value_range": fair_value_range,
        "margin_of_safety_vs_conservative": record.get(
            "margin_of_safety_vs_conservative"),
        "provisional": True,
    }


def diff_runs(previous, current):
    """What changed between two scan results.

    ``previous`` is None on the very first run with no stored
    scan at all — then every current band is new, and the diff
    says so (``previous_scanned_at`` is None) instead of
    pretending a baseline existed.  A ticker whose current
    status is not ``ok`` is never called "disappeared": an
    unknown band is unknown, not gone — it is listed under
    ``newly_unavailable`` instead.  Returns a dict with
    ``new_strongest_alert``, ``new_watch``, ``disappeared``,
    ``newly_scannable``, ``newly_unavailable`` and
    ``unchanged_bands``.
    """
    previous_map = _band_map(previous)
    current_map = _band_map(current)
    current_records = {
        r.get("ticker"): r
        for r in (current or {}).get("results", []) or []
        if r.get("ticker")}
    previous_records = {
        r.get("ticker"): r
        for r in (previous or {}).get("results", []) or []
        if r.get("ticker")}
    diff = {
        "previous_scanned_at": (
            (previous or {}).get("scanned_at")
            if previous else None),
        "current_scanned_at": (current or {}).get("scanned_at"),
        "new_strongest_alert": [],
        "new_watch": [],
        "disappeared": [],
        "newly_scannable": [],
        "newly_unavailable": [],
        "unchanged_bands": 0,
    }
    for ticker, record in current_records.items():
        previous_band, previous_status = previous_map.get(
            ticker, (None, None))
        band = record.get("detection_band")
        status = record.get("status")
        if band == lws.BAND_STRONGEST_ALERT \
                and previous_band != lws.BAND_STRONGEST_ALERT:
            diff["new_strongest_alert"].append(
                _diff_entry(record, previous_band))
        elif band in (lws.BAND_WATCH_DISCOUNT,
                      lws.BAND_WATCH_BELOW_BASE) \
                and previous_band != band:
            diff["new_watch"].append(_diff_entry(record, previous_band))
        elif band is not None and band == previous_band:
            diff["unchanged_bands"] += 1
        if previous_status is not None \
                and previous_status != "ok" and status == "ok":
            diff["newly_scannable"].append({
                "ticker": ticker,
                "previous_status": previous_status,
                "status": status})
        if previous_status == "ok" and status != "ok":
            diff["newly_unavailable"].append({
                "ticker": ticker,
                "previous_status": previous_status,
                "status": status,
                "reason": record.get("reason")})
    for ticker, (previous_band, _status) in previous_map.items():
        record = current_records.get(ticker)
        if previous_band is None or record is None:
            continue
        if record.get("status") == "ok" \
                and record.get("detection_band") is None:
            diff["disappeared"].append({
                "ticker": ticker,
                "previous_band": previous_band,
                "price": record.get("price"),
                "price_as_of": record.get("price_as_of"),
                "fair_value_range":
                    record.get("fair_value_range") or {},
                "margin_of_safety_vs_conservative": record.get(
                    "margin_of_safety_vs_conservative"),
            })
    return diff


# ---------------------------------------------------------------------------
# Alerts — new bands through the Step 38 engine
# ---------------------------------------------------------------------------

def raise_live_alerts(scan_result, store_dir=None,
                      alert_state_path=None, as_of=None):
    """Raise/clear Step 38 alerts for one live-scan result.

    Banded records become Step 26-shaped candidates via the
    Step 58 helper (which carries the provisional flag, so the
    engine's reasons stay honest — see
    ``agents/watchlist_alerts.py``).  The engine dedupes: a
    band that was already raised stays ``already_active``, a
    band that disappeared is cleared.  Returns the evaluation
    result plus ``"state_file"``.  No network, no database
    writes, no sending.
    """
    tickers = list_tickers(store_dir)
    # Candidates for EVERY scannable record, banded or not: the
    # engine only clears an alert for a ticker it evaluated,
    # so a banded ticker whose band disappeared must still be
    # evaluated (with no band, so nothing fires) for its alert
    # to clear.  A ticker whose live pull failed is not
    # evaluable and keeps its alerts — absence of evidence is
    # not evidence of clearing.
    candidates = [
        lfb.candidate_from_live_record(record, "scheduled_scan")
        for record in scan_result.get("results", []) or []
        if record.get("status") == "ok"]
    detection = {"candidates": candidates}
    state = wa.load_state(alert_state_path)
    result = wa.evaluate_alerts(
        tickers, detection=detection, state=state,
        as_of=as_of or _utcnow_iso())
    result["state_file"] = wa.save_state(state, alert_state_path)
    return result


# ---------------------------------------------------------------------------
# The daily run
# ---------------------------------------------------------------------------

def run_daily_scan(scans_dir=None, store_dir=None,
                   alert_state_path=None, live_scan_path=None,
                   fetcher=None,
                   rate_limit_seconds=lws.DEFAULT_RATE_LIMIT_SECONDS,
                   sleep=None, as_of=None):
    """Run one scheduled daily scan, store it, diff it, alert on it.

    Returns the run record.  The run file is written under
    ``data/scans/YYYY-MM-DD.json`` (SGT date of the run) and
    the latest run also refreshes ``step54-live-scan.json`` in
    the repo root, which the dashboard already reads.  A run
    with provider-unavailable tickers still completes and
    stores its partial results with the failures listed —
    that is a partial run, not a failed one.  Raises
    :class:`ScanAlreadyRunning` when the lock is held.  Never
    writes to the production database.
    """
    directory = _scans_dir(scans_dir)
    lock_path, stale_removed = acquire_lock(directory)
    try:
        previous = previous_scan(directory, live_scan_path)
        started = time.monotonic()
        result = lws.scan_watchlist_live(
            store_dir=store_dir, fetcher=fetcher,
            rate_limit_seconds=rate_limit_seconds, sleep=sleep,
            as_of=as_of)
        run_seconds = time.monotonic() - started
        result["run_seconds"] = run_seconds
        diff = diff_runs(previous, result)
        alerts = {"new_alerts": [], "already_active": [],
                  "cleared_alerts": [], "state_file": None}
        if result.get("results"):
            evaluated = raise_live_alerts(
                result, store_dir=store_dir,
                alert_state_path=alert_state_path,
                as_of=result.get("scanned_at"))
            alerts = {
                "new_alerts": [
                    a.get("alert_key")
                    for a in evaluated.get("new_alerts", [])],
                "already_active": [
                    a.get("alert_key")
                    for a in evaluated.get("already_active", [])],
                "cleared_alerts": [
                    a.get("alert_key")
                    for a in evaluated.get("cleared_alerts", [])],
                "state_file": evaluated.get("state_file"),
            }
        run = dict(result)
        run.update({
            "run_id": sgt_date(result.get("scanned_at")),
            "scheduled": True,
            "scheduled_time_sgt": SCHEDULED_TIME_SGT,
            "run_seconds": run_seconds,
            "diff": diff,
            "alerts": alerts,
            "stale_lock_removed": stale_removed,
        })
        run_path = directory / _run_filename(run["run_id"])
        run_path.write_text(
            json.dumps(run, indent=2), encoding="utf-8")
        # Refresh the file the dashboard already reads, so the
        # latest scheduled scan shows there without any other
        # change.  A failure here must not fail the run — the
        # dated run file above is the record of truth.
        pointer_error = None
        try:
            target = (Path(live_scan_path)
                      if live_scan_path else DEFAULT_LIVE_SCAN_PATH)
            target.write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        except OSError as exc:
            pointer_error = str(exc)
        run["live_scan_pointer_error"] = pointer_error
        if pointer_error is not None:
            run_path.write_text(
                json.dumps(run, indent=2), encoding="utf-8")
        return run
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Text output — no "buy"/"sell"/"order" language anywhere
# ---------------------------------------------------------------------------

def _money(value):
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "?"


def summary_lines(run):
    """Short text lines for one run (CLI stdout, reports)."""
    summary = run.get("summary", {})
    diff = run.get("diff", {})
    lines = [
        f"Scheduled daily scan — run {run.get('run_id')} "
        f"(scanned {run.get('scanned_at')}, "
        f"{run.get('run_seconds', 0):.0f}s): "
        f"{summary.get('selected', '?')} tickers, "
        f"{summary.get('scannable', '?')} scannable, "
        f"{summary.get('strongest_alerts', 0)} strongest-alert "
        f"bands, {summary.get('watch', 0)} watch bands, "
        f"{summary.get('provider_unavailable', 0)} "
        f"provider-unavailable, "
        f"{summary.get('insufficient_data', 0)} insufficient data.",
        "Basis: 10-year FCF DCF on the latest annual free cash "
        "flow, WACC 11%, terminal growth 2.5%. Bands are "
        "provisional — quality and deterioration are unknown "
        "from a live pull, so no band is a confirmed Step 26 "
        "candidate. Worth a closer look, never a recommendation.",
    ]
    new_strongest = diff.get("new_strongest_alert", [])
    if new_strongest:
        lines.append("New strongest-alert bands since the "
                     "previous run:")
        for entry in new_strongest:
            lines.append(
                f"- {entry['ticker']}: price "
                f"{_money(entry.get('price'))} vs conservative "
                f"{_money((entry.get('fair_value_range') or {}).get('conservative'))} "
                f"({entry.get('margin_of_safety_vs_conservative', 0):.1%} "
                f"below conservative; was "
                f"{entry.get('previous_band') or 'no band'}). "
                f"Provisional — worth a closer look.")
    else:
        lines.append("No new strongest-alert bands since the "
                     "previous run.")
    new_watch = diff.get("new_watch", [])
    if new_watch:
        lines.append("New watch bands: " + ", ".join(
            f"{e['ticker']} ({e['band']})" for e in new_watch))
    disappeared = diff.get("disappeared", [])
    if disappeared:
        lines.append("Bands that disappeared: " + ", ".join(
            f"{e['ticker']} (was {e['previous_band']})"
            for e in disappeared))
    unavailable = diff.get("newly_unavailable", [])
    if unavailable:
        lines.append("Newly unavailable: " + ", ".join(
            f"{e['ticker']} ({e['status']})" for e in unavailable))
    if run.get("stale_lock_removed"):
        lines.append("Note: a stale lock from a crashed run was "
                     "removed before this run started.")
    if run.get("live_scan_pointer_error"):
        lines.append(
            "Warning: the dashboard scan file could not be "
            f"refreshed ({run['live_scan_pointer_error']}) — the "
            "dated run file is the record of truth.")
    return lines


# ---------------------------------------------------------------------------
# CLI — `python -m agents.scheduled_scan run`
# ---------------------------------------------------------------------------

def _fixture_fetcher(path):
    """A fetcher reading canned pulls from a JSON file.

    A test/debug seam, not for daily use: the file maps Yahoo
    symbol -> ``get_company_financials`` dict.  A symbol with
    no entry returns data_available False (genuine no-data),
    never a crash.
    """
    pulls = json.loads(Path(path).read_text(encoding="utf-8"))

    def fetch(symbol):
        pull = pulls.get(symbol)
        if pull is None:
            return {"data_available": False,
                    "ticker": symbol}
        return pull

    return fetch


def main(argv=None):
    """CLI entry point.  Exit 0 on a completed run (even with
    provider-unavailable tickers — those are listed, not
    fatal); exit 2 when another scan is already running; exit
    1 when the scan itself could not run at all.  Never
    requires terminal interaction."""
    parser = argparse.ArgumentParser(
        prog="agents.scheduled_scan",
        description="Run one scheduled daily live scan after the "
                    "US close.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser(
        "run", help="run one daily scan, store it, diff it, alert")
    run_parser.add_argument("--scans-dir", default=None)
    run_parser.add_argument("--store-dir", default=None)
    run_parser.add_argument("--alert-state-path", default=None)
    run_parser.add_argument("--live-scan-path", default=None)
    run_parser.add_argument(
        "--fixture", default=None,
        help="JSON file of canned pulls, keyed by Yahoo symbol "
             "(test/debug only — a real run fetches live)")
    run_parser.add_argument("--as-of", default=None)
    run_parser.add_argument(
        "--log-file", default=None,
        help="append the summary lines to this file as well as "
             "stdout (the Task Scheduler task uses it)")
    run_parser.add_argument("--rate-limit-seconds", type=float,
                            default=lws.DEFAULT_RATE_LIMIT_SECONDS)
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error(f"unknown command {args.command!r}")
    fetcher = None
    if args.fixture:
        try:
            fetcher = _fixture_fetcher(args.fixture)
        except (OSError, ValueError) as exc:
            print(f"scheduled scan: cannot read fixture: {exc}",
                  file=sys.stderr)
            return 1
    try:
        run = run_daily_scan(
            scans_dir=args.scans_dir, store_dir=args.store_dir,
            alert_state_path=args.alert_state_path,
            live_scan_path=args.live_scan_path, fetcher=fetcher,
            rate_limit_seconds=args.rate_limit_seconds,
            as_of=args.as_of)
    except ScanAlreadyRunning as exc:
        print(f"scheduled scan: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI must fail
        # loudly, with the reason, rather than exit 0 on a run
        # that never happened.
        print(f"scheduled scan: could not run: {exc}",
              file=sys.stderr)
        return 1
    lines = summary_lines(run)
    print("\n".join(lines))
    if args.log_file:
        try:
            with open(args.log_file, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as exc:
            print(f"scheduled scan: cannot write log file: {exc}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
