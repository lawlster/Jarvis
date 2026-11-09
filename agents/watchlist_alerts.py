"""Step 38 — Automated watchlist and valuation alerts (Phase E).

The alert ENGINE: it evaluates caller-supplied evidence against
the owner's alert rules and records what fires.  It fetches
nothing, sends nothing and schedules nothing — a live price feed
is a later concern, and delivery is Step 46.  No trades are ever
placed here; every alert is a record that a name is worth a
closer look, and the final decision is always the owner's.

What already existed (audited first, not duplicated):

* Step 26 (``agents/undervalued_detector.py``) — detection bands
  (``strongest_alert``, ``watch_discount``, ``watch_below_base``),
  value traps, "why not" explanations and alert-ready briefing
  lines.  This module consumes its detection records; it does
  not re-derive bands.
* Step 34 (``agents/news_monitoring.py``) — ``build_alerts``
  already turns a Step 26 detection result into
  ``strongest_alert`` alert records and Step 30 thesis reviews
  into ``thesis_confirmed`` / ``thesis_invalidated`` records,
  and ``run_monitor`` raises each once per ``alert_id`` in the
  monitoring state.  This module reuses ``build_alerts`` for
  the thesis rules and keeps its own state file, because the
  monitor's state is about news/events/what-changed, while
  this state is about alert rules, clearing and re-firing.
* The legacy ``agents/alert_manager.py`` (DB tables
  ``valuation_alerts`` / ``alert_states`` on the production
  database) is NOT reused: this step writes no production
  tables.  Alert state lives in one JSON file (default
  ``data/alerts/alert_state.json``).

Rules evaluated here (each fires at most once per key while the
condition holds; see :func:`evaluate_alerts`):

* ``strongest_alert`` — the owner's standing rule: price at
  least 20% below a conservative intrinsic-value estimate,
  no material deterioration, quality adequate or better.
  Fires from a Step 26 detection candidate in the
  ``strongest_alert`` band, or from a Step 23 summary whose
  tier is ``strongest_alert``.
* ``price_below_alert_price`` — a caller-supplied current
  price at or below the Step 23 strongest-alert threshold
  price.  This is the fresh-price path: the stored tier may
  still say overvalued (it was computed on an older price)
  while a new price has crossed the threshold.  It does not
  fire when ``strongest_alert`` already fired for the ticker
  in the same run — the same fact twice is noise, not signal.
* ``watch_discount`` / ``watch_below_base`` — a Step 26 watch
  band.  A watch name is worth keeping in mind; it is not a
  strongest alert, so these records carry severity ``watch``.
* ``deterioration_material`` — material deterioration turned
  on since the last evaluation (previous evidence snapshot
  said clear, current evidence says deteriorated).  Needs a
  previous snapshot: the first evaluation of a ticker only
  establishes the baseline, it never fires this rule.
* ``quality_changed`` — the Step 24 quality rating changed
  since the last evaluation (e.g. excellent -> good).  A
  first rating, or a move from "no rating" to a rating, is
  baseline establishment, not a change.
* ``thesis_confirmed`` / ``thesis_invalidated`` — a Step 30
  review suggesting the thesis is confirmed or invalidated
  (reused from ``news_monitoring.build_alerts``).

A ticker with no price and no conservative value — or a
Step 23 tier of ``insufficient_data`` — is recorded as
``not_evaluated`` with the reason, never as "no alert".
Silence and "nothing to report" are different things, and
this module never confuses them.

Dedupe and clearing: an alert is active once raised.  While
its condition still holds, re-evaluation reports it under
``already_active`` and raises nothing new.  When a later
evaluation finds the condition gone (and the ticker was
evaluated that run), the alert moves to ``cleared`` with a
``cleared_at`` timestamp.  If the condition fires again
later, a new record is raised with ``refire_count``
incremented.  An alert can be acknowledged (records only);
acknowledging does not clear it.
"""

import json
import os
import sqlite3

from agents import news_monitoring as nm
from agents import undervalued_detector as ud
from agents import margin_of_safety as mos
from agents import market_scanner as ms

# Alert types (rules) and their severities.
RULE_STRONGEST_ALERT = "strongest_alert"
RULE_PRICE_BELOW_ALERT_PRICE = "price_below_alert_price"
RULE_WATCH_DISCOUNT = "watch_discount"
RULE_WATCH_BELOW_BASE = "watch_below_base"
RULE_DETERIORATION_MATERIAL = "deterioration_material"
RULE_QUALITY_CHANGED = "quality_changed"
RULE_THESIS_CONFIRMED = "thesis_confirmed"
RULE_THESIS_INVALIDATED = "thesis_invalidated"

SEVERITY = {
    RULE_STRONGEST_ALERT: "strongest",
    RULE_PRICE_BELOW_ALERT_PRICE: "strongest",
    RULE_WATCH_DISCOUNT: "watch",
    RULE_WATCH_BELOW_BASE: "watch",
    RULE_DETERIORATION_MATERIAL: "warning",
    RULE_QUALITY_CHANGED: "watch",
    RULE_THESIS_CONFIRMED: "watch",
    RULE_THESIS_INVALIDATED: "warning",
}

# Quality ratings that pass the candidate gate (Step 24), from
# Step 26 — reused so the two steps cannot drift apart.
ADEQUATE_QUALITY = ud.ADEQUATE_QUALITY

# Default alert-state location, relative to the project root.
DEFAULT_STATE_PATH = os.path.join("data", "alerts", "alert_state.json")

_STATE_VERSION = 1


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _utcnow_iso():
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def _state_path(state_path):
    if state_path is None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, DEFAULT_STATE_PATH)
    return state_path


def new_state():
    """An empty alert state."""
    return {
        "version": _STATE_VERSION,
        "active_alerts": {},
        "cleared_alerts": [],
        "last_evidence": {},
    }


def load_state(state_path=None):
    """Load the alert state, or an empty state when none exists.

    A corrupt state file raises — it is better to fail loudly
    than to silently forget which alerts were already raised.
    """
    path = _state_path(state_path)
    if not os.path.exists(path):
        return new_state()
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("version", _STATE_VERSION)
    state.setdefault("active_alerts", {})
    state.setdefault("cleared_alerts", [])
    state.setdefault("last_evidence", {})
    return state


def save_state(state, state_path=None):
    """Write the alert state (creating the directory)."""
    path = _state_path(state_path)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    return path


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------

def _normalise_watchlist(watchlist):
    """Watchlist entries (Step 35 dicts) or plain tickers -> tickers."""
    tickers = []
    for entry in watchlist or []:
        if isinstance(entry, str):
            tickers.append(entry)
        elif isinstance(entry, dict):
            ticker = entry.get("ticker")
            if ticker:
                tickers.append(ticker)
    return tickers


def _tier_of(value):
    """Tier string from a tier value.

    Step 25 scanner candidates carry the tier as a plain string
    ("overvalued", "strongest_alert", ...), but a raw Step 23
    ``margin_of_safety_summary`` carries it as a dict
    ({"tier": ..., "reasons": [...], ...}) — the scanner unwraps
    it, and this module accepts summaries directly, so it must
    unwrap too.  Returns None for anything else.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("tier")
    return None


def _quality_of(candidate, summary):
    """Quality rating string from a detection candidate or a
    Step 23 summary, or None."""
    if candidate is not None:
        rating = candidate.get("quality_rating")
        if rating:
            return rating
    if summary is not None:
        quality = summary.get("business_quality")
        if isinstance(quality, dict):
            return quality.get("rating")
        if isinstance(quality, str):
            return quality
    return None


def _deteriorated_of(candidate, summary):
    """Material-deterioration boolean from a detection candidate
    or a Step 23 summary, or None when unknown."""
    if candidate is not None and candidate.get("deteriorated") is not None:
        return candidate.get("deteriorated")
    if summary is not None:
        deterioration = summary.get("deterioration") or {}
        if deterioration.get("deteriorated") is not None:
            return deterioration.get("deteriorated")
    return None


def _evidence_snapshot(ticker, candidate, summary, price, alert_price,
                       as_of):
    """The evidence snapshot stored per ticker and carried on
    alert records — everything needed to judge the alert later,
    nothing invented."""
    fair_value_range = None
    tier = None
    health = None
    margin_cons = None
    if candidate is not None:
        fair_value_range = candidate.get("fair_value_range")
        tier = _tier_of(candidate.get("tier"))
        health = candidate.get("health_verdict")
        margin_cons = candidate.get("margin_of_safety_vs_conservative")
    if summary is not None:
        fair_value_range = fair_value_range or summary.get(
            "fair_value_range")
        tier = tier or _tier_of(summary.get("tier"))
        margin_cons = (margin_cons if margin_cons is not None
                       else summary.get("margin_of_safety_vs_conservative"))
        if health is None:
            # A raw Step 23 summary carries no top-level health
            # verdict; it lives in the deterioration inputs
            # (Step 18 verdict, also in the Step 27 inputs).
            deterioration = summary.get("deterioration") or {}
            health = (deterioration.get("inputs") or {}).get(
                "health_verdict")
            if health is None:
                deterioration_27 = summary.get("deterioration_27") or {}
                health = (deterioration_27.get("inputs") or {}).get(
                    "health_verdict")
    return {
        "ticker": ticker,
        "tier": tier,
        "price": price,
        "price_as_of": (candidate or {}).get("price_as_of")
                       if candidate is not None else None,
        "alert_price": alert_price,
        "fair_value_range": fair_value_range,
        "margin_of_safety_vs_conservative": margin_cons,
        "quality_rating": _quality_of(candidate, summary),
        "health_verdict": health,
        "deteriorated": _deteriorated_of(candidate, summary),
        "as_of": as_of,
        # Step 59: a live-derived candidate is provisional —
        # quality and deterioration are unknown from a live
        # pull.  The alert engine uses this flag so its reasons
        # never claim otherwise.
        "live_fallback": bool(
            (candidate or {}).get("live_fallback")),
    }


def _money(value):
    return f"${value:,.2f}" if value is not None else "unknown"


def _briefing_line(alert_type, ticker, evidence, reason):
    """One-line briefing text in the Step 32 summary shape.

    Plain text, no recommendation: what fired, the trigger
    value against its threshold, and the evidence.  The word
    "buy" never appears in a briefing line — an alert says a
    name is worth a closer look, and the final decision is
    always the owner's.
    """
    price = evidence.get("price")
    alert_price = evidence.get("alert_price")
    fair_value_range = evidence.get("fair_value_range") or {}
    conservative = fair_value_range.get("conservative")
    margin = evidence.get("margin_of_safety_vs_conservative")
    quality = evidence.get("quality_rating")
    health = evidence.get("health_verdict")
    label = {
        RULE_STRONGEST_ALERT: "STRONGEST ALERT",
        RULE_PRICE_BELOW_ALERT_PRICE: "PRICE BELOW ALERT PRICE",
        RULE_WATCH_DISCOUNT: "WATCH (near alert)",
        RULE_WATCH_BELOW_BASE: "WATCH (below base value)",
        RULE_DETERIORATION_MATERIAL: "DETERIORATION",
        RULE_QUALITY_CHANGED: "QUALITY CHANGED",
        RULE_THESIS_CONFIRMED: "THESIS CONFIRMED",
        RULE_THESIS_INVALIDATED: "THESIS INVALIDATED",
    }.get(alert_type, alert_type)
    if alert_type == RULE_STRONGEST_ALERT:
        margin_text = (f"{margin:.1%} below conservative value"
                       if margin is not None else
                       "below conservative value")
        body = (f"price {_money(price)} is {margin_text} "
                f"(conservative value {_money(conservative)}), "
                f"quality {quality}, health {health}")
    elif alert_type == RULE_PRICE_BELOW_ALERT_PRICE:
        body = (f"price {_money(price)} is at or below the "
                f"strongest-alert price {_money(alert_price)} "
                f"(conservative value {_money(conservative)}), "
                f"quality {quality}, health {health}")
    elif alert_type in (RULE_WATCH_DISCOUNT, RULE_WATCH_BELOW_BASE):
        margin_text = (f"{margin:.1%} below conservative value"
                       if margin is not None else
                       "below conservative value")
        body = (f"price {_money(price)} is {margin_text} "
                f"(conservative value {_money(conservative)}), "
                f"quality {quality}, health {health} — not yet at "
                "the 20% strongest-alert threshold")
    elif alert_type == RULE_DETERIORATION_MATERIAL:
        body = (f"material deterioration now flagged (was clear) "
                f"at price {_money(price)}, quality {quality}, "
                f"health {health}")
    elif alert_type == RULE_QUALITY_CHANGED:
        body = (f"quality rating {evidence.get('quality_from')} -> "
                f"{evidence.get('quality_to')} at price "
                f"{_money(price)}, health {health}")
    else:  # thesis rules
        body = reason
    # Step 59: a live-derived band is provisional — say so in
    # the briefing line, next to the (already honest) "unknown
    # (live pull)" quality and health.
    provisional_note = (
        " — PROVISIONAL: live-derived value; business quality "
        "and deterioration are unknown from a live pull, so "
        "this is not a confirmed Step 26 candidate"
        if evidence.get("live_fallback") else "")
    return (f"{ticker} — {label}: {body}{provisional_note}"
            + " (worth a closer look; the final decision is "
            "always the owner's)")


def _alert_record(alert_type, ticker, reason, evidence, trigger_value,
                  threshold, as_of, refire_count=0):
    """One alert record."""
    return {
        "alert_key": f"{alert_type}:{ticker}",
        "alert_type": alert_type,
        "ticker": ticker,
        "severity": SEVERITY.get(alert_type, "watch"),
        "status": "active",
        "raised_at": as_of,
        "cleared_at": None,
        "acknowledged": False,
        "acknowledged_at": None,
        "refire_count": refire_count,
        "trigger_value": trigger_value,
        "threshold": threshold,
        "reason": reason,
        "evidence": evidence,
        "briefing_line": _briefing_line(alert_type, ticker, evidence,
                                         reason),
    }


# ---------------------------------------------------------------------------
# Rule evaluation for one ticker
# ---------------------------------------------------------------------------

def _rules_for_ticker(ticker, candidate, summary, price, alert_price,
                      previous_evidence, as_of):
    """Fired rules for one evaluable ticker this run.

    Returns (fired, evidence) where fired is a list of
    :func:`_alert_record` dicts (without dedupe applied) and
    evidence is the snapshot to store.  A ticker is evaluable
    only when it has both a price and a conservative value;
    callers must check that first (see :func:`evaluate_alerts`).
    """
    evidence = _evidence_snapshot(ticker, candidate, summary, price,
                                  alert_price, as_of)
    fired = []
    tier = evidence.get("tier")
    margin_cons = evidence.get("margin_of_safety_vs_conservative")
    deteriorated = evidence.get("deteriorated")
    quality = evidence.get("quality_rating")
    band = (candidate or {}).get("detection_band")

    strongest = False
    # Step 59: a live-derived candidate is provisional — the
    # live pull says nothing about business quality or
    # deterioration, so the engine must never claim "no
    # material deterioration and quality adequate or better"
    # for one.  The alert still fires on the band (that is
    # the point of the daily scan), but the reason says
    # exactly what is and is not known.
    provisional = bool((candidate or {}).get("live_fallback"))
    if band == ud.BAND_STRONGEST_ALERT or tier == "strongest_alert":
        strongest = True
        if provisional:
            strongest_reason = (
                "PROVISIONAL strongest alert from a "
                "live-derived margin band: price at least 20% "
                "below the live-derived conservative value. "
                "Business quality and deterioration are unknown "
                "from a live pull, so this is NOT a confirmed "
                "Step 26 candidate — worth a closer look, never "
                "a recommendation.")
        else:
            strongest_reason = (
                "strongest alert: price at least 20% below the "
                "conservative intrinsic value with no material "
                "deterioration and quality adequate or better")
        fired.append(_alert_record(
            RULE_STRONGEST_ALERT, ticker,
            strongest_reason,
            evidence, trigger_value=margin_cons,
            threshold=mos.STRONGEST_ALERT_MARGIN, as_of=as_of))

    if (not strongest and price is not None and alert_price is not None
            and price <= alert_price):
        fired.append(_alert_record(
            RULE_PRICE_BELOW_ALERT_PRICE, ticker,
            "current price is at or below the strongest-alert "
            "threshold price (the stored tier was computed on an "
            "older price, so the tier has not caught up yet)",
            evidence, trigger_value=price, threshold=alert_price,
            as_of=as_of))

    if band in (ud.BAND_WATCH_DISCOUNT, ud.BAND_WATCH_BELOW_BASE):
        if provisional:
            watch_reason = (
                f"PROVISIONAL watch band '{band}' from a "
                "live-derived margin band: price is below the "
                "live-derived conservative value (or below "
                "base value) but by less than the 20% "
                "strongest-alert threshold. Business quality "
                "and deterioration are unknown from a live "
                "pull, so this is NOT a confirmed Step 26 "
                "candidate — worth a closer look, never a "
                "recommendation.")
        else:
            watch_reason = (
                f"watch band '{band}': price is below the "
                "conservative value (or below base value) but "
                "by less than the 20% strongest-alert "
                "threshold, with quality adequate or better "
                "and no material deterioration")
        fired.append(_alert_record(
            band, ticker,
            watch_reason,
            evidence, trigger_value=margin_cons,
            threshold=mos.STRONGEST_ALERT_MARGIN, as_of=as_of))

    if previous_evidence is not None:
        prev_deteriorated = previous_evidence.get("deteriorated")
        if prev_deteriorated is False and deteriorated is True:
            fired.append(_alert_record(
                RULE_DETERIORATION_MATERIAL, ticker,
                "material deterioration turned on since the last "
                "evaluation (was clear, now deteriorated)",
                evidence, trigger_value=True, threshold=False,
                as_of=as_of))
        prev_quality = previous_evidence.get("quality_rating")
        # Step 59: no quality-change alert for a provisional
        # candidate — its quality is "unknown (live pull)", so
        # a change from a stored-data rating to "unknown" is a
        # change in what evidence is available, not a change in
        # the business, and must not raise an alert.
        if (not provisional and prev_quality and quality
                and prev_quality != quality):
            quality_evidence = dict(evidence)
            quality_evidence["quality_from"] = prev_quality
            quality_evidence["quality_to"] = quality
            fired.append(_alert_record(
                RULE_QUALITY_CHANGED, ticker,
                f"business quality rating changed: {prev_quality} "
                f"-> {quality}",
                quality_evidence, trigger_value=quality,
                threshold=prev_quality, as_of=as_of))
    return fired, evidence


# ---------------------------------------------------------------------------
# Evaluation over a watchlist
# ---------------------------------------------------------------------------

def evaluate_alerts(watchlist, detection=None, summaries=None,
                    prices=None, thesis_reviews=None, state=None,
                    as_of=None):
    """Evaluate the alert rules over a watchlist.

    ``watchlist`` is a list of tickers or Step 35 watchlist
    entries.  ``detection`` is a Step 26 ``detect_from_scan``
    result (candidates carry band, tier, price, range, quality,
    health, deterioration).  ``summaries`` maps ticker -> Step 23
    ``margin_of_safety_summary`` dict.  ``prices`` maps ticker
    -> caller-supplied current price (a fresh price beats the
    stored one for the price-crossing rule).  ``thesis_reviews``
    is a list of Step 30 review dicts.  ``state`` is an alert
    state dict (see :func:`new_state`); when omitted, a fresh
    empty state is used and nothing is persisted — use
    :func:`run_alerts` to persist.

    Returns {"new_alerts", "already_active", "cleared_alerts",
    "not_evaluated", "evaluated", "skipped_thesis_reviews",
    "state"}.  The state dict is mutated in place (active
    alerts added/cleared, last evidence updated) and also
    returned.
    """
    as_of = as_of or _utcnow_iso()
    if state is None:
        state = new_state()
    tickers = _normalise_watchlist(watchlist)
    candidates = {}
    for record in (detection or {}).get("candidates", []):
        candidates[record.get("ticker")] = record
    summaries = summaries or {}
    prices = prices or {}
    active = state.setdefault("active_alerts", {})
    cleared = state.setdefault("cleared_alerts", [])
    last_evidence = state.setdefault("last_evidence", {})

    new_alerts = []
    already_active = []
    cleared_this_run = []
    not_evaluated = []
    evaluated = []
    fired_keys = set()

    for ticker in tickers:
        candidate = candidates.get(ticker)
        summary = summaries.get(ticker)
        fair_value_range = None
        if candidate is not None:
            fair_value_range = candidate.get("fair_value_range")
        if summary is not None:
            fair_value_range = (fair_value_range
                                or summary.get("fair_value_range"))
        conservative = (fair_value_range or {}).get("conservative")
        price = prices.get(ticker)
        if price is None and summary is not None:
            price = summary.get("price")
        if price is None and candidate is not None:
            price = candidate.get("price")
        alert_price = None
        if candidate is not None:
            alert_price = candidate.get(
                "strongest_alert_threshold_price")
        if summary is not None:
            alert_price = (alert_price if alert_price is not None
                           else summary.get(
                               "strongest_alert_threshold_price"))
        tier = _tier_of((candidate or {}).get("tier")) or \
            _tier_of((summary or {}).get("tier"))

        # Not evaluable: no price, no conservative value, or the
        # pipeline itself reported insufficient data.  Recorded
        # with the reason — never "no alert".
        if tier == "insufficient_data":
            not_evaluated.append({
                "ticker": ticker, "status": "not_evaluated",
                "reason": "Step 23 tier is insufficient_data: no "
                          "valuation could be computed from the "
                          "available data"})
            continue
        if price is None:
            not_evaluated.append({
                "ticker": ticker, "status": "not_evaluated",
                "reason": "no price available (no caller-supplied "
                          "price, no summary price, no detection "
                          "price)"})
            continue
        if conservative is None:
            not_evaluated.append({
                "ticker": ticker, "status": "not_evaluated",
                "reason": "no conservative intrinsic value "
                          "available (no fair-value range)"})
            continue

        evaluated.append(ticker)
        fired, evidence = _rules_for_ticker(
            ticker, candidate, summary, price, alert_price,
            last_evidence.get(ticker), as_of)
        last_evidence[ticker] = evidence
        for record in fired:
            key = record["alert_key"]
            fired_keys.add(key)
            if key in active:
                already_active.append(active[key])
            else:
                refire_count = 0
                for old in cleared:
                    if old.get("alert_key") == key:
                        refire_count = max(
                            refire_count,
                            old.get("refire_count", 0) + 1)
                record["refire_count"] = refire_count
                active[key] = record
                new_alerts.append(record)

    # Thesis rules, reused from Step 34 (which already maps
    # Step 30 reviews to thesis_confirmed / thesis_invalidated
    # records).  Only reviews for watchlist tickers are
    # evaluated; the rest are listed as skipped, not dropped
    # silently.
    skipped_thesis = []
    thesis_alerts = nm.build_alerts(
        detection=None, thesis_reviews=thesis_reviews, as_of=as_of)
    for alert in thesis_alerts:
        ticker = alert.get("ticker")
        if ticker not in tickers:
            skipped_thesis.append({
                "ticker": ticker,
                "alert_type": alert.get("alert_type"),
                "reason": "ticker is not on the watchlist"})
            continue
        alert_type = alert.get("alert_type")
        evidence = dict(alert.get("evidence") or {})
        evidence["ticker"] = ticker
        evidence["price"] = prices.get(ticker)
        record = _alert_record(
            alert_type, ticker, alert.get("reason", ""),
            evidence, trigger_value=evidence.get("suggested_status"),
            threshold=None, as_of=as_of)
        key = record["alert_key"]
        fired_keys.add(key)
        if key in active:
            already_active.append(active[key])
        else:
            refire_count = 0
            for old in cleared:
                if old.get("alert_key") == key:
                    refire_count = max(
                        refire_count,
                        old.get("refire_count", 0) + 1)
            record["refire_count"] = refire_count
            active[key] = record
            new_alerts.append(record)

    # Clearing: an active alert whose rule did not fire this run
    # — and whose ticker WAS evaluated (or whose thesis review
    # was supplied and did not suggest that status) — has
    # cleared.  A ticker that was not evaluated keeps its
    # active alerts: absence of evidence is not evidence of
    # clearing.
    evaluated_set = set(evaluated)
    thesis_tickers = {r.get("ticker") for r in thesis_reviews or []}
    for key in list(active):
        if key in fired_keys:
            continue
        alert = active[key]
        ticker = alert.get("ticker")
        rule = alert.get("alert_type")
        if rule in (RULE_THESIS_CONFIRMED, RULE_THESIS_INVALIDATED):
            if ticker in thesis_tickers:
                cleared_record = _clear_alert(alert, as_of)
                cleared.append(cleared_record)
                cleared_this_run.append(cleared_record)
                del active[key]
        elif ticker in evaluated_set:
            cleared_record = _clear_alert(alert, as_of)
            cleared.append(cleared_record)
            cleared_this_run.append(cleared_record)
            del active[key]

    return {
        "new_alerts": new_alerts,
        "already_active": already_active,
        "cleared_alerts": cleared_this_run,
        "not_evaluated": not_evaluated,
        "evaluated": evaluated,
        "skipped_thesis_reviews": skipped_thesis,
        "state": state,
    }


def _clear_alert(alert, as_of):
    """A copy of an active alert marked cleared."""
    cleared = dict(alert)
    cleared["status"] = "cleared"
    cleared["cleared_at"] = as_of
    return cleared


# ---------------------------------------------------------------------------
# Listing and acknowledging (records only)
# ---------------------------------------------------------------------------

def list_active_alerts(state, ticker=None, severity=None):
    """Active alerts, newest first (optionally filtered)."""
    alerts = list((state or {}).get("active_alerts", {}).values())
    if ticker is not None:
        alerts = [a for a in alerts if a.get("ticker") == ticker]
    if severity is not None:
        alerts = [a for a in alerts if a.get("severity") == severity]
    alerts.sort(key=lambda a: (a.get("raised_at") or "",
                               a.get("alert_key") or ""),
                reverse=True)
    return alerts


def list_cleared_alerts(state, ticker=None):
    """Cleared alerts, most recently cleared first."""
    alerts = list((state or {}).get("cleared_alerts", []))
    if ticker is not None:
        alerts = [a for a in alerts if a.get("ticker") == ticker]
    alerts.sort(key=lambda a: (a.get("cleared_at") or "",
                               a.get("alert_key") or ""),
                reverse=True)
    return alerts


def acknowledge_alert(state, alert_key, as_of=None):
    """Mark an active alert acknowledged (records only).

    Returns the alert record, or None when no active alert
    carries that key.  Acknowledging does not clear the alert.
    """
    active = (state or {}).get("active_alerts", {})
    alert = active.get(alert_key)
    if alert is None:
        return None
    alert["acknowledged"] = True
    alert["acknowledged_at"] = as_of or _utcnow_iso()
    return alert


def briefing_lines(alerts):
    """One briefing line per alert record."""
    return [a.get("briefing_line", "") for a in alerts or []]


# ---------------------------------------------------------------------------
# Full run: evaluate, persist
# ---------------------------------------------------------------------------

def run_alerts(watchlist, detection=None, summaries=None, prices=None,
               thesis_reviews=None, state_path=None, as_of=None):
    """Evaluate the watchlist, raise/clear alerts, persist state.

    Loads the alert state from ``state_path`` (default
    ``data/alerts/alert_state.json`` in the project), runs
    :func:`evaluate_alerts`, saves the state.  Returns the
    evaluation result plus ``"state_file"``.  No network, no
    database writes, no sending, no scheduling.
    """
    state = load_state(state_path)
    result = evaluate_alerts(
        watchlist, detection=detection, summaries=summaries,
        prices=prices, thesis_reviews=thesis_reviews, state=state,
        as_of=as_of)
    result["state_file"] = save_state(state, state_path)
    return result


def evaluate_from_db(conn, watchlist, state=None, prices=None,
                     as_of=None, sector_overrides=None):
    """Evaluate the watchlist against a database connection.

    Runs the Step 26 detector over the watchlist (snapshot
    prices from the database) and a Step 23 summary per
    ticker, then evaluates the alert rules on those results.
    The connection is read-only in effect: the scanner and
    the margin summary open nothing for writing, and this
    function makes no writes of its own.  ``prices`` are
    caller-supplied current prices that override the stored
    snapshot for the price-crossing rule.  Returns the same
    dict as :func:`evaluate_alerts`.
    """
    tickers = _normalise_watchlist(watchlist)
    detection = ud.detect_with_snapshot_prices(
        conn, tickers=tickers, sector_overrides=sector_overrides)
    summaries = {}
    for ticker in tickers:
        try:
            # Step 22/23 take a price-input dict, not a bare
            # number — normalise first (Step 25's helper), or a
            # caller-supplied price crashes the valuation with
            # "'float' object has no attribute 'get'".
            summaries[ticker] = mos.margin_of_safety_summary(
                conn, ticker,
                price_input=ms.normalise_price_input(
                    (prices or {}).get(ticker)),
                sector_override=(sector_overrides or {}).get(ticker))
        except Exception as exc:  # noqa: BLE001 - report, never crash
            summaries[ticker] = {
                "ticker": ticker, "tier": "insufficient_data",
                "error": str(exc)}
    return evaluate_alerts(
        watchlist, detection=detection, summaries=summaries,
        prices=prices, state=state, as_of=as_of)


def run_alerts_from_db(db_path=None, watchlist=None, state_path=None,
                       prices=None, as_of=None, sector_overrides=None):
    """Dry-run entry: evaluate the watchlist in a DB file.

    Opens the database read-only (``mode=ro``).  ``db_path``
    defaults to ``database/assistant.db`` next to this
    package's project root.  Returns the evaluation result
    plus ``"state_file"``.
    """
    if db_path is None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "database",
                               "assistant.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        state = load_state(state_path)
        result = evaluate_from_db(
            conn, watchlist, state=state, prices=prices, as_of=as_of,
            sector_overrides=sector_overrides)
        result["state_file"] = save_state(state, state_path)
        return result
    finally:
        conn.close()
