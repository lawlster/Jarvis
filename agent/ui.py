"""Step 48 — complete user interface (local web UI).

A small local web interface served by Python's standard-library
``http.server`` — no new dependencies, no hosted service, no cloud
account or key. The owner runs Jarvis locally on a Windows PC with
Ollama, so the UI is a localhost server he opens in a browser.

Pages (all data comes from the existing modules' public functions;
the UI derives nothing new about value):

- ``/`` — home: the daily summary (Step 32 shape), the watchlist
  view (Step 35 one line per ticker), and active alerts (Step 38)
  with their evidence.
- ``/ticker/<TICKER>`` — the Step 29 research report for a ticker
  (tier, quality, health, DCF range, alert price, deterioration,
  news, events). A ticker with no data gets a report whose sections
  say so — nothing is invented. The DCF range is also drawn as a
  bar chart (Step 55).
- ``/research`` — a question box that runs a Step 41 research
  session (caller-driven, read-only database) and shows the sourced
  brief.
- ``/chat`` — a text box that runs the Step 39 orchestrator and
  shows the reply with its sources labelled. Step 60: turns
  submit with ``fetch`` to ``/api/chat`` and append to an
  on-screen conversation history (the plain form post still
  works without JavaScript); the Step 58 live fallback is on
  when the UI instance enables it, and the model notice names
  the model client actually wired in.
- ``/status`` — what the UI can and cannot do.
- ``/dashboard`` — Step 49: the status snapshot (module health,
  recent activity, gaps) over the logs and state files the other
  steps already write. Read-only; it never runs a module. Step 55
  adds the stored Step 54 live-scan results and the Step 53
  deployment-readiness verdict to this page.

Step 55 redesign (2026-11-09): the same pages, URLs, localhost-
only binding, read-only database and no-trading-controls rules,
restyled as a dark mission-control dashboard — deep navy
background, cyan/amber accents, card layout, sortable watchlist
table, canvas DCF charts drawn by vanilla JavaScript in
``agent/ui_static/app.js``. No new Python dependencies, no CDN,
no external resources: everything works offline from localhost.
The dashboard shows STORED scan results (``step54-live-scan.json``)
and stored status — it never re-runs a live scan on page load
and nothing on it updates live; a refresh re-reads the stored
files. Step 56 adds browser voice to the chat page (Web Speech
API — microphone input and spoken replies, opt-in, off by
default, no wake word, no audio ever leaves the browser).
Step 60 rebuilds the chat so it works like a real chat: the
Step 58 live fallback wired into the chat, ticker and research
pages; turns submitted with ``fetch`` to ``/api/chat`` and
appended to an on-screen conversation history; the model notice
reflects the model client actually wired in (a stub warning
only when a stub is in use).

Rules the UI enforces:

- The server binds ``127.0.0.1`` only — ``make_server`` refuses
  any other host.
- The database is opened read-only. The research page does not
  save sessions (``save_dir=False``).
- There is NO trade button, NO order form, and NO control whose
  name, action, or label suggests trading anywhere in the served
  HTML. A trade request typed into the chat box is refused by the
  Step 39 guardrail, never executed.
- Candidates are worth a closer look, never recommendations; the
  final decision is always the owner's.

Not built here: wiring into ``main.py`` (Step 50). No real
browser session has ever run through this UI — it is tested
with a test HTTP client against a server in a thread.
"""

import html
import json
import os
import re
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent import cloud_models as cm
from agent import dashboard as dash
from agent import deployment as dep
from agent import orchestrator as orch
from agent import research_agents as ra
from agents import daily_summary as ds
from agents import live_fallback as lfb
from agents import research_report as rr
from agents import scheduled_scan as ss
from agents import watchlist_alerts as wa
from agents import watchlist_foundation as wf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_POST_BYTES = 64 * 1024

_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,9}")

# Words that must never appear on a control (form, input, button,
# select) in the served HTML. Quoted article titles may contain
# "Buy" — those are the articles' words, labelled as such, and are
# not controls.
_FORBIDDEN_CONTROL_RE = re.compile(
    r"<(?:button|input|select|form)[^>]*(?:buy|sell|trade|order|"
    r"place_order|checkout)",
    re.IGNORECASE,
)


def _project_root():
    return Path(__file__).resolve().parent.parent


def _open_read_only(db_path):
    """Open a SQLite database read-only, or None when no path given."""
    if db_path is None:
        return None
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


class JarvisUI:
    """Configuration for one UI instance.

    ``db_path`` is opened read-only by every page. ``store_dir``
    is the Step 35 watchlist store, ``news_dir`` / ``events_dir``
    the Step 31 / Step 33 stored-data directories,
    ``alert_state_path`` the Step 38 state file. ``model_client``
    is the Step 39 model client (a stub in tests, the real Ollama
    client on the owner's PC). ``log_dir=False`` disables the Step
    39 turn log (tests); ``None`` keeps the default log location.
    ``data_dir`` is the Step 49 dashboard's data root (defaults
    to the project's ``data/`` directory). ``live_scan_path`` is
    the Step 54 stored live-scan JSON (defaults to
    ``step54-live-scan.json`` next to the project directory, if
    it exists); the dashboard reads it, never re-runs a scan.
    ``readiness`` is a precomputed Step 53 readiness report dict
    (tests); when None the dashboard computes it once per UI
    instance and caches it, because the secrets check takes
    seconds and a page load must not re-run it.
    ``live_fallback`` (Step 60) is the Step 58 live fallback for
    the chat, ticker and research pages — off by default, the
    same rule as everywhere else; ``main.py`` and ``run_local``
    turn it on for the owner's PC. ``live_fetcher`` injects the
    live pull (None means the real Yahoo fetch).
    """

    def __init__(self, db_path=None, store_dir=None, news_dir=None,
                 events_dir=None, alert_state_path=None,
                 model_client=None, log_dir=None, data_dir=None,
                 live_scan_path=None, readiness=None,
                 live_fallback=False, live_fetcher=None):
        base = _project_root()
        self.db_path = db_path or str(base / "database" / "assistant.db")
        self.store_dir = store_dir
        self.news_dir = news_dir
        self.events_dir = events_dir
        self.alert_state_path = alert_state_path
        self.model_client = model_client
        self.log_dir = log_dir
        self.data_dir = data_dir
        self.live_scan_path = live_scan_path
        self.readiness = readiness
        self.live_fallback = live_fallback
        self.live_fetcher = live_fetcher
        self._readiness_cache = None
        self._readiness_error = None
        self._readiness_computing = False
        self._readiness_lock = threading.Lock()


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _esc(value):
    return html.escape("" if value is None else str(value))


def _md_to_html(markdown):
    """A small, safe Markdown subset for report/brief bodies.

    The text is escaped first, so nothing in a stored title or
    snippet can break out of the page; only headings, bullet
    lists, and bold spans are then recognised.
    """
    out = []
    in_list = False
    for raw in (markdown or "").splitlines():
        line = _esc(raw)
        heading = re.match(r"^(#{1,3}) (.*)$", line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            out.append(f"<h{level + 1}>{heading.group(2)}</h{level + 1}>")
            continue
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    text = "\n".join(out)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return text


def _page(title, body, active=""):
    """A full HTML document with navigation and the Step 55 shell.

    Dark mission-control header (brand, local-only status pill,
    nav), card-ready main, disclaimer footer, and the vanilla
    JavaScript that draws charts and sorts tables. No external
    resources of any kind.
    """
    nav = [
        ("/", "Home", "home"),
        ("/dashboard", "Dashboard", "dashboard"),
        ("/research", "Research", "research"),
        ("/chat", "Chat", "chat"),
        ("/status", "Status", "status"),
    ]
    links = " ".join(
        f'<a href="{href}"{" class=\"active\"" if key == active else ""}>'
        f"{label}</a>"
        for href, label, key in nav
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\">\n"
        f"<title>{_esc(title)} — Jarvis</title>\n"
        "<link rel=\"stylesheet\" href=\"/static/style.css\">\n"
        "</head>\n<body>\n"
        "<header><div class=\"brand\">J.A.R.V.I.S."
        "<small>JUST A RATHER VERY INTELLIGENT SYSTEM</small></div>"
        "<span class=\"status-pill\">\u25cf LOCAL ONLY \u00b7 "
        "127.0.0.1</span>"
        f"<nav>{links}</nav></header>\n"
        "<main>\n"
        f"<h2>{_esc(title)}</h2>\n"
        f"{body}\n"
        "</main>\n"
        "<footer><p>Research, not recommendations. Candidates are "
        "worth a closer look; the final decision is always the "
        "owner's. This interface contains no trading controls by "
        "design.</p><p>Stored data only — this page re-reads stored "
        "files on refresh; nothing here updates live.</p></footer>\n"
        "<script src=\"/static/app.js\"></script>\n"
        "</body>\n</html>\n"
    )


def _card(title, inner, cls=""):
    """One dashboard card: a titled panel in the grid."""
    extra = f" {cls}" if cls else ""
    return (f"<section class=\"card{extra}\">\n"
            f"<h3>{_esc(title)}</h3>\n{inner}\n</section>")


def _badge(text, cls=""):
    """A coloured badge; ``cls`` is a known badge class suffix."""
    suffix = f" badge-{cls}" if cls else ""
    return f"<span class=\"badge{suffix}\">{_esc(text)}</span>"


def _money(value):
    """A dollar amount with two decimals, or an em dash."""
    if value is None:
        return "\u2014"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "\u2014"


def _load_live_scan(ui):
    """(data, error) for the stored Step 54 live-scan JSON.

    ``ui.live_scan_path`` wins when set; otherwise the file is
    looked for next to the project directory, then inside it.
    Read-only, never raises — a missing file returns (None,
    None) and the dashboard says so plainly.
    """
    candidates = []
    if getattr(ui, "live_scan_path", None):
        candidates.append(Path(ui.live_scan_path))
    else:
        root = _project_root()
        candidates.append(root.parent / "step54-live-scan.json")
        candidates.append(root / "step54-live-scan.json")
    for path in candidates:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"Could not read {path}: {exc}"
        if isinstance(data, dict) and isinstance(
                data.get("results"), list):
            return data, None
        return None, f"{path} is not a live-scan file."
    if getattr(ui, "live_scan_path", None):
        return None, (f"No live scan stored at "
                      f"{ui.live_scan_path}.")
    return None, None


def _scheduled_scans_dir(ui):
    """The Step 59 scans directory for this UI instance."""
    if getattr(ui, "data_dir", None):
        return Path(ui.data_dir) / "scans"
    return _project_root() / "data" / "scans"


def _load_latest_scheduled_run(ui):
    """(run, error) for the latest Step 59 scheduled run.

    Read-only, never raises — no run yet returns (None, None)
    and the dashboard says so plainly.
    """
    try:
        run, _path = ss.latest_run(_scheduled_scans_dir(ui))
    except Exception as exc:  # noqa: BLE001 - a broken scan
        # directory must not take the dashboard down.
        return None, f"Could not read the scheduled runs: {exc}"
    return run, None


def _compute_readiness_background(ui):
    """Compute the Step 53 readiness report off the request path.

    The secrets check alone takes ~13 s, so a dashboard visit
    must never wait for it. The result (or the error) is stored
    on the UI instance; the next dashboard visit shows it.
    """
    try:
        ui._readiness_cache = dep.readiness_report()
    except Exception as exc:  # noqa: BLE001 - a slow/broken check
        # must not take the dashboard down; say so plainly.
        ui._readiness_error = f"Readiness could not be computed: {exc}"
    finally:
        with ui._readiness_lock:
            ui._readiness_computing = False


def _readiness(ui):
    """(report, error) for the Step 53 readiness verdict.

    A precomputed report passed to JarvisUI wins (tests). Otherwise
    the report is computed ONCE, in a background thread, and cached
    per UI instance — the secrets check takes seconds and a page
    load must not wait for it. While it computes, the caller gets
    (None, "computing") and the dashboard says so plainly.
    Never raises.
    """
    if getattr(ui, "readiness", None) is not None:
        return ui.readiness, None
    if getattr(ui, "_readiness_cache", None) is not None:
        return ui._readiness_cache, None
    if getattr(ui, "_readiness_error", None) is not None:
        return None, ui._readiness_error
    with ui._readiness_lock:
        if ui._readiness_cache is not None:
            return ui._readiness_cache, None
        if ui._readiness_error is not None:
            return None, ui._readiness_error
        if not ui._readiness_computing:
            ui._readiness_computing = True
            thread = threading.Thread(
                target=_compute_readiness_background, args=(ui,),
                daemon=True)
            thread.start()
    return None, "computing"


def _dcf_chart_html(chart_id, rows):
    """A canvas DCF-range chart plus its embedded data.

    ``rows`` are dicts with ticker, price, conservative, base,
    optimistic. The JSON is embedded in a script block (escaped so
    no value can break out of it); app.js draws the bars.
    """
    clean = []
    for row in rows:
        clean.append({
            "ticker": str(row.get("ticker") or ""),
            "price": row.get("price"),
            "conservative": (row.get("fair_value_range") or {}).get(
                "conservative"),
            "base": (row.get("fair_value_range") or {}).get("base"),
            "optimistic": (row.get("fair_value_range") or {}).get(
                "optimistic"),
        })
    payload = json.dumps(clean).replace("&", "\\u0026").replace(
        "<", "\\u003c").replace(">", "\\u003e")
    return (
        f"<canvas class=\"dcf-chart\" data-json-id=\"{chart_id}-data\" "
        f"width=\"900\" height=\"200\"></canvas>\n"
        f"<script type=\"application/json\" id=\"{chart_id}-data\">"
        f"{payload}</script>\n"
        "<p class=\"chart-note\">Bars: conservative (amber), base "
        "(cyan), optimistic (violet). Red line: price. "
        "10-year FCF DCF, WACC 11%, terminal growth 2.5%.</p>\n"
    )


def _lines(items):
    if not items:
        return "<p>None recorded.</p>"
    return "<ul>\n" + "\n".join(
        f"<li>{_esc(item)}</li>" for item in items) + "\n</ul>"


def _form(action, label, placeholder, value="", form_id=None):
    ident = f" id=\"{_esc(form_id)}\"" if form_id else ""
    return (
        f"<form method=\"post\" action=\"{action}\"{ident}>\n"
        f"<label for=\"q\">{_esc(label)}</label><br>\n"
        f"<input type=\"text\" id=\"q\" name=\"q\" size=\"80\" "
        f"placeholder=\"{_esc(placeholder)}\" value=\"{_esc(value)}\">\n"
        "<input type=\"submit\" value=\"Ask\">\n"
        "</form>\n"
    )


# ---------------------------------------------------------------------------
# Step 60 — chat rebuild: live fallback, model notice, conversation
# ---------------------------------------------------------------------------

def _model_client_kind(client):
    """'live' | 'stub' | 'none' | 'unknown' for the model client.

    'live' is a real model client — the local Ollama client or
    the Step 40 local-first router (which answers from Ollama
    unless its cloud fallback is configured and used).  A stub
    test client is 'stub'; no client at all is 'none'.  Anything
    else is 'unknown': the notice names it and claims neither a
    live model nor a stub.
    """
    if client is None:
        return "none"
    if isinstance(client, orch.OllamaModelClient):
        return "live"
    if isinstance(client, orch.StubModelClient):
        return "stub"
    if isinstance(client, cm.ModelRouter):
        return "live"
    return "unknown"


def _model_notice(ui):
    """(heading, text) for the chat page's model notice.

    The notice reflects the model client actually wired into
    this UI instance — never a stub claim when the real Ollama
    client is wired, never a live-model claim when a stub is.
    """
    kind = _model_client_kind(ui.model_client)
    voice_tail = (" Switch voice on in the Voice panel below to "
                  "talk and hear replies (Chrome or Edge).")
    if kind == "live":
        return ("Model:",
                "Live model connected: Ollama on this machine "
                "(qwen3:8b) — replies come from the local model, "
                "not a stub. Valuation answers use stored data "
                "first and live data only when the stored path "
                "has none — a live-derived value is always "
                "labelled provisional. What you type is answered "
                "from stored data and the valuation engines, never "
                "from a live price feed except through that "
                f"labelled fallback. Voice is off by default.{voice_tail}")
    if kind == "none":
        return ("What is missing here:",
                "No model is connected, so general questions "
                "cannot be answered — and no answer is invented "
                "instead. Valuation and watchlist questions are "
                "still answered from stored data and the valuation "
                "engines, never from a live price feed except "
                "through the labelled live fallback. Voice is off "
                f"by default.{voice_tail}")
    if kind == "unknown":
        name = type(ui.model_client).__name__
        return ("Model:",
                f"Model client: {name} — the model type could not "
                "be confirmed from here, so this page claims "
                "neither a live model nor a stub. Valuation "
                "answers use stored data and the valuation engines, "
                "never from a live price feed except through the "
                f"labelled live fallback. Voice is off by default.{voice_tail}")
    return ("What is missing here:",
            "no live Ollama turn has run through this chat on this "
            "machine — replies come from a stub model in tests, and "
            "voice is off by default. What you type is answered from "
            "stored data and the valuation engines, never from a live "
            "price feed. Switch voice on in the Voice panel below to "
            "talk and hear replies (Chrome or Edge).")


def _answer_question(ui, question):
    """One chat turn through the Step 39 orchestrator.

    The Step 58 live fallback is on exactly when this UI
    instance enables it (``main.py`` and ``run_local`` do, for
    the owner's PC) — so asking about a ticker with no stored
    statements returns the labelled live-derived value instead
    of "I do not have enough data".
    """
    return orch.handle(
        question, model_client=ui.model_client,
        db_path=ui.db_path, store_dir=ui.store_dir,
        news_dir=ui.news_dir, log_dir=ui.log_dir,
        live_fallback=ui.live_fallback,
        live_fetcher=ui.live_fetcher,
        live_scan_path=ui.live_scan_path)


def _recent_turns(ui, limit=10):
    """Recent orchestrator turns (oldest first) from the turn log.

    The log is the Step 39 JSON-lines turn log — the same
    conversation the terminal writes, so turns asked in the
    terminal appear here too.  Read-only; a missing or
    unreadable log returns no turns, never a crash.  When
    logging is off (``log_dir=False``) there is no history to
    read.
    """
    if ui.log_dir is False:
        return []
    log_dir = Path(ui.log_dir) if ui.log_dir else orch.DEFAULT_LOG_DIR
    records, _skipped = dash._read_json_lines(
        log_dir / orch.DEFAULT_LOG_NAME)
    turns = [record for record in records
             if isinstance(record, dict) and record.get("request")]
    return turns[-limit:]


def _turn_html(turn, latest=False):
    """One conversation turn: the question, the reply, its sources.

    The latest turn's reply paragraph carries
    id="jarvis-reply-text" so the Step 56 voice panel can speak
    it; exactly one element on the page ever has that id.
    """
    reply_id = " id=\"jarvis-reply-text\"" if latest else ""
    sources = turn.get("sources") or []
    sources_html = ""
    if sources:
        sources_html = (
            "<p class=\"chat-sources\"><strong>Sources:</strong> "
            + _esc("; ".join(str(source) for source in sources))
            + "</p>")
    return (
        "<div class=\"chat-turn\">"
        f"<p class=\"chat-question\"><strong>You:</strong> "
        f"{_esc(turn.get('request'))}</p>"
        "<div class=\"chat-reply\">"
        f"<p class=\"chat-meta\">Intent: "
        f"{_esc(turn.get('intent'))} "
        f"({_esc(turn.get('intent_method'))}). "
        f"Status: {_esc(turn.get('status'))}.</p>"
        f"<p class=\"chat-reply-text\"{reply_id}>"
        f"{_esc(turn.get('reply'))}</p>"
        f"{sources_html}"
        "</div>"
        "</div>")


# ---------------------------------------------------------------------------
# Page builders (pure functions over a JarvisUI — testable without a server)
# ---------------------------------------------------------------------------

def home_html(ui, summary_date=None):
    """Home page: daily summary, watchlist view, active alerts."""
    conn = _open_read_only(ui.db_path)
    try:
        summary = ds.generate_summary(
            conn, date=summary_date, news_dir=ui.news_dir)
    finally:
        if conn is not None:
            conn.close()
    coverage = summary.get("coverage") or {}
    sections = [_card(
        "Today",
        f"<p>{_esc(summary['headline'])}</p>"
        "<p>Coverage: "
        f"{_esc(coverage.get('selected', '?'))} tickers selected, "
        f"{_esc(coverage.get('scannable', '?'))} scannable.</p>")]
    sections.append(_card(
        "Candidates worth a closer look",
        _lines(summary.get("candidate_lines") or [])))
    if summary.get("trap_lines"):
        sections.append(_card(
            "Value traps (not candidates)",
            _lines(summary["trap_lines"])))
    # Step 35's watchlist_view returns one dict per ticker; the
    # display line is its "note" field, not the whole dict.
    view = wf.watchlist_view(store_dir=ui.store_dir)
    notes = [entry.get("note") if isinstance(entry, dict) else str(entry)
             for entry in view]
    sections.append(_card("Watchlist", _lines(notes)))
    try:
        state = wa.load_state(ui.alert_state_path)
        alerts = wa.list_active_alerts(state)
        alert_body = (_lines(wa.briefing_lines(alerts)) if alerts
                      else "<p>No active alerts.</p>")
    except Exception as exc:  # noqa: BLE001 - a corrupt state file
        # must not take the home page down; say so plainly.
        alert_body = (
            f"<p>Alert state could not be read: {_esc(exc)}</p>")
    sections.append(_card("Active alerts", alert_body,
                          cls="alert-card"))
    sections.append(_card(
        "Limitations", _lines(summary.get("limitations") or [])))
    return _page("Daily summary",
                 "<div class=\"grid\">\n" + "\n".join(sections) +
                 "\n</div>", active="home")


def _live_valuation_card(ui, ticker):
    """A Step 58 live-derived value card for a ticker page.

    Only called when the Step 29 report has no valuation range
    (``insufficient_data``).  The Step 58 exclusions run here
    exactly as in the terminal — banks / insurers / REITs never
    get an FCF DCF, a currency mismatch is refused, a negative
    FCF is insufficient data, and the 95% magnitude guard is
    the backstop — and a live-derived value is always labelled
    provisional.  A refusal is shown as a refusal with its
    reason; no figure is ever estimated.  Visiting the ticker
    page is the owner explicitly naming the ticker, so the
    watchlist gate is satisfied the same way as the chat's
    ``explicitly_named``.  The stored scan is preferred, so a
    watchlist ticker with a fresh stored scan needs no network;
    otherwise one live pull is made for this one ticker.
    """
    result = lfb.live_valuation(
        ticker, store_dir=ui.store_dir, fetcher=ui.live_fetcher,
        live_scan_path=ui.live_scan_path, explicitly_named=True)
    if result["status"] == "ok":
        return _card("Live-derived value (Step 58)",
                     f"<p>{_esc(lfb.format_live_valuation(result))}"
                     "</p>")
    return _card(
        "Live-derived value (Step 58)",
        "<p>No live-derived value: "
        f"{_esc(result.get('reason') or result['status'])}.</p>")


def ticker_html(ui, ticker):
    """Ticker page: the Step 29 research report for one ticker."""
    raw = (ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(raw):
        return _page("Ticker not found",
                     f"<p>No ticker matches {_esc(ticker)}.</p>"), 400
    conn = _open_read_only(ui.db_path)
    try:
        report = rr.generate_report(conn, raw)
    finally:
        if conn is not None:
            conn.close()
    name = report.get("company_name") or raw
    header = (
        f"<p>{_esc(name)} ({_esc(raw)}) — "
        f"exchange {_esc(report.get('exchange') or 'unknown')}, "
        f"country {_esc(report.get('country') or 'unknown')}.</p>"
        f"<p>Report status: <strong>{_esc(report.get('status'))}"
        "</strong></p>"
    )
    section_rows = "".join(
        f"<li>{_esc(title)}: {_esc(section.get('status'))}</li>"
        for title, section in (
            (name, report["sections"][name])
            for name in report.get("sections", {}))
    )
    header += f"<ul>\n{section_rows}</ul>" if section_rows else ""
    chart = ""
    valuation = (report.get("sections") or {}).get(
        "valuation", {}) or {}
    range_values = (valuation.get("data") or {}).get(
        "fair_value_range") or {}
    if any(isinstance(range_values.get(key), (int, float))
           for key in ("conservative", "base", "optimistic")):
        chart = _card(
            "Intrinsic-value range vs price",
            _dcf_chart_html("ticker-dcf", [{
                "ticker": raw,
                "price": report.get("price"),
                "fair_value_range": range_values,
            }]) +
            f"<p>Price {_money(report.get('price'))} as of "
            f"{_esc(report.get('price_as_of') or 'unknown')} "
            f"({_esc(report.get('price_source') or 'unknown')}). "
            f"Conservative {_money(range_values.get('conservative'))}, "
            f"base {_money(range_values.get('base'))}, optimistic "
            f"{_money(range_values.get('optimistic'))}. "
            "A single-method FCF DCF — research, not a fair-value "
            "estimate and never a recommendation.</p>")
    body = _md_to_html(rr.report_to_markdown(report))
    live_card = ""
    if ui.live_fallback and (report.get("sections") or {}).get(
            "valuation", {}).get("status") == "insufficient_data":
        # The stored report has no valuation range for this
        # ticker — the Step 58 fallback fills the gap with a
        # labelled live-derived value (or a stated refusal).
        live_card = _live_valuation_card(ui, raw)
    return _page(f"Research report: {raw}",
                 header + chart + live_card + body), 200


def research_html(ui, question=None):
    """Research page: a question box plus the Step 41 sourced brief.

    The Step 58 live fallback is on exactly when this UI
    instance enables it, so a research session about a ticker
    with no stored statements uses the labelled live-derived
    value for its scan and valuation steps (the Step 29 report
    step still has no fallback, as in Step 58).
    """
    body = [_form("/research", "Research question",
                  "Research MSFT", value=question or "")]
    if question:
        brief = ra.run_research_session(
            question, db_path=ui.db_path, news_dir=ui.news_dir,
            events_dir=ui.events_dir, save_dir=False,
            store_dir=ui.store_dir, live_fallback=ui.live_fallback,
            live_fetcher=ui.live_fetcher,
            live_scan_path=ui.live_scan_path)
        body.append(
            f"<p>Kind: {_esc(brief.get('kind'))}. "
            f"Status: {_esc(brief.get('status'))} — "
            f"{_esc(brief.get('status_reason') or '')}</p>")
        body.append(_md_to_html(ra.brief_to_markdown(brief)))
    return _page("Research", "\n".join(body), active="research")


def chat_html(ui, request=None):
    """Chat page: a text box plus the Step 39 orchestrator reply.

    Step 60 rebuild: the conversation stays on screen — turns
    are submitted with ``fetch`` to ``/api/chat`` (see
    ``agent/ui_static/app.js``) and appended to the history
    below, instead of a full-page reload that showed only the
    latest reply. The page still works without JavaScript: the
    form posts to ``/chat`` and the server renders the same
    history from the Step 39 turn log (or from the turn just
    answered, when logging is off). The model notice reflects
    the model client actually wired in — a stub warning only
    when a stub is in use. The Step 58 live fallback is on
    exactly when this UI instance enables it, so asking about
    a ticker with no stored statements returns the labelled
    live-derived value instead of "I do not have enough data".

    Command chips fill the box (the owner edits before asking).
    Step 56 adds the browser-voice panel: a microphone button,
    a voice on/off switch (OFF by default, honouring the stored
    text preference), a hands-free toggle, and a reply-voice
    picker — all Web Speech API in the browser, no server
    endpoint, no audio ever sent anywhere. The latest reply
    paragraph carries id="jarvis-reply-text" so the page script
    can speak it when voice is on.
    """
    commands = ["/research", "/watchlist", "/alerts", "/summary",
                "/memory", "/dashboard", "/ui", "/voice", "/help",
                "/quit", "/livescan"]
    chips = "".join(
        f"<button type=\"button\" class=\"cmd-chip\" "
        f"data-command=\"{_esc(cmd)}\">{_esc(cmd)}</button>"
        for cmd in commands)
    voice_panel = (
        "<div class=\"voice-panel\" id=\"voice-panel\">\n"
        "<h3>Voice</h3>\n"
        "<p class=\"voice-status\" id=\"voice-status\">Voice off.</p>\n"
        "<button type=\"button\" id=\"voice-mic\" class=\"voice-mic\" "
        "disabled aria-label=\"Talk to Jarvis\">\U0001f399\ufe0f Talk"
        "</button>\n"
        "<label class=\"voice-toggle\"><input type=\"checkbox\" "
        "id=\"voice-toggle\"> Voice on</label>\n"
        "<label class=\"voice-toggle\"><input type=\"checkbox\" "
        "id=\"voice-handsfree\" disabled> Hands-free</label>\n"
        "<div class=\"voice-picker-row\"><label for=\"voice-picker\">"
        "Reply voice</label>\n"
        "<select id=\"voice-picker\" disabled></select></div>\n"
        "<p class=\"voice-help\">Voice runs entirely in this "
        "browser — nothing is recorded or sent anywhere. Switch "
        "voice on, click the microphone and talk; Jarvis answers "
        "in text and speaks the reply. Hands-free resumes "
        "listening after each spoken reply. There is no wake "
        "word: browsers do not allow always-on background "
        "listening, so Jarvis cannot listen while this page is "
        "closed. Voice input is not supported in this browser "
        "unless it offers speech recognition — try Chrome or "
        "Edge. Spoken replies need a browser that offers speech "
        "synthesis — try Chrome or Edge. The reply voice prefers "
        "a British male voice when the browser offers one.</p>\n"
        "</div>\n"
    )
    heading, notice = _model_notice(ui)
    turns = _recent_turns(ui)
    if request:
        result = _answer_question(ui, request)
        # The turn was just logged (unless logging is off), so
        # do not render it twice — but when logging is off the
        # turn exists only in memory and must be rendered.
        if (not turns or turns[-1].get("request") != request
                or turns[-1].get("reply") != result.get("reply")):
            turns = turns + [{
                "request": request,
                "intent": result.get("intent"),
                "intent_method": result.get("intent_method"),
                "status": result.get("status"),
                "reply": result.get("reply"),
                "sources": result.get("sources") or [],
            }]
    if turns:
        history = ("<div id=\"chat-history\">\n" + "\n".join(
            _turn_html(turn, latest=(index == len(turns) - 1))
            for index, turn in enumerate(turns)) + "\n</div>")
    else:
        history = ("<div id=\"chat-history\">"
                   "<p class=\"chat-empty\">No conversation yet — "
                   "ask something below.</p></div>")
    body = [
        f"<div class=\"notice\"><strong>{_esc(heading)}</strong> "
        f"{_esc(notice)}</div>",
        f"<div class=\"cmd-chips\">{chips}</div>",
        history,
        _form("/chat", "Ask Jarvis",
              "What is the intrinsic value of MSFT?",
              value=request or "", form_id="chat-form"),
        voice_panel,
    ]
    return _page("Chat", "\n".join(body), active="chat")


def dashboard_html(ui):
    """Dashboard page: Step 49 status snapshot + Step 54 live scan
    + Step 59 scheduled daily scan + Step 53 readiness verdict.

    Module health, recent activity, and gaps, read from the logs
    and state files the other steps already write. The live-scan
    panel shows the STORED ``step54-live-scan.json`` results —
    it never re-runs a scan. The scheduled-scan panel shows the
    latest Step 59 run (its diff against the previous run and
    the run history), or says plainly that no scheduled scan
    has run yet. The readiness panel shows the Step 53
    verdict (computed once per UI instance and cached). Read-only —
    the page runs nothing, writes nothing, and sends nothing.
    """
    snapshot = dash.collect_status(data_dir=ui.data_dir)
    sections = []

    # -- Live watchlist scan (stored Step 54 results) ---------------
    scan, scan_error = _load_live_scan(ui)
    if scan is None:
        scan_body = (
            "<p>No live scan has been stored yet. "
            "Run <code>/livescan</code> in main.py (about 20 minutes "
            "for the 38-ticker watchlist) and the results will "
            "appear here.</p>")
        if scan_error:
            scan_body += f"<p>{_esc(scan_error)}</p>"
        sections.append(_card("Live watchlist scan", scan_body))
    else:
        summary = scan.get("summary") or {}
        banded = []
        others = []
        not_scannable = []
        band_rank = {"strongest_alert": 0, "watch_discount": 1,
                     "watch_below_base": 2}
        for row in scan.get("results") or []:
            if row.get("status") != "ok":
                not_scannable.append(row)
            elif row.get("detection_band"):
                banded.append(row)
            else:
                others.append(row)
        banded.sort(key=lambda r: (
            band_rank.get(r.get("detection_band"), 3),
            -(r.get("margin_of_safety_vs_conservative") or 0)))
        others.sort(key=lambda r: r.get("ticker") or "")
        scanned_at = scan.get("scanned_at") or "unknown"
        as_of = None
        for row in scan.get("results") or []:
            if row.get("price_as_of"):
                as_of = str(row["price_as_of"])[:10]
                break
        scan_body = [
            f"<p>Live scan run {_esc(scanned_at)}"
            + (f" ({scan.get('run_seconds'):.0f}s)"
               if isinstance(scan.get("run_seconds"), (int, float))
               else "")
            + f"; prices as of {_esc(as_of or 'unknown')}. "
            f"{_esc(summary.get('selected', '?'))} tickers selected, "
            f"{_esc(summary.get('scannable', '?'))} scannable, "
            f"{_esc(summary.get('strongest_alerts', 0))} "
            "strongest-alert bands, "
            f"{_esc(summary.get('watch', 0))} watch bands, "
            f"{_esc(summary.get('provider_unavailable', 0))} "
            "provider-unavailable, "
            f"{_esc(summary.get('insufficient_data', 0))} "
            "insufficient data.</p>",
            "<p><strong>Margin bands are provisional</strong> — "
            "business quality and deterioration are unknown from a "
            "live pull, so no band is a confirmed Step 26 "
            "candidate. Worth a closer look, never a "
            "recommendation.</p>",
        ]
        if banded:
            # The chart shows the genuine bands only: the known
            # exclusions (KSPI's tenge artifact, DBS/OCBC bank
            # bands) would stretch the scale to meaninglessness.
            # They stay in the table below, with the notice.
            chart_rows = [
                row for row in banded
                if not (row.get("ticker") == "KSPI"
                        and row.get("detection_band")
                        == "strongest_alert")
                and not (row.get("ticker") in ("DBS", "OCBC")
                         and row.get("detection_band")
                         == "watch_discount")]
            if chart_rows:
                scan_body.append(
                    _dcf_chart_html("live-scan-dcf", chart_rows))
            rows_html = []
            for row in banded + others:
                range_values = row.get("fair_value_range") or {}
                band = row.get("detection_band")
                tier = row.get("tier") or ""
                rows_html.append(
                    "<tr" + (" class=\"row-alert\""
                             if band == "strongest_alert" else "") +
                    f"><td>{_esc(row.get('ticker'))}</td>"
                    f"<td class=\"num\">{_money(row.get('price'))}</td>"
                    f"<td class=\"num\">"
                    f"{_money(range_values.get('conservative'))}</td>"
                    f"<td class=\"num\">"
                    f"{_money(range_values.get('base'))}</td>"
                    f"<td class=\"num\">"
                    f"{_money(range_values.get('optimistic'))}</td>"
                    f"<td>{_badge(band, band) if band else chr(8212)}"
                    "</td>"
                    f"<td>{_badge(tier, tier) if tier else chr(8212)}"
                    "</td></tr>")
            scan_body.append(
                "<table class=\"sortable\">\n<thead><tr>"
                "<th>Ticker</th><th class=\"num\">Price</th>"
                "<th class=\"num\">Conservative</th>"
                "<th class=\"num\">Base</th>"
                "<th class=\"num\">Optimistic</th>"
                "<th>Band</th><th>Tier</th></tr></thead>\n<tbody>\n"
                + "\n".join(rows_html) +
                "\n</tbody>\n</table>\n"
                "<p class=\"chart-note\">Click a column header to "
                "sort. DCF: 10-year FCF DCF on the latest annual "
                "free cash flow, WACC 11%, terminal growth 2.5%, "
                "growth 5% / 10% / 15%.</p>")
        if not_scannable:
            items = []
            for row in not_scannable:
                reason = str(row.get("reason") or
                             row.get("status") or "")[:160]
                items.append(
                    f"{row.get('ticker')}: {row.get('status')} — "
                    f"{reason}")
            scan_body.append("<h4>Not scannable</h4>")
            scan_body.append(_lines(items))
        # Known exclusions documented in the Step 54 report: the
        # stored file still carries these bands, so the dashboard
        # must not present them as candidates. Only shown when
        # the stored results actually contain them.
        exclusion_notes = []
        for row in scan.get("results") or []:
            if (row.get("ticker") == "KSPI"
                    and row.get("detection_band")
                    == "strongest_alert"):
                exclusion_notes.append(
                    "KSPI's strongest-alert band is a currency "
                    "artifact — its free cash flow is in "
                    "Kazakhstani tenge while the price is in US "
                    "dollars — so it is not a candidate.")
            if (row.get("ticker") in ("DBS", "OCBC")
                    and row.get("detection_band")
                    == "watch_discount"):
                exclusion_notes.append(
                    f"{row.get('ticker')}'s watch-discount band "
                    "is model-invalid: a free-cash-flow DCF does "
                    "not apply to banks (Step 22).")
        if exclusion_notes:
            seen = []
            for note in exclusion_notes:
                if note not in seen:
                    seen.append(note)
            scan_body.append(
                "<div class=\"notice\"><strong>Known exclusions "
                "in this scan (Step 54 report):</strong> "
                + " ".join(seen) + " The table above shows the "
                "stored file as-is; the stored summary counts "
                "include these bands, but the Step 54 report "
                "excludes them from the genuine candidate list."
                "</div>")
        for line in scan.get("limitations") or []:
            scan_body.append(f"<p class=\"chart-note\">{_esc(line)}"
                             "</p>")
        sections.append(_card("Live watchlist scan",
                              "\n".join(scan_body)))

    # -- Scheduled daily scan (Step 59) -----------------------------
    run, run_error = _load_latest_scheduled_run(ui)
    if run is None:
        sched_body = (
            "<p>No scheduled scan has run yet. The daily scan "
            "runs once a day at 05:30 SGT, after the US close — "
            "set it up on the owner's PC with "
            "<code>scripts/setup_daily_scan.ps1</code> (run "
            "PowerShell as administrator). The PC must be on or "
            "asleep at 05:30; a missed run is recorded as missed, "
            "never backfilled.</p>")
        if run_error:
            sched_body += f"<p>{_esc(run_error)}</p>"
        sections.append(_card("Scheduled daily scan", sched_body))
    else:
        summary = run.get("summary") or {}
        diff = run.get("diff") or {}
        sched_body = []
        sched_body.append(
            f"<p>Latest run {_esc(run.get('run_id') or '')} — "
            f"scanned {_esc(run.get('scanned_at') or '')}, "
            f"{run.get('run_seconds', 0):.0f}s. "
            f"{summary.get('selected', '?')} tickers, "
            f"{summary.get('scannable', '?')} scannable, "
            f"{summary.get('strongest_alerts', 0)} strongest-alert "
            f"bands, {summary.get('watch', 0)} watch bands, "
            f"{summary.get('provider_unavailable', 0)} "
            f"provider-unavailable, "
            f"{summary.get('insufficient_data', 0)} insufficient "
            f"data.</p>")
        new_strongest = diff.get("new_strongest_alert") or []
        if new_strongest:
            items = []
            for entry in new_strongest:
                rng = entry.get("fair_value_range") or {}
                margin = entry.get(
                    "margin_of_safety_vs_conservative")
                margin_text = (
                    f"{margin:.1%} below conservative"
                    if isinstance(margin, (int, float))
                    else "below conservative")
                items.append(
                    f"{entry.get('ticker')}: price "
                    f"{_money(entry.get('price'))} vs conservative "
                    f"{_money(rng.get('conservative'))} "
                    f"({margin_text}; was "
                    f"{entry.get('previous_band') or 'no band'}) "
                    f"— PROVISIONAL, worth a closer look.")
            sched_body.append("<h4>New strongest-alert bands</h4>")
            sched_body.append(_lines(items))
        else:
            sched_body.append(
                "<p>No new strongest-alert bands since the "
                "previous run.</p>")
        new_watch = diff.get("new_watch") or []
        if new_watch:
            sched_body.append(
                "<p>New watch bands: " + _esc(", ".join(
                    f"{e.get('ticker')} ({e.get('band')})"
                    for e in new_watch)) + "</p>")
        disappeared = diff.get("disappeared") or []
        if disappeared:
            sched_body.append(
                "<p>Bands that disappeared: " + _esc(", ".join(
                    f"{e.get('ticker')} (was "
                    f"{e.get('previous_band')})"
                    for e in disappeared)) + "</p>")
        unavailable = diff.get("newly_unavailable") or []
        if unavailable:
            sched_body.append(
                "<p>Newly unavailable: " + _esc(", ".join(
                    f"{e.get('ticker')} ({e.get('status')})"
                    for e in unavailable)) + "</p>")
        # Scan history: every dated run file, newest first.
        history_items = []
        for path in reversed(
                ss.list_runs(_scheduled_scans_dir(ui))):
            try:
                entry = json.loads(
                    path.read_text(encoding="utf-8"))
                entry_summary = entry.get("summary") or {}
                history_items.append(
                    f"{path.stem}: "
                    f"{entry_summary.get('scannable', '?')} "
                    f"scannable, "
                    f"{entry_summary.get('strongest_alerts', 0)} "
                    f"strongest-alert bands, "
                    f"{entry_summary.get('watch', 0)} watch bands")
            except (OSError, ValueError):
                history_items.append(f"{path.stem}: unreadable")
        if history_items:
            sched_body.append("<h4>Scan history</h4>")
            sched_body.append(_lines(history_items))
        sections.append(_card("Scheduled daily scan",
                              "\n".join(sched_body)))

    # -- Deployment readiness (Step 53 verdict) ----------------------
    report, readiness_error = _readiness(ui)
    if report is None and readiness_error == "computing":
        readiness_body = (
            "<p>The readiness verdict is being computed in the "
            "background (the secrets check takes a few seconds) — "
            "refresh this page and it will appear here.</p>")
    elif report is None:
        readiness_body = (
            f"<p>{_esc(readiness_error or 'Readiness unavailable.')}</p>")
    else:
        verdict = report.get("verdict") or "unknown"
        badge_cls = ("ready" if verdict == "ready_for_always_on"
                     else "not_ready")
        readiness_body = (
            f"<p>Verdict: {_badge(verdict, badge_cls)} "
            f"(generated {_esc(report.get('generated_at') or '')}). "
            "Computed once per server run, in the background — "
            "the first dashboard visit shows a placeholder while "
            "it computes.</p>")
        blockers = report.get("blockers") or []
        if blockers:
            readiness_body += (f"<p>{len(blockers)} blocker(s):</p>"
                               + _lines(blockers))
        else:
            readiness_body += ("<p>No blockers — ready for "
                               "always-on deployment.</p>")
    sections.append(_card("Deployment readiness", readiness_body))

    # -- Modules (Step 49 snapshot, as cards) ------------------------
    module_cards = []
    for name, entry in (snapshot.get("modules") or {}).items():
        last = entry.get("last_run_at") or "never"
        module_cards.append(_card(
            name,
            f"<p>{_badge(entry.get('status'), entry.get('status'))}"
            f" last run { _esc(last)}"
            f"{' (' + entry['age'] + ' ago)' if entry.get('age') and entry.get('last_run_at') else ''}"
            f"; {entry.get('records')} record(s)</p>"
            f"<p>{_esc(entry.get('detail'))}</p>"))
    sections.append(
        "<div class=\"grid\">\n" + "\n".join(module_cards) +
        "\n</div>")

    # -- Recent activity and gaps ------------------------------------
    activity = snapshot.get("recent_activity") or []
    if activity:
        activity_body = _lines([
            f"{item.get('at') or 'no date'}: {item.get('kind')} — "
            f"{item.get('summary')} [{item.get('status')}]"
            for item in activity])
    else:
        activity_body = "<p>No activity recorded yet.</p>"
    sections.append(_card(
        f"Recent activity (generated "
        f"{snapshot.get('generated_at')})", activity_body))
    gaps = snapshot.get("gaps") or []
    if gaps:
        gaps_body = _lines([
            f"{item.get('text')} ({item.get('source')})"
            for item in gaps])
    else:
        gaps_body = "<p>None recorded.</p>"
    sections.append(_card("Gaps", gaps_body))
    return _page("Dashboard", "\n".join(sections), active="dashboard")


def status_html(ui):
    """Status page: what the UI can and cannot do."""
    items = [
        "Local web interface only — this server binds 127.0.0.1 "
        "and is not a hosted service; no cloud account or key is "
        "required.",
        "Prices come from the 2026-11-03 financial_snapshots rows "
        "unless labelled otherwise — there is no live price feed.",
        "No holdings store exists: the owner's actual holdings "
        "have never been entered, so no portfolio is shown here.",
        "No alert has ever fired on real data; every alert rule "
        "is fixture-proven only.",
        "Voice is off by default — the owner prefers text replies "
        "and declined ongoing voice replies on 2026-11-03. "
        "Browser voice (Step 56, Web Speech API, Chrome/Edge) is "
        "an opt-in mode on the chat page: microphone input and "
        "spoken replies with a British male voice preferred when "
        "the browser offers one. No wake word — browsers do not "
        "allow always-on background listening — and no audio ever "
        "leaves the browser.",
        "No live Ollama turn has run through the orchestrator on "
        "this machine; model behaviour here is stub-tested.",
        "This interface contains no trading controls by design: "
        "no order is ever placed from here, and a trade request "
        "is refused until the owner approves that exact order.",
        "Statements exist for MSFT only; every other ticker is "
        "shown as insufficient data, never estimated.",
    ]
    return _page("Status",
                 _card("What this interface can and cannot do",
                       _lines(items)), active="status")


# ---------------------------------------------------------------------------
# HTTP server (localhost only)
# ---------------------------------------------------------------------------

class JarvisRequestHandler(BaseHTTPRequestHandler):
    """One handler; the JarvisUI lives on the server object."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - keep test output clean
        pass

    @property
    def ui(self):
        return self.server.ui

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The client gave up (e.g. its own timeout) before the
            # response was written — nothing to do; the handler
            # thread just ends quietly instead of logging a stack
            # trace.
            pass

    def _read_question(self):
        """The 'q' question from a JSON or form-encoded POST body.

        The chat page submits turns as JSON to ``/api/chat``;
        the plain form posts (and the research page) send
        ``application/x-www-form-urlencoded``.  A missing or
        unreadable body is an empty question, never a crash.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_POST_BYTES:
            return ""
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type") or ""
        if "application/json" in content_type:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                return ""
            if isinstance(payload, dict):
                return str(payload.get("q") or "").strip()
            return ""
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        return (form.get("q") or [""])[0].strip()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler name
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, home_html(self.ui))
        elif path.startswith("/ticker/"):
            body, status = ticker_html(
                self.ui, urllib.parse.unquote(path[len("/ticker/"):]))
            self._send(status, body)
        elif path == "/research":
            self._send(200, research_html(self.ui))
        elif path == "/chat":
            self._send(200, chat_html(self.ui))
        elif path == "/dashboard":
            self._send(200, dashboard_html(self.ui))
        elif path == "/status":
            self._send(200, status_html(self.ui))
        elif path == "/static/style.css":
            css = _project_root() / "agent" / "ui_static" / "style.css"
            if css.exists():
                self._send(200, css.read_text(encoding="utf-8"),
                           "text/css; charset=utf-8")
            else:
                self._send(404, _page("Not found",
                                      "<p>No stylesheet.</p>"))
        elif path == "/static/app.js":
            js = _project_root() / "agent" / "ui_static" / "app.js"
            if js.exists():
                self._send(200, js.read_text(encoding="utf-8"),
                           "text/javascript; charset=utf-8")
            else:
                self._send(404, _page("Not found",
                                      "<p>No script.</p>"))
        else:
            self._send(404, _page("Not found",
                                  f"<p>No page at {_esc(path)}.</p>"))

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler name
        path = urllib.parse.urlparse(self.path).path
        question = self._read_question()
        if path == "/research":
            self._send(200, research_html(self.ui, question or None))
        elif path == "/chat":
            self._send(200, chat_html(self.ui, question or None))
        elif path == "/api/chat":
            # Step 60: one chat turn as JSON, for the fetch-based
            # chat page — the same orchestrator call (with the
            # live fallback when enabled) as the form post, so
            # the two paths can never disagree about an answer.
            result = _answer_question(self.ui, question)
            payload = {
                "request": result.get("request"),
                "intent": result.get("intent"),
                "intent_method": result.get("intent_method"),
                "status": result.get("status"),
                "reply": result.get("reply"),
                "sources": result.get("sources") or [],
            }
            self._send(200, json.dumps(payload),
                       "application/json; charset=utf-8")
        else:
            self._send(404, _page("Not found",
                                  f"<p>No page at {_esc(path)}.</p>"))


def make_server(ui, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Build the localhost-only server (does not start serving).

    ``host`` must be a loopback address; anything else is refused
    so the UI can never be exposed on a network by accident.
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            f"refused: the Jarvis UI binds localhost only, not {host!r}")
    server = ThreadingHTTPServer((host, port), JarvisRequestHandler)
    server.ui = ui
    server.daemon_threads = True
    return server


def serve(ui, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Serve the UI until interrupted (localhost only)."""
    server = make_server(ui, host=host, port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def run_local(port=DEFAULT_PORT):
    """Start the UI with the project's defaults (owner's PC path).

    The real Ollama client is wired, and the Step 58 live
    fallback is on — the same configuration ``main.py`` starts
    the UI with — so the chat answers about a ticker with no
    stored statements instead of "I do not have enough data".
    """
    ui = JarvisUI(model_client=orch.OllamaModelClient(),
                  live_fallback=True)
    print(f"Jarvis UI on http://127.0.0.1:{port}/ "
          "(local only — open it in a browser)")
    serve(ui, port=port)


if __name__ == "__main__":
    run_local()
