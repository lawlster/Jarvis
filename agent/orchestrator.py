"""Step 39 — Unified AI orchestration.

A single request-handling pipeline for Jarvis, built on top of
what already exists rather than replacing it:

- ``agent/memory.py`` still owns persistent conversation memory
  (last 20 messages in the production database).
- ``main.py`` now delegates to this pipeline (Step 50): the
  interactive loop hands every request to :func:`handle`, with
  the Step 40 router as the model client and the Step 44 tool
  loop available to it (``tool_caller``).
- ``agent/brain.py`` and ``agent/planner.py`` are empty stubs
  (0 lines each, verified 2026-11-09); nothing is reused from
  them because there is nothing in them.

The pipeline has explicit stages:

1. Receive a user request (text).
2. Classify intent into a small, explicit set — keyword rules
   first; a model call only as a fallback, clearly labelled.
3. Enforce guardrails BEFORE acting: no trade without explicit
   per-order approval (a trade request is refused as a record,
   never executed), no sending or scheduling (Step 46), no
   production database writes from orchestration, no invented
   data.
4. Route to the right capability — financial intents call the
   existing Step 16-38 modules by their public functions
   (read-only where the module is read-only); chat goes to the
   model.
5. Compose the response — the model's words plus the structured
   results from the modules, with sources labelled (module name
   + what it computed) so a financial answer can be traced to
   its engine.
6. Record the turn (request, intent, route taken, modules
   called, outcome) in a JSON-lines log under
   ``data/orchestration/`` (caller-chosen directory), never the
   production database.

The orchestrator works with any model client that exposes a
``chat(messages)`` method returning a string — the real Ollama
client (``OllamaModelClient``) or a stub (``StubModelClient``,
used by the tests). When the model is unreachable it returns a
structured "model unavailable" result: it never crashes and
never invents an answer.

Scope is deliberately tight: routing + guardrails + turn log.
Cloud models are Step 40, financial-agent integration is
Step 41, web research Step 42, memory Step 43, tool calling
Step 44, voice Step 45, scheduling Step 46, computer
interaction Step 47. None of those are built here.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "data" / "orchestration"
DEFAULT_LOG_NAME = "turns.jsonl"

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:8b"

INTENTS = (
    "chat",
    "financial_research",
    "valuation",
    "watchlist",
    "news",
    "portfolio",
    "trade",
    "schedule_send",
    "unknown",
)

# Guardrail intents are enforced before any routing or model call.
GUARDRAIL_INTENTS = ("trade", "schedule_send")

SYSTEM_PROMPT = (
    "You are AiAssistant, a personal AI agent. You help with "
    "investment research, business analysis, personal tasks and "
    "general questions. Never invent financial figures or company "
    "information. Distinguish facts from assumptions. Never make "
    "an investment decision for the user, and never present a "
    "valuation estimate as an exact stock price. Candidates are "
    "worth a closer look, never buy recommendations; the final "
    "decision is always the user's."
)


class ModelUnavailable(Exception):
    """The model client could not be reached."""


class OllamaModelClient:
    """Thin client for the local Ollama chat endpoint.

    Mirrors the call ``main.py`` makes, without tool calling:
    one plain chat turn. Any transport failure raises
    :class:`ModelUnavailable` so the orchestrator can report it
    instead of inventing an answer.
    """

    def __init__(self, model=MODEL, url=OLLAMA_URL, timeout=180):
        self.model = model
        self.url = url
        self.timeout = timeout

    def chat(self, messages):
        """Return the model's reply text for ``messages``."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        try:
            response = requests.post(
                self.url, json=payload, timeout=self.timeout
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise ModelUnavailable(
                f"Ollama at {self.url} is unreachable: {exc}"
            ) from exc
        try:
            content = response.json()["message"].get("content", "")
        except (ValueError, KeyError, AttributeError) as exc:
            raise ModelUnavailable(
                "Ollama returned a response that could not be read"
            ) from exc
        if not content:
            raise ModelUnavailable("Ollama returned an empty reply")
        return content


class StubModelClient:
    """A model client for tests and offline use.

    Returns a canned reply (or the reply for a matching
    substring), and can be told to raise :class:`ModelUnavailable`
    so the graceful-failure path can be exercised without a
    network.
    """

    def __init__(self, reply="Stub reply.", replies=None,
                 raise_unavailable=False):
        self.reply = reply
        self.replies = replies or {}
        self.raise_unavailable = raise_unavailable
        self.calls = []

    def chat(self, messages):
        self.calls.append(list(messages))
        if self.raise_unavailable:
            raise ModelUnavailable("stub model unavailable")
        text = " ".join(
            str(m.get("content", "")) for m in messages
        )
        for needle, reply in self.replies.items():
            if needle.lower() in text.lower():
                return reply
        return self.reply


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

# Keyword rules, checked in order. The first intent whose
# keywords match wins; trade and schedule/send are checked
# first because they are guardrails, not routes.
_KEYWORD_RULES = (
    ("trade", (
        "buy ", "sell ", "buy shares", "sell shares", "place an order",
        "place a trade", "buy 100", "buy 10", "sell 100", "sell 10",
        "buy some", "sell some", "short ", "purchase shares",
        "order 100", "order 10",
    )),
    ("schedule_send", (
        "remind me", "schedule ", "set a reminder", "send a message",
        "send an email", "send a telegram", "send it to",
        "message vernon", "email vernon",
    )),
    ("valuation", (
        "valuation", "intrinsic value", "fair value", "worth",
        "dcf", "discounted cash flow", "margin of safety",
        "cheap", "overvalued", "undervalued", "good price",
        "entry price", "what should i pay", "is it a buy",
        "strongest alert", "alert price",
    )),
    ("watchlist", (
        "watchlist", "watch list", "what am i watching",
        "my watchlist", "add to watchlist", "remove from watchlist",
    )),
    ("news", (
        "news", "headlines", "latest on", "what happened",
        "any news",
    )),
    ("portfolio", (
        "portfolio", "my holdings", "my positions", "my shares",
        "performance", "quarterly report", "how is my portfolio",
        "how did my portfolio",
    )),
    ("financial_research", (
        "financials", "revenue", "earnings", "balance sheet",
        "cash flow", "free cash flow", "ratios", "health",
        "growth", "quality", "moat", "debt", "dividend",
        "return on", "margin", "profit",
    )),
    ("chat", (
        "hello", "hi ", "hey ", "good morning", "good evening",
        "thank", "thanks", "how are you", "what can you do",
        "who are you",
    )),
)

# Tokens that look like tickers but are ordinary words, so they
# are never treated as a ticker when extracting one from text.
_TICKER_STOPWORDS = {
    "I", "A", "AN", "THE", "AND", "OR", "IS", "IT", "IN", "ON", "TO",
    "OF", "FOR", "MY", "ME", "YOU", "WE", "US", "AT", "BY", "SO",
    "DO", "NO", "YES", "HI", "HEY", "Q", "Q1", "Q2", "Q3", "Q4",
    "BUY", "SELL", "NEWS", "VALUE", "WORTH", "CHEAP", "GOOD",
    "PRICE", "WHAT", "HOW", "WHY", "WHEN", "WITH", "FROM",
    "ARE",  # ARE is a real ticker; "what are…" must not match it
    "ABOUT", "THERE", "THIS", "THAT", "WILL", "CAN", "SHOULD",
    "LATEST", "ANY", "SOME", "ALL", "ONE", "TWO", "TEN",
}


def _keyword_intent(text):
    lowered = f" {text.lower()} "
    for intent, keywords in _KEYWORD_RULES:
        matched = [k.strip() for k in keywords if k in lowered]
        if matched:
            return intent, matched
    return None, []


def classify_intent(text, model_client=None):
    """Classify ``text`` into one of :data:`INTENTS`.

    Keyword rules first. When no rule matches and a model
    client is supplied, the model is asked to classify — that
    result is labelled ``method="model_fallback"``. With no
    model client, an unmatched request is ``"unknown"``.

    Returns ``{"intent", "method", "matched_keywords",
    "model_classification"}``.
    """
    text = (text or "").strip()
    intent, matched = _keyword_intent(text)
    if intent is not None:
        return {
            "intent": intent,
            "method": "keyword",
            "matched_keywords": matched,
            "model_classification": None,
        }
    if model_client is not None:
        try:
            reply = model_client.chat([
                {"role": "system", "content": (
                    "Classify the user's request into exactly one "
                    "of these words: " + ", ".join(INTENTS) + ". "
                    "Reply with only that word."
                )},
                {"role": "user", "content": text},
            ])
        except ModelUnavailable:
            reply = ""
        word = (reply or "").strip().lower()
        for candidate in INTENTS:
            if candidate in word:
                return {
                    "intent": candidate,
                    "method": "model_fallback",
                    "matched_keywords": [],
                    "model_classification": candidate,
                }
    return {
        "intent": "unknown",
        "method": "keyword",
        "matched_keywords": [],
        "model_classification": None,
    }


# ---------------------------------------------------------------------------
# Ticker extraction
# ---------------------------------------------------------------------------

_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?\b")


def _normalise_ticker(token):
    return token.upper().replace(".", "-")


def _universe_tickers(conn):
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM market_universe"
        ).fetchall()
    except Exception:  # noqa: BLE001 - no universe table, no validation
        return set()
    return {_normalise_ticker(str(r[0])) for r in rows if r[0]}


def extract_tickers(text, conn=None):
    """Tickers mentioned in ``text``, in order of appearance.

    When a database connection is supplied, a token is only
    accepted if it is a ticker in ``market_universe``; without
    one, any uppercase token that is not a stopword is accepted.
    """
    universe = _universe_tickers(conn) if conn is not None else None
    found = []
    for match in _TICKER_RE.finditer((text or "").upper()):
        token = match.group(0)
        if token in _TICKER_STOPWORDS:
            continue
        ticker = _normalise_ticker(token)
        if universe is not None and ticker not in universe:
            continue
        if ticker not in found:
            found.append(ticker)
    return found


# ---------------------------------------------------------------------------
# Read-only database access
# ---------------------------------------------------------------------------

def _open_read_only(db_path):
    """Open ``db_path`` read-only, or return None."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# ---------------------------------------------------------------------------
# Route handlers — each returns (reply, modules_called, sources)
# ---------------------------------------------------------------------------

def _tier_text(value):
    """A Step 23 tier dict or a Step 25 tier string, as text."""
    if isinstance(value, dict):
        return str(value.get("tier") or "unknown")
    return str(value or "unknown")


def _fmt_money(value):
    if value is None:
        return "unknown"
    return f"${value:,.2f}"


def _route_valuation(ticker, db_path):
    from agents import margin_of_safety as mos

    conn = _open_read_only(db_path)
    if conn is None:
        return (
            "I cannot value a company without the database — no "
            "database path was supplied or the file does not exist, "
            "so there is nothing to value from.",
            [], ["agents/margin_of_safety.py (not run: no database)"],
        )
    try:
        summary = mos.margin_of_safety_summary(conn, ticker)
    finally:
        conn.close()
    tier = _tier_text(summary.get("tier"))
    if tier == "insufficient_data":
        return (
            f"{ticker}: I do not have enough data to value it — "
            f"{summary.get('warnings') or 'no valuation range could '
              'be computed'}. No figure is estimated.",
            ["agents/margin_of_safety.py"],
            ["agents/margin_of_safety.py::margin_of_safety_summary "
             f"(tier {tier})"],
        )
    rng = summary.get("fair_value_range") or {}
    quality = summary.get("business_quality") or {}
    deterioration = summary.get("deterioration") or {}
    price = summary.get("price")
    price_as_of = summary.get("price_as_of") or "date unknown"
    # The Step 21/22 range is keyed conservative / base /
    # optimistic; the conservative value is the bear case.
    # The Step 23 summary carries no health verdict (that lives
    # in the Step 25 scanner candidate), so none is claimed here.
    lines = [
        f"{ticker}: tier {tier}.",
        "Fair-value range (10-year DCF, WACC 11%, terminal "
        f"growth 2.5%): bear {_fmt_money(rng.get('conservative'))}, "
        f"base {_fmt_money(rng.get('base'))}, optimistic "
        f"{_fmt_money(rng.get('optimistic'))}.",
        f"Price {_fmt_money(price)} (as of {price_as_of}).",
        "Strongest-alert price (20% below the conservative "
        f"value): {_fmt_money(summary.get('strongest_alert_threshold_price'))}.",
        f"Business quality: {quality.get('rating', 'unknown')}.",
        "Material deterioration: "
        f"{deterioration.get('deteriorated', 'unknown')}.",
        "This is a research figure, not a recommendation — the "
        "final decision is yours.",
    ]
    return (
        " ".join(lines),
        ["agents/margin_of_safety.py"],
        ["agents/margin_of_safety.py::margin_of_safety_summary "
         f"(Step 23, tier {tier})"],
    )


def _route_financial_research(ticker, db_path):
    from agents import market_scanner as ms

    conn = _open_read_only(db_path)
    if conn is None:
        return (
            "I cannot run the financial research without the "
            "database — no database path was supplied or the file "
            "does not exist.",
            [], ["agents/market_scanner.py (not run: no database)"],
        )
    try:
        candidate = ms.scan_ticker(conn, ticker)
    finally:
        conn.close()
    tier = _tier_text(candidate.get("tier"))
    if tier == "insufficient_data":
        missing = candidate.get("missing_inputs") or []
        return (
            f"{ticker}: not enough data to research — "
            f"{'; '.join(str(m) for m in missing) or 'no statements '
              'or price available'}. Nothing is estimated.",
            ["agents/market_scanner.py"],
            ["agents/market_scanner.py::scan_ticker "
             f"(tier {tier})"],
        )
    lines = [
        f"{ticker}: tier {tier}, quality "
        f"{candidate.get('quality_rating', 'unknown')}, health "
        f"{candidate.get('health_verdict', 'unknown')}.",
        f"Price {_fmt_money(candidate.get('price'))}.",
    ]
    rng = candidate.get("fair_value_range") or {}
    if rng.get("base") is not None:
        lines.append(
            "Fair-value range: bear "
            f"{_fmt_money(rng.get('bear'))}, base "
            f"{_fmt_money(rng.get('base'))}, optimistic "
            f"{_fmt_money(rng.get('optimistic'))}."
        )
    return (
        " ".join(lines),
        ["agents/market_scanner.py"],
        ["agents/market_scanner.py::scan_ticker "
         f"(Steps 16-24 pipeline, tier {tier})"],
    )


def _route_watchlist(store_dir):
    from agents import watchlist_foundation as wf

    entries = wf.load_watchlist(store_dir=store_dir)
    if not entries:
        return (
            "The watchlist is empty.",
            ["agents/watchlist_foundation.py"],
            ["agents/watchlist_foundation.py::load_watchlist"],
        )
    lines = wf.watchlist_view(store_dir=store_dir)
    if not lines:
        # No tracker records yet (never refreshed) — list the
        # entries themselves rather than an empty view.
        lines = []
        for entry in entries:
            sources = ", ".join(entry.get("sources") or [])
            reason = entry.get("reason") or "watch"
            line = f"{entry['ticker']}: {reason}"
            if sources:
                line += f" (sources: {sources})"
            lines.append(line)
    return (
        "Watchlist:\n" + "\n".join(f"- {line}" for line in lines),
        ["agents/watchlist_foundation.py"],
        ["agents/watchlist_foundation.py::watchlist_view"],
    )


def _route_news(ticker, news_dir):
    from agents import news_collection as nc

    articles = nc.list_articles(ticker=ticker, news_dir=news_dir)
    if not articles:
        return (
            f"No stored news articles for {ticker}. Nothing has "
            "been collected for it — no article is invented.",
            ["agents/news_collection.py"],
            ["agents/news_collection.py::list_articles"],
        )
    lines = [f"Stored news for {ticker} (newest first):"]
    for article in articles[:5]:
        title = article.get("title") or "(no title)"
        source = article.get("source") or "unknown source"
        published = article.get("published_date") or "date unknown"
        lines.append(f"- {title} — {source}, {published}")
    if len(articles) > 5:
        lines.append(f"... and {len(articles) - 5} more stored.")
    return (
        "\n".join(lines),
        ["agents/news_collection.py"],
        ["agents/news_collection.py::list_articles"],
    )


def _route_portfolio(context):
    from agents import portfolio_performance as pp

    holdings = (context or {}).get("holdings")
    price_history = (context or {}).get("price_history")
    if not holdings or not price_history:
        return (
            "I cannot analyse a portfolio without holdings and "
            "price history — neither is stored here, and no "
            "holdings are invented. Supply them and I will run "
            "the Step 36 analysis.",
            [], ["agents/portfolio_performance.py (not run: no inputs)"],
        )
    summary = pp.portfolio_summary(
        holdings, price_history,
        current_prices=(context or {}).get("current_prices"),
        benchmark_history=(context or {}).get("benchmark_history"),
        cash_flows=(context or {}).get("cash_flows"),
        currency=(context or {}).get("currency"),
    )
    lines = pp.summary_lines(summary)
    return (
        "Portfolio summary:\n" + "\n".join(f"- {line}" for line in lines),
        ["agents/portfolio_performance.py"],
        ["agents/portfolio_performance.py::portfolio_summary "
         "(Step 36)"],
    )


def _route_chat(text, model_client, history=None):
    if model_client is None:
        return (
            "No model is connected, so I cannot answer that — and "
            "I will not invent an answer instead.",
            [], ["model (not connected)"],
        )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        # The caller's recent conversation (last 20 messages in
        # main.py), so a chat turn is not amnesiac. Only the
        # current turn is sent when no history is given.
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    reply = model_client.chat(messages)
    # Step 40: when the client is a router, label the model that
    # actually answered (local or cloud fallback) instead of
    # always claiming Ollama.
    routing = getattr(model_client, "last_routing", None)
    if routing and routing.get("used"):
        label = f"model ({routing['used']}"
        if routing.get("fallback_used"):
            label += ", cloud fallback"
        label += ")"
        return reply, [], [label]
    return reply, [], ["model (qwen3:8b via Ollama)"]


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _log_turn(record, log_dir=None):
    """Append one turn record as a JSON line. Returns the path."""
    directory = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DEFAULT_LOG_NAME
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return str(path)


def log_turn(record, log_dir=None):
    """Append one turn record as a JSON line (public wrapper).

    Step 50: ``main.py`` records its slash-command turns in the
    same log with this, rather than duplicating the writer.
    """
    return _log_turn(record, log_dir=log_dir)


def handle(request, model_client=None, db_path=None,
           store_dir=None, news_dir=None, context=None,
           log_dir=None, as_of=None, tool_caller=None,
           history=None):
    """Handle one user request through the pipeline.

    Returns a dict: ``request``, ``intent``, ``intent_method``,
    ``route``, ``status``, ``reply``, ``modules_called``,
    ``sources`` and ``checked_at``. ``status`` is ``"ok"``,
    ``"refused"`` (a guardrail stopped the request),
    ``"unavailable"`` (the route needed something missing) or
    ``"model_unavailable"`` (the model could not be reached).
    The turn is appended to the JSON-lines log unless
    ``log_dir`` is False.

    ``tool_caller`` (Step 44, added in Step 50) is a
    :class:`agent.tool_calling.ToolCaller`: for a chat/unknown
    request the tool loop runs first (keyword selection only,
    no model fallback) and its result is the reply when it
    succeeds; otherwise the model answers as before. When
    ``tool_caller`` is None the chat route is unchanged.
    ``history`` is the caller's recent conversation
    (``{"role", "content"}`` messages, newest last), sent to
    the model with the current turn; None sends only this turn.
    """
    text = (request or "").strip()
    checked_at = as_of or _utcnow_iso()
    # Step 40: snapshot the router's last routing decision so
    # the turn record only claims routing for a turn that
    # actually called the model (see the record step below).
    _routing_before = getattr(model_client, "last_routing", None)

    classification = classify_intent(text, model_client=model_client)
    intent = classification["intent"]
    method = classification["method"]

    modules_called = []
    sources = []
    status = "ok"
    route = intent

    if not text:
        reply = "I did not receive a request."
        status = "unavailable"
        route = "none"
    elif intent == "trade":
        reply = (
            "I will not place a trade on my own. If you want to "
            "trade, tell me the exact order — ticker, side, "
            "quantity and price — and I will show it back to you "
            "for your approval first. No order is placed without "
            "that approval."
        )
        status = "refused"
        route = "guardrail_trade"
    elif intent == "schedule_send":
        reply = (
            "Sending messages and scheduling reminders are not "
            "wired up yet (Step 46). I have not sent or scheduled "
            "anything."
        )
        status = "refused"
        route = "guardrail_schedule_send"
    else:
        conn = _open_read_only(db_path) if db_path else None
        tickers = extract_tickers(text, conn=conn)
        if conn is not None:
            conn.close()
        ticker = tickers[0] if tickers else None

        if intent == "valuation":
            if ticker is None:
                reply = (
                    "Which company would you like valued? Give me "
                    "a ticker and I will run the Step 23 "
                    "margin-of-safety summary on it."
                )
                status = "unavailable"
            else:
                reply, modules_called, sources = _route_valuation(
                    ticker, db_path)
                if not modules_called:
                    status = "unavailable"
        elif intent == "financial_research":
            if ticker is None:
                reply = (
                    "Which company should I research? Give me a "
                    "ticker and I will run the Step 25 scanner on it."
                )
                status = "unavailable"
            else:
                reply, modules_called, sources = (
                    _route_financial_research(ticker, db_path))
                if not modules_called:
                    status = "unavailable"
        elif intent == "watchlist":
            reply, modules_called, sources = _route_watchlist(store_dir)
        elif intent == "news":
            if ticker is None:
                reply = (
                    "Which company should I check the news for? "
                    "Give me a ticker and I will list the stored "
                    "articles for it."
                )
                status = "unavailable"
            else:
                reply, modules_called, sources = _route_news(
                    ticker, news_dir)
        elif intent == "portfolio":
            reply, modules_called, sources = _route_portfolio(context)
            if not modules_called:
                status = "unavailable"
        else:  # chat or unknown — tools first, then the model
            tool_result = None
            if tool_caller is not None:
                # Keyword selection only (model_client=None): the
                # model fallback would spend a model call to pick
                # a tool for a question the model can answer
                # directly. Only a successful tool run answers
                # here; anything else falls through to the model.
                loop = tool_caller.run(
                    text, model_client=None, db_path=db_path)
                if loop.get("status") == "ok" and loop.get("calls"):
                    tool_result = loop
            if tool_result is not None:
                reply = (tool_result.get("final_result_summary")
                         or str(tool_result.get("final_result")))
                modules_called = list(
                    tool_result.get("tools_called") or [])
                sources = [f"tool ({name})" for name in modules_called]
            else:
                try:
                    reply, modules_called, sources = _route_chat(
                        text, model_client, history=history)
                except ModelUnavailable as exc:
                    reply = (
                        "The local model is unreachable right now "
                        f"({exc}), so I cannot answer that. I have not "
                        "invented an answer instead."
                    )
                    status = "model_unavailable"
                    sources = ["model (unreachable)"]

    result = {
        "request": text,
        "intent": intent,
        "intent_method": method,
        "route": route,
        "status": status,
        "reply": reply,
        "modules_called": modules_called,
        "sources": sources,
        "checked_at": checked_at,
    }
    # Step 40: when the model client is a router, record which
    # model answered and whether the cloud fallback ran, so the
    # fallback decision is in the turn record — but only when
    # this turn actually called the model. A stale routing
    # record from an earlier turn (e.g. a refused trade turn)
    # must not be copied into a turn that never used it.
    routing = getattr(model_client, "last_routing", None)
    if routing is not None and routing is not _routing_before:
        result["model_routing"] = routing
    if log_dir is not False:
        result["log_file"] = _log_turn(result, log_dir=log_dir)
    return result


class Orchestrator:
    """A reusable orchestrator holding the default clients/paths.

    ``handle`` delegates to the module-level :func:`handle` with
    these defaults; any argument can be overridden per call.
    """

    def __init__(self, model_client=None, db_path=None,
                 store_dir=None, news_dir=None, log_dir=None,
                 tool_caller=None):
        self.model_client = model_client
        self.db_path = db_path
        self.store_dir = store_dir
        self.news_dir = news_dir
        self.log_dir = log_dir
        self.tool_caller = tool_caller

    def handle(self, request, **overrides):
        kwargs = {
            "model_client": self.model_client,
            "db_path": self.db_path,
            "store_dir": self.store_dir,
            "news_dir": self.news_dir,
            "log_dir": self.log_dir,
            "tool_caller": self.tool_caller,
        }
        kwargs.update(overrides)
        return handle(request, **kwargs)

    def classify(self, text):
        return classify_intent(text, model_client=self.model_client)
