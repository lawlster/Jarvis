"""Tests for Step 47 — computer and application interaction.

Temp directories only. No network, no real remote connections,
no production writes. The production database is never opened
by this module; its hash is asserted unchanged anyway.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import computer as cp

PROD_DB_HASH = ("36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8"
                "aa56cc089187")


def _db_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ComputerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "workspace"
        cls.root.mkdir()
        (cls.root / "notes.txt").write_text(
            "MSFT notes\n", encoding="utf-8")
        (cls.root / "sub").mkdir()
        (cls.root / "sub" / "inner.txt").write_text(
            "inner\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        prod = Path(__file__).resolve().parent / "database" / "assistant.db"
        assert _db_hash(prod) == PROD_DB_HASH, "production DB hash changed"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=self._tmp.name))
        self.log_dir = self.dir / "audit"
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))


class RegistryTests(ComputerTestBase):
    def test_default_capabilities_present(self):
        reg = cp.CapabilityRegistry()
        names = [c.name for c in reg.list()]
        for expected in ("read_file", "write_file", "list_directory",
                         "run_command", "launch_application",
                         "system_info"):
            self.assertIn(expected, names)

    def test_approval_flags(self):
        reg = cp.CapabilityRegistry()
        self.assertFalse(reg.get("read_file").requires_approval)
        self.assertFalse(reg.get("list_directory").requires_approval)
        self.assertFalse(reg.get("system_info").requires_approval)
        self.assertTrue(reg.get("write_file").requires_approval)
        self.assertTrue(reg.get("run_command").requires_approval)
        self.assertTrue(reg.get("launch_application").requires_approval)

    def test_describe_lines(self):
        reg = cp.CapabilityRegistry()
        lines = reg.describe()
        self.assertEqual(len(lines), 6)
        self.assertTrue(any("read_file" in line for line in lines))
        self.assertTrue(any("needs approval" in line for line in lines))

    def test_duplicate_registration_refused(self):
        reg = cp.CapabilityRegistry()
        with self.assertRaises(cp.DuplicateCapabilityError):
            reg.register(cp.Capability(
                name="read_file", description="again", scope="x"))

    def test_forbidden_names_refused(self):
        reg = cp.CapabilityRegistry()
        for name in ("buy_shares", "place_order", "send_email",
                     "schedule_reminder", "trade_msft", "sell_stock",
                     "telegram_alert", "short_msft", "remind_me"):
            with self.assertRaises(cp.ForbiddenCapabilityError,
                                   msg=name):
                reg.register(cp.Capability(
                    name=name, description="forbidden", scope="x"))

    def test_innocent_names_allowed(self):
        reg = cp.CapabilityRegistry()
        reg.register(cp.Capability(
            name="buyback_info", description="buyback data",
            scope="workspace root"))
        self.assertIsNotNone(reg.get("buyback_info"))

    def test_windows_control_unavailable(self):
        result = cp.windows_control_available()
        self.assertFalse(result["available"])
        self.assertIn("Mac-only", result["reason"])
        self.assertIn("VS Code", result["reason"])


class LocalFileTests(ComputerTestBase):
    def test_read_file_inside_root(self):
        result = cp.read_file(self.root, "notes.txt",
                              log_dir=self.log_dir)
        self.assertEqual(result["content"], "MSFT notes\n")
        self.assertEqual(result["characters"], 11)

    def test_read_file_nested(self):
        result = cp.read_file(self.root, "sub/inner.txt",
                              log_dir=self.log_dir)
        self.assertEqual(result["content"], "inner\n")

    def test_read_file_missing_is_error(self):
        with self.assertRaises(cp.ComputerError):
            cp.read_file(self.root, "nope.txt", log_dir=self.log_dir)

    def test_read_file_outside_root_dotdot_refused(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.read_file(self.root, "../outside.txt",
                         log_dir=self.log_dir)

    def test_read_file_absolute_outside_root_refused(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.read_file(self.root, "/etc/hostname",
                         log_dir=self.log_dir)

    def test_read_file_symlink_escape_refused(self):
        outside = self.dir / "secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = self.root / "escape.txt"
        link.symlink_to(outside)
        with self.assertRaises(cp.RefusedActionError):
            cp.read_file(self.root, "escape.txt", log_dir=self.log_dir)

    def test_list_directory(self):
        result = cp.list_directory(self.root, log_dir=self.log_dir)
        self.assertIn("notes.txt", result["entries"])
        self.assertIn("sub", result["entries"])
        self.assertEqual(result["entries"],
                         sorted(result["entries"]))

    def test_list_directory_outside_root_refused(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.list_directory(self.root, "..", log_dir=self.log_dir)

    def test_write_file_requires_approval(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.write_file(self.root, "new.txt", "hi",
                          approved=False, log_dir=self.log_dir)
        self.assertFalse((self.root / "new.txt").exists())

    def test_write_file_with_approval(self):
        result = cp.write_file(self.root, "new.txt", "hello",
                               approved=True, log_dir=self.log_dir)
        self.assertEqual(result["characters"], 5)
        self.assertEqual((self.root / "new.txt").read_text(), "hello")

    def test_write_file_nested_creates_parents(self):
        cp.write_file(self.root, "a/b/c.txt", "deep", approved=True,
                      log_dir=self.log_dir)
        self.assertEqual((self.root / "a/b/c.txt").read_text(), "deep")

    def test_write_file_outside_root_refused(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.write_file(self.root, "../evil.txt", "x", approved=True,
                          log_dir=self.log_dir)


class CommandTests(ComputerTestBase):
    def test_allowlisted_command_runs(self):
        result = cp.run_command(self.root, "echo", ["hello"],
                                approved=True, log_dir=self.log_dir)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"].strip(), "hello")
        self.assertIn("duration_s", result)

    def test_allowlisted_ls_runs_in_root(self):
        result = cp.run_command(self.root, "ls", [], approved=True,
                                log_dir=self.log_dir)
        self.assertEqual(result["returncode"], 0)
        self.assertIn("notes.txt", result["stdout"])

    def test_run_command_requires_approval(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.run_command(self.root, "echo", ["hi"], approved=False,
                           log_dir=self.log_dir)

    def test_destructive_commands_refused_by_construction(self):
        for cmd in ("rm", "mv", "cp", "kill", "killall", "sudo",
                    "shutdown", "reboot", "chmod", "chown", "dd",
                    "curl", "wget", "ssh", "scp", "python", "bash",
                    "sh", "pip", "apt"):
            with self.assertRaises(cp.RefusedActionError, msg=cmd):
                cp.run_command(self.root, cmd, [], approved=True,
                               log_dir=self.log_dir)

    def test_shell_metacharacters_refused(self):
        for bad in ("a;b", "a|b", "a&b", "a$b", "a`b", "a>b", "a<b",
                    "a\nb"):
            with self.assertRaises(cp.RefusedActionError, msg=bad):
                cp.run_command(self.root, "echo", [bad], approved=True,
                               log_dir=self.log_dir)

    def test_command_timeout(self):
        # "sleep" is allowlisted and harmless; a 5s sleep with a
        # 0.2s timeout must raise (and must take ~0.2s, not 5s).
        import time
        started = time.monotonic()
        with self.assertRaises(cp.ComputerError):
            cp.run_command(self.root, "sleep", ["5"],
                           approved=True, timeout=0.2,
                           log_dir=self.log_dir)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_failing_command_recorded_not_raised(self):
        result = cp.run_command(self.root, "grep", ["zzz", "notes.txt"],
                                approved=True, log_dir=self.log_dir)
        self.assertEqual(result["returncode"], 1)

    def test_allowlist_is_a_fixed_constant(self):
        self.assertNotIn("rm", cp.ALLOWED_COMMANDS)
        self.assertNotIn("mv", cp.ALLOWED_COMMANDS)
        self.assertIn("ls", cp.ALLOWED_COMMANDS)
        self.assertIsInstance(cp.ALLOWED_COMMANDS, frozenset)


class ApplicationTests(ComputerTestBase):
    def test_launch_not_in_allowlist_refused(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.launch_application("evilapp", approved=True,
                                  log_dir=self.log_dir)

    def test_launch_requires_approval(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.launch_application("code", approved=False,
                                  log_dir=self.log_dir)

    def test_launch_without_launcher_refused_nothing_started(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.launch_application("code", approved=True,
                                  launcher=None, log_dir=self.log_dir)

    def test_launch_with_stub_launcher(self):
        launched = []

        def stub(name):
            launched.append(name)
            return f"started {name}"

        result = cp.launch_application("code", approved=True,
                                       launcher=stub,
                                       log_dir=self.log_dir)
        self.assertTrue(result["launched"])
        self.assertEqual(launched, ["code"])
        self.assertEqual(result["result"], "started code")

    def test_launcher_failure_recorded(self):
        def failing(name):
            raise RuntimeError("no display")

        with self.assertRaises(cp.ComputerError):
            cp.launch_application("code", approved=True,
                                  launcher=failing, log_dir=self.log_dir)


class SystemInfoTests(ComputerTestBase):
    def test_system_info(self):
        info = cp.system_info(log_dir=self.log_dir)
        self.assertEqual(info["platform"], "Linux")
        self.assertIn("python_version", info)
        self.assertIsInstance(info["cpu_count"], int)


class RemoteGateTests(ComputerTestBase):
    def test_remote_action_without_target(self):
        result = cp.remote_action(None, "list_files",
                                  log_dir=self.log_dir)
        self.assertEqual(result["status"], "remote_not_configured")
        self.assertIn("nothing was attempted", result["reason"])

    def test_remote_action_with_disabled_target(self):
        target = cp.RemoteTarget(host="192.0.2.1", user="vernon")
        self.assertFalse(target.enabled)
        result = cp.remote_action(target, "list_files",
                                  log_dir=self.log_dir)
        self.assertEqual(result["status"], "remote_not_configured")

    def test_configure_without_approval_stays_disabled(self):
        target = cp.configure_remote_target("192.0.2.1", "vernon",
                                            log_dir=self.log_dir)
        self.assertFalse(target.enabled)
        self.assertIsNone(target.approved_by)
        self.assertIsNone(target.approved_at)

    def test_configure_with_approval_enables_and_records(self):
        target = cp.configure_remote_target(
            "192.0.2.1", "vernon", method="ssh",
            approved_by="Vernon (owner)", log_dir=self.log_dir)
        self.assertTrue(target.enabled)
        self.assertEqual(target.approved_by, "Vernon (owner)")
        self.assertIsNotNone(target.approved_at)
        self.assertEqual(target.as_dict()["host"], "192.0.2.1")

    def test_remote_action_enabled_without_transport(self):
        target = cp.configure_remote_target(
            "192.0.2.1", "vernon", approved_by="Vernon (owner)",
            log_dir=self.log_dir)
        result = cp.remote_action(target, "list_files",
                                  log_dir=self.log_dir)
        self.assertEqual(result["status"], "remote_not_configured")
        self.assertIn("no remote transport", result["reason"])

    def test_remote_action_with_stub_transport(self):
        calls = []

        def stub_transport(target, action, arguments):
            calls.append((target.host, action, arguments))
            return {"files": ["a.txt"]}

        target = cp.configure_remote_target(
            "192.0.2.1", "vernon", approved_by="Vernon (owner)",
            log_dir=self.log_dir)
        result = cp.remote_action(target, "list_files",
                                  {"path": "/tmp"},
                                  transport=stub_transport,
                                  log_dir=self.log_dir)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"], {"files": ["a.txt"]})
        self.assertEqual(calls,
                         [("192.0.2.1", "list_files", {"path": "/tmp"})])

    def test_disable_remote_target(self):
        target = cp.configure_remote_target(
            "192.0.2.1", "vernon", approved_by="Vernon (owner)",
            log_dir=self.log_dir)
        cp.disable_remote_target(target, log_dir=self.log_dir)
        self.assertFalse(target.enabled)
        result = cp.remote_action(target, "list_files",
                                  log_dir=self.log_dir)
        self.assertEqual(result["status"], "remote_not_configured")

    def test_remote_transport_failure_recorded(self):
        def failing(target, action, arguments):
            raise RuntimeError("connection reset")

        target = cp.configure_remote_target(
            "192.0.2.1", "vernon", approved_by="Vernon (owner)",
            log_dir=self.log_dir)
        with self.assertRaises(cp.ComputerError):
            cp.remote_action(target, "list_files",
                             transport=failing, log_dir=self.log_dir)


class AuditLogTests(ComputerTestBase):
    def test_allowed_action_logs_attempt_then_outcome(self):
        cp.read_file(self.root, "notes.txt", log_dir=self.log_dir)
        log = cp.read_audit_log(self.log_dir)
        self.assertEqual(len(log["records"]), 2)
        self.assertEqual(log["records"][0]["event"], "attempt")
        self.assertEqual(log["records"][0]["decision"], "allowed")
        self.assertEqual(log["records"][1]["event"], "outcome")
        self.assertEqual(log["records"][1]["outcome"], "ok")
        self.assertEqual(log["skipped_corrupt_lines"], 0)

    def test_refused_action_is_logged(self):
        with self.assertRaises(cp.RefusedActionError):
            cp.run_command(self.root, "rm", ["-rf", "/"], approved=True,
                           log_dir=self.log_dir)
        log = cp.read_audit_log(self.log_dir)
        self.assertEqual(len(log["records"]), 1)
        self.assertEqual(log["records"][0]["decision"], "refused")
        self.assertEqual(log["records"][0]["outcome"], "refused")
        self.assertEqual(log["records"][0]["action"], "run_command")

    def test_audit_log_round_trip_and_corrupt_line(self):
        cp.list_directory(self.root, log_dir=self.log_dir)
        path = Path(self.log_dir) / "actions.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        log = cp.read_audit_log(self.log_dir)
        self.assertEqual(log["skipped_corrupt_lines"], 1)
        self.assertEqual(len(log["records"]), 2)

    def test_empty_log_dir(self):
        log = cp.read_audit_log(self.log_dir)
        self.assertEqual(log["records"], [])
        self.assertEqual(log["skipped_corrupt_lines"], 0)


class PerformActionTests(ComputerTestBase):
    def test_perform_read_file(self):
        result = cp.perform_action(
            self.root, "read_file", {"path": "notes.txt"},
            log_dir=self.log_dir)
        self.assertEqual(result["content"], "MSFT notes\n")
        self.assertIn("record_id", result)

    def test_perform_unknown_action_refused_not_raised(self):
        result = cp.perform_action(
            self.root, "delete_everything", {}, log_dir=self.log_dir)
        self.assertEqual(result["status"], "refused")
        self.assertIn("unknown action", result["reason"])

    def test_perform_approval_required_without_approval(self):
        result = cp.perform_action(
            self.root, "write_file",
            {"path": "x.txt", "content": "x"}, approved=False,
            log_dir=self.log_dir)
        self.assertEqual(result["status"], "refused")
        self.assertFalse((self.root / "x.txt").exists())

    def test_perform_refused_command_recorded(self):
        result = cp.perform_action(
            self.root, "run_command", {"command": "rm", "args": []},
            approved=True, log_dir=self.log_dir)
        self.assertEqual(result["status"], "refused")

    def test_perform_remote_without_target(self):
        result = cp.perform_action(
            self.root, "remote_action",
            {"remote_action": "list_files"}, log_dir=self.log_dir)
        self.assertEqual(result["status"], "remote_not_configured")


class ComputerClassTests(ComputerTestBase):
    def test_computer_class(self):
        comp = cp.Computer(self.root, log_dir=self.log_dir)
        self.assertEqual(len(comp.capabilities()), 6)
        result = comp.action("list_directory", {"path": "."})
        self.assertIn("notes.txt", result["entries"])
        log = comp.audit()
        self.assertEqual(len(log["records"]), 2)

    def test_computer_remote_lifecycle(self):
        comp = cp.Computer(self.root, log_dir=self.log_dir)
        self.assertIsNone(comp.remote_target)
        target = comp.configure_remote("192.0.2.1", "vernon",
                                       approved_by="Vernon (owner)")
        self.assertTrue(target.enabled)
        result = comp.action("remote_action",
                             {"remote_action": "list_files"})
        self.assertEqual(result["status"], "remote_not_configured")
        comp.disable_remote()
        self.assertFalse(comp.remote_target.enabled)


class MsftReportReadTests(ComputerTestBase):
    def test_read_step29_msft_report_through_executor(self):
        report = Path(__file__).resolve().parent.parent / "step29-msft-report.md"
        # Copy the Step 29 MSFT report into a temp workspace and
        # read it back through the local executor.
        temp_root = Path(tempfile.mkdtemp(dir=self._tmp.name))
        self.addCleanup(lambda: shutil.rmtree(temp_root,
                                              ignore_errors=True))
        shutil.copy(report, temp_root / "step29-msft-report.md")
        direct = (temp_root / "step29-msft-report.md").read_text(
            encoding="utf-8")
        via_executor = cp.read_file(
            temp_root, "step29-msft-report.md", log_dir=self.log_dir)
        self.assertEqual(via_executor["content"], direct)
        self.assertIn("MICROSOFT CORP (MSFT)",
                      via_executor["content"])
        self.assertIn("partial", via_executor["content"])


if __name__ == "__main__":
    unittest.main()
