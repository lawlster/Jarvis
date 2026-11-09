"""Step 44 — Natural-language tool calling.

A tool registry and a call loop the Step 39 orchestrator can
use, built on top of what already exists rather than replacing
it:

- ``main.py`` still owns the interactive loop and its own
  inline Ollama tool-calling loop (tools: calculate, search_web,
  get_company_financials, calculate_dcf). This module does NOT
  replace it and is NOT wired into ``main.py`` (that integration
  is Step 50). The four ``main.py`` tools are registered here
  by wrapping their existing implementations — never
  reimplemented.
- The Step 16-38 engines are registered as read-only tools
  where they fit (valuation, scan, watchlist view, stored news,
  events, research session), calling their existing public
  functions.
- Step 39's ``classify_intent`` / ``extract_tickers`` are
  reused for selection inputs; this module adds tool-level
  selection (which tool, with which arguments) on top.

The registry + loop have four parts:

1. Registry — each tool has a name, a plain-language
   description, a JSON schema for its arguments, and a Python
   function. Registration refuses, loudly and at registration
   time, any tool whose name suggests trading, sending or
   scheduling (trade / buy / sell / send / schedule / remind /
   order / place / purchase / email / message / telegram) — the
   registry must never contain such a tool at all, because the
   owner places trades only by explicit per-order approval and
   sending/scheduling is Step 46.
2. Selection — given a user request, pick the tool to call.
   Keyword rules first (explicit, testable); a model call only
   as a fallback that returns a structured tool choice
   (``{"tool": name, "arguments": {...}}``), labelled
   ``method="model_fallback"``. A request matching no tool
   returns "no tool" rather than guessing.
3. Execution — run the chosen tool with validated arguments.
   Arguments that fail validation are rejected with the reason,
   never coerced silently (a string where a number is required
   is an error, not a conversion). Every call is recorded:
   tool, arguments, result summary, duration, success/failure.
4. Multi-step — a tool's result may trigger a follow-up tool
   call by an explicit rule (financials then DCF, using the
   project's Step 21 defaults, labelled as such). The chain has
   a hard cap (default 5 calls) and a loop guard (the same tool
   with the same arguments may not repeat).

No network beyond what the wrapped tools already do
(``search_web`` already fetches; no new fetching is added).
No database writes beyond what the wrapped tools already do
(the engine tools open the database read-only). Nothing here
is a recommendation and nothing here places a trade, sends a
message or schedules anything.
"""

import json
import re
import sqlite3
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolCallingError(Exception):
    """Base error for the tool-calling module."""


class ForbiddenToolError(ToolCallingError):
    """A tool name suggests trading, sending or scheduling."""


class DuplicateToolError(ToolCallingError):
    """A tool with this name is already registered."""


class UnknownToolError(ToolCallingError):
    """No tool with this name is registered."""


class ArgumentValidationError(ToolCallingError):
    """Arguments failed validation; the reason is in the message."""


# Tokens in a tool name that mean the tool must never be
# registered: trading, sending, scheduling. Checked against the
# name split on non-alphanumeric characters, so "buyback" does
# not trip the rule but "buy_shares" and "place_order" do.
_FORBIDDEN_NAME_TOKENS = frozenset({
    "trade", "buy", "sell", "send", "schedule", "remind",
    "order", "place", "purchase", "email", "message", "telegram",
    "short",
})


def _name_tokens(name):
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _check_tool_name(name):
    """Raise :class:`ForbiddenToolError` for a forbidden name."""
    if not isinstance(name, str) or not name.strip():
        raise ForbiddenToolError("a tool name must be a non-empty string")
    bad = [t for t in _name_tokens(name) if t in _FORBIDDEN_NAME_TOKENS]
    if bad:
        raise ForbiddenToolError(
            f"tool name '{name}' is forbidden: it suggests "
            f"{' / '.join(sorted(set(bad)))} — trading, sending and "
            "scheduling tools are never registered here (trades need "
            "the owner's explicit per-order approval; sending and "
            "scheduling are Step 46)"
        )


# ---------------------------------------------------------------------------
# Tool definition and registry
# ---------------------------------------------------------------------------


class Tool:
    """One registered tool: name, description, argument schema, function."""

    def __init__(self, name, description, parameters, function,
                 read_only=True):
        _check_tool_name(name)
        if not isinstance(description, str) or not description.strip():
            raise ToolCallingError("a tool needs a plain-language description")
        if not isinstance(parameters, dict):
            raise ToolCallingError("a tool's parameters must be a JSON schema dict")
        if not callable(function):
            raise ToolCallingError("a tool needs a callable function")
        self.name = name
        self.description = description
        self.parameters = parameters
        self.function = function
        self.read_only = read_only

    def ollama_spec(self):
        """The tool in the ``main.py`` Ollama tool-call shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Tool({self.name!r})"


class ToolRegistry:
    """A registry of tools, with forbidden names refused at registration."""

    def __init__(self):
        self._tools = {}

    def register(self, tool):
        """Register ``tool``; forbidden names and duplicates fail loudly."""
        if not isinstance(tool, Tool):
            raise ToolCallingError("only a Tool can be registered")
        _check_tool_name(tool.name)
        if tool.name in self._tools:
            raise DuplicateToolError(
                f"tool '{tool.name}' is already registered"
            )
        self._tools[tool.name] = tool
        return tool

    def register_function(self, name, description, parameters, function,
                          read_only=True):
        """Register a plain function as a tool."""
        return self.register(Tool(
            name, description, parameters, function,
            read_only=read_only,
        ))

    def get(self, name):
        """Return the tool, or raise :class:`UnknownToolError`."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(
                f"no tool named '{name}' is registered"
            ) from exc

    def has(self, name):
        return name in self._tools

    def names(self):
        return sorted(self._tools)

    def ollama_tools(self):
        """All tools in the ``main.py`` Ollama tool-call shape."""
        return [self._tools[name].ollama_spec() for name in self.names()]

    def describe(self):
        """One plain-language line per tool, for a model prompt."""
        lines = []
        for name in self.names():
            tool = self._tools[name]
            required = (tool.parameters.get("required") or [])
            lines.append(
                f"- {name}: {tool.description} "
                f"(arguments: {json.dumps(tool.parameters.get('properties') or {})}; "
                f"required: {', '.join(required) or 'none'})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argument validation (JSON-schema subset, no silent coercion)
# ---------------------------------------------------------------------------

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_arguments(tool, arguments):
    """Validate ``arguments`` against ``tool``'s schema.

    Returns ``(ok, reason)`` — ``reason`` is None when ok. A
    string where a number is required is an error, never a
    conversion; missing required arguments and unknown
    arguments are errors too.
    """
    schema = tool.parameters or {}
    if not isinstance(arguments, dict):
        return False, "arguments must be an object (a JSON dict)"
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [name for name in required if name not in arguments]
    if missing:
        return False, (
            f"missing required argument(s): {', '.join(missing)}"
        )
    unknown = [name for name in arguments if name not in properties]
    if unknown:
        return False, (
            f"unknown argument(s): {', '.join(unknown)} — the tool "
            f"'{tool.name}' accepts only: "
            f"{', '.join(sorted(properties)) or 'none'}"
        )
    for name, value in arguments.items():
        spec = properties.get(name) or {}
        expected = spec.get("type")
        if expected:
            check = _TYPE_CHECKS.get(expected)
            if check is not None and not check(value):
                return False, (
                    f"argument '{name}' must be a {expected}, "
                    f"got {type(value).__name__} — values are not "
                    "converted silently"
                )
        allowed = spec.get("enum")
        if allowed is not None and value not in allowed:
            return False, (
                f"argument '{name}' must be one of "
                f"{', '.join(str(v) for v in allowed)}, got {value!r}"
            )
    return True, None


def _summarise_result(result, limit=500):
    """A short, log-safe summary of a tool result."""
    if isinstance(result, dict):
        if "error" in result:
            text = f"error: {result.get('error')}"
        else:
            keys = ", ".join(sorted(str(k) for k in result.keys()))
            text = f"dict with keys: {keys}"
            try:
                text += " | " + json.dumps(result, default=str)[:limit]
            except (TypeError, ValueError):
                pass
    else:
        text = str(result)
    if len(text) > limit:
        text = text[:limit] + f" … (truncated, {len(text)} chars total)"
    return text


# ---------------------------------------------------------------------------
# Default registry — wraps existing implementations, never reimplements
# ---------------------------------------------------------------------------

def _open_read_only(db_path):
    """Open ``db_path`` read-only, or return None."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _latest_number(value):
    """A number, or the latest numeric value of a dict of numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        numbers = [
            (key, float(v)) for key, v in value.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if not numbers:
            return None
        numbers.sort(key=lambda pair: str(pair[0]))
        return numbers[-1][1]
    return None


def default_registry(db_path=None, store_dir=None, news_dir=None,
                     events_dir=None, sessions_dir=None,
                     search_function=None, financials_function=None):
    """Build the registry of the tools Jarvis already has.

    The four ``main.py`` tools are wrapped from their existing
    modules (``tools/calculator.py``, ``tools/research.py``,
    ``tools/finance.py``, ``tools/valuation.py``); the engine
    tools call the existing Step 16-38 public functions. The
    database is opened read-only per call. ``search_function``
    and ``financials_function`` let tests substitute stubs for
    the two tools that touch the network; the defaults are the
    real implementations.
    """
    from tools.calculator import calculate
    from tools.research import search_web
    from tools.finance import get_company_financials
    from tools.valuation import calculate_dcf

    registry = ToolRegistry()

    # -- the four main.py tools -------------------------------------

    registry.register_function(
        "calculate",
        "Perform mathematical calculations. Give the expression as text.",
        {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        lambda expression: calculate(expression),
    )

    def _search_web(query, max_results=5):
        function = search_function or search_web
        return function(query, max_results=max_results)

    registry.register_function(
        "search_web",
        "Search the internet for recent company information and "
        "financial news. Returns a list of results with title, "
        "url, date and description.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
        _search_web,
        read_only=True,
    )

    def _get_company_financials(ticker, provider_name="yahoo"):
        function = financials_function or get_company_financials
        return function(ticker, provider_name=provider_name)

    registry.register_function(
        "get_company_financials",
        "Retrieve financial information and financial statements "
        "for a company using its ticker.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "provider_name": {"type": "string"},
            },
            "required": ["ticker"],
        },
        _get_company_financials,
        read_only=True,
    )

    registry.register_function(
        "calculate_dcf",
        "Calculate discounted cash flow valuation using explicitly "
        "supplied assumptions. Rates are decimals (10% = 0.10). "
        "Never present the result as an exact stock price.",
        {
            "type": "object",
            "properties": {
                "free_cash_flow": {"type": "number"},
                "growth_rate": {"type": "number"},
                "discount_rate": {"type": "number"},
                "terminal_growth_rate": {"type": "number"},
                "years": {"type": "integer"},
                "net_debt": {"type": "number"},
                "shares_outstanding": {"type": "number"},
            },
            "required": [
                "free_cash_flow", "growth_rate", "discount_rate",
                "terminal_growth_rate",
            ],
        },
        lambda **kwargs: calculate_dcf(**kwargs),
    )

    # -- the Step 16-38 engines, read-only --------------------------

    def _valuation_summary(ticker, price=None):
        from agents import margin_of_safety as mos

        conn = _open_read_only(db_path)
        if conn is None:
            return {
                "error": "no database path was supplied or the file "
                         "does not exist — nothing can be valued",
                "ticker": ticker,
            }
        try:
            price_input = {"price": price} if price is not None else None
            return mos.margin_of_safety_summary(
                conn, ticker, price_input=price_input)
        finally:
            conn.close()

    registry.register_function(
        "valuation_summary",
        "The Step 23 margin-of-safety summary for one ticker: "
        "fair-value range (10-year DCF), tier, strongest-alert "
        "price, business quality and deterioration. Read-only; "
        "never returns a buy/sell call.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "price": {"type": "number"},
            },
            "required": ["ticker"],
        },
        _valuation_summary,
        read_only=True,
    )

    def _scan_ticker(ticker, price=None):
        from agents import market_scanner as ms

        conn = _open_read_only(db_path)
        if conn is None:
            return {
                "error": "no database path was supplied or the file "
                         "does not exist — nothing can be scanned",
                "ticker": ticker,
            }
        try:
            price_input = {"price": price} if price is not None else None
            return ms.scan_ticker(
                conn, ticker, price_input=price_input)
        finally:
            conn.close()

    registry.register_function(
        "scan_ticker",
        "Scan one ticker through the Step 16-24 pipeline: tier, "
        "quality, health, fair-value range and price. Read-only.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "price": {"type": "number"},
            },
            "required": ["ticker"],
        },
        _scan_ticker,
        read_only=True,
    )

    def _watchlist_view():
        from agents import watchlist_foundation as wf

        return {"lines": wf.watchlist_view(store_dir=store_dir)}

    registry.register_function(
        "watchlist_view",
        "One line per ticker on the owner's watchlist (Step 35): "
        "tier, quality, health, margin vs the conservative value, "
        "price and alert price. Read-only.",
        {"type": "object", "properties": {}},
        lambda: _watchlist_view(),
        read_only=True,
    )

    def _list_news(ticker, since=None):
        from agents import news_collection as nc

        return {
            "articles": nc.list_articles(
                ticker=ticker, since=since, news_dir=news_dir)
        }

    registry.register_function(
        "list_news",
        "List the stored news articles for one ticker (Step 31 "
        "collection only — nothing is fetched live). Newest first.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "since": {"type": "string"},
            },
            "required": ["ticker"],
        },
        _list_news,
        read_only=True,
    )

    def _list_events(ticker=None, days=7):
        from agents import event_tracking as et

        return {
            "events": et.list_events(
                events_dir=events_dir, ticker=ticker)
        }

    registry.register_function(
        "list_events",
        "List stored events (Step 33) — earnings, dividends, "
        "economic releases, filings — for one ticker or all. "
        "Read-only; nothing is fetched live.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer"},
            },
        },
        _list_events,
        read_only=True,
    )

    def _research_session(question, peers=None):
        from agent import research_agents as ra

        # sessions_dir=None keeps the Step 41 default
        # (data/research_sessions/); pass a directory — or False
        # to skip writing — and the brief goes there instead.
        save_dir = sessions_dir if sessions_dir is not None else None
        return ra.run_research_session(
            question, db_path=db_path, peers=peers,
            news_dir=news_dir, events_dir=events_dir,
            save_dir=save_dir)

    registry.register_function(
        "research_session",
        "Run a planned, multi-step research session (Step 41) "
        "for a question: scan, valuation, report, news, events, "
        "then a sourced brief. Read-only; a trade request is "
        "refused before any step runs.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "peers": {"type": "array"},
            },
            "required": ["question"],
        },
        _research_session,
        read_only=True,
    )

    return registry


# ---------------------------------------------------------------------------
# Selection — keyword rules first, model fallback second
# ---------------------------------------------------------------------------

# Each rule: (tool name, trigger phrases, argument builder).
# The first rule whose trigger appears wins. Argument builders
# receive (request, tickers) and return the arguments dict, or
# None when the rule cannot build arguments for this request
# (then selection falls through to the next rule).
def _arg_ticker(tickers):
    if not tickers:
        return None
    return {"ticker": tickers[0]}


def _arg_expression(request):
    text = request.strip()
    for trigger in ("calculate", "compute", "what is", "how much is"):
        lowered = text.lower()
        if lowered.startswith(trigger):
            text = text[len(trigger):].strip()
            break
    text = text.rstrip("?").strip()
    if re.fullmatch(r"[0-9\s+\-*/().^]+", text) and re.search(r"\d", text):
        return {"expression": text}
    return None


def _arg_query(request):
    text = request.strip()
    for trigger in ("search the web for", "search for", "search",
                    "look up", "find"):
        lowered = text.lower()
        if lowered.startswith(trigger):
            text = text[len(trigger):].strip()
            break
    text = text.rstrip("?").strip()
    if text:
        return {"query": text}
    return None


def _arg_dcf(request):
    """Parse labelled DCF assumptions, e.g. "fcf 50000, growth
    10%, discount 11%, terminal 2.5%". Returns None unless all
    four required assumptions are found — a partial DCF is
    never run on guessed assumptions."""
    lowered = request.lower()

    def _find(patterns):
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                try:
                    return float(match.group(1).replace(",", ""))
                except ValueError:
                    return None
        return None

    fcf = _find([
        r"(?:free cash flow|fcf)\s*(?:of|=|:)?\s*([\d,]+(?:\.\d+)?)",
        r"([\d,]+(?:\.\d+)?)\s*(?:million|m)\b[^.]*?free cash flow",
    ])
    growth = _find([
        r"growth\s*(?:rate)?\s*(?:of|=|:)?\s*([\d.]+)\s*%",
        r"([\d.]+)\s*%\s*growth",
    ])
    discount = _find([
        r"(?:discount|wacc)\s*(?:rate)?\s*(?:of|=|:)?\s*([\d.]+)\s*%",
        r"([\d.]+)\s*%\s*discount",
    ])
    terminal = _find([
        r"terminal\s*(?:growth)?\s*(?:of|=|:)?\s*([\d.]+)\s*%",
        r"([\d.]+)\s*%\s*terminal",
    ])
    if None in (fcf, growth, discount, terminal):
        return None
    return {
        "free_cash_flow": fcf,
        "growth_rate": growth / 100.0,
        "discount_rate": discount / 100.0,
        "terminal_growth_rate": terminal / 100.0,
    }


def _keyword_rules():
    return (
        # DCF with explicit assumptions beats the valuation engine.
        ("calculate_dcf", ("calculate dcf", "dcf with",
                           "discounted cash flow with"),
         lambda request, tickers: _arg_dcf(request)),
        # Financials before valuation: a request that mentions
        # both ("get the financials and value it with a DCF")
        # must start at the financials so the follow-up rule can
        # run the DCF on the returned free cash flow.
        ("get_company_financials", (
            "financials", "financial statements", "balance sheet",
            "get the financials",
        ), lambda request, tickers: _arg_ticker(tickers)),
        ("valuation_summary", (
            "intrinsic value", "valuation", "fair value", "dcf value",
            "dcf", "discounted cash flow", "margin of safety",
            "overvalued", "undervalued", "cheap", "alert price",
            "what is it worth", "is it worth", "good price",
            "entry price",
        ), lambda request, tickers: _arg_ticker(tickers)),
        ("research_session", (
            "research", "deep dive", "compare",
        ), lambda request, tickers: {"question": request}),
        ("scan_ticker", (
            "scan", "analyse", "analyze",
        ), lambda request, tickers: _arg_ticker(tickers)),
        ("watchlist_view", (
            "watchlist", "watch list", "what am i watching",
            "my watchlist",
        ), lambda request, tickers: {}),
        ("list_news", (
            "news", "headlines", "latest on", "any news",
        ), lambda request, tickers: _arg_ticker(tickers)),
        # Search before events: "search the web for Microsoft
        # earnings" is a web search, not an events lookup — the
        # bare "earnings" trigger in list_events would swallow it.
        ("search_web", (
            "search", "look up", "find",
        ), lambda request, tickers: _arg_query(request)),
        ("list_events", (
            "upcoming events", "earnings calendar", "events",
            "earnings",
        ), lambda request, tickers: (
            {"ticker": tickers[0]} if tickers else {})),
        ("calculate", (
            "calculate", "compute", "what is", "how much is",
        ), lambda request, tickers: _arg_expression(request)),
    )


def _keyword_select(request, tickers, registry):
    lowered = f" {request.lower()} "
    for name, triggers, builder in _keyword_rules():
        if not registry.has(name):
            continue
        if not any(trigger in lowered for trigger in triggers):
            continue
        arguments = builder(request, tickers)
        if arguments is None:
            continue
        return {
            "tool": name,
            "arguments": arguments,
            "method": "keyword",
            "reason": f"keyword rule for '{name}' matched",
        }
    return None


def _model_fallback_select(request, registry, model_client):
    """Ask the model to choose a tool, as structured JSON.

    The model must return exactly ``{"tool": name,
    "arguments": {...}}``. Anything else — unparseable JSON, an
    unknown tool, invalid arguments — is "no tool", never a
    guess.
    """
    from agent.orchestrator import ModelUnavailable

    prompt = (
        "You choose one tool for the user's request, or none. "
        "Reply with exactly one JSON object and nothing else: "
        '{"tool": "<tool name>", "arguments": {...}}. '
        "If no tool fits the request, reply "
        '{"tool": null, "arguments": {}}. Available tools:\n'
        + registry.describe()
        + f"\nUser request: {request}"
    )
    try:
        reply = model_client.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": request},
        ])
    except ModelUnavailable:
        return {
            "tool": None, "arguments": {}, "method": "model_fallback",
            "reason": "model unavailable — no tool chosen",
        }
    try:
        choice = json.loads((reply or "").strip())
    except (ValueError, TypeError):
        return {
            "tool": None, "arguments": {}, "method": "model_fallback",
            "reason": "model did not return JSON — no tool chosen",
        }
    if not isinstance(choice, dict):
        return {
            "tool": None, "arguments": {}, "method": "model_fallback",
            "reason": "model did not return a JSON object — no tool chosen",
        }
    name = choice.get("tool")
    if name is None:
        return {
            "tool": None, "arguments": {}, "method": "model_fallback",
            "reason": "model chose no tool",
        }
    if not registry.has(name):
        return {
            "tool": None, "arguments": {}, "method": "model_fallback",
            "reason": f"model chose unregistered tool '{name}' — no tool chosen",
        }
    tool = registry.get(name)
    arguments = choice.get("arguments") or {}
    ok, reason = validate_arguments(tool, arguments)
    if not ok:
        return {
            "tool": None, "arguments": {}, "method": "model_fallback",
            "reason": f"model's arguments for '{name}' failed validation: {reason}",
        }
    return {
        "tool": name,
        "arguments": arguments,
        "method": "model_fallback",
        "reason": f"model chose '{name}'",
    }


def _extract_tickers(text, conn=None):
    """Tickers mentioned in ``text``, in order of appearance.

    Wraps the Step 39 extractor with one refinement: tokens
    written in uppercase in the original text are tried first,
    and only if none validates is the text uppercased and tried
    again. A lowercase word that happens to spell a ticker —
    "are" is ARE (Alexandria Real Estate Equities) in the
    universe — is not a ticker mention in "what events are
    coming up for MSFT?", where MSFT is the ticker the user
    typed. A request typed entirely in lowercase ("what is the
    intrinsic value of msft?") still works via the fallback.
    """
    from agent.orchestrator import (
        _TICKER_RE,
        _TICKER_STOPWORDS,
        _normalise_ticker,
        _universe_tickers,
    )

    universe = _universe_tickers(conn) if conn is not None else None
    for source in (text, text.upper()):
        found = []
        for match in _TICKER_RE.finditer(source):
            token = match.group(0)
            if token in _TICKER_STOPWORDS:
                continue
            ticker = _normalise_ticker(token)
            if universe is not None and ticker not in universe:
                continue
            if ticker not in found:
                found.append(ticker)
        if found:
            return found
    return []


def select_tool(request, registry, model_client=None, db_path=None):
    """Pick the tool for ``request``.

    Keyword rules first; a model call only as a fallback,
    labelled ``method="model_fallback"``. A request matching no
    tool returns ``tool=None`` rather than guessing. Returns
    ``{"tool", "arguments", "method", "reason"}``.
    """
    text = (request or "").strip()
    if not text:
        return {
            "tool": None, "arguments": {}, "method": "none",
            "reason": "empty request — no tool chosen",
        }
    conn = _open_read_only(db_path) if db_path else None
    try:
        tickers = _extract_tickers(text, conn=conn)
    finally:
        if conn is not None:
            conn.close()
    selected = _keyword_select(text, tickers, registry)
    if selected is not None:
        return selected
    if model_client is not None:
        return _model_fallback_select(text, registry, model_client)
    return {
        "tool": None, "arguments": {}, "method": "none",
        "reason": "no keyword rule matched and no model client "
                  "was supplied — no tool chosen",
    }


# ---------------------------------------------------------------------------
# Follow-up rules (multi-step chains)
# ---------------------------------------------------------------------------

# Step 21 defaults, used only when a DCF follow-up runs: WACC
# 11%, terminal growth 2.5%, 10-year horizon, base growth 10%.
# They are the project's published defaults, labelled as such in
# the call record — never presented as the user's assumptions.
_DCF_DEFAULTS = {
    "growth_rate": 0.10,
    "discount_rate": 0.11,
    "terminal_growth_rate": 0.025,
    "years": 10,
}


def _follow_up(request, last_call, registry):
    """The next tool call a result triggers, or None.

    Rules, in order:
    - ``get_company_financials`` succeeded and the request is a
      valuation/DCF question and the result carries a usable
      free-cash-flow figure → ``calculate_dcf`` with that FCF
      and the Step 21 defaults (net debt and shares from the
      same financials where available).
    Everything else: no follow-up.
    """
    lowered = (request or "").lower()
    if last_call.get("tool") != "get_company_financials":
        return None
    if not last_call.get("success"):
        return None
    if not any(word in lowered for word in (
        "dcf", "discounted cash flow", "valuation", "intrinsic value",
        "fair value", "worth",
    )):
        return None
    if not registry.has("calculate_dcf"):
        return None
    result = last_call.get("result")
    if not isinstance(result, dict):
        return None
    fcf = _latest_number(result.get("free_cash_flow"))
    if fcf is None or fcf <= 0:
        return None
    arguments = {
        "free_cash_flow": fcf,
        "growth_rate": _DCF_DEFAULTS["growth_rate"],
        "discount_rate": _DCF_DEFAULTS["discount_rate"],
        "terminal_growth_rate": _DCF_DEFAULTS["terminal_growth_rate"],
        "years": _DCF_DEFAULTS["years"],
    }
    net_debt = _latest_number(result.get("total_debt"))
    cash = _latest_number(result.get("cash"))
    if net_debt is not None:
        arguments["net_debt"] = net_debt - (cash or 0.0)
    shares = result.get("shares_outstanding")
    if isinstance(shares, (int, float)) and not isinstance(shares, bool) \
            and shares > 0:
        arguments["shares_outstanding"] = shares
    return {
        "tool": "calculate_dcf",
        "arguments": arguments,
        "method": "follow_up",
        "reason": (
            "financials returned a free-cash-flow figure and the "
            "request is a valuation question — DCF with the "
            "Step 21 defaults (growth 10%, WACC 11%, terminal "
            "2.5%, 10 years)"
        ),
    }


# ---------------------------------------------------------------------------
# The call loop
# ---------------------------------------------------------------------------

def _execute_call(registry, name, arguments):
    """Validate and run one tool call; returns the call record."""
    started = time.monotonic()
    record = {
        "tool": name,
        "arguments": arguments,
        "success": False,
        "result": None,
        "result_summary": None,
        "error": None,
        "duration_seconds": None,
    }
    try:
        tool = registry.get(name)
    except UnknownToolError as exc:
        record["error"] = str(exc)
        record["duration_seconds"] = round(time.monotonic() - started, 4)
        return record
    ok, reason = validate_arguments(tool, arguments)
    if not ok:
        record["error"] = f"argument validation failed: {reason}"
        record["duration_seconds"] = round(time.monotonic() - started, 4)
        return record
    try:
        result = tool.function(**arguments)
    except Exception as exc:  # noqa: BLE001 - record, never crash the loop
        record["error"] = f"tool raised {type(exc).__name__}: {exc}"
        record["duration_seconds"] = round(time.monotonic() - started, 4)
        return record
    record["success"] = True
    record["result"] = result
    record["result_summary"] = _summarise_result(result)
    record["duration_seconds"] = round(time.monotonic() - started, 4)
    return record


def run_tool_loop(request, registry, model_client=None, db_path=None,
                  max_calls=5):
    """Run the tool loop for ``request``.

    Selects the first tool (keyword rules, then the model
    fallback), executes it, and follows the explicit follow-up
    rules until no rule fires, the chain cap is reached, or the
    loop guard trips. Returns a record: ``request``,
    ``status`` (``ok`` / ``no_tool`` / ``argument_rejected`` /
    ``tool_error`` / ``max_calls_reached`` / ``loop_guard``),
    ``selection`` (the first selection), ``calls`` (one record
    per call), ``tools_called`` and ``final_result`` (the last
    call's result, or None).
    """
    text = (request or "").strip()
    selection = select_tool(
        text, registry, model_client=model_client, db_path=db_path)
    if selection["tool"] is None:
        return {
            "request": text,
            "status": "no_tool",
            "selection": selection,
            "calls": [],
            "tools_called": [],
            "final_result": None,
            "final_result_summary": None,
            "reason": selection["reason"],
        }
    calls = []
    seen = set()
    current = selection
    status = "ok"
    while current is not None and len(calls) < max_calls:
        name = current["tool"]
        arguments = current["arguments"] or {}
        key = (name, json.dumps(arguments, sort_keys=True, default=str))
        if key in seen:
            status = "loop_guard"
            break
        seen.add(key)
        record = _execute_call(registry, name, arguments)
        record["method"] = current.get("method", "keyword")
        record["selection_reason"] = current.get("reason")
        calls.append(record)
        if not record["success"]:
            if record["error"] and record["error"].startswith(
                    "argument validation failed"):
                status = "argument_rejected"
            else:
                status = "tool_error"
            break
        current = _follow_up(text, record, registry)
    else:
        if current is not None and len(calls) >= max_calls:
            status = "max_calls_reached"
    return {
        "request": text,
        "status": status,
        "selection": selection,
        "calls": calls,
        "tools_called": [call["tool"] for call in calls],
        "final_result": calls[-1]["result"] if calls else None,
        "final_result_summary": (
            calls[-1]["result_summary"] if calls else None
        ),
        "reason": selection["reason"],
    }


class ToolCaller:
    """A reusable tool caller holding the registry and defaults.

    ``run`` delegates to :func:`run_tool_loop` with these
    defaults; any argument can be overridden per call.
    """

    def __init__(self, registry=None, model_client=None, db_path=None,
                 max_calls=5):
        self.registry = registry
        self.model_client = model_client
        self.db_path = db_path
        self.max_calls = max_calls

    def run(self, request, **overrides):
        kwargs = {
            "registry": self.registry,
            "model_client": self.model_client,
            "db_path": self.db_path,
            "max_calls": self.max_calls,
        }
        kwargs.update(overrides)
        return run_tool_loop(request, **kwargs)

    def select(self, request, **overrides):
        kwargs = {
            "registry": self.registry,
            "model_client": self.model_client,
            "db_path": self.db_path,
        }
        kwargs.update(overrides)
        return select_tool(request, **kwargs)
