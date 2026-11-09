"""Step 50 — main.py integration tests.

The integrated loop is driven through :class:`main.JarvisApp`
with a stubbed model client, a temporary database copy and
temporary data directories. No network, no production writes:
``agent.memory.DB_PATH`` is monkeypatched to the temp copy, so
the last-20-messages persistence never touches the production
database. The production hash is asserted unchanged.
"""

import hashlib
import io
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent import memory as memory_mod  # noqa: E402
from agent.orchestrator import StubModelClient  # noqa: E402
import main as main_mod  # noqa: E402

EXPECTED_DB_HASH = (
    "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187")


def _db_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class MainIntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_hash_before = _db_hash(
            PROJECT_ROOT / "database" / "assistant.db")

    @classmethod
    def tearDownClass(cls):
        after = _db_hash(PROJECT_ROOT / "database" / "assistant.db")
        assert after == cls.production_hash_before, (
            "production DB changed during the integration tests")
        assert after == EXPECTED_DB_HASH

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "assistant.db"
        shutil.copy(PROJECT_ROOT / "database" / "assistant.db",
                    self.db_path)
        self.db_hash_before = _db_hash(self.db_path)
        # Memory persistence goes to the temp copy, never the
        # production database.
        self._old_db_path = memory_mod.DB_PATH
        memory_mod.DB_PATH = self.db_path
        from data.providers import provider_logger as _pl
        self._old_pl_db_path = _pl.DB_PATH
        self.log_dir = self.root / "orchestration"
        self.memory_dir = self.root / "memory"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.sessions_dir = self.root / "research_sessions"
        self.stub = StubModelClient(reply="Stub chat reply.")
        self.app = main_mod.JarvisApp(
            db_path=self.db_path, model_client=self.stub,
            log_dir=self.log_dir, store_dir=self.root / "watchlist",
            news_dir=self.root / "news",
            events_dir=self.root / "events",
            alert_state_path=self.root / "alerts" / "alert_state.json",
            memory_dir=self.memory_dir, data_dir=self.data_dir,
            research_save_dir=self.sessions_dir,
            search_function=lambda query: "stub search result",
            financials_function=lambda ticker: "stub financials")
        with redirect_stdout(io.StringIO()):
            self.app.start()

    def tearDown(self):
        self.app.stop_ui()
        memory_mod.DB_PATH = self._old_db_path
        # Step 52: JarvisApp also points provider logging at the
        # session database; restore that too.
        from data.providers import provider_logger as _pl
        _pl.DB_PATH = self._old_pl_db_path
        self.tmp.cleanup()

    # -- the loop delegates to the orchestrator ----------------------

    def test_valuation_question_traces_to_step_21_23(self):
        reply = self.app.ask("What is the intrinsic value of MSFT?")
        self.assertIn("163.30", reply)
        self.assertIn("overvalued", reply.lower())
        self.assertIn("78.82", reply)  # strongest-alert price
        self.assertIn("Sources:", reply)

    def test_trade_request_refused_and_model_never_called(self):
        calls_before = len(self.stub.calls)
        reply = self.app.ask("Buy 100 shares of MSFT at $500")
        self.assertIn("will not place a trade", reply)
        self.assertEqual(len(self.stub.calls), calls_before)

    def test_model_unavailable_is_structured_not_a_crash(self):
        self.app.model_client = StubModelClient(raise_unavailable=True)
        self.app.orchestrator.model_client = self.app.model_client
        reply = self.app.ask("Hello there")
        self.assertIn("unreachable", reply.lower())

    def test_calculate_tool_runs_through_the_step_44_loop(self):
        reply = self.app.ask("calculate 2 + 3")
        self.assertIn("5", reply)
        self.assertIn("tool (calculate)", reply)

    def test_conversation_history_reaches_the_model(self):
        self.app.ask("Hello, I am testing")
        self.app.ask("What did I just say?")
        last_call = self.stub.calls[-1]
        contents = " ".join(m.get("content", "") for m in last_call)
        self.assertIn("Hello, I am testing", contents)
        self.assertIn("Stub chat reply.", contents)

    def test_empty_input_returns_empty(self):
        self.assertEqual(self.app.ask("   "), "")

    # -- slash commands ----------------------------------------------

    def test_watchlist_command_empty_store(self):
        self.assertIn("watchlist is empty",
                      self.app.ask("/watchlist").lower())

    def test_watchlist_command_real_store(self):
        app = main_mod.JarvisApp(
            db_path=self.db_path, model_client=self.stub,
            log_dir=self.log_dir,
            store_dir=PROJECT_ROOT / "data" / "watchlist",
            news_dir=self.root / "news",
            memory_dir=self.memory_dir, data_dir=self.data_dir,
            search_function=lambda query: "stub search result",
            financials_function=lambda ticker: "stub financials")
        with redirect_stdout(io.StringIO()):
            app.start()
        reply = app.ask("/watchlist")
        self.assertIn("MSFT", reply)
        self.assertIn("overvalued", reply.lower())

    def test_alerts_command_no_active_alerts(self):
        self.assertIn("No active alerts", self.app.ask("/alerts"))

    def test_summary_command(self):
        reply = self.app.ask("/summary")
        self.assertIn("Daily market summary", reply)
        self.assertIn("10619", reply)

    def test_memory_command_on_request_only(self):
        reply = self.app.ask("/memory MSFT")
        self.assertIn("No long-term memory records match", reply)

    def test_memory_command_needs_a_question(self):
        self.assertIn("Usage: /memory", self.app.ask("/memory"))

    def test_dashboard_command(self):
        reply = self.app.ask("/dashboard")
        self.assertIn("no_data", reply)
        self.assertIn("undervalued_detection", reply)

    def test_research_command(self):
        reply = self.app.ask("/research MSFT")
        self.assertIn("Research: MSFT", reply)
        self.assertIn("163.30", reply)

    def test_help_and_unknown_command(self):
        self.assertIn("/research", self.app.ask("/help"))
        self.assertIn("Unknown command", self.app.ask("/frobnicate"))

    def test_commands_are_logged_in_the_turn_log(self):
        self.app.ask("/watchlist")
        self.app.ask("What is the intrinsic value of MSFT?")
        log_file = self.log_dir / "turns.jsonl"
        records = [json.loads(line) for line in
                   log_file.read_text(encoding="utf-8").splitlines()]
        routes = [record["route"] for record in records]
        self.assertIn("command_watchlist", routes)
        self.assertIn("valuation", routes)

    # -- voice gate ---------------------------------------------------

    def test_voice_command_explains_off_by_default(self):
        reply = self.app.ask("/voice")
        self.assertIn("off by default", reply)
        self.assertIn("never", reply)
        self.assertFalse(self.app.voice_armed)

    def test_voice_yes_arms_next_turn_only_and_preference_gates_it(self):
        self.assertIn("armed", self.app.ask("/voice yes"))
        self.assertTrue(self.app.voice_armed)
        reply = self.app.ask("What is the intrinsic value of MSFT?")
        self.assertIn("163.30", reply)
        self.assertIn("Spoken reply: no spoken reply: the owner "
                      "prefers text replies", reply)
        self.assertFalse(self.app.voice_armed)

    # -- UI on demand -------------------------------------------------

    def test_ui_starts_on_localhost_only_and_stops(self):
        port = _free_port()
        app = main_mod.JarvisApp(
            db_path=self.db_path, model_client=self.stub,
            log_dir=self.log_dir, memory_dir=self.memory_dir,
            data_dir=self.data_dir, ui_port=port,
            search_function=lambda query: "stub search result",
            financials_function=lambda ticker: "stub financials")
        with redirect_stdout(io.StringIO()):
            app.start()
        try:
            reply = app.ask("/ui")
            self.assertIn(f"http://127.0.0.1:{port}/", reply)
            self.assertIn("already running", app.ask("/ui"))
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=10) as resp:
                body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("Daily summary", body)
        finally:
            app.stop_ui()

    # -- banner and the old loop --------------------------------------

    def test_banner_lists_the_limitations(self):
        banner = "\n".join(self.app.banner_lines())
        self.assertIn("AiAssistant v0.7", banner)
        self.assertIn("Cloud models: not configured", banner)
        self.assertIn("Live price feed: none", banner)
        self.assertIn("Holdings: none entered", banner)
        self.assertIn("Remote computer: not configured", banner)
        self.assertIn("Voice: off by default", banner)
        self.assertIn("latest snapshot 2026-11-03", banner)

    def test_old_inline_tool_loop_is_gone(self):
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("def execute_tool", source)
        self.assertNotIn("def ask_ai", source)
        self.assertNotIn("TOOLS = [", source)
        self.assertNotIn('"name": "calculate_dcf"', source)
        self.assertIn("Orchestrator", source)
        self.assertIn("ToolCaller", source)
        self.assertIn("build_router", source)

    def test_scripted_run_exits_on_quit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.app.run(input_lines=[
                "What is the intrinsic value of MSFT?",
                "Buy 100 shares of MSFT at $500",
                "/dashboard",
                "/quit",
            ])
        text = out.getvalue()
        self.assertIn("163.30", text)
        self.assertIn("will not place a trade", text)
        self.assertIn("Goodbye!", text)

    def test_temp_db_unchanged_by_read_only_turns(self):
        self.app.ask("What is the intrinsic value of MSFT?")
        self.app.ask("/summary")
        # Memory writes go to the temp copy by design; the
        # financial content must be untouched.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM market_universe").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 10619)


class MainCliTestCase(unittest.TestCase):
    def test_help_flag_works(self):
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("AiAssistant", result.stdout)
        self.assertIn("--ui", result.stdout)


if __name__ == "__main__":
    unittest.main()
