"""Step 41 — Integrate financial research agents.

A research-session layer on top of the Step 39 orchestrator.
The orchestrator routes one intent to one module; this module
turns a research question into a planned, multi-step
investigation and synthesises a brief from what the modules
actually returned:

1. Plan — given a question, produce an explicit step plan
   naming the modules to call in order and the question each
   step answers.
2. Execute — run the plan step by step against a caller-supplied
   database path, collecting each module's output with its
   source labelled.  The first step that returns
   ``insufficient_data`` stops the session: the remaining steps
   are recorded as not attempted with the reason, never invented.
   (A peer ticker with no statements does not stop the session —
   the Step 28b comparison reports it as not comparable.)
3. Synthesise — compose a structured research brief (dict +
   Markdown) from the collected outputs only: what the evidence
   says, what is missing, what would change the view, and the
   limitations.  Every claim carries the source module that
   produced it.

The session is caller-driven (a function call), not autonomous:
no background runs, no sending, no scheduling (Step 46). No
price fetching, no network (Step 31 news is stored files only),
no database writes, no trades. A trade request is refused as a
record before any step runs — the same guardrail as Step 39.

Scope is deliberately tight: plan-execute-synthesise only.
Web research is Step 42, memory Step 43, tool calling
Step 44, voice Step 45, scheduling Step 46, computer
interaction Step 47, main.py wiring Step 50. None of those
are built here.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SESSIONS_DIR = PROJECT_ROOT / "data" / "research_sessions"

# Sources — every claim in a brief names one of these, and the
# tests assert no claim names a module that did not run.
SOURCE_SCAN = "agents/market_scanner.py::scan_ticker (Step 25)"
SOURCE_VALUATION = (
    "agents/margin_of_safety.py::margin_of_safety_summary (Step 23)"
)
SOURCE_REPORT = "agents/research_report.py::generate_report (Step 29)"
SOURCE_COMPARISON = (
    "agents/company_comparison.py::compare_companies (Step 28b)"
)
SOURCE_NEWS = (
    "agents/news_collection.py::list_articles "
    "(Step 31, stored files only)"
)
SOURCE_EVENTS = (
    "agents/event_tracking.py::list_events "
    "(Step 33, stored files only)"
)
SOURCE_CHANGED = "agents/news_monitoring.py::check_ticker (Step 34)"
# Step 58: claims from a live-derived value name this module,
# not the stored-data scanner/valuation engine — the value came
# from a live pull or a stored live scan, and is provisional.
SOURCE_SCAN_LIVE = (
    "agents/live_fallback.py (Step 58, live-derived value — "
    "provisional)")
SOURCE_VALUATION_LIVE = (
    "agents/live_fallback.py (Step 58, live-derived value — "
    "provisional)")

QUESTION_KINDS = ("single", "comparison", "what_changed", "unknown")

_WHAT_CHANGED_KEYWORDS = (
    "what changed", "what's changed", "whats changed",
    "what is new", "what's new", "whats new", "any changes",
    "updates on", "update on", "what happened to",
    "anything new",
)

_COMPARISON_KEYWORDS = ("compare", " vs ", " versus ", "against ")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _today_iso():
    return datetime.now(timezone.utc).date().isoformat()


def _open_read_only(db_path):
    """Open ``db_path`` read-only, or return None."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _fmt_money(value):
    if value is None:
        return "unknown"
    return f"${value:,.2f}"


def _fmt_pct(value):
    if value is None:
        return "unknown"
    return f"{value:.1%}"


def _tier_text(value):
    """A Step 23 tier dict or a Step 25 tier string, as text."""
    if isinstance(value, dict):
        return str(value.get("tier") or "unknown")
    return str(value or "unknown")


def _jsonable(value):
    """Make ``value`` safe for json.dumps (no NaN/inf, no objects)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "research-session"


# ---------------------------------------------------------------------------
# Question classification and planning
# ---------------------------------------------------------------------------

def classify_question(question, tickers=None):
    """The kind of research question this is.

    ``"comparison"`` needs two or more tickers and a comparison
    word; ``"what_changed"`` needs a change word and one ticker;
    ``"single"`` is one ticker; anything else is ``"unknown"``.
    """
    text = (question or "").strip()
    lowered = f" {text.lower()} "
    tickers = list(tickers or [])
    if not text or not tickers:
        return "unknown"
    if len(tickers) >= 2 and any(
            k in lowered for k in _COMPARISON_KEYWORDS):
        return "comparison"
    if any(k in lowered for k in _WHAT_CHANGED_KEYWORDS):
        return "what_changed"
    return "single"


def _step(step_id, module, title, question, tickers):
    return {
        "id": step_id,
        "module": module,
        "title": title,
        "question": question,
        "tickers": list(tickers),
    }


def plan_research(question, tickers=None, peers=None, db_path=None):
    """An explicit step plan for ``question``.

    Names the modules to call, in order, and the question each
    step answers.  Planning never touches the database and never
    runs a module — it only describes the work.
    """
    from agent import orchestrator as orch

    text = (question or "").strip()
    conn = _open_read_only(db_path) if db_path else None
    try:
        found = orch.extract_tickers(text, conn=conn)
    finally:
        if conn is not None:
            conn.close()
    if tickers is not None:
        found = [t.upper() for t in tickers]
    peer_list = [p.upper() for p in (peers or [])]
    if not peer_list and len(found) > 1:
        peer_list = found[1:]
    kind = classify_question(text, found)
    target = found[0] if found else None

    steps = []
    if kind == "single" and target:
        steps = [
            _step("scan", SOURCE_SCAN, "Scan the ticker",
                  f"What does the Step 16-24 pipeline say about "
                  f"{target}?", [target]),
            _step("valuation", SOURCE_VALUATION,
                  "Valuation and margin of safety",
                  f"What is {target} worth, and at what price "
                  f"would it be a strongest alert?", [target]),
            _step("report", SOURCE_REPORT,
                  "Full research report",
                  f"What do the financials, health, quality and "
                  f"risks of {target} look like in full?", [target]),
            _step("news", SOURCE_NEWS, "Stored news",
                  f"What has been collected about {target}?",
                  [target]),
            _step("events", SOURCE_EVENTS, "Stored events",
                  f"What events are recorded for {target}?",
                  [target]),
        ]
    elif kind == "comparison" and target:
        all_tickers = [target] + [p for p in peer_list
                                  if p != target]
        steps = [
            _step("scan", SOURCE_SCAN, "Scan the tickers",
                  "What does the Step 16-24 pipeline say about "
                  "each ticker?", all_tickers),
            _step("valuation", SOURCE_VALUATION,
                  "Valuation and margin of safety",
                  "What is each ticker worth, and at what price "
                  "would it be a strongest alert?", all_tickers),
            _step("comparison", SOURCE_COMPARISON,
                  "Peer comparison",
                  f"How does {target} compare with "
                  f"{', '.join(peer_list)}?", all_tickers),
            _step("news", SOURCE_NEWS, "Stored news",
                  "What has been collected about each ticker?",
                  all_tickers),
            _step("events", SOURCE_EVENTS, "Stored events",
                  "What events are recorded for each ticker?",
                  all_tickers),
        ]
    elif kind == "what_changed" and target:
        steps = [
            _step("scan", SOURCE_SCAN, "Scan the ticker",
                  f"What does the Step 16-24 pipeline say about "
                  f"{target}?", [target]),
            _step("what_changed", SOURCE_CHANGED,
                  "What changed since the last check",
                  f"What is new for {target} since the last "
                  f"check?", [target]),
            _step("news", SOURCE_NEWS, "Stored news",
                  f"What has been collected about {target}?",
                  [target]),
            _step("events", SOURCE_EVENTS, "Stored events",
                  f"What events are recorded for {target}?",
                  [target]),
        ]
    return {
        "question": text,
        "kind": kind,
        "tickers": found,
        "peers": peer_list,
        "target": target,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Step runners — each returns a step record, never raises
# ---------------------------------------------------------------------------

def _step_record(step, status, output=None, reason=None):
    return {
        "id": step["id"],
        "module": step["module"],
        "title": step["title"],
        "question": step["question"],
        "tickers": step["tickers"],
        "status": status,
        "output": _jsonable(output) if output is not None else None,
        "reason": reason,
        "source": step["module"],
    }


def _run_scan(conn, tickers, prices, live_fallback=False,
              live_fetcher=None, live_scan_path=None,
              force_live=False, live_cache=None, store_dir=None):
    from agents import market_scanner as ms
    from agents import live_fallback as lfb

    results = {}
    for ticker in tickers:
        try:
            results[ticker] = ms.scan_ticker(
                conn, ticker,
                price_input=(prices or {}).get(ticker))
        except Exception as exc:  # noqa: BLE001 - one ticker must not stop the session
            results[ticker] = {
                "ticker": ticker, "tier": "insufficient_data",
                "missing_inputs": [f"scan failed: {exc}"],
            }
        if (_tier_text(results[ticker].get("tier"))
                == "insufficient_data" and live_fallback):
            # Step 58: the stored pipeline has nothing for this
            # ticker — fall back to the Step 54 live machinery
            # for this one ticker (a watchlist ticker, or one
            # the owner named in the question).
            fallback = lfb.live_valuation(
                ticker, store_dir=store_dir, fetcher=live_fetcher,
                live_scan_path=live_scan_path, force_live=force_live,
                explicitly_named=True, cache=live_cache)
            if fallback["status"] == "ok":
                results[ticker] = lfb.candidate_from_live_record(
                    fallback["record"], fallback["source"])
    return results


def _run_valuation(conn, tickers, prices, live_fallback=False,
                   live_fetcher=None, live_scan_path=None,
                   force_live=False, live_cache=None,
                   store_dir=None):
    from agents import margin_of_safety as mos
    from agents import live_fallback as lfb

    results = {}
    for ticker in tickers:
        try:
            results[ticker] = mos.margin_of_safety_summary(
                conn, ticker,
                price_input=(prices or {}).get(ticker))
        except Exception as exc:  # noqa: BLE001 - one ticker must not stop the session
            results[ticker] = {
                "ticker": ticker,
                "tier": {"tier": "insufficient_data"},
                "warnings": [f"valuation failed: {exc}"],
            }
        if (_tier_text(results[ticker].get("tier"))
                == "insufficient_data" and live_fallback):
            # Step 58: same fallback as the scan step, sharing
            # the session's live cache so one pull serves both.
            fallback = lfb.live_valuation(
                ticker, store_dir=store_dir, fetcher=live_fetcher,
                live_scan_path=live_scan_path, force_live=force_live,
                explicitly_named=True, cache=live_cache)
            if fallback["status"] == "ok":
                results[ticker] = (
                    lfb.valuation_summary_from_live_record(
                        fallback["record"], fallback["source"]))
    return results


def _run_report(conn, ticker, prices):
    from agents import research_report as rr

    try:
        return rr.generate_report(
            conn, ticker,
            price_input=(prices or {}).get(ticker))
    except Exception as exc:  # noqa: BLE001 - a failed report is a failed step, not a crash
        return {
            "ticker": ticker, "status": "insufficient_data",
            "status_reason": f"report failed: {exc}",
            "sections": {},
        }


def _run_comparison(conn, target, peers, prices):
    from agents import company_comparison as cc

    try:
        return cc.compare_companies(
            conn, target, peers,
            prices={t: (prices or {}).get(t)
                    for t in [target] + list(peers or [])
                    if (prices or {}).get(t) is not None})
    except Exception as exc:  # noqa: BLE001 - a failed comparison is a failed step, not a crash
        return {
            "target": target, "peers_requested": list(peers or []),
            "status": "insufficient_data",
            "ranking_meaningful": False,
            "ranking_note": f"comparison failed: {exc}",
            "not_comparable_peers": list(peers or []),
        }


def _run_news(tickers, news_dir):
    from agents import news_collection as nc

    results = {}
    for ticker in tickers:
        articles = nc.list_articles(ticker=ticker, news_dir=news_dir)
        results[ticker] = {
            "count": len(articles),
            "articles": [
                {
                    "title": a.get("title"),
                    "source": a.get("source"),
                    "published_date": a.get("published_date"),
                    "url": a.get("url"),
                }
                for a in articles
            ],
        }
    return results


def _run_events(tickers, events_dir):
    from agents import event_tracking as et

    results = {}
    for ticker in tickers:
        events = et.list_events(events_dir=events_dir, ticker=ticker)
        results[ticker] = {
            "count": len(events),
            "events": [
                {
                    "event_type": e.get("event_type"),
                    "date": e.get("date"),
                    "title": e.get("title"),
                    "ticker": e.get("ticker"),
                }
                for e in events
            ],
        }
    market = et.list_events(events_dir=events_dir, ticker="MARKET")
    results["MARKET"] = {
        "count": len(market),
        "events": [
            {
                "event_type": e.get("event_type"),
                "date": e.get("date"),
                "title": e.get("title"),
                "ticker": e.get("ticker"),
            }
            for e in market
        ],
    }
    return results


def _run_what_changed(conn, ticker, prices, state, as_of):
    from agents import news_monitoring as nm

    ticker_state = None
    if isinstance(state, dict):
        ticker_state = (state.get("tickers") or {}).get(ticker)
    try:
        return nm.check_ticker(
            conn, ticker, ticker_state=ticker_state,
            as_of=as_of, price_input=(prices or {}).get(ticker))
    except Exception as exc:  # noqa: BLE001 - a failed check is a failed step, not a crash
        return {
            "ticker": ticker, "snapshot_status": "insufficient_data",
            "snapshot_error": f"what-changed check failed: {exc}",
            "new_article_count": 0, "upcoming_event_count": 0,
            "changes": [], "change_count": 0,
            "price_vs_alert": {},
        }


# ---------------------------------------------------------------------------
# Synthesis — claims only from steps that ran
# ---------------------------------------------------------------------------

def _claims_from_scan(scan_results, source):
    claims = []
    for ticker, candidate in (scan_results or {}).items():
        if _tier_text(candidate.get("tier")) == "insufficient_data":
            continue
        # Step 58: a live-derived candidate is sourced to the
        # fallback, not to the stored-data scanner.
        claim_source = (SOURCE_SCAN_LIVE
                        if candidate.get("live_fallback") else source)
        rng = candidate.get("fair_value_range") or {}
        claims.append({
            "text": (
                f"{ticker}: tier "
                f"{_tier_text(candidate.get('tier'))}, quality "
                f"{candidate.get('quality_rating', 'unknown')}, "
                f"health {candidate.get('health_verdict', 'unknown')}."
            ),
            "source": claim_source,
        })
        claims.append({
            "text": (
                f"{ticker}: fair-value range bear "
                f"{_fmt_money(rng.get('conservative'))}, base "
                f"{_fmt_money(rng.get('base'))}, optimistic "
                f"{_fmt_money(rng.get('optimistic'))}; price "
                f"{_fmt_money(candidate.get('price'))} (as of "
                f"{candidate.get('price_as_of') or 'date unknown'})."
            ),
            "source": claim_source,
        })
        if candidate.get("strongest_alert_threshold_price") is not None:
            claims.append({
                "text": (
                    f"{ticker}: strongest-alert price "
                    f"{_fmt_money(candidate.get('strongest_alert_threshold_price'))} "
                    f"(20% below the conservative value)."
                ),
                "source": claim_source,
            })
    return claims


def _claims_from_valuation(valuation_results, source):
    claims = []
    for ticker, summary in (valuation_results or {}).items():
        if _tier_text(summary.get("tier")) == "insufficient_data":
            continue
        # Step 58: a live-derived summary is sourced to the
        # fallback, not to the stored-data valuation engine.
        claim_source = (SOURCE_VALUATION_LIVE
                        if summary.get("live_fallback") else source)
        margin = summary.get("margin_of_safety_vs_conservative")
        deterioration = summary.get("deterioration") or {}
        # A live-derived summary carries deteriorated=None
        # (unknown from a live pull); render that as "unknown".
        deteriorated = deterioration.get("deteriorated")
        if deteriorated is None:
            deteriorated = "unknown"
        claims.append({
            "text": (
                f"{ticker}: margin of safety vs the conservative "
                f"value {_fmt_pct(margin)}; material deterioration: "
                f"{deteriorated}."
            ),
            "source": claim_source,
        })
    return claims


def _claims_from_report(report, source):
    claims = []
    if not report:
        return claims
    ticker = report.get("ticker")
    sections = report.get("sections") or {}
    claims.append({
        "text": (
            f"{ticker}: full research report status "
            f"{report.get('status', 'unknown')}"
            + (f" — {report.get('status_reason')}"
               if report.get("status_reason") else "")
            + "."
        ),
        "source": source,
    })
    for name, section in sections.items():
        claims.append({
            "text": (
                f"{ticker}: report section "
                f"'{section.get('title', name)}' is "
                f"{section.get('status', 'unknown')}"
                + (f" — {section.get('reason')}"
                   if section.get("reason") else "")
                + "."
            ),
            "source": source,
        })
    return claims


def _claims_from_comparison(comparison, source):
    claims = []
    if not comparison:
        return claims
    target = comparison.get("target")
    claims.append({
        "text": (
            f"{target}: peer comparison status "
            f"{comparison.get('status', 'unknown')}; ranking "
            f"{'meaningful' if comparison.get('ranking_meaningful') else 'not meaningful'}"
            + (f" — {comparison.get('ranking_note')}"
               if comparison.get("ranking_note") else "")
            + "."
        ),
        "source": source,
    })
    for peer in comparison.get("not_comparable_peers") or []:
        claims.append({
            "text": (
                f"{peer}: not comparable — no statements or price "
                f"available, so no ranking is claimed for it."
            ),
            "source": source,
        })
    for item in comparison.get("stands_out") or []:
        claims.append({
            "text": (
                f"{item.get('ticker')}: stands out on "
                f"{item.get('label', item.get('metric'))} "
                f"(rank {item.get('target_rank')} of "
                f"{item.get('ranked_count')})."
            ),
            "source": source,
        })
    for item in comparison.get("lags") or []:
        claims.append({
            "text": (
                f"{item.get('ticker')}: lags on "
                f"{item.get('label', item.get('metric'))} "
                f"(rank {item.get('target_rank')} of "
                f"{item.get('ranked_count')})."
            ),
            "source": source,
        })
    return claims


def _claims_from_news(news_results, source):
    claims = []
    for ticker, result in (news_results or {}).items():
        count = result.get("count", 0)
        if count == 0:
            claims.append({
                "text": (
                    f"{ticker}: no stored news articles — nothing "
                    f"has been collected for it."
                ),
                "source": source,
            })
            continue
        claims.append({
            "text": (
                f"{ticker}: {count} stored news article(s); newest "
                f"first, titles quoted from the articles "
                f"themselves."
            ),
            "source": source,
        })
        for article in result.get("articles") or []:
            claims.append({
                "text": (
                    f"{ticker} article: "
                    f"\"{article.get('title') or '(no title)'}\" — "
                    f"{article.get('source') or 'unknown source'}, "
                    f"{article.get('published_date') or 'date unknown'}."
                ),
                "source": source,
            })
    return claims


def _claims_from_events(event_results, source):
    claims = []
    for ticker, result in (event_results or {}).items():
        if ticker == "MARKET":
            continue
        count = result.get("count", 0)
        if count == 0:
            claims.append({
                "text": f"{ticker}: no stored events.",
                "source": source,
            })
            continue
        claims.append({
            "text": f"{ticker}: {count} stored event(s).",
            "source": source,
        })
        for event in result.get("events") or []:
            claims.append({
                "text": (
                    f"{ticker} event: {event.get('title') or event.get('event_type')} "
                    f"on {event.get('date') or 'date unknown'}."
                ),
                "source": source,
            })
    market = (event_results or {}).get("MARKET") or {}
    if market.get("count"):
        claims.append({
            "text": (
                f"Market-wide: {market['count']} stored event(s)."
            ),
            "source": source,
        })
        for event in market.get("events") or []:
            claims.append({
                "text": (
                    f"Market-wide event: "
                    f"{event.get('title') or event.get('event_type')} "
                    f"on {event.get('date') or 'date unknown'}."
                ),
                "source": source,
            })
    return claims


def _claims_from_what_changed(changed, source):
    claims = []
    if not changed:
        return claims
    ticker = changed.get("ticker")
    claims.append({
        "text": (
            f"{ticker}: {changed.get('new_article_count', 0)} new "
            f"article(s), {changed.get('upcoming_event_count', 0)} "
            f"upcoming event(s), {changed.get('change_count', 0)} "
            f"change(s) since the last check."
        ),
        "source": source,
    })
    for change in changed.get("changes") or []:
        claims.append({
            "text": (
                f"{ticker}: changed — "
                f"{change.get('field')}: "
                f"{change.get('previous')} -> {change.get('current')}."
            ),
            "source": source,
        })
    position = changed.get("price_vs_alert") or {}
    if position.get("below_alert") is True:
        claims.append({
            "text": (
                f"{ticker}: price is at or below the "
                f"strongest-alert price."
            ),
            "source": source,
        })
    return claims


def _synthesise(plan, step_records):
    """Claims, what-would-change-the-view and limitations.

    Only steps with status ``"ok"`` contribute claims, and
    every claim names the module that produced it.
    """
    by_id = {s["id"]: s for s in step_records}
    claims = []
    limitations = []
    what_would_change = []

    scan = by_id.get("scan")
    if scan and scan["status"] == "ok":
        claims += _claims_from_scan(scan["output"], SOURCE_SCAN)
        for ticker, candidate in (scan["output"] or {}).items():
            for missing in candidate.get("missing_inputs") or []:
                limitations.append(
                    f"{ticker}: {missing} (Step 25 scan).")
            for condition in candidate.get("conditions_to_change") or []:
                what_would_change.append({
                    "text": f"{ticker}: {condition}.",
                    "source": SOURCE_SCAN,
                })
    valuation = by_id.get("valuation")
    if valuation and valuation["status"] == "ok":
        claims += _claims_from_valuation(
            valuation["output"], SOURCE_VALUATION)
        for ticker, summary in (valuation["output"] or {}).items():
            tier = summary.get("tier") or {}
            for condition in tier.get("conditions_to_change") or []:
                entry = {"text": f"{ticker}: {condition}.",
                         "source": SOURCE_VALUATION}
                if entry not in what_would_change:
                    what_would_change.append(entry)
    report = by_id.get("report")
    if report and report["status"] == "ok":
        claims += _claims_from_report(report["output"], SOURCE_REPORT)
        rep = report["output"] or {}
        if rep.get("status") in ("partial", "insufficient_data"):
            limitations.append(
                f"Step 29 research report is {rep['status']}: "
                f"{rep.get('status_reason') or 'some sections could '
                  'not be produced'}.")
        for name, section in (rep.get("sections") or {}).items():
            if section.get("status") in ("partial",
                                         "insufficient_data"):
                limitations.append(
                    f"Report section "
                    f"'{section.get('title', name)}' is "
                    f"{section['status']}"
                    + (f" — {section['reason']}"
                       if section.get("reason") else "")
                    + ".")
    comparison = by_id.get("comparison")
    if comparison and comparison["status"] == "ok":
        claims += _claims_from_comparison(
            comparison["output"], SOURCE_COMPARISON)
        comp = comparison["output"] or {}
        if not comp.get("ranking_meaningful"):
            limitations.append(
                "Peer comparison ranking is not meaningful: "
                + (comp.get("ranking_note") or
                   "too few tickers have statements or prices."))
        if comp.get("not_comparable_peers"):
            limitations.append(
                "Peers not comparable: "
                + ", ".join(comp["not_comparable_peers"])
                + " — no statements or price available.")
    news = by_id.get("news")
    if news and news["status"] == "ok":
        claims += _claims_from_news(news["output"], SOURCE_NEWS)
    events = by_id.get("events")
    if events and events["status"] == "ok":
        claims += _claims_from_events(events["output"], SOURCE_EVENTS)
    changed = by_id.get("what_changed")
    if changed and changed["status"] == "ok":
        claims += _claims_from_what_changed(
            changed["output"], SOURCE_CHANGED)

    for step in step_records:
        if step["status"] == "not_attempted":
            limitations.append(
                f"Step '{step['id']}' ({step['title']}) was not "
                f"attempted: {step['reason']}.")
        elif step["status"] == "insufficient_data":
            limitations.append(
                f"Step '{step['id']}' ({step['title']}) could not "
                f"run: {step['reason']}.")
    if not what_would_change:
        if any(s["status"] == "insufficient_data"
               for s in step_records):
            what_would_change.append({
                "text": (
                    "Statements or a price becoming available for "
                    "the ticker would let the valuation run."
                ),
                "source": SOURCE_SCAN,
            })
    return claims, what_would_change, limitations


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def run_research_session(question, db_path=None, peers=None,
                         prices=None, news_dir=None, events_dir=None,
                         state=None, as_of=None, save_dir=None,
                         store_dir=None, live_fallback=False,
                         live_fetcher=None, live_scan_path=None,
                         force_live=False):
    """Run one research session for ``question``.

    Returns a brief dict: ``question``, ``kind``, ``tickers``,
    ``plan``, ``steps`` (one record per planned step, with
    ``status`` and ``output``), ``claims`` (each with a source
    module that ran), ``what_would_change_view``,
    ``limitations``, ``status`` and ``status_reason``.  The
    session opens the database read-only and writes nothing to
    it; when ``save_dir`` is given the brief is also written as
    JSON + Markdown there (default
    ``data/research_sessions/``).  A trade request is refused
    before any step runs.

    ``live_fallback`` (Step 58, off by default) lets the scan
    and valuation steps fall back to the Step 54 live machinery
    for a ticker the stored pipeline cannot judge;
    ``live_fetcher`` injects the live pull (None means the real
    Yahoo fetch), ``live_scan_path`` points at a stored Step 54
    scan file, and ``force_live`` skips the stored scan.  Off by
    default so a caller that does not opt in never touches the
    network.  The full research report (Step 29) has no live
    fallback: for a ticker with no stored statements the
    session still stops at the report step, with the live
    scan/valuation claims kept and the limitation recorded.
    """
    from agent import orchestrator as orch

    text = (question or "").strip()
    created_at = _utcnow_iso()
    as_of = as_of or _today_iso()
    prices = prices or {}

    # Guardrail first, as in Step 39: a trade request is refused
    # as a record, never executed, and no module is called.
    classification = orch.classify_intent(text)
    if classification["intent"] == "trade":
        brief = {
            "question": text,
            "kind": "unknown",
            "tickers": [],
            "peers": [],
            "target": None,
            "plan": {"question": text, "kind": "unknown",
                     "tickers": [], "peers": [], "target": None,
                     "steps": []},
            "steps": [],
            "claims": [],
            "what_would_change_view": [],
            "limitations": [
                "Trade requests are refused: no order is placed "
                "without the owner's explicit approval for that "
                "exact order.",
            ],
            "status": "refused",
            "status_reason": (
                "The question asks for a trade. Show the exact "
                "order and get approval first; this session "
                "places none."),
            "created_at": created_at,
            "as_of": as_of,
        }
        if save_dir is not False:
            brief["files"] = write_session(brief, save_dir)
        return brief

    plan = plan_research(text, peers=peers, db_path=db_path)
    kind = plan["kind"]
    target = plan["target"]
    tickers = plan["tickers"]

    steps = []
    status = "complete"
    status_reason = "All planned steps ran."

    if kind == "unknown":
        status = "unknown"
        status_reason = (
            "No ticker could be identified in the question, or "
            "the question is not a research question — nothing "
            "was run and nothing is estimated.")
    else:
        conn = _open_read_only(db_path)
        if conn is None:
            # No database: every data step is insufficient, and
            # the strict early stop leaves the rest unattempted.
            reason = ("no database path was supplied or the file "
                      "does not exist")
            for i, step in enumerate(plan["steps"]):
                if i == 0:
                    steps.append(_step_record(
                        step, "insufficient_data", reason=reason))
                else:
                    steps.append(_step_record(
                        step, "not_attempted",
                        reason=f"not attempted: step "
                               f"'{plan['steps'][0]['id']}' returned "
                               f"insufficient_data — {reason}"))
            status = "insufficient_data"
            status_reason = reason
        else:
            try:
                steps, stopped = _execute_plan(
                    conn, plan, prices, news_dir, events_dir,
                    state, as_of, live_fallback=live_fallback,
                    live_fetcher=live_fetcher,
                    live_scan_path=live_scan_path,
                    force_live=force_live, store_dir=store_dir)
                if stopped:
                    status = "insufficient_data"
                    status_reason = (
                        "A step returned insufficient_data, so the "
                        "remaining steps were not attempted.")
            finally:
                conn.close()

    claims, what_would_change, limitations = _synthesise(plan, steps)
    if status == "complete" and limitations:
        status = "partial"
        status_reason = (
            "All planned steps ran, but some evidence is missing: "
            + "; ".join(limitations[:3]))
    if status == "complete" and not claims:
        status = "partial"
        status_reason = "All planned steps ran but produced no claims."

    brief = {
        "question": text,
        "kind": kind,
        "tickers": tickers,
        "peers": plan["peers"],
        "target": target,
        "plan": plan,
        "steps": steps,
        "claims": claims,
        "what_would_change_view": what_would_change,
        "limitations": limitations,
        "status": status,
        "status_reason": status_reason,
        "created_at": created_at,
        "as_of": as_of,
    }
    if save_dir is not False:
        brief["files"] = write_session(brief, save_dir)
    return brief


def _execute_plan(conn, plan, prices, news_dir, events_dir,
                  state, as_of, live_fallback=False,
                  live_fetcher=None, live_scan_path=None,
                  force_live=False, store_dir=None):
    """Run the planned steps in order. Returns (steps, stopped).

    ``stopped`` is True when a step returned insufficient_data
    and the remaining steps were recorded as not attempted.
    ``live_cache`` memoises live-fallback results by ticker so
    the scan and valuation steps share one pull.
    """
    steps = []
    stopped = False
    target = plan["target"]
    tickers = plan["tickers"]
    peers = plan["peers"]
    live_cache = {}

    for step in plan["steps"]:
        if stopped:
            steps.append(_step_record(
                step, "not_attempted",
                reason=f"not attempted: an earlier step returned "
                       f"insufficient_data"))
            continue
        step_id = step["id"]
        if step_id == "scan":
            results = _run_scan(
                conn, tickers, prices, live_fallback=live_fallback,
                live_fetcher=live_fetcher,
                live_scan_path=live_scan_path, force_live=force_live,
                live_cache=live_cache, store_dir=store_dir)
            target_result = results.get(target) or {}
            if _tier_text(target_result.get("tier")) == "insufficient_data":
                missing = target_result.get("missing_inputs") or []
                reason = ("; ".join(str(m) for m in missing)
                          or "no statements or price available")
                steps.append(_step_record(
                    step, "insufficient_data", output=results,
                    reason=f"{target}: {reason}"))
                stopped = True
            else:
                steps.append(_step_record(
                    step, "ok", output=results))
        elif step_id == "valuation":
            results = _run_valuation(
                conn, tickers, prices, live_fallback=live_fallback,
                live_fetcher=live_fetcher,
                live_scan_path=live_scan_path, force_live=force_live,
                live_cache=live_cache, store_dir=store_dir)
            target_result = results.get(target) or {}
            if _tier_text(target_result.get("tier")) == "insufficient_data":
                warnings = target_result.get("warnings") or []
                reason = ("; ".join(str(w) for w in warnings)
                          or "no valuation range could be computed")
                steps.append(_step_record(
                    step, "insufficient_data", output=results,
                    reason=f"{target}: {reason}"))
                stopped = True
            else:
                steps.append(_step_record(
                    step, "ok", output=results))
        elif step_id == "report":
            report = _run_report(conn, target, prices)
            if report.get("status") == "insufficient_data":
                steps.append(_step_record(
                    step, "insufficient_data", output=report,
                    reason=f"{target}: "
                           f"{report.get('status_reason') or 'no data'}"))
                stopped = True
            else:
                steps.append(_step_record(
                    step, "ok", output=report))
        elif step_id == "comparison":
            comparison = _run_comparison(conn, target, peers, prices)
            steps.append(_step_record(step, "ok", output=comparison))
        elif step_id == "news":
            results = _run_news(tickers, news_dir)
            steps.append(_step_record(step, "ok", output=results))
        elif step_id == "events":
            results = _run_events(tickers, events_dir)
            steps.append(_step_record(step, "ok", output=results))
        elif step_id == "what_changed":
            changed = _run_what_changed(
                conn, target, prices, state, as_of)
            if changed.get("snapshot_status") == "insufficient_data":
                steps.append(_step_record(
                    step, "insufficient_data", output=changed,
                    reason=f"{target}: "
                           f"{changed.get('snapshot_error') or 'no data'}"))
                stopped = True
            else:
                steps.append(_step_record(
                    step, "ok", output=changed))
        else:  # pragma: no cover - plans only contain known steps
            steps.append(_step_record(
                step, "not_attempted",
                reason=f"unknown step id '{step_id}'"))
    return steps, stopped


# ---------------------------------------------------------------------------
# Markdown rendering and saving
# ---------------------------------------------------------------------------

def brief_to_markdown(brief):
    """The brief as Markdown. The brief's own words contain no
    recommendation: quoted news-article titles are the articles'
    words, not the brief's, and are labelled as such."""
    lines = [
        f"# Research brief: {brief.get('question') or '(no question)'}",
        "",
        f"Kind: {brief.get('kind')}. "
        f"Status: {brief.get('status')} — "
        f"{brief.get('status_reason') or ''}",
        "",
        "Tickers: "
        + (", ".join(brief.get("tickers") or []) or "none identified")
        + ".",
        "",
        "## Plan",
        "",
    ]
    for step in (brief.get("plan") or {}).get("steps") or []:
        lines.append(
            f"- {step['id']}: {step['title']} — {step['question']} "
            f"({step['module']})")
    if not (brief.get("plan") or {}).get("steps"):
        lines.append("- No steps planned.")
    lines += ["", "## What the evidence says", ""]
    if brief.get("claims"):
        for claim in brief["claims"]:
            lines.append(f"- {claim['text']} (Source: {claim['source']})")
    else:
        lines.append("- No claims — no step produced evidence.")
    lines += ["", "## What would change the view", ""]
    if brief.get("what_would_change_view"):
        for item in brief["what_would_change_view"]:
            lines.append(
                f"- {item['text']} (Source: {item['source']})")
    else:
        lines.append("- Nothing recorded.")
    lines += ["", "## Limitations", ""]
    if brief.get("limitations"):
        for item in brief["limitations"]:
            lines.append(f"- {item}")
    else:
        lines.append("- None recorded.")
    lines += [
        "",
        "## Steps",
        "",
    ]
    for step in brief.get("steps") or []:
        lines.append(
            f"- {step['id']} ({step['title']}): {step['status']}"
            + (f" — {step['reason']}" if step.get("reason") else ""))
    if not brief.get("steps"):
        lines.append("- No steps ran.")
    lines += [
        "",
        "This brief is research, not a recommendation. Candidates "
        "are worth a closer look; the final decision is always "
        "the owner's.",
        "",
    ]
    return "\n".join(lines)


def write_session(brief, save_dir=None):
    """Write the brief as JSON + Markdown. Returns the paths."""
    directory = Path(save_dir) if save_dir else DEFAULT_SESSIONS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    date = (brief.get("as_of") or _today_iso())
    name = f"{date}-{_slug(brief.get('question'))}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    data = {k: v for k, v in brief.items() if k != "files"}
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(_jsonable(data), handle, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(brief_to_markdown(brief))
    return {"json": str(json_path), "markdown": str(md_path)}
