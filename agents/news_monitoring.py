"""Step 34 — Company news and event monitoring.

A watchlist monitor that answers "what is new since I last
looked?" for the tickers the owner follows. It reads only what
other steps already stored — Step 31 news run files under
``data/news/`` and Step 33 event files under ``data/events/`` —
and compares the latest Step 23/24/27 evidence with the
evidence at the last check (Step 30 snapshots). It never
fetches, never sends, never schedules, and never writes to the
production database. Everything it produces is a record; the
alerts it raises are records only, for a later step (or the
owner) to deliver.

Monitoring state is one JSON file (default
``data/monitoring/monitoring_state.json``), following the
Step 26/30/31/33 file pattern: per ticker, the last-checked
timestamp, the article keys and event ids already seen, the
last evidence snapshot, and the alert keys already raised.
The next run reports only what is new.

Per-ticker record fields: ticker, checked_at, as_of,
new_articles (title, source, published date, URL — the
stored snippet is kept on the record too), upcoming events
in the next N days, past events with any recorded outcome,
the evidence-snapshot status, the list of changes since the
last check, and the price-vs-alert-price position. A ticker
with no stored news and no events returns an empty record
with its reason, never an error.

Alert records: a watched ticker appearing as a
strongest_alert candidate in a caller-supplied Step 26
detection result, or a thesis whose review suggests
``invalidated`` (or ``confirmed``) in a caller-supplied
Step 30 review. An alert is raised once per key — re-running
the monitor does not re-raise an alert already in the state.

No buy/sell call anywhere; news and events are input to
research, not recommendations.
"""

import json
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from agents import event_tracking as et
from agents import news_collection as nc
from agents import thesis_tracking as tt

DEFAULT_MONITORING_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "monitoring")
DEFAULT_STATE_FILE = DEFAULT_MONITORING_DIR / "monitoring_state.json"
DEFAULT_UPCOMING_DAYS = 7
DEFAULT_PAST_EVENT_LIMIT = 5
DEFAULT_TIMEZONE = "Asia/Singapore"

BAND_STRONGEST_ALERT = "strongest_alert"


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_sgt():
    return datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()


def _parse_date(value):
    """Validate a YYYY-MM-DD date; return the string or raise."""
    if not isinstance(value, str):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    _date.fromisoformat(value)  # raises ValueError on a bad date
    return value


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def new_state(watchlist=None):
    """An empty monitoring state dict."""
    return {
        "version": 1,
        "watchlist": [t.upper() for t in (watchlist or [])],
        "last_checked_at": None,
        "tickers": {},
        "alerted_keys": [],
    }


def _state_path(state_path):
    return Path(state_path) if state_path else DEFAULT_STATE_FILE


def load_state(state_path=None):
    """Load the monitoring state file, or a fresh state.

    A missing file is a first run, not an error. A corrupt
    file raises — state is the monitor's memory, and silently
    discarding it would re-report everything as new.
    """
    path = _state_path(state_path)
    if not path.exists():
        return new_state()
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise ValueError(f"monitoring state is not an object: {path}")
    state.setdefault("version", 1)
    state.setdefault("watchlist", [])
    state.setdefault("last_checked_at", None)
    state.setdefault("tickers", {})
    state.setdefault("alerted_keys", [])
    return state


def save_state(state, state_path=None):
    """Write the monitoring state file; return its path."""
    path = _state_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return str(path)


def _ticker_state(state, ticker):
    """The per-ticker state entry, created if absent."""
    tickers = state.setdefault("tickers", {})
    entry = tickers.setdefault(ticker.upper(), {
        "last_checked_at": None,
        "seen_article_keys": [],
        "seen_event_ids": [],
        "last_snapshot": None,
    })
    entry.setdefault("seen_article_keys", [])
    entry.setdefault("seen_event_ids", [])
    return entry


# ---------------------------------------------------------------------------
# Articles: what is new
# ---------------------------------------------------------------------------

def article_key(article):
    """A stable string key for a stored article (Step 31 dedupe)."""
    kind, *parts = nc.article_key(article)
    return kind + ":" + "|".join(str(p) for p in parts)


def _article_since_cutoff(article, last_checked_at):
    """True when an article cannot be shown to predate the check.

    Used only on a first run, when there is no seen-set yet:
    an article counts as new when it was fetched at or after
    the last check, or was published on or after that date, or
    carries neither a fetch time nor a publishable date (so its
    age cannot be established and it is reported rather than
    silently dropped).
    """
    fetched_at = article.get("fetched_at") or ""
    if fetched_at and fetched_at >= last_checked_at:
        return True
    iso = article.get("published_date_iso") or ""
    if iso and iso >= last_checked_at[:10]:
        return True
    return not fetched_at and not iso


def new_articles(ticker, ticker_state, news_dir=None):
    """Stored articles for ``ticker`` that are new since last check.

    Returns ``(new, all)`` — the new articles and the full
    stored list, newest first. Newness is the seen-set when
    one exists; on a first run it is the last-checked
    timestamp (or everything, when there is no timestamp).
    """
    ticker = ticker.upper()
    articles = nc.list_articles(ticker, news_dir=news_dir)
    seen = set((ticker_state or {}).get("seen_article_keys") or [])
    last_checked_at = (ticker_state or {}).get("last_checked_at")
    if seen:
        fresh = [a for a in articles
                 if article_key(a) not in seen]
    elif last_checked_at:
        fresh = [a for a in articles
                 if _article_since_cutoff(a, last_checked_at)]
    else:
        fresh = list(articles)
    return fresh, articles


def _article_summary(article):
    """The fields a monitoring record carries for one article."""
    return {
        "title": article.get("title", ""),
        "source": article.get("source", ""),
        "published_date": article.get("published_date") or "",
        "published_date_iso": article.get("published_date_iso"),
        "url": article.get("url", ""),
        "snippet": article.get("snippet", ""),
        "fetched_at": article.get("fetched_at") or "",
    }


# ---------------------------------------------------------------------------
# Events: upcoming and past
# ---------------------------------------------------------------------------

def upcoming_events_for_ticker(ticker, events_dir=None, as_of=None,
                               days=DEFAULT_UPCOMING_DAYS):
    """Ticker events from ``as_of`` for the next ``days`` days."""
    if as_of is None:
        as_of = _today_sgt()
    start = _parse_date(as_of)
    end = (_date.fromisoformat(start)
           + timedelta(days=max(int(days), 1) - 1)).isoformat()
    return et.list_events(events_dir, ticker=ticker.upper(),
                          start=start, end=end)


def upcoming_market_events(events_dir=None, as_of=None,
                           days=DEFAULT_UPCOMING_DAYS):
    """Market-wide events (no ticker) from ``as_of`` for ``days``."""
    if as_of is None:
        as_of = _today_sgt()
    start = _parse_date(as_of)
    end = (_date.fromisoformat(start)
           + timedelta(days=max(int(days), 1) - 1)).isoformat()
    return et.list_events(events_dir, ticker="MARKET",
                          start=start, end=end)


def past_events_for_ticker(ticker, events_dir=None, as_of=None,
                           limit=DEFAULT_PAST_EVENT_LIMIT):
    """Ticker events before ``as_of``, most recent first, capped.

    Each record carries whether an outcome has been recorded
    yet (``outcome_recorded``), so a past event with no outcome
    is visibly open rather than silently complete.
    """
    if as_of is None:
        as_of = _today_sgt()
    day = _parse_date(as_of)
    events = et.list_events(events_dir, ticker=ticker.upper(),
                            end=(_date.fromisoformat(day)
                                 - timedelta(days=1)).isoformat())
    events = sorted(events, key=lambda e: e.get("date", ""),
                    reverse=True)[:max(int(limit), 0)]
    for event in events:
        event = dict(event)
        event["outcome_recorded"] = event.get("outcome") is not None
        yield event


# ---------------------------------------------------------------------------
# What changed: evidence snapshots
# ---------------------------------------------------------------------------

def compare_snapshots(old, new):
    """Field-by-field changes between two Step 30 snapshots.

    The same fields Step 30 compares (price, tier, quality,
    deterioration, health, strongest-alert price) plus the
    fair-value range and the latest key figures. A field that
    changed from a value to None (or the reverse) is a change,
    not a missing value.
    """
    if not old or not new:
        return []
    changes = []
    for field, label in tt.COMPARE_FIELDS:
        before, after = old.get(field), new.get(field)
        if before != after:
            changes.append({"field": field, "label": label,
                            "before": before, "after": after})
    old_range = old.get("fair_value_range") or {}
    new_range = new.get("fair_value_range") or {}
    for point in ("conservative", "base", "optimistic"):
        before, after = old_range.get(point), new_range.get(point)
        if before != after:
            changes.append({
                "field": f"fair_value_range.{point}",
                "label": f"Fair value ({point})",
                "before": before, "after": after})
    old_figures = old.get("key_figures") or {}
    new_figures = new.get("key_figures") or {}
    for concept in ("revenue", "net_income", "free_cash_flow"):
        before, after = (old_figures.get(concept),
                         new_figures.get(concept))
        if before != after:
            changes.append({
                "field": f"key_figures.{concept}",
                "label": f"Latest {concept.replace('_', ' ')}",
                "before": before, "after": after})
    return changes


def price_vs_alert(snapshot):
    """Where the price sits against the strongest-alert price.

    ``below_alert`` is True only when both a price and an
    alert price exist and the price is at or below it; None
    when either is missing, so a missing value is never
    reported as a position.
    """
    snapshot = snapshot or {}
    price = snapshot.get("price")
    alert_price = snapshot.get("strongest_alert_threshold_price")
    if price is None or alert_price is None:
        return {"price": price, "alert_price": alert_price,
                "below_alert": None, "distance_pct": None}
    distance = (price - alert_price) / alert_price
    return {"price": price, "alert_price": alert_price,
            "below_alert": price <= alert_price,
            "distance_pct": distance}


def _current_snapshot(conn, ticker, price_input=None,
                      sector_override=None):
    """The Step 30 evidence snapshot now, or None on failure.

    A snapshot that cannot be built is recorded as a failure
    (``snapshot_error``), never raised — one ticker's missing
    data must not stop the whole watchlist check.
    """
    if conn is None:
        return None, "no database connection supplied — valuation "
    try:
        snapshot = tt.build_evidence_snapshot(
            conn, ticker, price_input=price_input,
            sector_override=sector_override)
    except Exception as exc:  # noqa: BLE001 - record, never crash
        return None, f"evidence snapshot failed: {exc}"
    if snapshot.get("status") == "insufficient_data":
        return snapshot, snapshot.get("reason") or (
            "no valuation range could be computed")
    return snapshot, None


# ---------------------------------------------------------------------------
# Per-ticker and watchlist checks
# ---------------------------------------------------------------------------

def check_ticker(conn, ticker, ticker_state=None, news_dir=None,
                 events_dir=None, as_of=None, upcoming_days=None,
                 price_input=None, sector_override=None):
    """One ticker's monitoring record.

    ``ticker_state`` is the per-ticker entry from the state
    (or None for a stateless check — everything is then new).
    ``conn`` may be None: the news/event part still runs and
    the snapshot part is reported as not checked. ``as_of``
    defaults to today (Asia/Singapore).
    """
    ticker = ticker.upper()
    if as_of is None:
        as_of = _today_sgt()
    as_of = _parse_date(as_of)
    days = (DEFAULT_UPCOMING_DAYS if upcoming_days is None
            else max(int(upcoming_days), 1))
    checked_at = _utcnow_iso()
    reasons = []

    fresh, all_articles = new_articles(
        ticker, ticker_state, news_dir=news_dir)
    if not all_articles:
        reasons.append("no stored news articles for this ticker")

    upcoming = upcoming_events_for_ticker(
        ticker, events_dir, as_of=as_of, days=days)
    past = list(past_events_for_ticker(
        ticker, events_dir, as_of=as_of))
    if not upcoming and not past:
        reasons.append("no stored events for this ticker")

    snapshot, snapshot_error = _current_snapshot(
        conn, ticker, price_input=price_input,
        sector_override=sector_override)
    if conn is None:
        snapshot_status = "not_checked"
    elif snapshot is None:
        snapshot_status = "insufficient_data"
    else:
        snapshot_status = snapshot.get("status")
    if snapshot_error and conn is not None:
        reasons.append(snapshot_error)

    old_snapshot = (ticker_state or {}).get("last_snapshot")
    changes = compare_snapshots(old_snapshot, snapshot) \
        if old_snapshot and snapshot else []
    position = price_vs_alert(snapshot)

    return {
        "ticker": ticker,
        "checked_at": checked_at,
        "as_of": as_of,
        "new_articles": [_article_summary(a) for a in fresh],
        "new_article_count": len(fresh),
        "total_articles": len(all_articles),
        "upcoming_events": upcoming,
        "upcoming_event_count": len(upcoming),
        "past_events": past,
        "past_event_count": len(past),
        "snapshot_status": snapshot_status,
        "snapshot_error": snapshot_error,
        "changes": changes,
        "change_count": len(changes),
        "price_vs_alert": position,
        "reasons": reasons,
    }


def check_watchlist(conn, watchlist, state=None, news_dir=None,
                    events_dir=None, as_of=None, upcoming_days=None,
                    prices=None, sector_overrides=None):
    """The whole watchlist's monitoring record.

    ``state`` is the loaded state dict (not saved here —
    :func:`run_monitor` saves it). Market-wide events are
    reported once at watchlist level, not repeated per ticker.
    ``prices`` maps ticker to a price input for the snapshot
    check; ``sector_overrides`` maps ticker to a sector.
    """
    if as_of is None:
        as_of = _today_sgt()
    as_of = _parse_date(as_of)
    watchlist = [t.upper() for t in (watchlist or [])]
    state = state or new_state(watchlist)
    prices = prices or {}
    sector_overrides = sector_overrides or {}
    checked_at = _utcnow_iso()

    tickers = {}
    for ticker in watchlist:
        entry = (state.get("tickers") or {}).get(ticker)
        tickers[ticker] = check_ticker(
            conn, ticker, ticker_state=entry, news_dir=news_dir,
            events_dir=events_dir, as_of=as_of,
            upcoming_days=upcoming_days,
            price_input=prices.get(ticker),
            sector_override=sector_overrides.get(ticker))

    market_events = upcoming_market_events(
        events_dir, as_of=as_of,
        days=(DEFAULT_UPCOMING_DAYS if upcoming_days is None
              else upcoming_days))
    summary = {
        "tickers_checked": len(watchlist),
        "new_articles": sum(
            r["new_article_count"] for r in tickers.values()),
        "upcoming_events": sum(
            r["upcoming_event_count"] for r in tickers.values()),
        "market_events": len(market_events),
        "tickers_with_changes": sum(
            1 for r in tickers.values() if r["change_count"]),
        "total_changes": sum(
            r["change_count"] for r in tickers.values()),
        "tickers_with_new_articles": [
            t for t, r in tickers.items()
            if r["new_article_count"]],
    }
    return {
        "checked_at": checked_at,
        "as_of": as_of,
        "watchlist": watchlist,
        "tickers": tickers,
        "market_events": market_events,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Alerts (records only — never sent)
# ---------------------------------------------------------------------------

def _alert(alert_type, ticker, reason, evidence, as_of=None):
    return {
        "alert_id": f"{alert_type}:{ticker}",
        "alert_type": alert_type,
        "ticker": ticker,
        "detected_at": as_of or _utcnow_iso(),
        "reason": reason,
        "evidence": evidence or {},
    }


def build_alerts(detection=None, thesis_reviews=None, as_of=None):
    """Alert records from caller-supplied inputs.

    ``detection`` is a Step 26 ``detect_from_scan`` result:
    every ``strongest_alert`` candidate becomes an alert.
    ``thesis_reviews`` is a list of Step 30 review dicts: a
    review suggesting ``invalidated`` becomes a
    ``thesis_invalidated`` alert, one suggesting
    ``confirmed`` a ``thesis_confirmed`` alert. Watch-band
    candidates and still-active theses are not alerts.
    """
    alerts = []
    for candidate in (detection or {}).get("candidates", []):
        if candidate.get("detection_band") != BAND_STRONGEST_ALERT:
            continue
        ticker = candidate.get("ticker")
        alerts.append(_alert(
            "strongest_alert", ticker,
            "strongest alert: price at least 20% below the "
            "conservative intrinsic value with no material "
            "deterioration and quality adequate or better",
            {
                "detection_band": candidate.get("detection_band"),
                "tier": candidate.get("tier"),
                "price": candidate.get("price"),
                "price_as_of": candidate.get("price_as_of"),
                "fair_value_range": candidate.get("fair_value_range"),
                "margin_of_safety_vs_conservative": candidate.get(
                    "margin_of_safety_vs_conservative"),
                "quality_rating": candidate.get("quality_rating"),
                "health_verdict": candidate.get("health_verdict"),
                "qualifies_reasons": candidate.get(
                    "qualifies_reasons", []),
            },
            as_of=as_of))
    for review in thesis_reviews or []:
        suggested = review.get("suggested_status")
        if suggested not in ("invalidated", "confirmed"):
            continue
        ticker = review.get("ticker")
        if suggested == "invalidated":
            reason = ("thesis invalidation condition met: "
                      + (review.get("suggested_reason") or ""))
        else:
            reason = ("thesis confirmation condition met: "
                      + (review.get("suggested_reason") or ""))
        alerts.append(_alert(
            f"thesis_{suggested}", ticker, reason,
            {
                "thesis_id": review.get("thesis_id"),
                "suggested_status": suggested,
                "suggested_reason": review.get("suggested_reason"),
                "confirm_met": review.get("confirm_met"),
                "confirm_total": review.get("confirm_total"),
                "invalidate_met": review.get("invalidate_met"),
                "invalidate_total": review.get("invalidate_total"),
                "changes": review.get("changes", []),
            },
            as_of=as_of))
    return alerts


# ---------------------------------------------------------------------------
# Full run: check, alert, persist
# ---------------------------------------------------------------------------

def run_monitor(conn, watchlist, state_path=None, news_dir=None,
                events_dir=None, detection=None, thesis_reviews=None,
                as_of=None, upcoming_days=None, prices=None,
                sector_overrides=None):
    """Check the watchlist, raise new alerts, persist the state.

    Loads the state, runs :func:`check_watchlist`, builds
    alerts from the caller-supplied detection and thesis
    reviews, keeps only alerts whose key has not been raised
    before, then marks everything seen — the new article keys
    and event ids, the fresh evidence snapshot per ticker, the
    last-checked timestamp, the new alert keys — and saves
    the state. Returns the monitoring record plus ``alerts``
    (new this run) and ``alerts_already_seen``.
    """
    state = load_state(state_path)
    record = check_watchlist(
        conn, watchlist, state=state, news_dir=news_dir,
        events_dir=events_dir, as_of=as_of,
        upcoming_days=upcoming_days, prices=prices,
        sector_overrides=sector_overrides)
    checked_at = record["checked_at"]
    as_of = record["as_of"]

    all_alerts = build_alerts(detection, thesis_reviews,
                              as_of=checked_at)
    already = set(state.get("alerted_keys") or [])
    new_alerts = [a for a in all_alerts
                  if a["alert_id"] not in already]

    watchlist = record["watchlist"]
    state["watchlist"] = watchlist
    state["last_checked_at"] = checked_at
    for ticker in watchlist:
        entry = _ticker_state(state, ticker)
        entry["last_checked_at"] = checked_at
        ticker_record = record["tickers"][ticker]
        for article in nc.list_articles(ticker, news_dir=news_dir):
            key = article_key(article)
            if key not in entry["seen_article_keys"]:
                entry["seen_article_keys"].append(key)
        for event in et.list_events(events_dir, ticker=ticker):
            event_id = event.get("event_id")
            if event_id and event_id not in entry["seen_event_ids"]:
                entry["seen_event_ids"].append(event_id)
        snapshot, _error = _current_snapshot(
            conn, ticker, price_input=(prices or {}).get(ticker),
            sector_override=(sector_overrides or {}).get(ticker))
        if snapshot is not None:
            entry["last_snapshot"] = snapshot
    for alert in new_alerts:
        state.setdefault("alerted_keys", []).append(alert["alert_id"])
    state_file = save_state(state, state_path)

    record["alerts"] = new_alerts
    record["alerts_already_seen"] = len(all_alerts) - len(new_alerts)
    record["alert_count"] = len(new_alerts)
    record["state_file"] = state_file
    return record


# ---------------------------------------------------------------------------
# Briefing lines
# ---------------------------------------------------------------------------

def briefing_lines(record):
    """One line per ticker, plus one per alert, for a briefing.

    Plain text, no recommendation: what is new, what changed,
    what is coming up. A ticker with nothing new gets a line
    saying so, so a quiet ticker is visibly quiet rather than
    silently absent.
    """
    lines = []
    for ticker in (record or {}).get("watchlist", []):
        item = (record.get("tickers") or {}).get(ticker) or {}
        parts = [ticker]
        if item.get("new_article_count"):
            parts.append(
                f"{item['new_article_count']} new article(s)")
        if item.get("upcoming_event_count"):
            parts.append(
                f"{item['upcoming_event_count']} upcoming event(s)")
        if item.get("change_count"):
            parts.append(f"{item['change_count']} change(s)")
        position = item.get("price_vs_alert") or {}
        if position.get("below_alert") is True:
            parts.append("price at or below strongest-alert price")
        if len(parts) == 1:
            parts.append("nothing new")
        lines.append(" — ".join(parts))
    for alert in (record or {}).get("alerts", []):
        lines.append(
            f"ALERT {alert['alert_type']}: {alert['ticker']} — "
            f"{alert['reason']}")
    return lines
