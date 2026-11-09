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
  shows the reply with its sources labelled.
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
files. Voice is out of scope here: no microphone or speech
features were added.

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

from agent import dashboard as dash
from agent import deployment as dep
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
    ``data_dir`` is the Step 49 dashboard's data root (defaults
    to the project's ``data/`` directory). ``live_scan_path`` is
    the Step 54 stored live-scan JSON (defaults to
    ``step54-live-scan.json`` next to the project directory, if
    it exists); the dashboard reads it, never re-runs a scan.
    ``readiness`` is a precomputed Step 53 readiness report dict
    (tests); when None the dashboard computes it once per UI
    instance and caches it, because the secrets check takes
    seconds and a page load must not re-run it.
    """

    def __init__(self, db_path=None, store_dir=None, news_dir=None,
                 events_dir=None, alert_state_path=None,
                 model_client=None, log_dir=None, data_dir=None,
                 live_scan_path=None, readiness=None):
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
    return _page(f"Research report: {raw}", header + chart + body), 200


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
    """Chat page: a text box plus the Step 39 orchestrator reply.

    Command chips fill the box (the owner edits before asking);
    the "what is missing" notice stays visible so the stub model
    on this machine is never mistaken for a live turn.
    """
    commands = ["/research", "/watchlist", "/alerts", "/summary",
                "/memory", "/dashboard", "/ui", "/voice", "/help",
                "/quit", "/livescan"]
    chips = "".join(
        f"<button type=\"button\" class=\"cmd-chip\" "
        f"data-command=\"{_esc(cmd)}\">{_esc(cmd)}</button>"
        for cmd in commands)
    body = [
        "<div class=\"notice\"><strong>What is missing here:</strong> "
        "no live Ollama turn has run through this chat on this "
        "machine — replies come from a stub model in tests, and "
        "voice is off by default. What you type is answered from "
        "stored data and the valuation engines, never from a live "
        "price feed.</div>",
        f"<div class=\"cmd-chips\">{chips}</div>",
        _form("/chat", "Ask Jarvis",
              "What is the intrinsic value of MSFT?",
              value=request or ""),
    ]
    if request:
        result = orch.handle(
            request, model_client=ui.model_client,
            db_path=ui.db_path, store_dir=ui.store_dir,
            news_dir=ui.news_dir, log_dir=ui.log_dir)
        body.append(
            "<div class=\"chat-reply\">"
            f"<p class=\"chat-meta\">Intent: "
            f"{_esc(result.get('intent'))} "
            f"({_esc(result.get('intent_method'))}). "
            f"Status: {_esc(result.get('status'))}.</p>"
            f"<p>{_esc(result.get('reply'))}</p>"
            "</div>")
        if result.get("sources"):
            body.append("<h3>Sources</h3>")
            body.append(_lines(result["sources"]))
    return _page("Chat", "\n".join(body), active="chat")


def dashboard_html(ui):
    """Dashboard page: Step 49 status snapshot + Step 54 live scan
    + Step 53 readiness verdict.

    Module health, recent activity, and gaps, read from the logs
    and state files the other steps already write. The live-scan
    panel shows the STORED ``step54-live-scan.json`` results —
    it never re-runs a scan. The readiness panel shows the Step 53
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
        "and declined ongoing voice replies on 2026-11-03.",
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
