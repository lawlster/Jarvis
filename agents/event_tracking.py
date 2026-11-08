"""Step 33 — Earnings calendar and event tracking.

An event store and calendar for the owner's research, built on
caller-supplied events only. There is no live earnings-calendar
feed in this step (that is a Step 38 concern): every event in the
store was supplied by a caller — the owner's research, a
filing, or another module — and its ``source`` says so. Dates
are never invented: an event with an unknown time has no time,
and an event whose date is a window carries the window in its
notes.

Storage is JSON files, one file per event, under a
caller-chosen directory (default ``data/events/``), following
the Step 26/30/31 file pattern. Nothing in this module writes
to the production database: there is no events table, and no
migration is created here. No network requests, no sending, no
scheduling, and no buy/sell call anywhere — events are input
to research, not recommendations.

Event record fields: event_id, ticker (null for market-wide or
economic events), event_type, title, date (YYYY-MM-DD),
time (HH:MM, optional), timezone (IANA, default
Asia/Singapore), source, notes, created_at (UTC ISO), outcome
(caller-supplied actuals, null until recorded),
outcome_recorded_at.

The week-ahead output is shaped for the Step 32 daily
summary's "what to watch" section: a list of ``{"item",
"date"}`` dicts, listed exactly as the stored events say.
"""

import json
import re
from datetime import date as _date
from datetime import datetime, time as _time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_EVENTS_DIR = Path(__file__).resolve().parent.parent / "data" / "events"
DEFAULT_TIMEZONE = "Asia/Singapore"

EVENT_TYPES = ("earnings", "dividend", "economic", "filing", "other")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# The owner's known week ahead (Mon 9 – Fri 13 Nov 2026), as
# caller-supplied research — used by seed_week_ahead(). These
# are the example events for testing; the module itself never
# invents dates.
SEED_EVENTS = (
    {
        "ticker": "KSPI",
        "event_type": "earnings",
        "title": "KSPI earnings",
        "date": "2026-11-09",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "",
    },
    {
        "ticker": "ALC",
        "event_type": "earnings",
        "title": "ALC earnings",
        "date": "2026-11-10",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "",
    },
    {
        "ticker": None,
        "event_type": "economic",
        "title": "US Oct CPI",
        "date": "2026-11-10",
        "time": "21:30",
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "US October CPI release.",
    },
    {
        "ticker": None,
        "event_type": "other",
        "title": "US bond market closed (Veterans Day)",
        "date": "2026-11-11",
        "time": None,
        "timezone": "America/New_York",
        "source": "owner research (caller-supplied)",
        "notes": "US bond market closed for Veterans Day; "
                 "equity markets open.",
    },
    {
        "ticker": "C6L",
        "event_type": "earnings",
        "title": "Singapore Airlines (SIA) results",
        "date": "2026-11-11",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "",
    },
    {
        "ticker": "S58",
        "event_type": "earnings",
        "title": "SATS results",
        "date": "2026-11-11",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "",
    },
    {
        "ticker": "CSCO",
        "event_type": "earnings",
        "title": "CSCO earnings",
        "date": "2026-11-12",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "After close.",
    },
    {
        "ticker": "AMAT",
        "event_type": "earnings",
        "title": "AMAT earnings",
        "date": "2026-11-12",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "After close.",
    },
    {
        "ticker": "Z74",
        "event_type": "earnings",
        "title": "Singtel results",
        "date": "2026-11-13",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "",
    },
    {
        "ticker": None,
        "event_type": "economic",
        "title": "US PPI",
        "date": "2026-11-13",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "US producer price index release.",
    },
    {
        "ticker": "NVDA",
        "event_type": "earnings",
        "title": "NVDA earnings",
        "date": "2026-11-17",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "After close.",
    },
    {
        "ticker": None,
        "event_type": "filing",
        "title": "Pabrai / Ackman Q3 13F filings due",
        "date": "2026-11-16",
        "time": None,
        "timezone": DEFAULT_TIMEZONE,
        "source": "owner research (caller-supplied)",
        "notes": "Q3 13F filings due ~14–16 Nov; this event is "
                 "dated to the last day of the window.",
    },
)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(value):
    """Validate a YYYY-MM-DD date; return the string or raise."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    try:
        _date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r}") from exc
    return value


def _parse_time(value):
    """Validate an HH:MM time (or None); return the string or None."""
    if value is None:
        return None
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError(f"time must be HH:MM, got {value!r}")
    return value


def _parse_timezone(value):
    """Validate an IANA timezone name; return the string or raise."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"timezone must be an IANA name, got {value!r}")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {value!r}") from exc
    return value


def validate_event(event):
    """Validate an event dict; return a normalised copy or raise.

    Normalisation: ticker uppercased (or None), event_type
    checked against EVENT_TYPES, date/time/timezone validated,
    outcome left as given. A ticker is required for earnings
    and dividend events; economic, filing and other events may
    be market-wide (ticker None).
    """
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"event_type must be one of {EVENT_TYPES}, got "
            f"{event_type!r}")
    ticker = event.get("ticker")
    if ticker is not None:
        ticker = str(ticker).strip().upper()
        if not ticker:
            ticker = None
    if ticker is None and event_type in ("earnings", "dividend"):
        raise ValueError(
            f"event_type {event_type!r} requires a ticker")
    title = (event.get("title") or "").strip()
    if not title:
        raise ValueError("event title is required")
    normalised = {
        "event_id": event.get("event_id"),
        "ticker": ticker,
        "event_type": event_type,
        "title": title,
        "date": _parse_date(event.get("date")),
        "time": _parse_time(event.get("time")),
        "timezone": _parse_timezone(
            event.get("timezone") or DEFAULT_TIMEZONE),
        "source": (event.get("source") or "").strip(),
        "notes": (event.get("notes") or "").strip(),
        "created_at": event.get("created_at") or _utcnow_iso(),
        "outcome": event.get("outcome"),
        "outcome_recorded_at": event.get("outcome_recorded_at"),
    }
    return normalised


def _event_id(event, events_dir):
    """Next id for this date+ticker: ``YYYY-MM-DD_TICKER_01``."""
    ticker = event["ticker"] or "MARKET"
    prefix = f"{event['date']}_{ticker}_"
    existing = sorted(Path(events_dir).glob(f"{prefix}*.json")) \
        if Path(events_dir).exists() else []
    return f"{prefix}{len(existing) + 1:02d}"


def _event_file(events_dir, event_id):
    return Path(events_dir) / f"{event_id}.json"


def add_event(events_dir=None, ticker=None, event_type=None, date=None,
              time=None, timezone=None, title=None, source=None,
              notes=None):
    """Add one caller-supplied event; return the saved event dict.

    The event is stored as one JSON file under ``events_dir``
    (default ``data/events/``). Raises ValueError on invalid
    input. Nothing is fetched and nothing is written to the
    database.
    """
    directory = Path(events_dir) if events_dir else DEFAULT_EVENTS_DIR
    event = validate_event({
        "ticker": ticker,
        "event_type": event_type,
        "title": title,
        "date": date,
        "time": time,
        "timezone": timezone or DEFAULT_TIMEZONE,
        "source": source,
        "notes": notes,
    })
    event["event_id"] = _event_id(event, directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = _event_file(directory, event["event_id"])
    event["file_path"] = str(path)
    path.write_text(json.dumps(event, indent=2), encoding="utf-8")
    return event


def load_event(path):
    """Load one event JSON file; return the event dict."""
    with open(path, encoding="utf-8") as handle:
        event = json.load(handle)
    event["file_path"] = str(path)
    return event


def _event_sort_key(event):
    """(date, time or end-of-day, ticker) — dateless times sort last."""
    return (event.get("date", ""),
            event.get("time") or "99:99",
            event.get("ticker") or "")


def list_events(events_dir=None, ticker=None, event_type=None,
                start=None, end=None):
    """All stored events, optionally filtered, sorted by date/time.

    ``ticker`` matches case-insensitively (None matches only
    market-wide events when given as the string "MARKET").
    ``start``/``end`` are inclusive YYYY-MM-DD bounds. Corrupt
    or unreadable files are skipped, like the Step 31 news
    query.
    """
    directory = Path(events_dir) if events_dir else DEFAULT_EVENTS_DIR
    if not directory.exists():
        return []
    if start is not None:
        _parse_date(start)
    if end is not None:
        _parse_date(end)
    wanted_ticker = (str(ticker).strip().upper()
                     if ticker is not None else None)
    events = []
    for path in sorted(directory.glob("*.json")):
        try:
            event = load_event(path)
        except Exception:  # noqa: BLE001 - skip unreadable files
            continue
        if wanted_ticker is not None:
            stored = event.get("ticker")
            if wanted_ticker == "MARKET":
                if stored is not None:
                    continue
            elif (stored or "") != wanted_ticker:
                continue
        if event_type is not None and \
                event.get("event_type") != event_type:
            continue
        if start is not None and event.get("date", "") < start:
            continue
        if end is not None and event.get("date", "") > end:
            continue
        events.append(event)
    events.sort(key=_event_sort_key)
    return events


def events_on(events_dir=None, date=None):
    """Events on one YYYY-MM-DD date."""
    day = _parse_date(date)
    return list_events(events_dir, start=day, end=day)


def events_in_range(events_dir=None, start=None, end=None):
    """Events between two inclusive YYYY-MM-DD dates."""
    return list_events(events_dir, start=_parse_date(start),
                       end=_parse_date(end))


def events_for_ticker(events_dir=None, ticker=None):
    """Events for one ticker, earliest first."""
    if ticker is None:
        raise ValueError("ticker is required")
    return list_events(events_dir, ticker=ticker)


def upcoming_events(events_dir=None, from_date=None, days=7):
    """Events from ``from_date`` (default: today, Asia/Singapore).

    ``days`` counts calendar days starting at ``from_date``;
    the window is inclusive on both ends.
    """
    if from_date is None:
        from_date = datetime.now(
            ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()
    start = _parse_date(from_date)
    end = (_date.fromisoformat(start)
           + timedelta(days=max(int(days), 1) - 1)).isoformat()
    return list_events(events_dir, start=start, end=end)


def reminders(events_dir=None, from_date=None, days=3):
    """Events in the next ``days`` days — records only, never sent.

    Same shape as :func:`upcoming_events`; the name says what it
    is for, the function only lists.
    """
    return upcoming_events(events_dir, from_date=from_date, days=days)


# ---------------------------------------------------------------------------
# Timezones and display
# ---------------------------------------------------------------------------

def event_datetime(event):
    """The event's date+time as an aware datetime, or None.

    A date-only event has no time, so there is no datetime to
    return — callers must not invent one. Time is interpreted
    in the event's own timezone.
    """
    if not event.get("time"):
        return None
    hour, minute = (int(part) for part in event["time"].split(":"))
    return datetime.combine(
        _date.fromisoformat(event["date"]),
        _time(hour, minute),
        tzinfo=ZoneInfo(event["timezone"] or DEFAULT_TIMEZONE))


def event_time_in_timezone(event, timezone_name):
    """The event's time as ``HH:MM <tz>`` in another timezone.

    Returns None for date-only events (no time to convert) and
    for events whose stored time cannot be parsed.
    """
    moment = event_datetime(event)
    if moment is None:
        return None
    target = _parse_timezone(timezone_name)
    converted = moment.astimezone(ZoneInfo(target))
    return converted.strftime("%H:%M") + f" {target}"


def event_display(event):
    """One-line display text for an event: date, time, title.

    The time is shown in the event's own timezone. A date-only
    event shows just the date; notes such as "After close" stay
    in the notes field, not in the display line.
    """
    text = event.get("title", "")
    if event.get("ticker"):
        text = f"{event['ticker']} — {text}"
    when = event.get("date", "")
    if event.get("time"):
        when += f" {event['time']} {event.get('timezone') or ''}".rstrip()
    return f"{when}: {text}"


def events_to_upcoming_items(events):
    """Events as Step 32 ``{"item", "date"}`` "what to watch" items.

    Items are listed exactly as the stored events say, never
    invented. The item text is the event display line without
    the leading date (the date is carried in the ``date`` field).
    """
    items = []
    for event in events:
        text = event.get("title", "")
        if event.get("ticker"):
            text = f"{event['ticker']} — {text}"
        if event.get("time"):
            text += (f" {event['time']} "
                     f"{event.get('timezone') or ''}".rstrip())
        items.append({"item": text, "date": event.get("date")})
    return items


def week_ahead(events_dir=None, start_date=None):
    """The next 7 days of events, grouped by day.

    Returns ``{"start_date", "end_date", "days": [...],
    "items": [...]}`` where each day is ``{"date", "weekday",
    "events": [...]}`` and ``items`` is the Step 32
    "what to watch" list for the whole week. ``start_date``
    defaults to today (Asia/Singapore). Days with no events
    are listed with an empty event list, so the week is
    complete rather than silently gapped.
    """
    if start_date is None:
        start_date = datetime.now(
            ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()
    start = _parse_date(start_date)
    start_day = _date.fromisoformat(start)
    end_day = start_day + timedelta(days=6)
    events = list_events(events_dir, start=start,
                         end=end_day.isoformat())
    by_date = {}
    for event in events:
        by_date.setdefault(event["date"], []).append(event)
    days = []
    for offset in range(7):
        day = start_day + timedelta(days=offset)
        day_iso = day.isoformat()
        days.append({
            "date": day_iso,
            "weekday": day.strftime("%A"),
            "events": by_date.get(day_iso, []),
        })
    return {
        "start_date": start,
        "end_date": end_day.isoformat(),
        "days": days,
        "items": events_to_upcoming_items(events),
        "event_count": len(events),
    }


# ---------------------------------------------------------------------------
# Outcomes and post-event re-checks
# ---------------------------------------------------------------------------

def record_outcome(event, outcome, events_dir=None):
    """Record a caller-supplied outcome on an event and save it.

    ``outcome`` is a dict of actuals the caller supplies after
    the event — e.g. ``{"reported_eps": 1.23,
    "expected_eps": 1.20}``. Nothing is fetched. Where both a
    reported and an expected value exist for the same measure
    (EPS, revenue, net income), a surprise percentage is
    computed and stored alongside; it is arithmetic on
    caller-supplied numbers, not a fetched result. Returns the
    updated event dict; the file is rewritten in place.
    """
    if not isinstance(outcome, dict) or not outcome:
        raise ValueError("outcome must be a non-empty dict")
    event = dict(event)
    stored = dict(outcome)
    for measure in ("eps", "revenue", "net_income"):
        reported = stored.get(f"reported_{measure}")
        expected = stored.get(f"expected_{measure}")
        if reported is not None and expected not in (None, 0):
            try:
                stored[f"{measure}_surprise_pct"] = (
                    (reported - expected) / expected)
            except TypeError:
                pass
    event["outcome"] = stored
    event["outcome_recorded_at"] = _utcnow_iso()
    events_dir = (Path(events_dir) if events_dir
                  else DEFAULT_EVENTS_DIR)
    if event.get("event_id"):
        path = _event_file(events_dir, event["event_id"])
        event["file_path"] = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event, indent=2), encoding="utf-8")
    return event


def _summary_facts(summary, health_verdict=None):
    """The comparable facts from a Step 23 margin summary (or None).

    ``health_verdict`` is the Step 18 verdict, computed by the
    caller (``post_event_check``) — it is not part of the Step
    23 summary dict.
    """
    if not summary or not isinstance(summary, dict):
        return None
    quality = summary.get("business_quality") or {}
    deterioration = summary.get("deterioration") or {}
    # Step 23's tier is a dict {"tier": name, "reasons": [...]} —
    # the comparable fact is the tier name; the reasons are kept
    # alongside, not compared.
    tier = summary.get("tier")
    if isinstance(tier, dict):
        tier_name, tier_reasons = tier.get("tier"), tier.get("reasons")
    else:
        tier_name, tier_reasons = tier, None
    return {
        "tier": tier_name,
        "tier_reasons": tier_reasons,
        "quality_rating": (quality.get("rating")
                           if isinstance(quality, dict) else None),
        "health_verdict": health_verdict,
        "material_deterioration": deterioration.get(
            "material_deterioration") if isinstance(
                deterioration, dict) else None,
        "price": summary.get("price"),
        "price_as_of": summary.get("price_as_of"),
        "conservative_value": (summary.get("fair_value_range") or {})
        .get("conservative"),
        "base_value": (summary.get("fair_value_range") or {}).get("base"),
    }


def post_event_check(conn, event, before_summary=None,
                     price_input=None, sector_override=None):
    """Re-check a ticker after an event against a before snapshot.

    Runs the Step 23 margin-of-safety summary now (the caller
    runs the pipeline; this module records the before/after it
    is given) and compares tier, quality, health, deterioration
    and valuation range with ``before_summary`` when one is
    supplied. ``before_summary`` is a Step 23 summary dict as
    returned by ``margin_of_safety_summary`` — typically the
    one recorded when the event was added — plus an optional
    ``health_verdict`` key the caller may have added from the
    Step 18 health assessment. With no before summary the
    after facts are reported and ``changes`` is empty. A ticker
    with no data comes back as ``insufficient_data`` with its
    reason, never a crash: a failed pipeline run is recorded
    in ``after_error``, not raised.
    """
    from agents import financial_health as fh
    from agents import intrinsic_valuation as iv
    from agents import margin_of_safety as mos
    from agents import market_scanner as ms

    ticker = event.get("ticker")
    result = {
        "event_id": event.get("event_id"),
        "ticker": ticker,
        "event_date": event.get("date"),
        "event_type": event.get("event_type"),
        "before": _summary_facts(
            before_summary,
            (before_summary or {}).get("health_verdict")),
        "after": None,
        "after_error": None,
        "changes": [],
    }
    if ticker is None:
        result["after_error"] = (
            "no ticker on this event — a market-wide event has no "
            "single company to re-check")
        return result
    try:
        # A bare number is accepted, like the Step 25 scanner
        # accepts it; the Step 23 summary itself takes a dict.
        normalised_price = ms.normalise_price_input(price_input)
        # TTM or annual starting FCF for the DCF path, the same
        # rule the Step 25 scanner uses: TTM when the data
        # supports it, otherwise the labelled annual basis (an
        # annual-only history has no TTM to build).
        starting_fcf_basis = "ttm"
        try:
            inputs, _warnings = iv.valuation_inputs(conn, ticker)
            if inputs.get("ttm_free_cash_flow") is None:
                starting_fcf_basis = "annual"
        except Exception:  # noqa: BLE001 - basis falls back
            starting_fcf_basis = "annual"
        after = mos.margin_of_safety_summary(
            conn, ticker, price_input=normalised_price,
            sector_override=sector_override,
            starting_fcf_basis=starting_fcf_basis)
        try:
            health = fh.assess_health(conn, ticker)
            health_verdict = health.get("verdict")
        except Exception:  # noqa: BLE001 - record, never crash
            health_verdict = None
        result["after"] = _summary_facts(after, health_verdict)
        result["after_tier"] = result["after"]["tier"]
    except Exception as exc:  # noqa: BLE001 - report, never crash
        result["after_error"] = str(exc)
        return result
    before = result["before"]
    after = result["after"]
    if before is None:
        return result
    for field in ("tier", "quality_rating", "health_verdict",
                  "material_deterioration", "conservative_value",
                  "base_value"):
        old, new = before.get(field), after.get(field)
        if old != new:
            result["changes"].append({
                "field": field,
                "before": old,
                "after": new,
            })
    return result


# ---------------------------------------------------------------------------
# Seeding (caller-supplied research only)
# ---------------------------------------------------------------------------

def seed_week_ahead(events_dir=None, overwrite=False):
    """Seed the known week ahead from the owner's research.

    Adds the ``SEED_EVENTS`` (Mon 9 – Fri 13 Nov 2026, plus
    NVDA on Tue 17 Nov and the 13F due window) to the store,
    labelled with their source. An event is skipped when an
    identical one (same date, ticker, type and title) already
    exists, unless ``overwrite`` is set — seeding twice adds
    nothing the second time. Returns the list of events added.
    """
    directory = Path(events_dir) if events_dir else DEFAULT_EVENTS_DIR
    existing = list_events(directory)
    seen = {(e.get("date"), e.get("ticker"), e.get("event_type"),
             e.get("title")) for e in existing}
    added = []
    for seed in SEED_EVENTS:
        key = (seed["date"], seed["ticker"], seed["event_type"],
               seed["title"])
        if key in seen and not overwrite:
            continue
        added.append(add_event(
            events_dir=directory,
            ticker=seed["ticker"],
            event_type=seed["event_type"],
            date=seed["date"],
            time=seed["time"],
            timezone=seed["timezone"],
            title=seed["title"],
            source=seed["source"],
            notes=seed["notes"]))
        seen.add(key)
    return added
