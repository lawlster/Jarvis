"""AiAssistant v0.7 — integrated entry point (Step 50).

The interactive loop now delegates to the Step 39 orchestrator
as the single request pipeline, instead of owning its own
Ollama tool-calling loop:

- Model: the Step 40 :class:`agent.cloud_models.ModelRouter`
  (local Ollama first; cloud only as a configured fallback).
  With no cloud config — the normal case — it is local only.
- Tools: the Step 44 registry (:func:`agent.tool_calling.
  default_registry`) and call loop, which wrap the four tools
  this file used to define inline (calculate, search_web,
  get_company_financials, calculate_dcf) plus the Step 16-38
  engines as read-only tools. The old inline ``TOOLS`` list,
  ``execute_tool`` and ``ask_ai`` are gone; there is one tool
  loop, not two.
- Memory: the last 20 messages, still persisted in the
  production database by :mod:`agent.memory` (the one place
  this file writes to the database — everything else opens it
  read-only), and now also sent to the model with each turn so
  a chat turn is not amnesiac.
- Long-term memory (Step 43) is available on request only,
  via ``/memory`` — it is not pulled into every turn.
- Voice (Step 45) is off by default: the owner prefers text
  replies. ``/voice`` only arms a spoken reply for the next
  turn, and the stored preference still gates it.
- UI (Step 48) starts on demand only (``--ui`` flag or the
  ``/ui`` command), on localhost, never automatically.

Slash commands (all recorded in the Step 39 turn log):

- ``/research <question>`` — Step 41 research session
- ``/watchlist`` — Step 35 watchlist view
- ``/alerts`` — Step 38 active alerts
- ``/summary`` — Step 32 daily summary for today
- ``/memory <question>`` — Step 43 context block, on request
- ``/dashboard`` — Step 49 status snapshot as text
- ``/ui`` — start the Step 48 web UI on localhost:8765
- ``/voice`` — request a spoken reply for the next turn only
- ``/help`` — list the commands; ``/quit`` — exit

Guardrails (unchanged): a trade request is refused before
anything acts on it; the tool registry refuses trade/send/
schedule tools at registration; no scheduled job in this file
can place a trade; nothing here sends a message or connects
to a remote machine. Candidates are worth a closer look,
never buy recommendations; the final decision is the owner's.
"""

import argparse
import sqlite3
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "assistant.db"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_UI_PORT = 8765

from agent.memory import (
    initialise_memory,
    load_recent_messages,
    save_message,
)
from agent import orchestrator as orch
from agent.cloud_models import build_router, load_config
from agent.tool_calling import ToolCaller, default_registry
from agent.long_term_memory import build_context
from agent.research_agents import run_research_session
from agent import dashboard as dash
from agent import voice as voice_mod
from agent.ui import JarvisUI, make_server
from agents.daily_summary import generate_summary
from agents.watchlist_alerts import (
    briefing_lines,
    list_active_alerts,
    load_state as load_alert_state,
)
from agents.watchlist_foundation import watchlist_view

COMMANDS = (
    ("/research <question>", "Step 41 research session (sourced brief)"),
    ("/watchlist", "Step 35 watchlist view (one line per ticker)"),
    ("/alerts", "Step 38 active alerts"),
    ("/summary", "Step 32 daily summary for today"),
    ("/memory <question>", "Step 43 context block, on request only"),
    ("/dashboard", "Step 49 status snapshot as text"),
    ("/ui", "Start the Step 48 web UI on localhost:8765"),
    ("/voice", "Request a spoken reply for the next turn only"),
    ("/help", "List these commands"),
    ("/quit", "Exit"),
)


def _help_text():
    lines = ["Commands:"]
    for name, description in COMMANDS:
        lines.append(f"  {name} — {description}")
    lines.append(
        "Anything else is a request to the assistant "
        "(valuation, research, chat).")
    return "\n".join(lines)


class JarvisApp:
    """One integrated assistant session.

    ``db_path`` defaults to the project's database; everything
    except :mod:`agent.memory` opens it read-only.
    ``model_client`` defaults to the Step 40 router (local
    Ollama first, cloud only as a configured fallback).
    Directory arguments default to the project's ``data/``
    layout; tests pass temporary directories. ``log_dir=False``
    disables the Step 39 turn log (tests); ``None`` keeps the
    default log location.
    """

    def __init__(self, db_path=None, model_client=None, log_dir=None,
                 store_dir=None, news_dir=None, events_dir=None,
                 alert_state_path=None, memory_dir=None, data_dir=None,
                 research_save_dir=None, ui_port=DEFAULT_UI_PORT,
                 search_function=None, financials_function=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.store_dir = store_dir
        self.news_dir = news_dir
        self.events_dir = events_dir
        self.alert_state_path = alert_state_path
        # Long-term memory defaults to the project's store, so
        # the owner's recorded text preference is actually read.
        self.memory_dir = (
            memory_dir if memory_dir is not None
            else str(self.data_dir / "memory"))
        self.research_save_dir = research_save_dir
        self.ui_port = ui_port
        self.log_dir = log_dir

        self.model_client = model_client or build_router(load_config())
        self.registry = default_registry(
            db_path=str(self.db_path), store_dir=store_dir,
            news_dir=news_dir, events_dir=events_dir,
            search_function=search_function,
            financials_function=financials_function)
        self.tool_caller = ToolCaller(
            registry=self.registry, model_client=self.model_client,
            db_path=str(self.db_path))
        self.orchestrator = orch.Orchestrator(
            model_client=self.model_client, db_path=str(self.db_path),
            store_dir=store_dir, news_dir=news_dir, log_dir=log_dir,
            tool_caller=self.tool_caller)

        self.conversation_history = []
        self.voice_armed = False
        self.ui_server = None
        self.ui_thread = None

    # -- startup ----------------------------------------------------

    def _open_read_only(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def _latest_snapshot_date(self):
        """Latest price-snapshot date, or None when unknown."""
        try:
            conn = self._open_read_only()
            try:
                row = conn.execute(
                    "SELECT MAX(snapshot_date) FROM financial_snapshots"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if not row or not row[0]:
            return None
        return str(row[0])[:10]

    def banner_lines(self):
        """Startup banner, including what is missing — stated,
        never hidden and never faked."""
        config = load_config()
        cloud = [p for p in config.get("providers", [])
                 if (p.get("kind") or "cloud").lower() == "cloud"]
        snapshot = self._latest_snapshot_date() or "unknown"
        holdings_dir = self.data_dir / "holdings"
        preference = voice_mod.get_voice_preference(self.memory_dir)
        return [
            "=" * 45,
            "          AiAssistant v0.7",
            "=" * 45,
            "Persistent memory: Active (last 20 messages)",
            "Model: local Ollama qwen3:8b (checked on first use)",
            "Cloud models: " + (
                "not configured (local Ollama only)" if not cloud
                else f"{len(cloud)} provider(s) configured"),
            f"Live price feed: none (latest snapshot {snapshot})",
            "Holdings: " + (
                "none entered (portfolio answers need your holdings)"
                if not holdings_dir.exists()
                else f"store found at {holdings_dir}"),
            "Remote computer: not configured (no Windows control)",
            "Voice: off by default "
            f"(text replies; stored preference '{preference}')",
            "Commands: /research /watchlist /alerts /summary "
            "/memory /dashboard /ui /voice /help /quit",
            "Type 'exit' to end.",
        ]

    def start(self):
        """Initialise memory, load history, print the banner."""
        initialise_memory()
        self.conversation_history = load_recent_messages(20)
        for line in self.banner_lines():
            print(line)
        print()

    # -- one turn ---------------------------------------------------

    def _remember(self, user_text, assistant_text):
        self.conversation_history.append(
            {"role": "user", "content": user_text})
        self.conversation_history.append(
            {"role": "assistant", "content": assistant_text})
        self.conversation_history = self.conversation_history[-20:]
        save_message("user", user_text)
        save_message("assistant", assistant_text)

    def _log_command(self, name, argument, status, reply,
                     modules_called, sources):
        """Record a slash command in the Step 39 turn log."""
        record = {
            "request": f"/{name} {argument}".strip(),
            "intent": "command",
            "intent_method": "keyword",
            "route": f"command_{name}",
            "status": status,
            "reply": reply,
            "modules_called": modules_called,
            "sources": sources,
            "checked_at": orch._utcnow_iso(),
        }
        if self.log_dir is not False:
            record["log_file"] = orch.log_turn(
                record, log_dir=self.log_dir)
        return record

    def ask(self, user_input):
        """Handle one line of input. Returns the reply text."""
        text = (user_input or "").strip()
        if not text:
            return ""
        if text.startswith("/"):
            return self._run_command(text)
        if self.voice_armed:
            self.voice_armed = False
            result = voice_mod.voice_turn(
                transcript=text, model_client=self.model_client,
                db_path=str(self.db_path), store_dir=self.store_dir,
                news_dir=self.news_dir, log_dir=self.log_dir,
                speak_reply=True, memory_dir=self.memory_dir)
            reply = result["reply"]
            reason = result.get("spoken_reply_reason")
            if reason:
                reply = f"{reply}\n(Spoken reply: {reason})"
            self._remember(text, result["reply"])
            return reply
        result = self.orchestrator.handle(
            text, history=list(self.conversation_history))
        reply = result["reply"]
        sources = result.get("sources") or []
        if sources:
            reply = f"{reply}\nSources: {'; '.join(sources)}"
        self._remember(text, result["reply"])
        return reply

    # -- slash commands ----------------------------------------------

    def _run_command(self, line):
        parts = line[1:].split(None, 1)
        name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        handler = {
            "research": self._cmd_research,
            "watchlist": self._cmd_watchlist,
            "alerts": self._cmd_alerts,
            "summary": self._cmd_summary,
            "memory": self._cmd_memory,
            "dashboard": self._cmd_dashboard,
            "ui": self._cmd_ui,
            "voice": self._cmd_voice,
            "help": self._cmd_help,
        }.get(name)
        if handler is None:
            reply = ("Unknown command. " + _help_text())
            self._log_command(name or "unknown", argument, "unavailable",
                              reply, [], [])
            return reply
        reply, status, modules, sources = handler(argument)
        self._log_command(name, argument, status, reply, modules, sources)
        return reply

    def _cmd_research(self, argument):
        if not argument:
            return ("Usage: /research <question> — for example "
                    "'/research MSFT'.", "unavailable",
                    [], ["agent/research_agents.py"])
        brief = run_research_session(
            argument, db_path=str(self.db_path),
            save_dir=self.research_save_dir)
        lines = [f"Research: {brief.get('question', argument)}",
                 f"Status: {brief.get('status', 'unknown')}"]
        for claim in brief.get("claims") or []:
            lines.append(
                f"- {claim.get('text', '')} "
                f"[{claim.get('source', 'unknown')}]")
        for item in brief.get("what_would_change_view") or []:
            lines.append(f"Would change the view: {item}")
        for item in brief.get("limitations") or []:
            lines.append(f"Limitation: {item}")
        return ("\n".join(lines), brief.get("status", "ok"),
                ["agent/research_agents.py"],
                ["agent/research_agents.py::run_research_session"])

    def _cmd_watchlist(self, _argument):
        lines = watchlist_view(store_dir=self.store_dir)
        notes = [entry.get("note") if isinstance(entry, dict) else str(entry)
                 for entry in lines]
        if not notes:
            return ("The watchlist is empty.", "ok",
                    ["agents/watchlist_foundation.py"],
                    ["agents/watchlist_foundation.py::watchlist_view"])
        return ("Watchlist:\n" + "\n".join(f"- {line}" for line in notes),
                "ok", ["agents/watchlist_foundation.py"],
                ["agents/watchlist_foundation.py::watchlist_view"])

    def _cmd_alerts(self, _argument):
        try:
            state = load_alert_state(self.alert_state_path)
            alerts = list_active_alerts(state)
        except Exception as exc:  # noqa: BLE001 - a corrupt state
            # file must not take the loop down; say so plainly.
            return (f"Alert state could not be read: {exc}",
                    "unavailable", ["agents/watchlist_alerts.py"],
                    ["agents/watchlist_alerts.py::load_state"])
        if not alerts:
            return ("No active alerts.", "ok",
                    ["agents/watchlist_alerts.py"],
                    ["agents/watchlist_alerts.py::list_active_alerts"])
        return ("Active alerts:\n"
                + "\n".join(f"- {line}"
                            for line in briefing_lines(alerts)),
                "ok", ["agents/watchlist_alerts.py"],
                ["agents/watchlist_alerts.py::briefing_lines"])

    def _cmd_summary(self, _argument):
        conn = self._open_read_only()
        try:
            summary = generate_summary(conn, news_dir=self.news_dir)
        finally:
            conn.close()
        lines = [summary["headline"]]
        coverage = summary.get("coverage") or {}
        lines.append(
            f"Coverage: {coverage.get('selected', '?')} tickers "
            f"selected, {coverage.get('scannable', '?')} scannable.")
        lines.extend(summary.get("candidate_lines") or [])
        lines.extend(summary.get("limitations") or [])
        return ("\n".join(lines), "ok",
                ["agents/daily_summary.py"],
                ["agents/daily_summary.py::generate_summary"])

    def _cmd_memory(self, argument):
        if not argument:
            return ("Usage: /memory <question> — long-term memory "
                    "is read on request only.", "unavailable",
                    [], ["agent/long_term_memory.py"])
        context = build_context(
            question=argument, memory_dir=self.memory_dir)
        if not context.get("block"):
            return ("No long-term memory records match that "
                    "question.", "ok",
                    ["agent/long_term_memory.py"],
                    ["agent/long_term_memory.py::build_context"])
        return (context["block"], "ok",
                ["agent/long_term_memory.py"],
                ["agent/long_term_memory.py::build_context"])

    def _cmd_dashboard(self, _argument):
        snapshot = dash.collect_status(data_dir=self.data_dir)
        return ("\n".join(dash.summary_lines(snapshot)), "ok",
                ["agent/dashboard.py"],
                ["agent/dashboard.py::collect_status"])

    def _start_ui(self):
        if self.ui_server is not None:
            return (f"UI already running at "
                    f"http://127.0.0.1:{self.ui_port}/")
        ui = JarvisUI(
            db_path=str(self.db_path), store_dir=self.store_dir,
            news_dir=self.news_dir, events_dir=self.events_dir,
            alert_state_path=self.alert_state_path,
            model_client=self.model_client, log_dir=self.log_dir,
            data_dir=self.data_dir)
        server = make_server(ui, host="127.0.0.1", port=self.ui_port)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        self.ui_server = server
        self.ui_thread = thread
        return (f"Jarvis UI on http://127.0.0.1:{self.ui_port}/ "
                "(local only — open it in a browser)")

    def _cmd_ui(self, _argument):
        return (self._start_ui(), "ok", ["agent/ui.py"],
                ["agent/ui.py::make_server"])

    def stop_ui(self):
        """Stop a UI server started by :meth:`_start_ui`."""
        if self.ui_server is not None:
            self.ui_server.shutdown()
            self.ui_server.server_close()
            self.ui_server = None
            self.ui_thread = None

    def _cmd_voice(self, argument):
        preference = voice_mod.get_voice_preference(self.memory_dir)
        if argument.lower() == "yes":
            self.voice_armed = True
            return ("Spoken reply armed for your next turn only. "
                    f"Note: the stored voice preference is "
                    f"'{preference}', so the next turn will still "
                    "report why no spoken reply was produced unless "
                    "that preference changes. This command never "
                    "changes the stored preference.",
                    "ok", ["agent/voice.py"],
                    ["agent/voice.py::get_voice_preference"])
        return ("Voice replies are off by default — the stored "
                f"preference is '{preference}' (text replies). "
                "Reply '/voice yes' to request a spoken reply for "
                "your next turn only. A spoken reply still needs "
                "the stored preference to allow it and a "
                "text-to-speech client configured; this command "
                "never changes the stored preference.",
                "ok", ["agent/voice.py"],
                ["agent/voice.py::get_voice_preference"])

    def _cmd_help(self, _argument):
        return (_help_text(), "ok", [], [])

    # -- the loop -----------------------------------------------------

    def run(self, input_lines=None, start_ui=False):
        """Run the interactive loop.

        ``input_lines`` is a scripted list of lines (tests and
        the fixture example); ``None`` reads from stdin.
        ``start_ui`` starts the Step 48 UI in the background
        (the ``--ui`` flag); it is never started automatically.
        """
        self.start()
        if start_ui:
            print(self._start_ui())
            print()
        if input_lines is None:
            self._run_stdin()
        else:
            for line in input_lines:
                print(f"You: {line}")
                if line.strip().lower() in ("exit", "quit", "/quit"):
                    print("AiAssistant: Goodbye!")
                    break
                if not line.strip():
                    continue
                print("\nAiAssistant is thinking...\n")
                print(f"AiAssistant: {self.ask(line)}\n")
        self.stop_ui()

    def _run_stdin(self):
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAiAssistant: Goodbye!")
                break
            if user_input.lower() in ("exit", "quit", "/quit"):
                print("AiAssistant: Goodbye!")
                break
            if not user_input:
                continue
            print("\nAiAssistant is thinking...\n")
            print(f"AiAssistant: {self.ask(user_input)}\n")


def main(argv=None):
    """Entry point: ``python main.py [--ui] [--db-path PATH]``."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "AiAssistant v0.7 — personal AI assistant. Local "
            "Ollama first, cloud only as a configured fallback; "
            "financial engines open the database read-only; "
            "no trade is ever placed without the owner's explicit "
            "approval for that exact order."))
    parser.add_argument(
        "--ui", action="store_true",
        help="Start the local web UI (http://127.0.0.1:8765/) "
             "in the background (on demand, never automatic).")
    parser.add_argument(
        "--db-path", default=None,
        help="SQLite database path (default database/assistant.db).")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_UI_PORT,
        help="UI port (default 8765).")
    args = parser.parse_args(argv)
    app = JarvisApp(db_path=args.db_path, ui_port=args.port)
    app.run(start_ui=args.ui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
