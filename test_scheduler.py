"""Tests for Step 46 — reminders and scheduled automation.

Fake clock only: every test passes an explicit ``now``. No real
time waits, no network, no production writes. The production
database is copied to a temporary directory for the built-in
action tests and its content fingerprint is asserted unchanged.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
from agent import scheduler as sc

SGT = ZoneInfo("Asia/Singapore")
UTC = timezone.utc

# Monday 2026-11-09 09:00 Asia/Singapore (01:00 UTC).
MON_0900 = datetime(2026, 11, 9, 9, 0, tzinfo=SGT)
MON_0936 = datetime(2026, 11, 9, 9, 36, tzinfo=SGT)
MON_1000 = datetime(2026, 11, 9, 10, 0, tzinfo=SGT)
SUN_0900 = datetime(2026, 11, 8, 9, 0, tzinfo=SGT)


class SchedulerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = temp_dir_with_db_cleanup()
        cls.store = Path(cls._tmp.name) / "scheduler"
        cls.db_path = Path(cls._tmp.name) / "assistant.db"
        prod = PROJECT_ROOT / "database" / "assistant.db"
        shutil.copy(prod, cls.db_path)
        cls._prod_fp_before = content_fingerprint(prod)

    @classmethod
    def tearDownClass(cls):
        cleanup_temp_dir(cls._tmp)
        prod = PROJECT_ROOT / "database" / "assistant.db"
        assert content_fingerprint(prod) == cls._prod_fp_before, \
            "production DB changed"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=self._tmp.name))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def _actions(self):
        calls = []

        def ok_action(**args):
            calls.append(args)
            return {"summary": "ran fine", "detail": {"n": 1}}

        def boom_action(**args):
            raise RuntimeError("action blew up")

        return {"ok_action": ok_action, "boom_action": boom_action}, calls

    def _job(self, name="Watchlist check", kind="watchlist_check",
             schedule=None, payload=None, delivery=None, **kw):
        schedule = schedule or {"type": "daily", "time": "09:36",
                                "timezone": "Asia/Singapore"}
        payload = payload if payload is not None else {
            "action": "watchlist_check",
            "args": {"db_path": str(self.db_path),
                     "watchlist": ["MSFT"],
                     "state_path": str(self.dir / "monitor.json")}}
        return sc.new_job(name, kind, schedule, payload,
                          delivery=delivery,
                          now=kw.pop("now", MON_0900), **kw)


class TestJobCreation(SchedulerTestBase):
    def test_create_each_kind(self):
        kinds = {
            "reminder": {"action": "reminder",
                         "message": "NVDA earnings are on 17 Nov"},
            "daily_summary": {"action": "daily_summary", "args": {}},
            "watchlist_check": {"action": "watchlist_check",
                                "args": {}},
            "alert_digest": {"action": "alert_digest", "args": {}},
        }
        for kind, payload in kinds.items():
            job = self._job(name=f"A {kind}", kind=kind, payload=payload)
            self.assertEqual(job["kind"], kind)
            self.assertTrue(job["enabled"])
            self.assertEqual(job["created_by"], "owner")
            self.assertIsNotNone(job["next_run"])

    def test_each_schedule_shape(self):
        once = self._job(
            schedule={"type": "once",
                      "at": "2026-11-17T08:30:00+08:00",
                      "timezone": "Asia/Singapore"})
        self.assertEqual(once["next_run"], "2026-11-17T08:30:00+08:00")
        daily = self._job(
            schedule={"type": "daily", "time": "09:36",
                      "timezone": "Asia/Singapore"})
        self.assertEqual(daily["next_run"], MON_0936.isoformat())
        weekly = self._job(
            schedule={"type": "weekly", "weekday": 0, "time": "09:36",
                      "timezone": "Asia/Singapore"})
        self.assertEqual(weekly["next_run"], MON_0936.isoformat())

    def test_timezone_required(self):
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "daily", "time": "09:36"})
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "daily", "time": "09:36",
                                "timezone": "Not/AZone"})
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "once",
                                "at": "2026-11-17T08:30:00",
                                "timezone": "Asia/Singapore"})

    def test_bad_schedule_shapes(self):
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "hourly", "time": "09:36",
                                "timezone": "Asia/Singapore"})
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "daily", "time": "25:00",
                                "timezone": "Asia/Singapore"})
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "weekly", "weekday": 7,
                                "time": "09:36",
                                "timezone": "Asia/Singapore"})
        with self.assertRaises(sc.SchedulerError):
            self._job(schedule={"type": "once", "at": "tomorrow",
                                "timezone": "Asia/Singapore"})

    def test_unknown_kind_rejected(self):
        with self.assertRaises(sc.SchedulerError):
            self._job(kind="trade")


class TestForbiddenPayloads(SchedulerTestBase):
    def test_forbidden_actions_refused(self):
        for action in ("buy_shares", "place_order", "send_email",
                       "schedule_reminder", "trade_msft", "sell_stock",
                       "telegram_alert", "short_msft"):
            with self.assertRaises(sc.ForbiddenJobError, msg=action):
                self._job(payload={"action": action, "args": {}})

    def test_innocent_action_allowed(self):
        # "buyback" is one token, not "buy" — same rule as Step 44.
        job = self._job(payload={"action": "buyback_info", "args": {}})
        self.assertEqual(job["payload"]["action"], "buyback_info")

    def test_reminder_trade_instruction_refused(self):
        for message in ("Buy 100 shares of MSFT at $500",
                        "Sell all my MSFT stock now",
                        "Place an order to buy NVDA",
                        "Trade 50 shares of GOOGL"):
            with self.assertRaises(sc.ForbiddenJobError, msg=message):
                sc.create_reminder(self.dir, message,
                                   "2026-11-17T08:30:00+08:00",
                                   "Asia/Singapore", now=MON_0900)

    def test_innocent_reminder_allowed(self):
        job = sc.create_reminder(
            self.dir, "NVDA earnings are reported after the close "
                      "on Tuesday 17 November",
            "2026-11-17T08:30:00+08:00", "Asia/Singapore",
            now=MON_0900)
        self.assertEqual(job["kind"], "reminder")
        self.assertEqual(job["next_run"], "2026-11-17T08:30:00+08:00")
        self.assertEqual(job["payload"]["action"], "reminder")


class TestNextRun(SchedulerTestBase):
    def test_daily_before_time_same_day(self):
        nxt = sc.compute_next_run(
            {"type": "daily", "time": "09:36",
             "timezone": "Asia/Singapore"}, after=MON_0900)
        self.assertEqual(nxt, MON_0936)

    def test_daily_after_time_next_day(self):
        nxt = sc.compute_next_run(
            {"type": "daily", "time": "09:36",
             "timezone": "Asia/Singapore"}, after=MON_1000)
        self.assertEqual(nxt, datetime(2026, 11, 10, 9, 36, tzinfo=SGT))

    def test_daily_exactly_at_time_is_next_day(self):
        nxt = sc.compute_next_run(
            {"type": "daily", "time": "09:36",
             "timezone": "Asia/Singapore"}, after=MON_0936)
        self.assertEqual(nxt, datetime(2026, 11, 10, 9, 36, tzinfo=SGT))

    def test_weekly_same_weekday_before_time(self):
        nxt = sc.compute_next_run(
            {"type": "weekly", "weekday": 0, "time": "09:36",
             "timezone": "Asia/Singapore"}, after=MON_0900)
        self.assertEqual(nxt, MON_0936)

    def test_weekly_from_sunday(self):
        nxt = sc.compute_next_run(
            {"type": "weekly", "weekday": 0, "time": "09:36",
             "timezone": "Asia/Singapore"}, after=SUN_0900)
        self.assertEqual(nxt, MON_0936)

    def test_weekly_after_time_next_week(self):
        nxt = sc.compute_next_run(
            {"type": "weekly", "weekday": 0, "time": "09:36",
             "timezone": "Asia/Singapore"}, after=MON_1000)
        self.assertEqual(nxt, datetime(2026, 11, 16, 9, 36, tzinfo=SGT))

    def test_once_future_and_past(self):
        nxt = sc.compute_next_run(
            {"type": "once", "at": "2026-11-17T08:30:00+08:00",
             "timezone": "Asia/Singapore"}, after=MON_0900)
        self.assertEqual(nxt.isoformat(), "2026-11-17T08:30:00+08:00")
        past = sc.compute_next_run(
            {"type": "once", "at": "2026-11-08T08:30:00+08:00",
             "timezone": "Asia/Singapore"}, after=MON_0900)
        self.assertIsNone(past)

    def test_naive_now_rejected(self):
        with self.assertRaises(sc.SchedulerError):
            sc.compute_next_run(
                {"type": "daily", "time": "09:36",
                 "timezone": "Asia/Singapore"},
                after=datetime(2026, 11, 9, 9, 0))


class TestDueCheck(SchedulerTestBase):
    def test_due_ordering_and_future_not_due(self):
        # Jobs created Sunday 09:00: "Early" (08:00 daily) is next
        # due Monday 08:00, "Late" (09:36 daily) Monday 09:36 —
        # both due at the Monday 09:36 check, in that order.
        early = self._job(name="Early", schedule={
            "type": "daily", "time": "08:00",
            "timezone": "Asia/Singapore"}, now=SUN_0900)
        late = self._job(name="Late", schedule={
            "type": "daily", "time": "09:36",
            "timezone": "Asia/Singapore"}, now=SUN_0900)
        future = self._job(name="Future", schedule={
            "type": "once", "at": "2026-11-17T08:30:00+08:00",
            "timezone": "Asia/Singapore"})
        due = sc.due_jobs([future, late, early], now=MON_0936)
        # Ordered by next run: "Late" (daily 09:36, created Sunday
        # 09:00) was next due Sunday 09:36, before "Early" (daily
        # 08:00, created Sunday 09:00) due Monday 08:00. "Future"
        # (17 Nov) is not due.
        self.assertEqual([j["name"] for j in due], ["Late", "Early"])

    def test_disabled_never_due(self):
        job = self._job(enabled=False)
        self.assertEqual(sc.due_jobs([job], now=MON_1000), [])

    def test_once_without_next_run_never_due(self):
        job = self._job(schedule={
            "type": "once", "at": "2026-11-08T08:30:00+08:00",
            "timezone": "Asia/Singapore"})
        self.assertIsNone(job["next_run"])
        self.assertEqual(sc.due_jobs([job], now=MON_1000), [])


class TestRunner(SchedulerTestBase):
    def test_run_success_records_and_advances(self):
        actions, calls = self._actions()
        job = self._job(payload={"action": "ok_action",
                                 "args": {"ticker": "MSFT"}})
        sc.save_job(job, self.dir)
        record = sc.run_job(job, now=MON_0936, actions=actions,
                            store_dir=self.dir)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["summary"], "ran fine")
        self.assertEqual(calls, [{"ticker": "MSFT"}])
        saved = sc.get_job(job["id"], self.dir)
        self.assertEqual(saved["last_run"], MON_0936.isoformat())
        self.assertEqual(saved["next_run"],
                         datetime(2026, 11, 10, 9, 36,
                                  tzinfo=SGT).isoformat())
        self.assertEqual(saved["last_status"], "ok")
        log = sc.read_run_log(self.dir)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["job_id"], job["id"])

    def test_run_not_due_is_skipped(self):
        actions, calls = self._actions()
        job = self._job(payload={"action": "ok_action", "args": {}})
        record = sc.run_job(job, now=MON_0900, actions=actions,
                            store_dir=self.dir)
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(calls, [])

    def test_run_disabled_is_skipped(self):
        actions, calls = self._actions()
        job = self._job(payload={"action": "ok_action", "args": {}},
                        enabled=False)
        record = sc.run_job(job, now=MON_1000, actions=actions,
                            store_dir=self.dir)
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(calls, [])

    def test_run_failure_recorded_and_job_advances(self):
        actions, _calls = self._actions()
        job = self._job(payload={"action": "boom_action", "args": {}})
        record = sc.run_job(job, now=MON_0936, actions=actions,
                            store_dir=self.dir)
        self.assertEqual(record["status"], "failed")
        self.assertIn("RuntimeError", record["error"])
        saved = sc.get_job(job["id"], self.dir)
        self.assertEqual(saved["last_status"], "failed")
        self.assertEqual(saved["next_run"],
                         datetime(2026, 11, 10, 9, 36,
                                  tzinfo=SGT).isoformat())

    def test_failure_does_not_block_others(self):
        actions, calls = self._actions()
        bad = self._job(name="Bad", payload={"action": "boom_action",
                                             "args": {}})
        good = self._job(name="Good", payload={"action": "ok_action",
                                               "args": {}})
        sc.save_job(bad, self.dir)
        sc.save_job(good, self.dir)
        records = sc.run_due_jobs(self.dir, now=MON_0936,
                                  actions=actions)
        self.assertEqual([r["status"] for r in records],
                         ["failed", "ok"])
        self.assertEqual(len(calls), 1)

    def test_missing_action_function_is_failed(self):
        job = self._job(payload={"action": "no_such_action",
                                 "args": {}})
        record = sc.run_job(job, now=MON_0936, actions={},
                            store_dir=self.dir)
        self.assertEqual(record["status"], "failed")
        self.assertIn("no action function", record["error"])

    def test_once_job_runs_once_then_never_again(self):
        actions, calls = self._actions()
        job = self._job(
            schedule={"type": "once",
                      "at": "2026-11-09T09:36:00+08:00",
                      "timezone": "Asia/Singapore"},
            payload={"action": "ok_action", "args": {}})
        record = sc.run_job(job, now=MON_0936, actions=actions,
                            store_dir=self.dir)
        self.assertEqual(record["status"], "ok")
        saved = sc.get_job(job["id"], self.dir)
        self.assertIsNone(saved["next_run"])
        second = sc.run_job(saved, now=MON_1000, actions=actions,
                            store_dir=self.dir)
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(len(calls), 1)

    def test_reminder_runs_and_delivers_message(self):
        delivery = sc.StubDelivery()
        job = sc.create_reminder(
            self.dir, "NVDA earnings are reported after the close "
                      "on Tuesday 17 November",
            "2026-11-17T08:30:00+08:00", "Asia/Singapore",
            now=MON_0900)
        when = datetime(2026, 11, 17, 8, 30, tzinfo=SGT)
        record = sc.run_job(job, now=when, actions={},
                            delivery=delivery, store_dir=self.dir)
        self.assertEqual(record["status"], "ok")
        self.assertIn("NVDA earnings", record["summary"])
        self.assertEqual(len(delivery.deliveries), 1)
        self.assertIn("NVDA earnings",
                      delivery.deliveries[0]["body"])
        self.assertFalse(delivery.deliveries[0]["delivered"])

    def test_run_log_round_trip_and_corrupt_line(self):
        actions, _calls = self._actions()
        job = self._job(payload={"action": "ok_action", "args": {}})
        sc.run_job(job, now=MON_0936, actions=actions, store_dir=self.dir)
        path = self.dir / "runs.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        log = sc.read_run_log(self.dir)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "ok")


class TestDelivery(SchedulerTestBase):
    def test_stub_delivery_records_nothing_really_sent(self):
        delivery = sc.StubDelivery()
        receipt = delivery.deliver("owner", "Subject", "Body")
        self.assertFalse(receipt["delivered"])
        self.assertEqual(receipt["channel"], "stub")
        self.assertIn("nothing was", receipt["note"])
        self.assertEqual(len(delivery.deliveries), 1)
        self.assertEqual(delivery.deliveries[0]["receipt_id"],
                         receipt["receipt_id"])

    def test_stub_delivery_file_persistence(self):
        path = self.dir / "deliveries.jsonl"
        delivery = sc.StubDelivery(deliveries_path=path)
        delivery.deliver("owner", "S", "B")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(json.loads(lines[0])["delivered"])

    def test_delivery_failure_recorded_not_hidden(self):
        class BadDelivery(sc.DeliveryClient):
            def deliver(self, recipient, subject, body):
                raise ConnectionError("no channel")

        actions, _calls = self._actions()
        job = self._job(
            payload={"action": "ok_action", "args": {}},
            delivery={"channel": "stub", "recipient": "owner"})
        record = sc.run_job(job, now=MON_0936, actions=actions,
                            delivery=BadDelivery(), store_dir=self.dir)
        self.assertEqual(record["status"], "failed")
        self.assertIn("delivery failed", record["error"])

    def test_no_delivery_when_none_configured(self):
        actions, _calls = self._actions()
        job = self._job(payload={"action": "ok_action", "args": {}})
        delivery = sc.StubDelivery()
        record = sc.run_job(job, now=MON_0936, actions=actions,
                            delivery=delivery, store_dir=self.dir)
        self.assertEqual(record["status"], "ok")
        self.assertIsNone(record["delivery_receipt"])
        self.assertEqual(delivery.deliveries, [])


class TestStore(SchedulerTestBase):
    def test_save_load_round_trip(self):
        job = self._job()
        sc.save_job(job, self.dir)
        jobs, skipped = sc.load_jobs(self.dir)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(jobs[0]["id"], job["id"])

    def test_corrupt_job_file_skipped(self):
        good = self._job()
        sc.save_job(good, self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "bad.json").write_text("{not json",
                                           encoding="utf-8")
        (self.dir / "alsobad.json").write_text(
            json.dumps({"kind": "watchlist_check"}), encoding="utf-8")
        jobs, skipped = sc.load_jobs(self.dir)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(sorted(skipped), ["alsobad.json", "bad.json"])

    def test_get_and_delete_job(self):
        job = self._job()
        sc.save_job(job, self.dir)
        self.assertEqual(sc.get_job(job["id"], self.dir)["name"],
                         job["name"])
        self.assertTrue(sc.delete_job(job["id"], self.dir))
        self.assertIsNone(sc.get_job(job["id"], self.dir))
        self.assertFalse(sc.delete_job(job["id"], self.dir))

    def test_scheduler_class(self):
        calls = []

        def ok_action(**args):
            calls.append(1)
            return {"summary": "ok"}

        sched = sc.Scheduler(self.dir, actions={"ok_action": ok_action},
                             delivery=sc.StubDelivery())
        job = sched.create_job(
            "Daily check", "watchlist_check",
            {"type": "daily", "time": "09:36",
             "timezone": "Asia/Singapore"},
            {"action": "ok_action", "args": {}},
            delivery={"channel": "stub", "recipient": "owner"},
            now=MON_0900)
        self.assertEqual(len(sched.jobs()), 1)
        self.assertEqual(len(sched.due(now=MON_0900)), 0)
        records = sched.run_due(now=MON_0936)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(len(sched.run_log()), 1)
        self.assertEqual(len(sched.delivery.deliveries), 1)


class TestBuiltinActions(SchedulerTestBase):
    def test_watchlist_check_on_temp_db(self):
        actions = sc.builtin_actions()
        result = actions["watchlist_check"](
            db_path=str(self.db_path), watchlist=["MSFT"],
            state_path=str(self.dir / "monitor.json"),
            news_dir=str(self.dir / "news"),
            events_dir=str(self.dir / "events"),
            as_of="2026-11-09")
        self.assertIn("Watchlist check:", result["summary"])
        self.assertIn("1 ticker(s)", result["summary"])

    def test_watchlist_check_no_db(self):
        result = sc.builtin_actions()["watchlist_check"](
            db_path=str(self.dir / "missing.db"), watchlist=["MSFT"])
        self.assertIn("unavailable", result["summary"])

    def test_daily_summary_on_temp_db(self):
        result = sc.builtin_actions()["daily_summary"](
            db_path=str(self.db_path), date="2026-11-09",
            tickers=["MSFT"], watchlist=["MSFT"],
            news_dir=str(self.dir / "news"))
        self.assertTrue(result["summary"])
        self.assertIn("MSFT", str(result["detail"]))

    def test_alert_digest_empty_state(self):
        result = sc.builtin_actions()["alert_digest"](
            state_path=str(self.dir / "alerts.json"))
        self.assertEqual(result["summary"],
                         "Alert digest: no active alerts.")
        self.assertEqual(result["detail"]["active_alerts"], [])

    def test_alert_digest_with_active_alert(self):
        from agents import watchlist_alerts as wa

        state_path = str(self.dir / "alerts.json")
        state = wa.new_state()
        state["active_alerts"]["MSFT:watch_discount"] = {
            "alert_key": "MSFT:watch_discount",
            "ticker": "MSFT", "rule": "watch_discount",
            "severity": "watch", "raised_at": "2026-11-09T01:00:00Z",
            "briefing_line": "MSFT: watch discount — price below base",
        }
        wa.save_state(state, state_path)
        result = sc.builtin_actions()["alert_digest"](
            state_path=state_path)
        self.assertIn("1 active alert(s)", result["summary"])
        self.assertIn("MSFT", result["summary"])


if __name__ == "__main__":
    unittest.main()
