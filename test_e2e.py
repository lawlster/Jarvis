"""Step 51 — complete end-to-end testing.

This is NOT another unit-test file for one module: it is the
end-to-end pass over the whole integrated assistant. Every
scenario drives the INTEGRATED ``main.py`` (``JarvisApp``) from
typed input to observable outcome, starting from a fresh
temporary database copy and temporary data directories:

(a) a valuation question, typed in, answered with numbers that
    trace to the Step 21/23 engines;
(b) a research session (``/research MSFT``), command to brief
    file;
(c) a watchlist + alerts + summary + dashboard round trip,
    asserting the outputs agree with each other;
(d) a trade request, refused end to end, with no model call and
    no state change;
(e) a scheduled job end to end (fake clock, run log, stub
    delivery receipt);
(f) the UI end to end (localhost server, same numbers as the
    CLI path);
(g) a voice turn end to end (transcript in, reply out, spoken
    reply refused by default);
(h) model unavailable (no Ollama) — structured reply, and the
    session continues afterwards;
(i) the startup banner and ``--help`` (including a real
    subprocess run of ``main.py`` with a temp database);
(j) the audit open item O5 — the legacy valuation monitor
    still reports "no profile" rather than crashing;
(k) the two open decisions re-checked, not changed: the Step 23
    exact-20% boundary float edge, and the Step 27 "warning"
    calibration for MSFT.

What the stub covers, and what only a live run on the owner's
PC can prove: there is no Ollama on this machine, so every
model turn here uses ``StubModelClient``. The stub covers the
routing, the guardrails, the tool loop, the commands, the UI,
the scheduler and the voice gate — everything except the model
itself. Only a live run on the owner's PC with Ollama running
can prove that a real model answers through the integrated
loop, that the chat route sounds right, and that the
model-fallback selection in Step 44 picks the right tool.

No network, no production writes: ``agent.memory.DB_PATH`` is
monkeypatched to the temp copy in every scenario (the one place
``main.py`` writes to a database), and the production hash is
asserted unchanged. The only deliberate exception is the legacy
valuation monitor (j): it hardcodes the production database
path — the table already exists with 0 rows, so its
``CREATE TABLE IF NOT EXISTS`` is a no-op and its read is a
plain SELECT; the test asserts the production hash before and
after to prove it. That hardcoded path is a finding, not
something this step fixes.
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent import memory as memory_mod  # noqa: E402
from agent import long_term_memory as ltm  # noqa: E402
from agent import voice as voice_mod  # noqa: E402
from agent.orchestrator import StubModelClient  # noqa: E402
from agent.scheduler import (  # noqa: E402
    Scheduler,
    StubDelivery,
    builtin_actions,
    create_reminder,
    due_jobs,
    load_jobs,
    read_run_log,
    run_due_jobs,
)
from agents.deterioration import assess_deterioration  # noqa: E402
from agents.margin_of_safety import (  # noqa: E402
    classify_tier,
    margin_of_safety,
    margin_of_safety_summary,
)
from agents.valuation_monitor import (  # noqa: E402
    monitor_company_valuation,
)
import main as main_mod  # noqa: E402

EXPECTED_DB_HASH = (
    "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187")

# Small real data subdirectories copied (read-only source) into
# each scenario's temp data dir. Deliberately NOT the whole
# data/ tree: data/market_data is ~1.3 GB and
# data/financial_statements ~66 MB; the dashboard and the
# watchlist only read the small stores listed here.
DATA_SUBDIRS = ("watchlist", "memory", "news", "events", "macro",
                "monitoring")

FAKE_NOW = datetime(2026, 11, 9, 9, 36, tzinfo=ZoneInfo("Asia/Singapore"))
FAKE_CREATED = datetime(2026, 11, 8, 9, 0,
                        tzinfo=ZoneInfo("Asia/Singapore"))


def _db_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _file_hashes(root):
    """{relative path: sha256} for every file under ``root``."""
    root = Path(root)
    out = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


def _logical_counts(db_path):
    """Row counts for the tables the read-only paths must not
    change. The conversations table is excluded on purpose:
    ``main.py`` writes memory rows to the temp copy by design."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            "market_universe": conn.execute(
                "SELECT COUNT(*) FROM market_universe").fetchone()[0],
            "financial_snapshots": conn.execute(
                "SELECT COUNT(*) FROM financial_snapshots").fetchone()[0],
            "valuation_profiles": conn.execute(
                "SELECT COUNT(*) FROM valuation_profiles").fetchone()[0],
        }
    finally:
        conn.close()


def _boom(**_kwargs):
    raise RuntimeError("boom for the E2E failure path")


class EndToEndTestCase(unittest.TestCase):
    """One fresh integrated assistant per scenario."""

    @classmethod
    def setUpClass(cls):
        cls.production_hash_before = _db_hash(
            PROJECT_ROOT / "database" / "assistant.db")
        # The older _temp_db_copy helpers (first noted Step 27)
        # never delete their copies. This harness must add none:
        # it uses TemporaryDirectory only. Snapshot the names
        # here; tearDownClass asserts the set is unchanged.
        cls.tmp_db_before = {
            p.name for p in Path("/tmp").glob("tmp*.db")}

    @classmethod
    def tearDownClass(cls):
        after = _db_hash(PROJECT_ROOT / "database" / "assistant.db")
        assert after == cls.production_hash_before, (
            "production DB changed during the end-to-end tests")
        assert after == EXPECTED_DB_HASH
        tmp_db_after = {p.name for p in Path("/tmp").glob("tmp*.db")}
        assert tmp_db_after == cls.tmp_db_before, (
            "this harness left tmp*.db residue in /tmp: "
            f"{sorted(tmp_db_after - cls.tmp_db_before)}")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "assistant.db"
        shutil.copy(PROJECT_ROOT / "database" / "assistant.db",
                    self.db_path)
        self.counts_before = _logical_counts(self.db_path)
        self._old_db_path = memory_mod.DB_PATH
        memory_mod.DB_PATH = self.db_path
        from data.providers import provider_logger as _pl
        self._old_pl_db_path = _pl.DB_PATH

        # Temp data dir: copies of the small real stores.
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        for name in DATA_SUBDIRS:
            source = PROJECT_ROOT / "data" / name
            if source.exists():
                shutil.copytree(source, self.data_dir / name)
        self.data_hashes_before = _file_hashes(self.data_dir)

        self.log_dir = self.data_dir / "orchestration"
        self.sessions_dir = self.root / "research_sessions"
        self.scheduler_dir = self.root / "scheduler"
        self.stub = StubModelClient(reply="Stub chat reply.")
        self.app = main_mod.JarvisApp(
            db_path=self.db_path, model_client=self.stub,
            log_dir=self.log_dir,
            store_dir=self.data_dir / "watchlist",
            news_dir=self.data_dir / "news",
            events_dir=self.data_dir / "events",
            alert_state_path=self.data_dir / "alerts" / "alert_state.json",
            memory_dir=self.data_dir / "memory",
            data_dir=self.data_dir,
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
        # The harness must clean up after itself: the whole
        # scenario directory is gone when the test ends.
        self.assertFalse(self.root.exists(),
                         "scenario temp directory was not cleaned up")

    # -- helpers ------------------------------------------------------

    def _turn_log(self):
        path = self.log_dir / "turns.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def _valuation_reply(self):
        return self.app.ask("What is the intrinsic value of MSFT?")

    # -- (a) valuation question, typed in, answered -----------------

    def test_a_valuation_question_typed_to_printed_reply(self):
        reply = self._valuation_reply()
        # The numbers trace to the Step 21 DCF range and the
        # Step 23 summary: conservative/base/optimistic, tier,
        # alert price, quality, deterioration.
        for expected in ("98.52", "163.30", "234.64", "78.82",
                         "overvalued", "excellent"):
            self.assertIn(expected, reply,
                          f"valuation reply is missing {expected!r}: "
                          f"{reply[:300]}")
        self.assertIn("Sources:", reply)
        self.assertIn("margin_of_safety", reply)
        # The turn is in the log, and the read-only paths left
        # the temp database's financial content untouched.
        routes = [record["route"] for record in self._turn_log()]
        self.assertIn("valuation", routes)
        self.assertEqual(_logical_counts(self.db_path),
                         self.counts_before)

    def test_a2_scripted_session_end_to_end(self):
        """The whole loop, scripted: valuation, watchlist, a
        refused trade, dashboard, quit — printed output."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.app.run(input_lines=[
                "What is the intrinsic value of MSFT?",
                "/watchlist",
                "Buy 100 shares of MSFT at $500",
                "/dashboard",
                "/quit",
            ])
        text = out.getvalue()
        self.assertIn("163.30", text)
        self.assertIn("MSFT", text)
        self.assertIn("will not place a trade", text)
        self.assertIn("watchlist", text.lower())
        self.assertIn("Goodbye!", text)
        routes = [record["route"] for record in self._turn_log()]
        self.assertIn("valuation", routes)
        self.assertIn("command_watchlist", routes)
        self.assertIn("guardrail_trade", routes)
        self.assertIn("command_dashboard", routes)

    # -- (b) research session, command to brief file ----------------

    def test_b_research_session_command_to_brief_file(self):
        real_sessions = PROJECT_ROOT / "data" / "research_sessions"
        real_before = _file_hashes(real_sessions)
        reply = self.app.ask("/research MSFT")
        self.assertIn("Research: MSFT", reply)
        self.assertIn("163.30", reply)
        saved = sorted(self.sessions_dir.rglob("*"))
        saved_files = [p for p in saved if p.is_file()]
        self.assertGreaterEqual(len(saved_files), 2,
                                "no brief files were saved")
        brief_json = next(
            (p for p in saved_files if p.suffix == ".json"), None)
        self.assertIsNotNone(brief_json, "no JSON brief was saved")
        brief = json.loads(brief_json.read_text(encoding="utf-8"))
        self.assertEqual(brief.get("status"), "partial")
        claims = brief.get("claims") or []
        self.assertGreater(len(claims), 0, "the brief has no claims")
        # Every claim names the source module that produced it —
        # nothing in the brief is unsourced.
        for claim in claims:
            self.assertTrue((claim.get("source") or "").strip(),
                            f"unsourced claim in the brief: {claim}")
        self.assertTrue((brief.get("limitations") or []),
                        "the brief lists no limitations")
        # The brief landed in the temp directory only: nothing
        # new appeared under the project's real data/.
        self.assertEqual(_file_hashes(real_sessions), real_before)

    # -- (c) watchlist + alerts + summary + dashboard agree --------

    def test_c_round_trip_outputs_agree(self):
        watchlist = self.app.ask("/watchlist")
        lines = [line for line in watchlist.splitlines()
                 if line.startswith("- ")]
        self.assertEqual(len(lines), 38,
                         f"expected the seeded 38-ticker watchlist, "
                         f"got {len(lines)} lines")
        msft_line = next(line for line in lines if "MSFT" in line)
        self.assertIn("overvalued", msft_line.lower())
        self.assertIn("78.82", msft_line)

        alerts = self.app.ask("/alerts")
        self.assertIn("No active alerts", alerts)

        summary = self.app.ask("/summary")
        self.assertIn("10619", summary)
        self.assertIn("1 scannable", summary)
        self.assertIn("0 candidate", summary)

        dashboard = self.app.ask("/dashboard")
        self.assertIn("38 ticker(s)", dashboard)
        self.assertIn("No alert has ever fired on real data", dashboard)

        # Cross-checks: the same MSFT facts in every output.
        valuation = self._valuation_reply()
        for fact in ("overvalued", "78.82", "163.30"):
            self.assertIn(fact, valuation)
            self.assertIn(fact, watchlist if fact != "163.30"
                          else valuation)
        self.assertIn("overvalued", watchlist.lower())
        self.assertIn("78.82", watchlist)
        # The dashboard saw the turns this session logged.
        self.assertIn("orchestrator", dashboard)

    # -- (d) trade refusal, no model call, no state change ----------

    def test_d_trade_refusal_end_to_end_no_state_change(self):
        calls_before = len(self.stub.calls)
        data_before = _file_hashes(self.data_dir)
        counts_before = _logical_counts(self.db_path)
        reply = self.app.ask("Buy 100 shares of MSFT at $500")
        self.assertIn("will not place a trade", reply)
        self.assertEqual(len(self.stub.calls), calls_before,
                         "the model was called for a refused trade")
        refused = [record for record in self._turn_log()
                   if record.get("route") == "guardrail_trade"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].get("status"), "refused")
        # Nothing changed except the turn log itself (and the
        # memory rows main.py writes to the temp copy by design):
        # the seeded data files are byte-identical, and the
        # financial tables are untouched.
        after = _file_hashes(self.data_dir)
        changed = {name for name in set(data_before) | set(after)
                   if data_before.get(name) != after.get(name)}
        self.assertEqual(changed, {"orchestration/turns.jsonl"},
                         f"a refused trade changed more than the "
                         f"turn log: {sorted(changed)}")
        self.assertEqual(_logical_counts(self.db_path), counts_before)

    # -- (e) scheduled job, fake clock, run log, stub receipt -------

    def test_e_scheduler_job_end_to_end(self):
        delivery = StubDelivery(
            deliveries_path=self.root / "deliveries.jsonl")
        scheduler = Scheduler(
            store_dir=self.scheduler_dir,
            actions={**builtin_actions(), "boom": _boom},
            delivery=delivery)
        job = scheduler.create_job(
            "Morning watchlist check", "watchlist_check",
            {"type": "daily", "time": "09:36",
             "timezone": "Asia/Singapore"},
            {"action": "watchlist_check", "args": {
                "db_path": str(self.db_path),
                "watchlist": ["MSFT"],
                "state_path": str(
                    self.data_dir / "monitoring"
                    / "monitoring_state.json"),
                "news_dir": str(self.data_dir / "news"),
                "events_dir": str(self.data_dir / "events"),
                "as_of": "2026-11-09"}},
            delivery={"channel": "stub", "recipient": "owner"},
            now=FAKE_CREATED)
        failing = scheduler.create_job(
            "Failing probe", "watchlist_check",
            {"type": "daily", "time": "09:36",
             "timezone": "Asia/Singapore"},
            {"action": "boom", "args": {}},
            now=FAKE_CREATED)
        reminder = create_reminder(
            self.scheduler_dir,
            "NVDA earnings Tue 17 Nov after the close",
            "2026-11-17T08:30:00+08:00", "Asia/Singapore",
            now=FAKE_CREATED)
        self.assertEqual(reminder["kind"], "reminder")

        # The reminder is not due at the fake now; the two daily
        # jobs are.
        jobs, _skipped = load_jobs(self.scheduler_dir)
        due = due_jobs(jobs, now=FAKE_NOW)
        due_ids = {item["id"] for item in due}
        self.assertIn(job["id"], due_ids)
        self.assertIn(failing["id"], due_ids)
        self.assertNotIn(reminder["id"], due_ids)

        records = run_due_jobs(
            store_dir=self.scheduler_dir, now=FAKE_NOW,
            actions={**builtin_actions(), "boom": _boom},
            delivery=delivery)
        by_id = {record["job_id"]: record for record in records}
        self.assertEqual(len(records), 2,
                         "the failing job blocked the other one, "
                         "or a job did not run")
        watch = by_id[job["id"]]
        self.assertEqual(watch["status"], "ok")
        self.assertIn("Watchlist check: 1 ticker(s)",
                      watch["summary"])
        self.assertEqual(by_id[failing["id"]]["status"], "failed")
        self.assertIn("boom", by_id[failing["id"]]["error"])

        # The stub receipt says delivered: False — nothing was
        # really sent. Only the successful job delivered.
        self.assertEqual(len(delivery.deliveries), 1)
        receipt = delivery.deliveries[0]
        self.assertFalse(receipt["delivered"])
        self.assertIn("stub delivery", receipt["note"])
        self.assertEqual(watch["delivery_receipt"]["receipt_id"],
                         receipt["receipt_id"])

        # Next run advanced to Tuesday; the run log holds both
        # records; the reminder has no run record.
        updated_jobs, _skipped = load_jobs(self.scheduler_dir)
        updated = {item["id"]: item for item in updated_jobs}
        self.assertTrue(
            updated[job["id"]]["next_run"].startswith("2026-11-10"))
        self.assertTrue(
            updated[job["id"]]["next_run"].endswith("+08:00"))
        log = read_run_log(self.scheduler_dir)
        self.assertEqual(len(log), 2)
        self.assertNotIn(reminder["id"],
                         {record["job_id"] for record in log})
        # The run log and the deliveries file live in the temp
        # directory only.
        self.assertFalse((PROJECT_ROOT / "data" / "scheduler").exists())

    # -- (f) UI, same numbers as the CLI path ------------------------

    def test_f_ui_end_to_end_same_numbers_as_cli(self):
        port = _free_port()
        self.app.ui_port = port
        reply = self.app.ask("/ui")
        self.assertIn(f"http://127.0.0.1:{port}/", reply)
        try:
            def _fetch(path):
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}",
                        timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
                    return resp.read().decode("utf-8")

            home = _fetch("/")
            self.assertIn("10619", home)
            self.assertIn("78.82", home)
            self.assertIn("No active alerts", home)
            ticker = _fetch("/ticker/MSFT")
            self.assertIn("163.30", ticker)
            self.assertIn("overvalued", ticker.lower())
            self.assertIn("78.82", ticker)
            dashboard_page = _fetch("/dashboard")
            self.assertIn("38", dashboard_page)
            self.assertIn("watchlist", dashboard_page.lower())

            # The home page headline is the same summary the
            # CLI /summary command prints.
            summary = self.app.ask("/summary")
            headline = summary.splitlines()[0]
            self.assertIn(headline, home)
            # And the ticker page numbers match the CLI
            # valuation reply.
            valuation = self._valuation_reply()
            for fact in ("98.52", "163.30", "234.64", "78.82"):
                self.assertIn(fact, valuation)
                self.assertIn(fact, ticker)
        finally:
            self.app.stop_ui()

    # -- (g) voice turn, spoken reply refused by default ------------

    def test_g_voice_turn_end_to_end(self):
        self.assertIn("off by default", self.app.ask("/voice"))
        self.assertFalse(self.app.voice_armed)
        self.assertIn("armed", self.app.ask("/voice yes"))
        self.assertTrue(self.app.voice_armed)
        reply = self.app.ask("What is the intrinsic value of MSFT?")
        self.assertIn("163.30", reply)
        self.assertIn("Spoken reply: no spoken reply: the owner "
                      "prefers text replies", reply)
        self.assertFalse(self.app.voice_armed,
                         "voice stayed armed past one turn")

        # With a voice-allowed preference in a temporary store,
        # a spoken reply IS produced — as a file, via the stub.
        voice_dir = self.root / "voice_memory"
        record = ltm.new_record(
            "preference",
            "FIXTURE — voice replies allowed for this test only.",
            stated_by="owner", tags=["voice", "preference"])
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=voice_dir)
        tts = voice_mod.StubTTS(output_dir=self.root / "tts")
        result = voice_mod.voice_turn(
            transcript="What is the intrinsic value of MSFT?",
            tts_client=tts, model_client=self.stub,
            db_path=str(self.db_path),
            store_dir=self.data_dir / "watchlist",
            news_dir=self.data_dir / "news",
            log_dir=self.log_dir, speak_reply=True,
            memory_dir=voice_dir)
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["spoken_reply"])
        spoken = Path(result["spoken_reply"]).read_text(
            encoding="utf-8")
        self.assertIn("163.30", spoken)

    # -- (h) model unavailable, session continues -------------------

    def test_h_model_unavailable_session_continues(self):
        failing = StubModelClient(raise_unavailable=True)
        self.app.model_client = failing
        self.app.orchestrator.model_client = failing
        reply = self.app.ask("Hello there")
        self.assertIn("unreachable", reply.lower())
        self.assertIn("have not invented an answer", reply)
        unavailable = [record for record in self._turn_log()
                       if record.get("status") == "model_unavailable"]
        self.assertTrue(unavailable,
                        "the unavailable turn was not logged")
        # The session continues once the model is back.
        self.app.model_client = self.stub
        self.app.orchestrator.model_client = self.stub
        self.assertIn("163.30", self._valuation_reply())

    # -- (i) banner and --help, including a real subprocess ---------

    def test_i_banner_and_help(self):
        banner = "\n".join(self.app.banner_lines())
        for expected in (
                "AiAssistant v0.7",
                "Cloud models: not configured",
                "Live price feed: none",
                "latest snapshot 2026-11-03",
                "Holdings: none entered",
                "Remote computer: not configured",
                "Voice: off by default"):
            self.assertIn(expected, banner)
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("AiAssistant", result.stdout)
        self.assertIn("--ui", result.stdout)
        # A real subprocess run with a temp database: the banner
        # prints, it exits cleanly with no Ollama running, and
        # the production database is untouched (memory
        # initialisation is a CREATE TABLE IF NOT EXISTS no-op
        # and "exit" is handled before any message is saved).
        before = _db_hash(PROJECT_ROOT / "database" / "assistant.db")
        proc = subprocess.run(
            [sys.executable, "main.py", "--db-path",
             str(self.db_path)],
            cwd=str(PROJECT_ROOT), input="exit\n",
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("AiAssistant v0.7", proc.stdout)
        self.assertIn("Goodbye!", proc.stdout)
        self.assertEqual(
            _db_hash(PROJECT_ROOT / "database" / "assistant.db"),
            before)

    # -- (j) audit open item O5: empty valuation profiles -----------

    def test_j_valuation_monitor_empty_profiles(self):
        # The legacy monitor hardcodes the production database
        # path (a finding, not fixed here). The table exists
        # with 0 rows, so this is a read plus a no-op
        # CREATE TABLE IF NOT EXISTS — the production hash
        # must be unchanged afterwards.
        before = _db_hash(PROJECT_ROOT / "database" / "assistant.db")
        result = monitor_company_valuation("MSFT")
        self.assertEqual(result, {
            "error": "No valuation profile found for MSFT. "
                     "Please create a valuation profile first."})
        self.assertEqual(
            _db_hash(PROJECT_ROOT / "database" / "assistant.db"),
            before)

    # -- (k) open decisions re-checked, not changed -----------------

    def test_k_step23_boundary_float_edge_and_step27_warning(self):
        # Step 23: the exact-20% boundary is a float edge. A
        # price at exactly 80% of a DCF-derived conservative
        # value gives a margin of 0.19999999999999996 — just
        # under the threshold — so the tier is reasonable_price,
        # NOT strongest_alert. The threshold is unchanged.
        conservative, base, optimistic = 98.52, 163.30, 234.64
        price = 0.8 * conservative
        margin = margin_of_safety(conservative, price)
        self.assertLess(margin, 0.20)
        self.assertEqual(repr(margin), "0.19999999999999996")
        tier = classify_tier(
            margin, margin_of_safety(base, price),
            margin_of_safety(optimistic, price),
            {"deteriorated": False, "unknown": False},
            quality="excellent")
        self.assertEqual(tier["tier"], "reasonable_price")
        # A literal 20% margin does fire; material
        # deterioration blocks it.
        self.assertEqual(classify_tier(
            0.20, 0.52, 0.66,
            {"deteriorated": False, "unknown": False},
            quality="excellent")["tier"], "strongest_alert")
        blocked = classify_tier(
            0.20, 0.52, 0.66,
            {"deteriorated": True, "unknown": False,
             "signals": ["free cash flow softening"]},
            quality="excellent")
        self.assertTrue(blocked["strongest_alert_blocked"])
        self.assertEqual(blocked["tier"], "reasonable_price")

        # Step 27: MSFT's overall verdict is "warning" — a
        # level, not a change in trend — with no material
        # deterioration. That calibration question is still
        # open; this test records the current behaviour.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro",
                                uri=True)
        try:
            det = assess_deterioration(conn, "MSFT")
            summary = margin_of_safety_summary(conn, "MSFT")
        finally:
            conn.close()
        self.assertEqual(det["overall_verdict"], "warning")
        self.assertFalse(det["material_deterioration"])
        self.assertEqual(
            [flag["name"] for flag in det["risk_flags"]],
            ["free cash flow softening", "working capital falling",
             "capex surge compressing FCF"])
        self.assertAlmostEqual(
            summary["strongest_alert_threshold_price"], 78.82,
            places=1)
        self.assertEqual(summary["tier"]["tier"], "overvalued")


if __name__ == "__main__":
    unittest.main()
