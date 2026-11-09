"""Step 49 — live dashboards and agent monitoring.

A status collector that READS the logs and state files the other
steps already write and presents them as one snapshot. It creates
no new logging format, rewrites nothing, runs no module, sends
nothing, and never touches the production database.

Sources (all caller-supplied paths; defaults under ``data/``):

- ``data/detection_runs.jsonl`` — Step 26 undervalued-detection
  runs (one JSON line per run).
- ``data/orchestration/turns.jsonl`` — Step 39 orchestrator turns
  (one JSON line per turn).
- ``data/scheduler/runs.jsonl`` — Step 46 scheduled-job runs
  (one JSON line per run).
- ``data/alerts/alert_state.json`` — Step 38 alert state
  (active and cleared alerts).
- ``data/monitoring/monitoring_state.json`` — Step 34 watchlist
  monitoring state (last check, seen articles, last snapshot).
- ``data/watchlist/`` — Step 35 watchlist store and tracker
  records.
- ``data/memory/index.json`` — Step 43 long-term memory index.
- ``data/news/`` and ``data/events/`` — Step 31 news run files
  and Step 33 event files (counts only).
- ``data/cloud_models.json`` — Step 40 cloud-model config
  (existence only; it never carries a secret).

A missing log directory or file is reported as ``"no data"``,
never an error. A corrupt JSON line is skipped and counted,
never a crash. Every timestamp is aged against an injectable
``now``; anything older than ``stale_after_hours`` is labelled
stale with its age.

The snapshot is a plain dict:

- ``generated_at`` — when the snapshot was taken (UTC ISO).
- ``modules`` — one health entry per module: ``status``
  (``"ok"`` / ``"no_data"`` / ``"stale"`` / ``"corrupt"``),
  ``last_run_at``, ``records`` (how many records it holds),
  ``freshness`` (``"fresh"`` / ``"stale"`` / ``"unknown"``),
  ``age_seconds``, and a one-line ``detail``.
- ``recent_activity`` — the newest entries across all logs
  (turns, detection runs, scheduled runs, alerts, monitoring
  checks), newest first, capped by ``recent_limit``.
- ``gaps`` — what has never run on real data, which stores are
  empty, and which configs are missing. Each gap carries its
  ``text`` and its ``source`` so it can be traced.

Standing facts the dashboard states (labelled as such, because
no log records them): there is no live price feed, and no alert
has ever fired on real data is derived from the alert state
itself. Nothing here is a recommendation, and nothing here
places a trade — the dashboard contains no trading controls.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_STALE_AFTER_HOURS = 24
DEFAULT_RECENT_LIMIT = 20


def _project_root():
    return Path(__file__).resolve().parent.parent


def _default_data_dir():
    return _project_root() / "data"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_time(value):
    """Parse an ISO timestamp to an aware datetime, or None.

    Accepts a trailing ``Z`` and naive timestamps (read as UTC).
    Anything unparseable returns None — never raises.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_or(now):
    if now is None:
        return datetime.now(timezone.utc)
    parsed = _parse_time(now)
    return parsed if parsed is not None else datetime.now(timezone.utc)


def _freshness(at, now, stale_after_seconds):
    """(freshness label, age in seconds) for a timestamp.

    ``"fresh"`` when the timestamp is within the threshold,
    ``"stale"`` when it is older, ``"unknown"`` when there is no
    usable timestamp. A timestamp in the future counts as fresh
    (age 0 or negative is clamped to 0 for display).
    """
    parsed = _parse_time(at)
    if parsed is None:
        return "unknown", None
    age = (now - parsed).total_seconds()
    if age < 0:
        age = 0.0
    return ("fresh" if age <= stale_after_seconds else "stale",
            age)


def _age_text(age_seconds):
    """A short human-readable age, e.g. ``"3h 12m"`` or ``"5d"``."""
    if age_seconds is None:
        return "age unknown"
    seconds = int(age_seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d"


# ---------------------------------------------------------------------------
# File readers (read-only; corrupt input is skipped, never raised)
# ---------------------------------------------------------------------------

def _read_json_lines(path):
    """(records, skipped) from a JSON-lines file.

    A missing file returns ([], 0). A corrupt line is skipped
    and counted; a non-dict line is skipped and counted too.
    """
    records = []
    skipped = 0
    path = Path(path)
    if not path.exists():
        return records, skipped
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records, 1
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        records.append(record)
    return records, skipped


def _read_json(path):
    """(record, error) from one JSON file; error is None on success.

    A missing file returns (None, "missing"). A corrupt file
    returns (None, "corrupt: ..."). A non-dict file returns
    (None, "not a JSON object").
    """
    path = Path(path)
    if not path.exists():
        return None, "missing"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"corrupt: {exc}"
    if not isinstance(record, dict):
        return None, "not a JSON object"
    return record, None


def _count_json_files(directory, key=None):
    """(file count, summed record count) for *.json in a directory.

    When ``key`` is given, each file's list under that key is
    counted (e.g. a news run file's ``"articles"``); files that
    do not carry the key contribute 0. A missing directory
    returns (0, 0).
    """
    directory = Path(directory)
    if not directory.exists():
        return 0, 0
    files = 0
    total = 0
    for path in sorted(directory.glob("*.json")):
        files += 1
        if key is None:
            continue
        record, _error = _read_json(path)
        if record is None:
            continue
        items = record.get(key)
        if isinstance(items, list):
            total += len(items)
    return files, total


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

def _module_entry(status, last_run_at, records, detail, now,
                  stale_after_seconds):
    freshness, age = _freshness(last_run_at, now, stale_after_seconds)
    if status == "ok" and freshness == "stale":
        status = "stale"
    return {
        "status": status,
        "last_run_at": last_run_at,
        "records": records,
        "freshness": freshness,
        "age_seconds": age,
        "age": _age_text(age),
        "detail": detail,
    }


def collect_status(data_dir=None, now=None, stale_after_hours=None,
                   recent_limit=None):
    """One status snapshot over the project's logs and state files.

    ``data_dir`` defaults to the project's ``data/`` directory.
    ``now`` is an injectable clock (a datetime or ISO string;
    defaults to real time). ``stale_after_hours`` defaults to 24.
    ``recent_limit`` caps the recent-activity list (default 20).
    Reads only — no file is created, modified, or deleted.
    """
    base = Path(data_dir) if data_dir is not None else _default_data_dir()
    now = _now_or(now)
    stale_after_seconds = (
        DEFAULT_STALE_AFTER_HOURS * 3600
        if stale_after_hours is None
        else float(stale_after_hours) * 3600)
    recent_limit = (DEFAULT_RECENT_LIMIT if recent_limit is None
                    else int(recent_limit))

    modules = {}
    activity = []
    gaps = []

    def gap(text, source):
        gaps.append({"text": text, "source": source})

    # -- Step 26: undervalued detection runs -------------------------
    det_records, det_skipped = _read_json_lines(
        base / "detection_runs.jsonl")
    det_last = None
    det_summary = ""
    if det_records:
        det_last = det_records[-1].get("run_at_utc")
        latest = det_records[-1]
        det_summary = (
            f"{latest.get('candidates', 0)} candidate(s), "
            f"{latest.get('strongest_alerts', 0)} strongest alert(s), "
            f"{latest.get('value_traps', 0)} value trap(s), "
            f"{latest.get('scannable', 0)} scannable")
        activity.append({
            "kind": "detection run",
            "at": det_last,
            "summary": det_summary,
            "status": "ok",
        })
    if det_skipped:
        det_summary += (f" ({det_skipped} corrupt line(s) skipped)"
                        if det_summary else
                        f"{det_skipped} corrupt line(s) skipped")
    modules["undervalued_detection"] = _module_entry(
        "ok" if det_records else "no_data", det_last,
        len(det_records) if det_records else 0,
        det_summary or "No detection run has ever been logged.",
        now, stale_after_seconds)
    if not det_records:
        gap("No undervalued-detection run has ever been logged.",
            "detection run log (Step 26)")

    # -- Step 39: orchestrator turns ---------------------------------
    turn_records, turn_skipped = _read_json_lines(
        base / "orchestration" / "turns.jsonl")
    turn_last = None
    turn_summary = ""
    if turn_records:
        turn_last = turn_records[-1].get("checked_at")
        latest = turn_records[-1]
        turn_summary = (
            f"latest turn: intent {latest.get('intent')}, "
            f"route {latest.get('route')}, "
            f"status {latest.get('status')}")
        for record in turn_records[-3:]:
            activity.append({
                "kind": "orchestrator turn",
                "at": record.get("checked_at"),
                "summary": (f"{record.get('intent')} "
                            f"({record.get('route')}) — "
                            f"{record.get('status')}"),
                "status": record.get("status") or "unknown",
            })
    if turn_skipped:
        turn_summary += (f" ({turn_skipped} corrupt line(s) skipped)"
                         if turn_summary else
                         f"{turn_skipped} corrupt line(s) skipped")
    modules["orchestrator"] = _module_entry(
        "ok" if turn_records else "no_data", turn_last,
        len(turn_records) if turn_records else 0,
        turn_summary or "No orchestrator turn has ever been logged.",
        now, stale_after_seconds)
    if not turn_records:
        gap("No orchestrator turn has ever been logged — no live "
            "Ollama turn has run on this machine.",
            "turn log (Step 39)")

    # -- Step 46: scheduled-job runs ---------------------------------
    run_records, run_skipped = _read_json_lines(
        base / "scheduler" / "runs.jsonl")
    run_last = None
    run_summary = ""
    if run_records:
        run_last = run_records[-1].get("finished_at") or \
            run_records[-1].get("started_at")
        latest = run_records[-1]
        run_summary = (
            f"latest run: {latest.get('job_name')} "
            f"({latest.get('kind')}) — {latest.get('status')}")
        for record in run_records[-3:]:
            activity.append({
                "kind": "scheduled run",
                "at": record.get("finished_at") or
                record.get("started_at"),
                "summary": (f"{record.get('job_name')} "
                            f"({record.get('kind')}) — "
                            f"{record.get('status')}"
                            f"{': ' + record['summary'] if record.get('summary') else ''}"),
                "status": record.get("status") or "unknown",
            })
    if run_skipped:
        run_summary += (f" ({run_skipped} corrupt line(s) skipped)"
                        if run_summary else
                        f"{run_skipped} corrupt line(s) skipped")
    modules["scheduler"] = _module_entry(
        "ok" if run_records else "no_data", run_last,
        len(run_records) if run_records else 0,
        run_summary or "No scheduled job has ever run.",
        now, stale_after_seconds)
    if not run_records:
        gap("No scheduled job has ever run on a real clock.",
            "run log (Step 46)")

    # -- Step 38: alert state ----------------------------------------
    alert_state, alert_error = _read_json(
        base / "alerts" / "alert_state.json")
    if alert_state is None:
        modules["watchlist_alerts"] = _module_entry(
            "corrupt" if alert_error and
            alert_error.startswith("corrupt") else "no_data",
            None, 0,
            ("Alert state file is corrupt and could not be read."
             if alert_error and alert_error.startswith("corrupt")
             else "No alert state file exists yet."),
            now, stale_after_seconds)
        # No state file means no alert has ever been recorded
        # either — the same fact as an empty state file.
        gap("No alert has ever fired on real data.",
            "alert state (Step 38)")
    else:
        active = alert_state.get("active_alerts") or {}
        cleared = alert_state.get("cleared_alerts") or []
        raised = [a for a in active.values()
                  if isinstance(a, dict)]
        last_raised = None
        for alert in raised:
            at = alert.get("raised_at")
            if at and (last_raised is None or at > last_raised):
                last_raised = at
        modules["watchlist_alerts"] = _module_entry(
            "ok", last_raised, len(raised) + len(cleared),
            (f"{len(raised)} active alert(s), "
             f"{len(cleared)} cleared alert(s)")
            if raised or cleared else
            "No alerts raised or cleared yet.",
            now, stale_after_seconds)
        for alert in raised:
            activity.append({
                "kind": "alert raised",
                "at": alert.get("raised_at"),
                "summary": (f"{alert.get('ticker')}: "
                            f"{alert.get('rule')} "
                            f"({alert.get('severity')})"),
                "status": "active",
            })
        if not raised and not cleared:
            gap("No alert has ever fired on real data.",
                "alert state (Step 38)")

    # -- Step 34: watchlist monitoring state -------------------------
    mon_state, mon_error = _read_json(
        base / "monitoring" / "monitoring_state.json")
    if mon_state is None:
        modules["news_monitoring"] = _module_entry(
            "corrupt" if mon_error and
            mon_error.startswith("corrupt") else "no_data",
            None, 0,
            ("Monitoring state file is corrupt and could not be "
             "read." if mon_error and
             mon_error.startswith("corrupt")
             else "No monitoring state file exists yet."),
            now, stale_after_seconds)
    else:
        mon_last = mon_state.get("last_checked_at")
        tickers = mon_state.get("tickers") or {}
        seen = sum(len((t or {}).get("seen_article_keys") or [])
                   for t in tickers.values()
                   if isinstance(t, dict))
        modules["news_monitoring"] = _module_entry(
            "ok", mon_last, len(tickers),
            (f"{len(tickers)} ticker(s) checked, "
             f"{seen} article(s) seen")
            if tickers else "No ticker has been checked yet.",
            now, stale_after_seconds)
        if mon_last:
            activity.append({
                "kind": "watchlist check",
                "at": mon_last,
                "summary": (f"{len(tickers)} ticker(s) checked, "
                            f"{seen} article(s) seen"),
                "status": "ok",
            })

    # -- Step 35: watchlist store and tracker ------------------------
    watchlist, wl_error = _read_json(
        base / "watchlist" / "watchlist.json")
    tracker, tr_error = _read_json(
        base / "watchlist" / "tracker.json")
    if watchlist is None and tracker is None:
        modules["watchlist"] = _module_entry(
            "no_data", None, 0,
            "No watchlist store exists yet.", now, stale_after_seconds)
        gap("No watchlist has been seeded.",
            "watchlist store (Step 35)")
    else:
        entries = (watchlist or {}).get("entries") or []
        records = (tracker or {}).get("records") or {}
        scannable = sum(1 for r in records.values()
                        if isinstance(r, dict) and r.get("scannable"))
        last_checked = None
        for record in records.values():
            if not isinstance(record, dict):
                continue
            at = record.get("last_checked")
            if at and (last_checked is None or at > last_checked):
                last_checked = at
        modules["watchlist"] = _module_entry(
            "ok", last_checked, len(entries),
            (f"{len(entries)} ticker(s) on the watchlist, "
             f"{scannable} scannable")
            if entries else "Watchlist store exists but is empty.",
            now, stale_after_seconds)
        if entries and scannable < len(entries):
            gap(f"{len(entries) - scannable} of {len(entries)} "
                "watchlist tickers are insufficient data (only "
                "MSFT has statements).",
                "watchlist tracker (Step 35)")

    # -- Step 43: long-term memory -----------------------------------
    memory_index, _mem_error = _read_json(
        base / "memory" / "index.json")
    if memory_index is None:
        modules["long_term_memory"] = _module_entry(
            "no_data", None, 0,
            "No long-term memory store exists yet.",
            now, stale_after_seconds)
        gap("The long-term memory store is empty.",
            "memory index (Step 43)")
    else:
        count = memory_index.get("record_count")
        if count is None:
            count = len(memory_index.get("records") or [])
        voice_off = any(
            isinstance(r, dict) and "voice" in (r.get("tags") or [])
            and r.get("status") == "active"
            for r in (memory_index.get("records") or []))
        modules["long_term_memory"] = _module_entry(
            "ok", None, count,
            f"{count} record(s) in long-term memory.",
            now, stale_after_seconds)
        if voice_off:
            gap("Voice is off by default — the owner prefers "
                "text replies.",
                "memory preference (Step 45)")

    # -- Step 31 / Step 33: stored news and events (counts only) -----
    news_files, news_articles = _count_json_files(
        base / "news", key="articles")
    modules["news_collection"] = _module_entry(
        "ok" if news_files else "no_data", None, news_articles,
        (f"{news_files} news run file(s), "
         f"{news_articles} article(s) stored")
        if news_files else "No news has ever been collected.",
        now, stale_after_seconds)
    if not news_files:
        gap("No news has ever been collected.",
            "news run files (Step 31)")
    event_files, _ = _count_json_files(base / "events")
    modules["event_tracking"] = _module_entry(
        "ok" if event_files else "no_data", None, event_files,
        f"{event_files} event file(s) stored"
        if event_files else "No events have ever been recorded.",
        now, stale_after_seconds)
    if not event_files:
        gap("No events have ever been recorded.",
            "event files (Step 33)")

    # -- Step 40: cloud-model config (existence only) ----------------
    cloud_config, _cloud_error = _read_json(
        base / "cloud_models.json")
    modules["cloud_models"] = _module_entry(
        "ok" if cloud_config is not None else "no_data",
        None, 1 if cloud_config is not None else 0,
        "A cloud-model config exists."
        if cloud_config is not None else
        "No cloud-model config exists — local Ollama only.",
        now, stale_after_seconds)
    if cloud_config is None:
        gap("No cloud model configured — local Ollama only, and "
            "no cloud key exists.",
            "cloud config (Step 40)")

    # -- Standing gaps (documented project facts, labelled as such) --
    gap("No live price feed — prices are the 2026-11-03 snapshot "
        "unless labelled otherwise.",
        "standing fact (Step 48 status page)")
    holdings_dir = base / "holdings"
    if not holdings_dir.exists():
        gap("No holdings store exists — the owner's actual "
            "holdings have never been entered.",
            "standing fact (Steps 36–37)")

    # -- Recent activity: newest first, capped -----------------------
    def _sort_key(item):
        parsed = _parse_time(item.get("at"))
        return parsed or datetime.min.replace(tzinfo=timezone.utc)

    activity.sort(key=_sort_key, reverse=True)
    activity = activity[:recent_limit]

    return {
        "generated_at": _utcnow_iso(),
        "data_dir": str(base),
        "stale_after_hours": (stale_after_seconds / 3600),
        "modules": modules,
        "recent_activity": activity,
        "gaps": gaps,
    }


def summary_lines(snapshot):
    """Plain-text lines for the snapshot, for a page or a report.

    One line per module (name, status, last run, detail), then
    the recent activity, then the gaps. Nothing here is a
    recommendation.
    """
    lines = [
        "Jarvis status — "
        f"generated {snapshot.get('generated_at')}",
        "",
        "Modules:",
    ]
    for name, entry in (snapshot.get("modules") or {}).items():
        last = entry.get("last_run_at") or "never"
        lines.append(
            f"- {name}: {entry.get('status')} "
            f"(last run {last}"
            f"{', ' + entry['age'] + ' ago' if entry.get('age') and entry.get('last_run_at') else ''}"
            f"; {entry.get('records')} record(s)) — "
            f"{entry.get('detail')}")
    lines.append("")
    lines.append("Recent activity:")
    activity = snapshot.get("recent_activity") or []
    if not activity:
        lines.append("- No activity recorded yet.")
    for item in activity:
        lines.append(
            f"- {item.get('at') or 'no date'}: "
            f"{item.get('kind')} — {item.get('summary')} "
            f"[{item.get('status')}]")
    lines.append("")
    lines.append("Gaps:")
    gaps = snapshot.get("gaps") or []
    if not gaps:
        lines.append("- None recorded.")
    for item in gaps:
        lines.append(f"- {item.get('text')} ({item.get('source')})")
    return lines


def snapshot_to_markdown(snapshot):
    """The snapshot as Markdown (headings, lists, plain text)."""
    lines = summary_lines(snapshot)
    out = ["# Jarvis status", ""]
    in_section = False
    for line in lines[1:]:
        if line.endswith(":") and not line.startswith("-"):
            out.append(f"## {line[:-1]}")
            out.append("")
            in_section = True
        elif line == "":
            out.append("")
        else:
            out.append(line)
    return "\n".join(out) + "\n"
