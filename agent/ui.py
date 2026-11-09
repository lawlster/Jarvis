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
  say so — nothing is invented.
- ``/research`` — a question box that runs a Step 41 research
  session (caller-driven, read-only database) and shows the sourced
  brief.
- ``/chat`` — a text box that runs the Step 39 orchestrator and
  shows the reply with its sources labelled.
- ``/status`` — what the UI can and cannot do.

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

Not built here: live dashboards and agent monitoring (Step 49),
wiring into ``main.py`` (Step 50). No real browser session has
ever run through this UI — it is tested with a test HTTP client
against a server in a thread.
"""

import html
import os
import re
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent import orchestrator as orch
from agent import research_agents as ra
from agents import daily_summary as ds
from agents import research_report as rr
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
    """

    def __init__(self, db_path=None, store_dir=None, news_dir=None,
                 events_dir=None, alert_state_path=None,
                 model_client=None, log_dir=None):
        base = _project_root()
        self.db_path = db_path or str(base / "database" / "assistant.db")
        self.store_dir = store_dir
        self.news_dir = news_dir
        self.events_dir = events_dir
        self.alert_state_path = alert_state_path
        self.model_client = model_client
        self.log_dir = log_dir


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
    """A full HTML document with navigation. Text-first, no scripts."""
    nav = [
        ("/", "Home", "home"),
        ("/research", "Research", "research"),
        ("/chat", "Chat", "chat"),
        ("/status", "Status", "status"),
    ]
    links = " | ".join(
        f'<a href="{href}"{" class=\"active\"" if key == active else ""}>'
        f"{label}</a>"
        for href, label, key in nav
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{_esc(title)} — Jarvis</title>\n"
        "<link rel=\"stylesheet\" href=\"/static/style.css\">\n"
        "</head>\n<body>\n"
        "<header><h1>Jarvis</h1>"
        f"<nav>{links}</nav></header>\n"
        "<main>\n"
        f"<h2>{_esc(title)}</h2>\n"
        f"{body}\n"
        "</main>\n"
        "<footer><p>Research, not recommendations. Candidates are "
        "worth a closer look; the final decision is always the "
        "owner's. This interface contains no trading controls by "
        "design.</p></footer>\n"
        "</body>\n</html>\n"
    )


def _lines(items):
    if not items:
        return "<p>None recorded.</p>"
    return "<ul>\n" + "\n".join(
        f"<li>{_esc(item)}</li>" for item in items) + "\n</ul>"


def _form(action, label, placeholder, value=""):
    return (
        f"<form method=\"post\" action=\"{action}\">\n"
        f"<label for=\"q\">{_esc(label)}</label><br>\n"
        f"<input type=\"text\" id=\"q\" name=\"q\" size=\"80\" "
        f"placeholder=\"{_esc(placeholder)}\" value=\"{_esc(value)}\">\n"
        "<input type=\"submit\" value=\"Ask\">\n"
        "</form>\n"
    )


# ---------------------------------------------------------------------------
# Page builders (pure functions over a JarvisUI — testable without a server)
# ---------------------------------------------------------------------------

def home_html(ui, summary_date=None):
    """Home page: daily summary, watchlist view, active alerts."""
    sections = []
    conn = _open_read_only(ui.db_path)
    try:
        summary = ds.generate_summary(
            conn, date=summary_date, news_dir=ui.news_dir)
    finally:
        if conn is not None:
            conn.close()
    sections.append(f"<p>{_esc(summary['headline'])}</p>")
    coverage = summary.get("coverage") or {}
    sections.append(
        "<p>Coverage: "
        f"{_esc(coverage.get('selected', '?'))} tickers selected, "
        f"{_esc(coverage.get('scannable', '?'))} scannable.</p>")
    sections.append("<h3>Candidates worth a closer look</h3>")
    sections.append(_lines(summary.get("candidate_lines") or []))
    if summary.get("trap_lines"):
        sections.append("<h3>Value traps (not candidates)</h3>")
        sections.append(_lines(summary["trap_lines"]))
    sections.append("<h3>Watchlist</h3>")
    # Step 35's watchlist_view returns one dict per ticker; the
    # display line is its "note" field, not the whole dict.
    view = wf.watchlist_view(store_dir=ui.store_dir)
    notes = [entry.get("note") if isinstance(entry, dict) else str(entry)
             for entry in view]
    sections.append(_lines(notes))
    sections.append("<h3>Active alerts</h3>")
    try:
        state = wa.load_state(ui.alert_state_path)
        alerts = wa.list_active_alerts(state)
    except Exception as exc:  # noqa: BLE001 - a corrupt state file
        # must not take the home page down; say so plainly.
        alerts = []
        sections.append(
            f"<p>Alert state could not be read: {_esc(exc)}</p>")
    if alerts:
        sections.append(_lines(wa.briefing_lines(alerts)))
    else:
        sections.append("<p>No active alerts.</p>")
    sections.append("<h3>Limitations</h3>")
    sections.append(_lines(summary.get("limitations") or []))
    return _page("Daily summary", "\n".join(sections), active="home")


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
    body = _md_to_html(rr.report_to_markdown(report))
    return _page(f"Research report: {raw}", header + body), 200


def research_html(ui, question=None):
    """Research page: a question box plus the Step 41 sourced brief."""
    body = [_form("/research", "Research question",
                  "Research MSFT", value=question or "")]
    if question:
        brief = ra.run_research_session(
            question, db_path=ui.db_path, news_dir=ui.news_dir,
            events_dir=ui.events_dir, save_dir=False)
        body.append(
            f"<p>Kind: {_esc(brief.get('kind'))}. "
            f"Status: {_esc(brief.get('status'))} — "
            f"{_esc(brief.get('status_reason') or '')}</p>")
        body.append(_md_to_html(ra.brief_to_markdown(brief)))
    return _page("Research", "\n".join(body), active="research")


def chat_html(ui, request=None):
    """Chat page: a text box plus the Step 39 orchestrator reply."""
    body = [_form("/chat", "Ask Jarvis",
                  "What is the intrinsic value of MSFT?",
                  value=request or "")]
    if request:
        result = orch.handle(
            request, model_client=ui.model_client,
            db_path=ui.db_path, store_dir=ui.store_dir,
            news_dir=ui.news_dir, log_dir=ui.log_dir)
        body.append(
            f"<p>Intent: {_esc(result.get('intent'))} "
            f"({_esc(result.get('intent_method'))}). "
            f"Status: {_esc(result.get('status'))}.</p>")
        body.append(f"<p>{_esc(result.get('reply'))}</p>")
        if result.get("sources"):
            body.append("<h3>Sources</h3>")
            body.append(_lines(result["sources"]))
    return _page("Chat", "\n".join(body), active="chat")


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
        "and declined ongoing voice replies on 2026-11-03.",
        "No live Ollama turn has run through the orchestrator on "
        "this machine; model behaviour here is stub-tested.",
        "This interface contains no trading controls by design: "
        "no order is ever placed from here, and a trade request "
        "is refused until the owner approves that exact order.",
        "Statements exist for MSFT only; every other ticker is "
        "shown as insufficient data, never estimated.",
    ]
    return _page("Status", _lines(items), active="status")


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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_POST_BYTES:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw)

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
        else:
            self._send(404, _page("Not found",
                                  f"<p>No page at {_esc(path)}.</p>"))

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler name
        path = urllib.parse.urlparse(self.path).path
        form = self._read_form()
        question = (form.get("q") or [""])[0].strip()
        if path == "/research":
            self._send(200, research_html(self.ui, question or None))
        elif path == "/chat":
            self._send(200, chat_html(self.ui, question or None))
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
    """Start the UI with the project's defaults (owner's PC path)."""
    ui = JarvisUI(model_client=orch.OllamaModelClient())
    print(f"Jarvis UI on http://127.0.0.1:{port}/ "
          "(local only — open it in a browser)")
    serve(ui, port=port)


if __name__ == "__main__":
    run_local()
