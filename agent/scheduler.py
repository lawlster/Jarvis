"""Step 46 — Reminders and scheduled automation.

The job store, due-check, runner, and stub delivery for Jarvis
itself. This is the sending and scheduling half that Steps 32,
34, and 38 deferred: they produce summaries, monitoring records,
and alert records and never send or schedule anything. This
module is the layer that would run them on a schedule and deliver
the result — with a fake clock in tests and a stub delivery that
records what WOULD be sent, never a real send.

Design, in brief:

1. Job store — one JSON file per job under a caller-chosen
   directory (default ``data/scheduler/``). A job carries an id,
   name, kind (``reminder`` / ``daily_summary`` /
   ``watchlist_check`` / ``alert_digest``), a schedule (once at a
   datetime, daily at a time, or weekly on a weekday at a time —
   all with an explicit timezone), a payload (which action to run
   and with what arguments), a delivery (which pluggable delivery
   channel), an enabled flag, who created it, and its last/next
   run. No new production database tables; nothing here writes
   to the production database at all.
2. Due-check — given a ``now`` (an injectable clock, defaulting
   to real time), list the jobs due, in order, without running
   them.
3. Runner — run a due job by calling its payload function
   (caller-supplied functions only — the module never invents a
   payload), record the run (started, finished, status, output
   summary, error where one occurred) in a JSON-lines run log,
   and compute the next run. A failing job is recorded as failed
   and does not block the others.
4. Delivery — a pluggable ``DeliveryClient`` with one method,
   ``deliver(recipient, subject, body)``. The only implementation
   shipped here is ``StubDelivery``: it records deliveries and
   returns a fake receipt. No real sending, no Telegram, no
   email. A real channel is a later step, plugged in here.
5. Reminders — a reminder is a job of kind ``reminder`` with a
   message and a due time. Creating one records it; it does not
   send it.

Safety, stated plainly:

- The runner refuses a job whose payload names a trade, send,
  or schedule action — the same forbidden tokens as Step 44 —
  and fails loudly at creation time. A job that would place a
  trade can never be stored.
- A reminder must never be phrased as a trade instruction: a
  reminder message that reads as an order to buy, sell, or
  trade shares is rejected at creation time.
- The runner never runs a job whose next run is in the future.
  A disabled job never runs.
- Every schedule carries an explicit timezone; a schedule
  without one is rejected. The owner is in Asia/Singapore, and
  the fixture example uses it, but the timezone is always
  caller-supplied, never assumed.

This step creates NO live job and touches NO live cron or
scheduled job. The two jobs the owner already has running
outside Jarvis (morning briefing ~07:36 and daily valuation
check ~09:36 Asia/Singapore) are untouched and are NOT
duplicated here. A job has never run on a real clock — every
run so far is on a fake clock in tests and in the fixture
example. Nothing has ever really been sent.
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEDULER_DIR = PROJECT_ROOT / "data" / "scheduler"
DEFAULT_RUN_LOG = "runs.jsonl"

JOB_KINDS = ("reminder", "daily_summary", "watchlist_check",
             "alert_digest")

# Tokens in a payload action name that mean the job must never be
# stored: trading, sending, scheduling. This mirrors the Step 44
# registry rule (agent/tool_calling.py::_FORBIDDEN_NAME_TOKENS);
# the same set is kept here so a scheduled job cannot smuggle in
# an action the tool registry would refuse. Checked against the
# name split on non-alphanumeric characters, so "buyback" does
# not trip the rule but "buy_shares" and "place_order" do.
_FORBIDDEN_ACTION_TOKENS = frozenset({
    "trade", "buy", "sell", "send", "schedule", "remind",
    "order", "place", "purchase", "email", "message", "telegram",
    "short",
})

# A reminder message that reads as an order to trade is refused:
# a trade verb together with shares/stock, or "place an order".
_TRADE_INSTRUCTION_PATTERNS = (
    re.compile(r"\b(buy|sell|trade|short)\b.{0,40}\b(shares?|stock)\b",
               re.IGNORECASE),
    re.compile(r"\bplace\s+(an?\s+)?order\b", re.IGNORECASE),
    re.compile(r"\border\b.{0,20}\b(buy|sell)\b", re.IGNORECASE),
)


class SchedulerError(Exception):
    """A job, schedule, or run that cannot be stored or run."""


class ForbiddenJobError(SchedulerError):
    """A job whose payload would trade, send, or schedule."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_or(now):
    """``now`` as an aware datetime, or the real current time."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise SchedulerError(
            "now must be timezone-aware; a naive datetime has no "
            "meaning against a scheduled timezone")
    return now


def _name_tokens(name):
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _check_action_name(action):
    """Raise :class:`ForbiddenJobError` for a forbidden action."""
    if not isinstance(action, str) or not action.strip():
        raise ForbiddenJobError(
            "a job payload needs a non-empty action name")
    bad = [t for t in _name_tokens(action)
           if t in _FORBIDDEN_ACTION_TOKENS]
    if bad:
        raise ForbiddenJobError(
            f"payload action '{action}' is forbidden: it suggests "
            f"{' / '.join(sorted(set(bad)))} — a scheduled job "
            "must never trade, send, or schedule on its own "
            "(trades need the owner's explicit per-order approval; "
            "sending is through a delivery client, not a payload "
            "action)")


def _check_reminder_message(message):
    """Raise for a reminder phrased as a trade instruction."""
    if not isinstance(message, str) or not message.strip():
        raise SchedulerError("a reminder needs a non-empty message")
    for pattern in _TRADE_INSTRUCTION_PATTERNS:
        if pattern.search(message):
            raise ForbiddenJobError(
                "a reminder must never be phrased as a trade "
                f"instruction: {message!r} reads as an order to "
                "trade — reminders only record that something is "
                "due, they never tell anyone to buy or sell")


def _timezone(schedule):
    """The schedule's timezone, or raise when it has none."""
    name = (schedule or {}).get("timezone")
    if not isinstance(name, str) or not name.strip():
        raise SchedulerError(
            "a schedule needs an explicit timezone (the owner is "
            "in Asia/Singapore, but the timezone is always "
            "caller-supplied, never assumed)")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise SchedulerError(
            f"unknown timezone {name!r} — use an IANA name such as "
            "'Asia/Singapore'") from None


def _parse_hhmm(value, field="time"):
    """Validate a \"HH:MM\" time string; return (hour, minute)."""
    if not isinstance(value, str):
        raise SchedulerError(f"schedule {field} must be a \"HH:MM\" string")
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise SchedulerError(
            f"schedule {field} {value!r} is not a valid \"HH:MM\" time")
    return int(match.group(1)), int(match.group(2))


def _validate_schedule(schedule):
    """Validate a schedule dict; return it unchanged, or raise."""
    if not isinstance(schedule, dict):
        raise SchedulerError("a schedule must be a dict")
    kind = schedule.get("type")
    if kind not in ("once", "daily", "weekly"):
        raise SchedulerError(
            "schedule type must be 'once', 'daily', or 'weekly', "
            f"not {kind!r}")
    tz = _timezone(schedule)
    if kind == "once":
        at = schedule.get("at")
        if not isinstance(at, str):
            raise SchedulerError(
                "a 'once' schedule needs an 'at' ISO datetime string")
        try:
            parsed = datetime.fromisoformat(at)
        except ValueError:
            raise SchedulerError(
                f"'at' {at!r} is not a valid ISO datetime") from None
        if parsed.tzinfo is None:
            raise SchedulerError(
                f"'at' {at!r} has no timezone — an aware datetime "
                "is required")
        # The 'at' offset and the named timezone must agree in
        # the sense that the instant is unambiguous; the named
        # timezone is what future runs are computed in, so keep
        # both and let the instant win for a one-off.
        return dict(schedule)
    _parse_hhmm(schedule.get("time"))
    if kind == "weekly":
        weekday = schedule.get("weekday")
        if not isinstance(weekday, int) or isinstance(weekday, bool) \
                or not 0 <= weekday <= 6:
            raise SchedulerError(
                "a 'weekly' schedule needs a weekday integer "
                "0 (Monday) to 6 (Sunday)")
    return dict(schedule)


def compute_next_run(schedule, after=None):
    """The next run of ``schedule`` strictly after ``after``.

    ``after`` defaults to now (real time). Returns an aware
    datetime, or None when a 'once' schedule is already past.
    """
    schedule = _validate_schedule(schedule)
    after = _now_or(after)
    tz = _timezone(schedule)
    after_tz = after.astimezone(tz)
    kind = schedule["type"]
    if kind == "once":
        at = datetime.fromisoformat(schedule["at"])
        return at if at > after else None
    hour, minute = _parse_hhmm(schedule["time"])
    if kind == "daily":
        candidate = after_tz.replace(hour=hour, minute=minute,
                                     second=0, microsecond=0)
        if candidate <= after_tz:
            candidate += timedelta(days=1)
        return candidate
    # weekly
    weekday = schedule["weekday"]
    days_ahead = (weekday - after_tz.weekday()) % 7
    candidate = (after_tz + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after_tz:
        candidate += timedelta(days=7)
    return candidate


# ---------------------------------------------------------------------------
# Job store — one JSON file per job
# ---------------------------------------------------------------------------

def _store_dir(store_dir):
    return Path(store_dir) if store_dir is not None \
        else DEFAULT_SCHEDULER_DIR


def _job_path(store_dir, job_id):
    return _store_dir(store_dir) / f"{job_id}.json"


def new_job(name, kind, schedule, payload, delivery=None,
            created_by="owner", enabled=True, job_id=None, now=None):
    """Build a job dict, validated, with its next run computed.

    ``payload`` is ``{"action": <name>, "args": {...}}`` for a
    runnable job, or ``{"action": "reminder", "message": ...}``
    for a reminder. The action name is checked against the
    forbidden tokens at creation time — a job that would trade,
    send, or schedule can never be stored. ``delivery`` is
    ``{"channel": "stub", "recipient": ...}`` (or None: run the
    job, deliver nothing).
    """
    if not isinstance(name, str) or not name.strip():
        raise SchedulerError("a job needs a non-empty name")
    if kind not in JOB_KINDS:
        raise SchedulerError(
            f"job kind must be one of {JOB_KINDS}, not {kind!r}")
    schedule = _validate_schedule(schedule)
    if not isinstance(payload, dict):
        raise SchedulerError("a job payload must be a dict")
    action = payload.get("action")
    if kind == "reminder":
        if action != "reminder":
            raise SchedulerError(
                "a reminder job's payload action must be 'reminder'")
        _check_reminder_message(payload.get("message"))
    else:
        _check_action_name(action)
    if delivery is not None and not isinstance(delivery, dict):
        raise SchedulerError("a job delivery must be a dict or None")
    now = _now_or(now)
    job_id = job_id or uuid.uuid4().hex
    next_run = compute_next_run(schedule, after=now)
    return {
        "id": job_id,
        "name": name.strip(),
        "kind": kind,
        "schedule": schedule,
        "payload": payload,
        "delivery": delivery,
        "enabled": bool(enabled),
        "created_by": created_by,
        "created_at": now.isoformat(),
        "last_run": None,
        "next_run": next_run.isoformat() if next_run else None,
        "last_status": None,
    }


def create_reminder(store_dir, message, at, timezone_name,
                    name=None, recipient="owner", created_by="owner",
                    now=None):
    """Create and store a one-off reminder job.

    ``at`` is an aware ISO datetime string. Creating the job
    records it; it does not send it. Returns the job dict.
    """
    schedule = {"type": "once", "at": at, "timezone": timezone_name}
    job = new_job(
        name or f"Reminder: {message[:60]}",
        "reminder", schedule,
        {"action": "reminder", "message": message},
        delivery={"channel": "stub", "recipient": recipient},
        created_by=created_by, now=now)
    save_job(job, store_dir)
    return job


def save_job(job, store_dir=None):
    """Write one job to its JSON file; return the path."""
    directory = _store_dir(store_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{job['id']}.json"
    path.write_text(json.dumps(job, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def load_jobs(store_dir=None):
    """Load all jobs; return ``(jobs, skipped)``.

    ``jobs`` are the valid job dicts, newest-created first.
    ``skipped`` names the corrupt files that were skipped, never
    crashing the store — a corrupt job file is listed, not
    silently forgotten.
    """
    directory = _store_dir(store_dir)
    jobs = []
    skipped = []
    if not directory.exists():
        return jobs, skipped
    for path in sorted(directory.glob("*.json")):
        if path.name == DEFAULT_RUN_LOG:
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or "id" not in job \
                    or "kind" not in job or "schedule" not in job:
                raise ValueError("not a job record")
            _validate_schedule(job["schedule"])
            jobs.append(job)
        except (ValueError, OSError, SchedulerError):
            skipped.append(path.name)
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs, skipped


def list_jobs(store_dir=None):
    """All stored jobs, newest-created first (corrupt files skipped)."""
    jobs, _skipped = load_jobs(store_dir)
    return jobs


def get_job(job_id, store_dir=None):
    """One job by id, or None."""
    path = _job_path(store_dir, job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def delete_job(job_id, store_dir=None):
    """Delete a job's file; return True when one existed."""
    path = _job_path(store_dir, job_id)
    if not path.exists():
        return False
    path.unlink()
    return True


# ---------------------------------------------------------------------------
# Due-check
# ---------------------------------------------------------------------------

def due_jobs(jobs, now=None):
    """The jobs due at ``now``, in run order, without running them.

    A job is due when it is enabled, has a next run, and that
    next run is at or before ``now``. Ordered by next run, then
    name. A disabled job is never due; a job with no next run
    (a 'once' job already run) is never due again.
    """
    now = _now_or(now)
    due = []
    for job in jobs or []:
        if not job.get("enabled"):
            continue
        next_run = job.get("next_run")
        if not next_run:
            continue
        try:
            when = datetime.fromisoformat(next_run)
        except ValueError:
            continue
        if when.tzinfo is None:
            continue
        if when <= now:
            due.append(job)
    due.sort(key=lambda j: (j.get("next_run") or "", j.get("name") or ""))
    return due


# ---------------------------------------------------------------------------
# Delivery — pluggable; only a stub ships here
# ---------------------------------------------------------------------------

class DeliveryClient:
    """One method: deliver(recipient, subject, body) -> receipt."""

    def deliver(self, recipient, subject, body):
        raise NotImplementedError


class StubDelivery(DeliveryClient):
    """Records what WOULD be sent; never really sends anything.

    ``deliveries`` holds every recorded delivery in memory.
    When ``deliveries_path`` is given, each delivery is also
    appended as one JSON line to that file. The receipt says
    ``delivered: False`` and carries the note that nothing was
    really sent — a stub receipt must never be mistaken for a
    real send.
    """

    channel = "stub"

    def __init__(self, deliveries_path=None):
        self.deliveries = []
        self.deliveries_path = (
            Path(deliveries_path) if deliveries_path else None)

    def deliver(self, recipient, subject, body):
        receipt = {
            "receipt_id": uuid.uuid4().hex,
            "channel": "stub",
            "delivered": False,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "note": "stub delivery — recorded only, nothing was "
                    "really sent (no Telegram bot, no email sender "
                    "is configured for Jarvis)",
        }
        self.deliveries.append(receipt)
        if self.deliveries_path is not None:
            self.deliveries_path.parent.mkdir(parents=True,
                                              exist_ok=True)
            with self.deliveries_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(receipt, sort_keys=True) + "\n")
        return receipt


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _delivery_text(job, result):
    """(subject, body) for a job's delivery from its result."""
    summary = (result or {}).get("summary") or str(result or "")
    subject = f"[Jarvis] {job.get('name')} — {job.get('kind')}"
    body = (
        f"{job.get('name')} ran at "
        f"{(result or {}).get('finished_at') or ''}.\n\n"
        f"{summary}\n\n"
        "Recorded by the Jarvis scheduler. This is a scheduled "
        "record, not a recommendation — and nothing was traded."
    )
    return subject, body


def _append_run_record(store_dir, record):
    directory = _store_dir(store_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DEFAULT_RUN_LOG
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def read_run_log(store_dir=None):
    """The run log, oldest first; corrupt lines skipped."""
    path = _store_dir(store_dir) / DEFAULT_RUN_LOG
    records = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def run_job(job, now=None, actions=None, delivery=None, store_dir=None):
    """Run one job (when it is due) and record the run.

    ``actions`` maps payload action names to callables; the
    module never invents a payload. A job that is disabled, has
    no next run, or is not yet due is recorded as ``skipped``
    and not run. A job whose action raises is recorded as
    ``failed`` with the error; a failing job does not block the
    others. A successful job is delivered through ``delivery``
    when one is supplied and the job carries a delivery. The
    job's ``last_run`` / ``next_run`` / ``last_status`` are
    updated and the job file saved. Returns the run record.
    """
    now = _now_or(now)
    started = now
    job = dict(job)
    job_id = job.get("id")
    record = {
        "job_id": job_id,
        "job_name": job.get("name"),
        "kind": job.get("kind"),
        "started_at": started.isoformat(),
        "finished_at": None,
        "status": "skipped",
        "summary": "",
        "error": None,
        "delivery_receipt": None,
    }
    if not job.get("enabled"):
        record["summary"] = "skipped: job is disabled"
        record["finished_at"] = now.isoformat()
        _append_run_record(store_dir, record)
        return record
    next_run = job.get("next_run")
    if not next_run:
        record["summary"] = "skipped: no next run (one-off already run)"
        record["finished_at"] = now.isoformat()
        _append_run_record(store_dir, record)
        return record
    try:
        when = datetime.fromisoformat(next_run)
    except ValueError:
        record["status"] = "failed"
        record["error"] = f"next_run {next_run!r} is not a valid ISO datetime"
        record["finished_at"] = now.isoformat()
        _append_run_record(store_dir, record)
        return record
    if when > now:
        record["summary"] = "skipped: not due yet"
        record["finished_at"] = now.isoformat()
        _append_run_record(store_dir, record)
        return record

    payload = job.get("payload") or {}
    action = payload.get("action")
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        record["status"] = "failed"
        record["error"] = "payload args must be a dict"
        record["finished_at"] = now.isoformat()
        _append_run_record(store_dir, record)
        return record
    if job.get("kind") == "reminder":
        # A reminder has no action function: running it means
        # delivering its message, which happens below.
        result = {"summary": payload.get("message") or "",
                  "detail": {"message": payload.get("message")}}
    else:
        fn = (actions or {}).get(action)
        if fn is None:
            record["status"] = "failed"
            record["error"] = (
                f"no action function supplied for {action!r} — "
                "the scheduler never invents a payload")
            record["finished_at"] = now.isoformat()
            _append_run_record(store_dir, record)
            return record
        try:
            result = fn(**args)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["finished_at"] = now.isoformat()
            job["last_run"] = now.isoformat()
            job["last_status"] = "failed"
            job["next_run"] = _next_after(job, now)
            save_job(job, store_dir)
            _append_run_record(store_dir, record)
            return record
    if not isinstance(result, dict):
        result = {"summary": str(result)}
    result = dict(result)
    result["finished_at"] = now.isoformat()
    record["status"] = "ok"
    record["summary"] = result.get("summary") or ""

    # Deliver, when the job carries a delivery and a client was
    # supplied. A delivery failure is recorded on the run, not
    # hidden: the job still ran, the send did not go out.
    delivery_spec = job.get("delivery") or {}
    if delivery is not None and delivery_spec:
        try:
            subject, body = _delivery_text(job, result)
            receipt = delivery.deliver(
                delivery_spec.get("recipient") or "owner",
                subject, body)
            record["delivery_receipt"] = receipt
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            record["status"] = "failed"
            record["error"] = (
                f"delivery failed ({type(exc).__name__}: {exc})")

    record["finished_at"] = now.isoformat()
    job["last_run"] = now.isoformat()
    job["last_status"] = record["status"]
    job["next_run"] = _next_after(job, now)
    save_job(job, store_dir)
    _append_run_record(store_dir, record)
    return record


def _next_after(job, now):
    """The job's next run after ``now``, as ISO, or None."""
    try:
        nxt = compute_next_run(job.get("schedule") or {}, after=now)
    except SchedulerError:
        return None
    return nxt.isoformat() if nxt else None


def run_due_jobs(store_dir=None, now=None, actions=None, delivery=None):
    """Run every due job in order; return the run records.

    Jobs are loaded from ``store_dir``; a failing job is
    recorded as failed and does not block the others. Nothing
    is run on a real clock unless the caller passes real time.
    """
    now = _now_or(now)
    jobs, _skipped = load_jobs(store_dir)
    records = []
    for job in due_jobs(jobs, now=now):
        records.append(run_job(job, now=now, actions=actions,
                               delivery=delivery, store_dir=store_dir))
    return records


# ---------------------------------------------------------------------------
# Built-in actions — thin wrappers over Steps 32 / 34 / 38
# ---------------------------------------------------------------------------

def _open_read_only(db_path):
    """Open ``db_path`` read-only, or return None."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _action_watchlist_check(db_path=None, watchlist=None,
                            state_path=None, news_dir=None,
                            events_dir=None, as_of=None):
    """Step 34 monitor over a caller-supplied watchlist.

    Returns a summary of what is new and what changed, plus the
    full monitoring record as detail. Read-only on the database.
    """
    from agents import news_monitoring as nm

    conn = _open_read_only(db_path)
    if conn is None:
        return {"summary": "watchlist check unavailable: no readable "
                           "database supplied",
                "detail": {"error": "no database"}}
    try:
        record = nm.run_monitor(
            conn, watchlist or [], state_path=state_path,
            news_dir=news_dir, events_dir=events_dir, as_of=as_of)
    finally:
        conn.close()
    tickers = record.get("tickers") or {}
    new_articles = sum(
        (t.get("new_article_count") or 0) for t in tickers.values())
    upcoming = sum(
        (t.get("upcoming_event_count") or 0) for t in tickers.values())
    changes = sum((t.get("change_count") or 0) for t in tickers.values())
    alerts = record.get("alert_count") or 0
    summary = (
        f"Watchlist check: {len(tickers)} ticker(s), "
        f"{new_articles} new article(s), {upcoming} upcoming "
        f"event(s), {changes} change(s), {alerts} new alert(s).")
    return {"summary": summary, "detail": record}


def _action_daily_summary(db_path=None, date=None, tickers=None,
                          watchlist=None, news_dir=None):
    """Step 32 daily summary for ``date`` (default: today, UTC)."""
    from agents import daily_summary as ds

    conn = _open_read_only(db_path)
    if conn is None:
        return {"summary": "daily summary unavailable: no readable "
                           "database supplied",
                "detail": {"error": "no database"}}
    try:
        summary = ds.generate_summary(
            conn, date=date, tickers=tickers, watchlist=watchlist,
            news_dir=news_dir)
    finally:
        conn.close()
    return {"summary": summary.get("headline") or "daily summary",
            "detail": summary}


def _action_alert_digest(state_path=None, ticker=None, severity=None):
    """Step 38 active alerts, read from the stored state file.

    Read-only: this lists what the alert engine already raised;
    it never evaluates anything itself.
    """
    from agents import watchlist_alerts as wa

    state = wa.load_state(state_path)
    active = wa.list_active_alerts(state, ticker=ticker,
                                   severity=severity)
    if not active:
        summary = "Alert digest: no active alerts."
    else:
        lines = wa.briefing_lines(active)
        summary = ("Alert digest: "
                   f"{len(active)} active alert(s).\n" + "\n".join(lines))
    return {"summary": summary, "detail": {"active_alerts": active}}


def builtin_actions():
    """The three Step 32/34/38 actions, by payload action name."""
    return {
        "watchlist_check": _action_watchlist_check,
        "daily_summary": _action_daily_summary,
        "alert_digest": _action_alert_digest,
    }


# ---------------------------------------------------------------------------
# Store wrapper
# ---------------------------------------------------------------------------

class Scheduler:
    """A thin wrapper holding a store directory, actions, and delivery."""

    def __init__(self, store_dir=None, actions=None, delivery=None):
        self.store_dir = _store_dir(store_dir)
        self.actions = dict(actions) if actions else builtin_actions()
        self.delivery = delivery

    def create_job(self, *args, **kwargs):
        job = new_job(*args, **kwargs)
        save_job(job, self.store_dir)
        return job

    def create_reminder(self, *args, **kwargs):
        return create_reminder(self.store_dir, *args, **kwargs)

    def jobs(self):
        return list_jobs(self.store_dir)

    def due(self, now=None):
        jobs, _skipped = load_jobs(self.store_dir)
        return due_jobs(jobs, now=now)

    def run_due(self, now=None):
        return run_due_jobs(self.store_dir, now=now,
                            actions=self.actions,
                            delivery=self.delivery)

    def run_log(self):
        return read_run_log(self.store_dir)
