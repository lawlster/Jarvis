"""Step 53 — deploy reliable always-on assistant: readiness tests.

Every test uses TemporaryDirectory and cleans up after
itself.  Tests that need a database start from a copy of the
production database (read-only use) or from a small synthetic
one built in the temp directory.  No test takes a real
backup, starts a service, or touches the production
database; the production content fingerprint is asserted unchanged by
ProductionHashTestCase (there is no module-level setUp/tearDown).  No secret value appears
anywhere — fixtures use obviously fake values.
"""

import gc
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_DB = PROJECT_ROOT / "database" / "assistant.db"

sys.path.insert(0, str(PROJECT_ROOT))

from agent import deployment  # noqa: E402
from agents.backup_recovery import backup_database, sha256_file  # noqa: E402
from db_fingerprint import (  # noqa: E402
    content_fingerprint,
    temp_dir_with_db_cleanup,
)


def _copy_production_db(directory):
    target = Path(directory) / "assistant.db"
    shutil.copy2(PRODUCTION_DB, target)
    return target


def _synthetic_db(path, tickers=("AAA", "BBB")):
    """A small database with statement line items for `tickers`."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE market_universe (ticker TEXT)")
    conn.execute(
        "CREATE TABLE financial_snapshots "
        "(ticker TEXT, snapshot_date TEXT)")
    conn.execute(
        "CREATE TABLE financial_statements "
        "(ticker TEXT, data_json TEXT)")
    today = datetime.now(timezone.utc).date().isoformat()
    for ticker in tickers:
        conn.execute(
            "INSERT INTO market_universe VALUES (?)", (ticker,))
        conn.execute(
            "INSERT INTO financial_snapshots VALUES (?, ?)",
            (ticker, today))
        conn.execute(
            "INSERT INTO financial_statements VALUES (?, ?)",
            (ticker, json.dumps({"revenue": 100, "net_income": 10})))
    conn.execute("PRAGMA integrity_check")
    conn.commit()
    conn.close()
    return Path(path)


class ProductionHashTestCase(unittest.TestCase):
    """The suite never changes the production database.

    Step 57: the old test asserted the production file's raw
    SHA-256 against a hardcoded value from this machine.  The
    owner's file has the same logical content but different
    bytes (his hash is 355f80644af1...), so that assertion
    failed on his PC for no real reason.  The guard is now the
    content fingerprint (see db_fingerprint.py): reading the
    database must not change its protected content.
    """

    def test_production_content_unchanged_by_reads(self):
        before = content_fingerprint(PRODUCTION_DB)
        check = deployment.check_database(PRODUCTION_DB)
        self.assertEqual(check["status"], "pass")
        self.assertEqual(content_fingerprint(PRODUCTION_DB), before)
        self.assertEqual(check["evidence"]["universe_rows"], 10619)


class OllamaReachableTestCase(unittest.TestCase):
    def test_closed_port_is_not_reachable(self):
        self.assertFalse(deployment.ollama_reachable(port=59998))

    def test_listening_socket_is_reachable(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", 59998))
            server.listen(1)
            self.assertTrue(deployment.ollama_reachable(port=59998))
        finally:
            server.close()


class DatabaseCheckTestCase(unittest.TestCase):
    def test_production_copy_passes(self):
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            check = deployment.check_database(db)
            self.assertEqual(check["status"], "pass")
            evidence = check["evidence"]
            # Step 57: the module reports the file's own SHA-256;
            # assert it reports honestly rather than matching a
            # hardcoded hash from another machine.
            self.assertEqual(evidence["sha256"], sha256_file(db))
            self.assertEqual(evidence["integrity"], "ok")
            self.assertEqual(evidence["universe_rows"], 10619)
            self.assertEqual(evidence["tickers_with_line_items"],
                             ["MSFT"])
            self.assertEqual(evidence["latest_snapshot"], "2026-11-03")
            gc.collect()

    def test_missing_database_fails(self):
        check = deployment.check_database(
            Path("/nonexistent/assistant.db"))
        self.assertEqual(check["status"], "fail")
        self.assertIn("not found", check["detail"])

    def test_corrupt_database_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "assistant.db"
            bad.write_bytes(b"this is not a sqlite database")
            check = deployment.check_database(bad)
            self.assertEqual(check["status"], "fail")


class BackupsCheckTestCase(unittest.TestCase):
    def test_missing_backup_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = deployment.check_backups(Path(tmp) / "no-such-dir")
            self.assertEqual(check["status"], "fail")

    def test_empty_backup_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = deployment.check_backups(Path(tmp))
            self.assertEqual(check["status"], "fail")
            self.assertIn("no backups", check["detail"])

    def test_fresh_manifest_backup_passes(self):
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            backup_dir = Path(tmp) / "backups"
            backup_database(db, backup_dir=backup_dir, label="test")
            check = deployment.check_backups(backup_dir)
            self.assertEqual(check["status"], "pass")
            self.assertTrue(check["evidence"]["newest_in_manifest"])
            self.assertTrue(check["evidence"]["newest_verified"])
            self.assertEqual(check["evidence"]["count"], 1)
            gc.collect()

    def test_pre_manifest_backup_is_a_warning(self):
        # A backup file dropped into the directory without a
        # manifest (the state of the real 2026-11-08 backups):
        # it verifies by integrity, but has no recorded hash.
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            target = backup_dir / "assistant.db.old.bak"
            shutil.copy2(db, target)
            check = deployment.check_backups(backup_dir)
            self.assertEqual(check["status"], "warning")
            self.assertFalse(check["evidence"]["newest_in_manifest"])
            self.assertTrue(check["evidence"]["newest_verified"])
            gc.collect()

    def test_pre_manifest_backups_sort_newest_by_age(self):
        # Regression (Step 53): pre-manifest backups used to
        # sort newest-first by file name, which put the oldest
        # of the real 2026-11-08 backups first.  They must sort
        # by file age.
        import time
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            older = backup_dir / "zzz.bak"
            newer = backup_dir / "aaa.bak"
            shutil.copy2(db, older)
            shutil.copy2(db, newer)
            now = time.time()
            os.utime(older, (now - 3600, now - 3600))
            os.utime(newer, (now, now))
            check = deployment.check_backups(backup_dir)
            self.assertEqual(check["status"], "warning")
            self.assertEqual(check["evidence"]["newest"], "aaa.bak")
            gc.collect()

    def test_unverifiable_backup_fails(self):
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            backup_dir = Path(tmp) / "backups"
            record = backup_database(db, backup_dir=backup_dir,
                                     label="test")
            path = backup_dir / record["backup_file"]
            path.write_bytes(b"tampered")
            check = deployment.check_backups(backup_dir)
            self.assertEqual(check["status"], "fail")
            gc.collect()


class TestsCheckTestCase(unittest.TestCase):
    def test_not_run_is_unknown(self):
        check = deployment.check_tests(run=False)
        self.assertEqual(check["status"], "unknown")
        self.assertIn("unittest discover", check["detail"])

    def test_run_with_passing_command(self):
        check = deployment.check_tests(
            run=True, command=[sys.executable, "-c",
                               "print('Ran 3 tests in 0.1s\\nOK')"])
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["evidence"]["tests_run"], 3)

    def test_run_with_failing_command(self):
        check = deployment.check_tests(
            run=True, command=[sys.executable, "-c",
                               "import sys; sys.exit(1)"])
        self.assertEqual(check["status"], "fail")


class OllamaCheckTestCase(unittest.TestCase):
    def test_check_matches_actual_reachability(self):
        # Step 57: the old test asserted Ollama is NOT reachable,
        # which is true on this machine but false on the owner's
        # PC, where Ollama is running (his `ollama serve` failed
        # with a bind error — already running).  The check must
        # report reality either way.
        reachable = deployment.ollama_reachable(timeout=0.2)
        check = deployment.check_ollama(timeout=0.2)
        if reachable:
            self.assertEqual(check["status"], "pass")
            self.assertIn("reachable", check["detail"])
        else:
            self.assertEqual(check["status"], "fail")
            self.assertIn("not reachable", check["detail"])

    def test_reachable_when_listening(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", deployment.OLLAMA_PORT))
            server.listen(1)
            check = deployment.check_ollama(timeout=0.5)
            self.assertEqual(check["status"], "pass")
        finally:
            server.close()


class CloudCheckTestCase(unittest.TestCase):
    def test_missing_config_is_local_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = deployment.check_cloud(Path(tmp) / "nope.json")
            self.assertEqual(check["status"], "pass")
            self.assertIn("local Ollama only", check["detail"])
            self.assertEqual(check["evidence"]["cloud_providers"], 0)

    def test_cloud_provider_counted_without_printing_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cloud_models.json"
            path.write_text(json.dumps({
                "providers": [{
                    "name": "example-cloud", "kind": "cloud",
                    "endpoint": "https://example.invalid/v1",
                    "model": "example-model",
                    "api_key_env": "EXAMPLE_API_KEY",
                }]}), encoding="utf-8")
            check = deployment.check_cloud(path)
            self.assertEqual(check["status"], "pass")
            self.assertEqual(check["evidence"]["cloud_providers"], 1)
            self.assertNotIn("sk-", json.dumps(check))

    def test_invalid_config_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cloud_models.json"
            path.write_text(json.dumps({
                "providers": [{
                    "name": "bad", "kind": "cloud",
                    "api_key": "sk-test-fake-key-for-test-only",
                }]}), encoding="utf-8")
            check = deployment.check_cloud(path)
            self.assertEqual(check["status"], "fail")


class SecretsCheckTestCase(unittest.TestCase):
    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(
                "# nothing secret here\n", encoding="utf-8")
            check = deployment.check_secrets(root,
                                             include_history=False)
            self.assertEqual(check["status"], "pass")
            self.assertEqual(check["evidence"]["tree_findings"], 0)

    def test_fake_key_is_a_warning_not_a_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A bare fake key matches exactly one pattern
            # ("openai_key"); an "api_key = sk-..." line would
            # match two, which is correct scan behaviour but
            # not what this test counts.
            (root / "notes.txt").write_text(
                "sk-test-fake-key-for-test-only\n",
                encoding="utf-8")
            check = deployment.check_secrets(root,
                                             include_history=False)
            self.assertEqual(check["status"], "warning")
            self.assertEqual(check["evidence"]["tree_findings"], 1)
            # The finding is a location, never the value.
            self.assertNotIn("sk-test", json.dumps(check))


class UiCheckTestCase(unittest.TestCase):
    def test_real_project_binds_localhost_only(self):
        check = deployment.check_ui(PROJECT_ROOT)
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["evidence"]["default_host"], "127.0.0.1")
        self.assertTrue(check["evidence"]["refuses_other_hosts"])

    def test_wildcard_host_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent").mkdir()
            (root / "agent" / "ui.py").write_text(
                'DEFAULT_HOST = "0.0.0.0"\n', encoding="utf-8")
            check = deployment.check_ui(root)
            self.assertEqual(check["status"], "fail")


class GuardrailsCheckTestCase(unittest.TestCase):
    def test_real_project_guardrails_present(self):
        check = deployment.check_guardrails(PROJECT_ROOT)
        self.assertEqual(check["status"], "pass")
        self.assertTrue(all(check["evidence"].values()))

    def test_missing_guardrail_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent").mkdir()
            (root / "agent" / "orchestrator.py").write_text(
                "# no guardrails here\n", encoding="utf-8")
            (root / "agent" / "tool_calling.py").write_text(
                "# no forbidden names here\n", encoding="utf-8")
            check = deployment.check_guardrails(root)
            self.assertEqual(check["status"], "fail")
            self.assertIn("trade_guardrail", check["detail"])


class DataGapsCheckTestCase(unittest.TestCase):
    def test_production_data_gaps(self):
        with temp_dir_with_db_cleanup() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            check = deployment.check_data_gaps(
                _copy_production_db(tmp), data_dir)
            self.assertEqual(check["status"], "warning")
            self.assertIn("only 1 ticker(s) with statement line "
                          "items (MSFT)", check["detail"])
            self.assertIn("no live price feed (latest snapshot "
                          "2026-11-03)", check["detail"])
            self.assertIn("no holdings store", check["detail"])
            self.assertIn("voice off by default", check["detail"])
            gc.collect()

    def test_voice_preference_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "abc.json").write_text(json.dumps({
                "id": "abc", "type": "preference",
                "status": "active", "tags": ["voice"],
                "voice_mode": "voice"}), encoding="utf-8")
            self.assertEqual(
                deployment._voice_preference(data_dir), "voice")


class ReadinessReportTestCase(unittest.TestCase):
    def _fake_passing_command(self):
        return [sys.executable, "-c",
                "print('Ran 3 tests in 0.1s\\nOK')"]

    def test_real_project_is_not_ready(self):
        report = deployment.readiness_report(
            run_tests=False, test_command=self._fake_passing_command())
        self.assertEqual(report["verdict"], "not_ready_for_always_on")
        by_name = {c["name"]: c for c in report["checks"]}
        self.assertEqual(by_name["ollama"]["status"], "fail")
        self.assertEqual(by_name["database"]["status"], "pass")
        self.assertEqual(by_name["guardrails"]["status"], "pass")
        self.assertEqual(by_name["ui"]["status"], "pass")
        # All four live checks are pending.
        self.assertEqual(report["live_checks_done"],
                         {key: False for key in deployment.LIVE_CHECKS})
        self.assertTrue(any("live_ollama_turn" in b
                            for b in report["blockers"]))
        # The real backups predate the Step 52 manifest.
        self.assertEqual(by_name["backups"]["status"], "warning")

    def test_synthetic_ready_project(self):
        # A fully provisioned fake deployment: every check
        # passes and all four live checks are evidenced.
        with temp_dir_with_db_cleanup() as tmp:
            root = Path(tmp) / "project"
            (root / "agent").mkdir(parents=True)
            (root / "agent" / "ui.py").write_text(
                'DEFAULT_HOST = "127.0.0.1"\n'
                'def make_server(ui, host=DEFAULT_HOST, port=8765):\n'
                '    if host not in ("127.0.0.1", "localhost"):\n'
                '        raise ValueError("refused: the Jarvis UI '
                'binds localhost only, not %r" % host)\n',
                encoding="utf-8")
            (root / "agent" / "orchestrator.py").write_text(
                '# guardrail_trade\nguardrail_schedule_send\n'
                '"I will not place a trade"\n', encoding="utf-8")
            (root / "agent" / "tool_calling.py").write_text(
                "# ForbiddenToolError\n", encoding="utf-8")
            db = _synthetic_db(root / "assistant.db")
            data_dir = root / "data"
            (data_dir / "holdings").mkdir(parents=True)
            memory_dir = data_dir / "memory"
            memory_dir.mkdir()
            (memory_dir / "abc.json").write_text(json.dumps({
                "id": "abc", "type": "preference",
                "status": "active", "tags": ["voice"],
                "voice_mode": "voice"}), encoding="utf-8")
            backup_dir = root / "backups"
            backup_database(db, backup_dir=backup_dir, label="test")
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET,
                              socket.SO_REUSEADDR, 1)
            try:
                server.bind(("127.0.0.1", deployment.OLLAMA_PORT))
                server.listen(1)
                report = deployment.readiness_report(
                    project_root=root, db_path=db, data_dir=data_dir,
                    backup_dir=backup_dir, run_tests=True,
                    test_command=self._fake_passing_command(),
                    live_checks={key: True
                                 for key in deployment.LIVE_CHECKS})
            finally:
                server.close()
            self.assertEqual(report["verdict"], "ready_for_always_on")
            self.assertEqual(report["blockers"], [])
            self.assertTrue(all(report["live_checks_done"].values()))
            gc.collect()

    def test_one_missing_live_check_blocks_ready(self):
        with temp_dir_with_db_cleanup() as tmp:
            root = Path(tmp) / "project"
            (root / "agent").mkdir(parents=True)
            (root / "agent" / "ui.py").write_text(
                'DEFAULT_HOST = "127.0.0.1"\n'
                'def make_server(ui, host=DEFAULT_HOST, port=8765):\n'
                '    if host not in ("127.0.0.1", "localhost"):\n'
                '        raise ValueError("refused: the Jarvis UI '
                'binds localhost only, not %r" % host)\n',
                encoding="utf-8")
            (root / "agent" / "orchestrator.py").write_text(
                '# guardrail_trade\nguardrail_schedule_send\n'
                '"I will not place a trade"\n', encoding="utf-8")
            (root / "agent" / "tool_calling.py").write_text(
                "# ForbiddenToolError\n", encoding="utf-8")
            db = _synthetic_db(root / "assistant.db")
            data_dir = root / "data"
            (data_dir / "holdings").mkdir(parents=True)
            memory_dir = data_dir / "memory"
            memory_dir.mkdir()
            (memory_dir / "abc.json").write_text(json.dumps({
                "id": "abc", "type": "preference",
                "status": "active", "tags": ["voice"],
                "voice_mode": "voice"}), encoding="utf-8")
            backup_dir = root / "backups"
            backup_database(db, backup_dir=backup_dir, label="test")
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET,
                              socket.SO_REUSEADDR, 1)
            try:
                server.bind(("127.0.0.1", deployment.OLLAMA_PORT))
                server.listen(1)
                report = deployment.readiness_report(
                    project_root=root, db_path=db, data_dir=data_dir,
                    backup_dir=backup_dir, run_tests=True,
                    test_command=self._fake_passing_command(),
                    live_checks={"live_ollama_turn": True,
                                 "live_backup_verified": True,
                                 "live_ui_session": True,
                                 "live_restore_drill": False})
            finally:
                server.close()
            self.assertEqual(report["verdict"],
                             "not_ready_for_always_on")
            self.assertTrue(any("live_restore_drill" in b
                                for b in report["blockers"]))
            gc.collect()

    def test_summary_and_markdown_shape(self):
        report = deployment.readiness_report(run_tests=False)
        lines = deployment.summary_lines(report)
        self.assertIn("Deployment readiness — "
                      "not_ready_for_always_on", lines[0])
        self.assertTrue(any(line.startswith("[pass] database")
                            for line in lines))
        markdown = deployment.report_to_markdown(report)
        self.assertIn("**Verdict: not_ready_for_always_on**", markdown)
        self.assertIn("- [ ] live_ollama_turn", markdown)


class StartupCheckTestCase(unittest.TestCase):
    def test_lines_on_production_copy(self):
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            lines = deployment.startup_check_lines(db_path=db)
            self.assertEqual(len(lines), 3)
            self.assertIn("database integrity ok", lines[0])
            # Step 57: the startup line prints the file's own
            # hash prefix — assert it matches this copy's hash,
            # not a hardcoded hash from another machine.
            self.assertIn(sha256_file(db)[:10], lines[0])
            self.assertIn("backup directory", lines[1])
            # Step 57: the Ollama line reports reality — reachable
            # on the owner's PC (Ollama running), not reachable
            # on this machine.
            if deployment.ollama_reachable(timeout=0.2):
                self.assertIn("reachable", lines[2])
                self.assertNotIn("not reachable", lines[2])
            else:
                self.assertIn("Ollama not reachable", lines[2])
            gc.collect()

    def test_missing_database_still_returns_three_lines(self):
        lines = deployment.startup_check_lines(
            db_path=Path("/nonexistent/assistant.db"))
        self.assertEqual(len(lines), 3)
        self.assertIn("database problem", lines[0])

    def test_main_start_prints_self_check(self):
        # The integrated main.py prints the self-check after the
        # banner and still exits cleanly with no Ollama.
        import io
        from contextlib import redirect_stdout
        from agent.orchestrator import StubModelClient
        from main import JarvisApp
        with temp_dir_with_db_cleanup() as tmp:
            db = _copy_production_db(tmp)
            app = JarvisApp(db_path=db, model_client=StubModelClient(),
                            log_dir=False, data_dir=Path(tmp) / "data",
                            memory_dir=str(Path(tmp) / "memory"))
            out = io.StringIO()
            with redirect_stdout(out):
                app.start()
            text = out.getvalue()
            self.assertIn("AiAssistant v0.7", text)
            self.assertIn("Self-check: database integrity ok", text)
            self.assertIn("Self-check: local Ollama not reachable",
                          text)
            gc.collect()


if __name__ == "__main__":
    unittest.main()
