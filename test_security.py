"""Step 52 — backup, recovery and security systems: security tests.

Covers the three Step 51 hardening fixes (valuation-profiles
``db_path``, ``classify_tier`` None margins, memory
``JARVIS_DB_PATH`` override) plus the secrets scan, the
permissions check and the database-write audit.  Fixture
secrets are obviously fake (``sk-test-...`` and the like) and
the owner's real address never appears in any file here.
The production database is read, never written: its content fingerprint is
asserted unchanged around the whole class.
"""

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)

from agent import memory as memory_mod  # noqa: E402
from agents import security as sec  # noqa: E402
from agents import valuation_profiles as vp  # noqa: E402
from agents.margin_of_safety import classify_tier  # noqa: E402
from agents.valuation_monitor import (  # noqa: E402
    monitor_company_valuation)

# Obviously fake fixture secrets.  Nothing here is real, and
# the owner's personal address never appears in this file.
FAKE_OPENAI_KEY = "sk-test-0123456789abcdef"
FAKE_EMAIL = "fixture-person@example.invalid"
FAKE_PASSWORD_LINE = 'password = "fixture-password-not-real"'


def _deterioration(deteriorated, unknown=False, signals=None):
    return {"deteriorated": deteriorated, "unknown": unknown,
            "signals": signals or []}


class Step51FixesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prod_fp_before = content_fingerprint(
            PROJECT_ROOT / "database" / "assistant.db")

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(
            PROJECT_ROOT / "database" / "assistant.db"
        ) == cls._prod_fp_before, (
            "production DB changed during the security tests")

    def setUp(self):
        self.tmp = temp_dir_with_db_cleanup()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "assistant.db"
        shutil.copy(PROJECT_ROOT / "database" / "assistant.db",
                    self.db_path)

    def tearDown(self):
        cleanup_temp_dir(self.tmp)

    # -- finding 1: valuation profiles take a db_path -----------

    def test_profiles_crud_on_temp_db(self):
        result = vp.save_valuation_profile(
            "FIXA", 0.10, 0.11, 0.025, db_path=self.db_path)
        self.assertIn("saved for FIXA", result)
        profile = vp.get_valuation_profile("FIXA",
                                           db_path=self.db_path)
        self.assertEqual(profile["growth_rate"], 0.10)
        self.assertEqual(profile["forecast_years"], 5)
        all_profiles = vp.get_all_valuation_profiles(
            db_path=self.db_path)
        self.assertEqual([p["ticker"] for p in all_profiles],
                         ["FIXA"])
        # The production database still has no profiles.
        self.assertEqual(
            vp.get_all_valuation_profiles(), [])

    def test_monitor_miss_path_on_temp_db(self):
        result = monitor_company_valuation(
            "MSFT", db_path=self.db_path)
        self.assertIn("No valuation profile found for MSFT",
                      result["error"])

    def test_profiles_default_still_production(self):
        # No db_path: the module default is unchanged.
        self.assertEqual(vp.DB_PATH,
                         PROJECT_ROOT / "database" / "assistant.db")

    # -- finding 2: classify_tier tolerates None margins --------

    def test_tier_missing_base_margin(self):
        tier = classify_tier(0.10, None, None,
                             _deterioration(False))
        self.assertEqual(tier["tier"], "insufficient_data")
        self.assertIn("base value", " ".join(tier["reasons"]))

    def test_tier_missing_optimistic_margin(self):
        # Price above base (negative base margin) with no
        # optimistic value: cannot split fairly_valued from
        # overvalued.
        tier = classify_tier(0.10, -0.05, None,
                             _deterioration(False))
        self.assertEqual(tier["tier"], "insufficient_data")
        self.assertIn("optimistic", " ".join(tier["reasons"]))

    def test_tier_missing_base_with_blocked_alert(self):
        tier = classify_tier(0.25, None, None,
                             _deterioration(True, signals=["weak"]))
        self.assertEqual(tier["tier"], "insufficient_data")
        self.assertTrue(tier["strongest_alert_blocked"])

    def test_tier_alert_needs_only_conservative(self):
        # A strongest alert only needs the conservative margin:
        # it still fires when base/optimistic are missing.
        tier = classify_tier(0.25, None, None,
                             _deterioration(False),
                             quality={"rating": "excellent"})
        self.assertEqual(tier["tier"], "strongest_alert")

    def test_tier_all_margins_present_unchanged(self):
        tier = classify_tier(-0.10, -0.30, -0.50,
                             _deterioration(False))
        self.assertEqual(tier["tier"], "overvalued")

    def test_tier_never_buy_or_sell_words(self):
        for margins in ((0.10, None, None), (0.10, -0.05, None),
                        (0.25, None, None)):
            tier = classify_tier(*margins, _deterioration(False))
            text = " ".join(tier["reasons"]
                            + tier["conditions_to_change"]).lower()
            self.assertNotIn("buy", text)
            self.assertNotIn("sell", text)

    # -- finding 3: memory honours JARVIS_DB_PATH --------------

    def test_memory_env_override(self):
        env_db = self.root / "env-assistant.db"
        old = os.environ.get("JARVIS_DB_PATH")
        os.environ["JARVIS_DB_PATH"] = str(env_db)
        try:
            importlib.reload(memory_mod)
            self.assertEqual(memory_mod.DB_PATH, env_db)
            memory_mod.save_message("user", "env override check")
            memory_mod.save_message("assistant", "stored in env db")
            loaded = memory_mod.load_recent_messages(20)
            self.assertEqual(
                [m["content"] for m in loaded],
                ["env override check", "stored in env db"])
            # Nothing landed in the temp copy or production.
            self.assertEqual(
                content_fingerprint(self.db_path),
                content_fingerprint(
                    PROJECT_ROOT / "database" / "assistant.db"))
        finally:
            if old is None:
                os.environ.pop("JARVIS_DB_PATH", None)
            else:
                os.environ["JARVIS_DB_PATH"] = old
            importlib.reload(memory_mod)

    def test_memory_explicit_db_path_param(self):
        memory_mod.save_message("user", "param check",
                                db_path=self.db_path)
        loaded = memory_mod.load_recent_messages(
            20, db_path=self.db_path)
        self.assertIn("param check",
                      [m["content"] for m in loaded])

    # -- audit finding: provider_logger is a second write path --

    def test_provider_logger_writes_to_given_db_only(self):
        from data.providers import provider_logger as pl
        # Step 58: the production row count is no longer hardcoded
        # — a live Yahoo pull (for example the Step 58 live smoke
        # test, which retries and logs each attempt) legitimately
        # adds provider_logs rows to production, so the test
        # compares before/after counts instead of asserting
        # exactly 1 pre-existing row (same fix as the Step 57
        # fingerprint guard: count the change, not the total).
        prod_before = pl.get_recent_provider_logs(
            500, db_path=PROJECT_ROOT / "database" / "assistant.db")
        temp_before = pl.get_recent_provider_logs(
            500, db_path=self.db_path)
        row_id = pl.log_provider_request(
            "FIXA", "fixture-provider", "success",
            db_path=self.db_path)
        self.assertIsInstance(row_id, int)
        logs = pl.get_recent_provider_logs(500, db_path=self.db_path)
        # The temp copy gains exactly the one fixture row.
        self.assertEqual(len(logs), len(temp_before) + 1)
        self.assertEqual(logs[0]["ticker"], "FIXA")
        summary = pl.get_provider_log_summary(db_path=self.db_path)
        by_provider = {row["provider"]: row["total_requests"]
                       for row in summary}
        self.assertEqual(by_provider["fixture-provider"], 1)
        self.assertEqual(sum(by_provider.values()),
                         len(temp_before) + 1)
        # Production is untouched by this test (read with an
        # explicit path: the module global may have been pointed
        # at a temp copy by an earlier suite).
        prod = pl.get_recent_provider_logs(
            500, db_path=PROJECT_ROOT / "database" / "assistant.db")
        self.assertEqual(len(prod), len(prod_before))

    def test_provider_logger_default_still_production(self):
        # Fresh interpreter: the import-time default, immune to
        # whatever another suite did to the module global.
        code = ("import sys; sys.path.insert(0, '.'); "
                "from data.providers import provider_logger as pl; "
                "print(pl.DB_PATH)")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True,
            text=True, cwd=str(PROJECT_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            str(PROJECT_ROOT / "database" / "assistant.db"))


class MainDbPathWiringTestCase(unittest.TestCase):
    """JarvisApp points conversation memory at its own db_path,
    with no monkeypatching — the Step 52 wiring fix."""

    @classmethod
    def setUpClass(cls):
        cls._prod_fp_before = content_fingerprint(
            PROJECT_ROOT / "database" / "assistant.db")

    def test_app_memory_goes_to_app_db(self):
        import main as main_mod
        from agent.orchestrator import StubModelClient
        from data.providers import provider_logger as pl
        tmp = temp_dir_with_db_cleanup()
        self.addCleanup(cleanup_temp_dir, tmp)
        root = Path(tmp.name)
        db_path = root / "assistant.db"
        shutil.copy(PROJECT_ROOT / "database" / "assistant.db",
                    db_path)
        old = memory_mod.DB_PATH
        old_pl = pl.DB_PATH
        try:
            app = main_mod.JarvisApp(
                db_path=db_path,
                model_client=StubModelClient(reply="Stub reply."),
                log_dir=root / "orchestration",
                store_dir=root / "watchlist",
                news_dir=root / "news",
                events_dir=root / "events",
                memory_dir=root / "memory",
                data_dir=root / "data",
                research_save_dir=root / "research")
            app.start()
            reply = app.ask("What is the intrinsic value of MSFT?")
            self.assertIn("163.30", reply)
            stored = memory_mod.load_recent_messages(20)
            self.assertIn("What is the intrinsic value of MSFT?",
                          [m["content"] for m in stored])
            # The session database governs provider logging too
            # (Step 52 wiring): the app pointed it at the temp
            # copy, not production.
            self.assertEqual(pl.DB_PATH, db_path)
            # The temp copy now holds the messages; production
            # is untouched (asserted in tearDownClass below).
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM conversations").fetchone()[0]
            finally:
                conn.close()
            self.assertGreaterEqual(count, 2)
        finally:
            memory_mod.DB_PATH = old
            pl.DB_PATH = old_pl

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(
            PROJECT_ROOT / "database" / "assistant.db"
        ) == cls._prod_fp_before, (
            "production DB changed during the wiring test")


class HardcodedSecretRemediationTestCase(unittest.TestCase):
    """Step 52 remediation: the SEC contact address no longer
    lives hardcoded in tracked source.

    The tree scan used to flag an email address in
    agents/us_market_discovery.py (a hardcoded SEC_USER_AGENT).
    The module now reads SEC_USER_AGENT from .env like the
    other SEC modules and refuses to contact the SEC until it
    is configured.  These tests assert the fix and the state
    of the local .env — without ever reproducing the address.
    """

    def test_no_email_in_tracked_discovery_module(self):
        import re
        path = PROJECT_ROOT / "agents" / "us_market_discovery.py"
        text = path.read_text(encoding="utf-8")
        self.assertNotRegex(
            text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        self.assertNotIn("gmail", text.lower())
        self.assertNotIn("ymail", text.lower())

    def test_tree_scan_no_longer_flags_discovery_module(self):
        result = sec.scan_tree(PROJECT_ROOT)
        flagged = [f for f in result["findings"]
                   if f["file"] == "agents/us_market_discovery.py"]
        self.assertEqual(flagged, [])

    def test_fetch_refuses_without_user_agent_and_no_network(self):
        from unittest import mock
        from agents import us_market_discovery as mod
        with mock.patch.object(mod, "SEC_USER_AGENT", ""), \
                mock.patch("agents.us_market_discovery.requests.get",
                           side_effect=AssertionError(
                               "no network call allowed")):
            result = mod.fetch_us_company_listings()
        self.assertIn("error", result)
        self.assertIn("SEC_USER_AGENT", result["error"])

    def test_local_env_is_untracked_and_ignored(self):
        env = PROJECT_ROOT / ".env"
        if not env.exists():
            self.skipTest("no local .env in this checkout")
        tracked = subprocess.run(
            ["git", "ls-files", ".env"], capture_output=True,
            text=True, cwd=str(PROJECT_ROOT))
        self.assertEqual(tracked.stdout.strip(), "",
                         ".env must not be tracked by git")
        ignored = subprocess.run(
            ["git", "check-ignore", ".env"], capture_output=True,
            text=True, cwd=str(PROJECT_ROOT))
        self.assertEqual(ignored.returncode, 0,
                         ".env must be gitignored")


class SecretsScanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_fixture_tree(self):
        (self.root / "config.py").write_text(
            f'OPENAI_KEY = "{FAKE_OPENAI_KEY}"\n'
            f"{FAKE_PASSWORD_LINE}\n"
            f"# contact {FAKE_EMAIL}\n",
            encoding="utf-8")
        (self.root / "key.pem").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogusFixtureKeyMaterial\n"
            "-----END RSA PRIVATE KEY-----\n",
            encoding="utf-8")
        clean = self.root / "clean.py"
        clean.write_text("x = 1  # nothing secret here\n",
                         encoding="utf-8")
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "ignored.py").write_text(
            f'LEAK = "{FAKE_OPENAI_KEY}"\n', encoding="utf-8")
        return clean

    def test_scan_tree_flags_fixtures_without_values(self):
        self._seed_fixture_tree()
        result = sec.scan_tree(self.root)
        self.assertEqual(result["files_scanned"], 3)
        by_pattern = {}
        for finding in result["findings"]:
            by_pattern.setdefault(finding["pattern"], []).append(
                finding)
        self.assertIn("openai_key", by_pattern)
        self.assertIn("secret_assignment", by_pattern)
        self.assertIn("email_address", by_pattern)
        self.assertIn("private_key", by_pattern)
        # The __pycache__ copy is skipped, not flagged.
        self.assertNotIn("__pycache__",
                         " ".join(f["file"]
                                  for f in result["findings"]))
        # No finding ever carries a secret value.
        blob = json.dumps(result)
        self.assertNotIn(FAKE_OPENAI_KEY, blob)
        self.assertNotIn("fixture-password-not-real", blob)
        self.assertNotIn(FAKE_EMAIL, blob)
        self.assertNotIn("MIIBogusFixtureKeyMaterial", blob)
        for finding in result["findings"]:
            self.assertEqual(set(finding),
                             {"kind", "file", "line", "pattern"})

    def test_scan_tree_clean_tree(self):
        (self.root / "clean.py").write_text("x = 1\n",
                                            encoding="utf-8")
        result = sec.scan_tree(self.root)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_scanned"], 1)

    def test_scan_git_history_flags_deleted_env(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "user.email", "fixture@example.invalid"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "user.name", "Fixture"], check=True)
        (repo / ".env").write_text(
            f"SEC_USER_AGENT=FixtureAgent {FAKE_EMAIL}\n"
            f"OPENAI_API_KEY={FAKE_OPENAI_KEY}\n",
            encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".env"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q",
                        "-m", "add env"], check=True)
        (repo / ".env").unlink()
        subprocess.run(["git", "-C", str(repo), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q",
                        "-m", "remove env"], check=True)
        result = sec.scan_git_history(repo)
        self.assertEqual(result["commits_scanned"], 2)
        patterns = {f["pattern"] for f in result["findings"]}
        self.assertIn("email_address", patterns)
        self.assertIn("openai_key", patterns)
        self.assertTrue(all(f["file"] == ".env"
                            for f in result["findings"]))
        self.assertTrue(all("commit" in f and len(f["commit"]) == 12
                            for f in result["findings"]))
        blob = json.dumps(result)
        self.assertNotIn(FAKE_OPENAI_KEY, blob)
        self.assertNotIn(FAKE_EMAIL, blob)


class PermissionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # Step 57: Unix permission modes do not exist on Windows —
    # os.chmod only toggles read-only there and os.stat reports
    # 0o666 for ordinary files, so a 0o660 file still reads as
    # world-writable.  These two tests assert Unix semantics and
    # are skipped on Windows; the missing-path test below is
    # platform-neutral and still runs everywhere.
    @unittest.skipIf(os.name == "nt",
                     "Unix permission modes do not exist on Windows")
    def test_world_writable_flagged(self):
        loose = self.root / "loose.txt"
        loose.write_text("x", encoding="utf-8")
        os.chmod(loose, 0o666)
        tight = self.root / "tight.txt"
        tight.write_text("x", encoding="utf-8")
        os.chmod(tight, 0o660)
        results = sec.check_permissions([loose, tight])
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["world_writable"])
        self.assertEqual(results[0]["mode"], "0o666")
        self.assertTrue(results[1]["ok"])
        self.assertFalse(results[1]["world_writable"])

    def test_missing_path_reported(self):
        results = sec.check_permissions([self.root / "nope"])
        self.assertFalse(results[0]["exists"])
        self.assertFalse(results[0]["ok"])

    @unittest.skipIf(os.name == "nt",
                     "Unix permission modes do not exist on Windows")
    def test_real_project_paths_not_world_writable(self):
        paths = [PROJECT_ROOT / "database" / "assistant.db",
                 PROJECT_ROOT / "data" / "watchlist",
                 PROJECT_ROOT / "data" / "memory"]
        results = sec.check_permissions(paths)
        self.assertTrue(all(r["ok"] for r in results),
                        f"world-writable project path: {results}")


class DbWriteAuditTestCase(unittest.TestCase):
    def test_runtime_write_paths_match_documented_set(self):
        audit = sec.audit_db_write_paths(PROJECT_ROOT)
        self.assertTrue(
            audit["matches_documented_set"],
            "runtime write-capable DB paths changed: "
            f"{audit['runtime_write_capable_sites']}")
        # Step 52 correction: TWO write paths, not one.  The
        # audit caught provider_logger (every
        # get_company_financials call logs a provider_logs row)
        # which the earlier steps had missed.
        # Step 53: THREE — the deployment readiness check
        # imports the backup module, so it joined the runtime.
        # Its only write-capable connect is the backup
        # destination file, never the production database.
        self.assertEqual(
            {s["file"] for s in audit["runtime_write_capable_sites"]},
            {"agent/memory.py",
             "data/providers/provider_logger.py",
             "agents/backup_recovery.py"})

    def test_legacy_write_paths_listed_not_hidden(self):
        audit = sec.audit_db_write_paths(PROJECT_ROOT)
        files = {s["file"] for s in audit["write_capable_sites"]}
        for legacy in ("agents/valuation_profiles.py",
                       "agents/alert_manager.py",
                       "agents/valuation_history.py",
                       "agents/watchlist.py"):
            self.assertIn(legacy, files)
        # The runtime itself opens the database read-only
        # everywhere else: spot-check a known read-only site.
        ro_files = {s["file"] for s in audit["read_only_sites"]}
        self.assertIn("agent/orchestrator.py", ro_files)
        self.assertIn("agents/market_scanner.py", ro_files)

    def test_audit_lists_many_runtime_modules(self):
        audit = sec.audit_db_write_paths(PROJECT_ROOT)
        self.assertIn("main.py", audit["runtime_modules"])
        self.assertIn("agent/orchestrator.py",
                      audit["runtime_modules"])
        self.assertIn("agents/margin_of_safety.py",
                      audit["runtime_modules"])
        self.assertGreater(len(audit["runtime_modules"]), 20)


if __name__ == "__main__":
    unittest.main()
