"""Tests for Step 33 — agents/event_tracking.py.

Fixture events / temporary directories and temporary database
copies only; no network requests, no production database
writes.

Fixture expectations:
- The seeded week ahead (Mon 9 – Fri 13 Nov 2026, plus NVDA
  on Tue 17 Nov and the 13F due window) is caller-supplied
  research from the owner's week ahead; the module never
  invents dates, so the seed list is the test's expected list.
- MSFT on a temp copy of the real DB: tier overvalued, quality
  excellent, health strong (Steps 23/24/18, verified in
  earlier steps). NVDA has no statements, so a post-event
  re-check comes back insufficient_data, never a crash.
- The production database has no events table and no event
  rows: events live only in JSON files under data/events/.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import test_business_quality as tbq
import test_market_scanner as tms
import test_undervalued_detector as tud
from agents import event_tracking as et
from agents import margin_of_safety as mos

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_fingerprint import content_fingerprint  # noqa: E402
DB_PATH = BASE_DIR / "database" / "assistant.db"
def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _sample_event(**overrides):
    event = {
        "ticker": "MSFT",
        "event_type": "earnings",
        "title": "MSFT earnings",
        "date": "2026-11-12",
        "time": None,
        "timezone": "Asia/Singapore",
        "source": "fixture",
        "notes": "",
    }
    event.update(overrides)
    return event


class TestEventValidation(unittest.TestCase):
    def test_add_and_list_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(tmp, **_sample_event())
            self.assertEqual(event["event_id"], "2026-11-12_MSFT_01")
            self.assertTrue(Path(event["file_path"]).exists())
            stored = et.list_events(tmp)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["title"], "MSFT earnings")
            self.assertIsNone(stored[0]["outcome"])

    def test_ticker_uppercased_and_market_event_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(
                tmp, ticker="msft", event_type="earnings",
                date="2026-11-12", title="MSFT earnings")
            self.assertEqual(event["ticker"], "MSFT")
            market = et.add_event(
                tmp, ticker=None, event_type="economic",
                date="2026-11-10", time="21:30",
                timezone="Asia/Singapore", title="US Oct CPI")
            self.assertIsNone(market["ticker"])
            self.assertEqual(market["event_id"], "2026-11-10_MARKET_01")

    def test_invalid_inputs_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker="MSFT", event_type="earnings",
                             date="12 Nov 2026", title="bad date")
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker="MSFT", event_type="earnings",
                             date="2026-13-01", title="bad month")
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker="MSFT", event_type="earnings",
                             date="2026-11-12", time="25:00",
                             title="bad time")
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker="MSFT", event_type="rumour",
                             date="2026-11-12", title="bad type")
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker="MSFT", event_type="earnings",
                             date="2026-11-12", time="21:30",
                             timezone="Mars/Olympus",
                             title="bad timezone")
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker=None, event_type="earnings",
                             date="2026-11-12",
                             title="earnings needs a ticker")
            with self.assertRaises(ValueError):
                et.add_event(tmp, ticker="MSFT", event_type="earnings",
                             date="2026-11-12", title="   ")
            self.assertEqual(et.list_events(tmp), [])

    def test_corrupt_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            et.add_event(tmp, **_sample_event())
            Path(tmp, "broken.json").write_text("{not json",
                                                encoding="utf-8")
            self.assertEqual(len(et.list_events(tmp)), 1)


class TestEventQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        et.add_event(self.dir, **_sample_event(
            ticker="KSPI", date="2026-11-09", title="KSPI earnings"))
        et.add_event(self.dir, **_sample_event(
            ticker="ALC", date="2026-11-10", title="ALC earnings"))
        et.add_event(self.dir, ticker=None, event_type="economic",
                     date="2026-11-10", time="21:30",
                     timezone="Asia/Singapore", title="US Oct CPI")
        et.add_event(self.dir, **_sample_event(
            ticker="NVDA", date="2026-11-17", title="NVDA earnings",
            notes="After close."))

    def tearDown(self):
        self.tmp.cleanup()

    def test_events_on_and_in_range(self):
        on_10th = et.events_on(self.dir, "2026-11-10")
        self.assertEqual([e["ticker"] for e in on_10th], [None, "ALC"])
        # A timed event sorts before a dateless one on the same day.
        self.assertEqual(on_10th[0]["title"], "US Oct CPI")
        ranged = et.events_in_range(self.dir, "2026-11-09", "2026-11-10")
        self.assertEqual(len(ranged), 3)
        self.assertEqual(et.events_in_range(
            self.dir, "2026-11-12", "2026-11-16"), [])

    def test_events_for_ticker(self):
        self.assertEqual(
            [e["title"] for e in et.events_for_ticker(self.dir, "kspi")],
            ["KSPI earnings"])
        self.assertEqual(et.events_for_ticker(self.dir, "MSFT"), [])
        with self.assertRaises(ValueError):
            et.events_for_ticker(self.dir, None)

    def test_upcoming_and_reminders(self):
        upcoming = et.upcoming_events(self.dir, from_date="2026-11-09",
                                      days=7)
        self.assertEqual([e["date"] for e in upcoming],
                         ["2026-11-09", "2026-11-10", "2026-11-10"])
        self.assertEqual(
            [e["title"] for e in et.reminders(
                self.dir, from_date="2026-11-09", days=2)],
            ["KSPI earnings", "US Oct CPI", "ALC earnings"])
        self.assertEqual(
            et.upcoming_events(self.dir, from_date="2026-11-18"), [])

    def test_list_filters(self):
        self.assertEqual(
            len(et.list_events(self.dir, event_type="economic")), 1)
        self.assertEqual(
            len(et.list_events(self.dir, ticker="MARKET")), 1)
        self.assertEqual(
            len(et.list_events(self.dir, ticker="NVDA")), 1)


class TestWeekAhead(unittest.TestCase):
    def test_week_ahead_shape_and_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            added = et.seed_week_ahead(tmp)
            self.assertEqual(len(added), 12)
            week = et.week_ahead(tmp, start_date="2026-11-09")
            self.assertEqual(week["start_date"], "2026-11-09")
            self.assertEqual(week["end_date"], "2026-11-15")
            self.assertEqual(len(week["days"]), 7)
            self.assertEqual(week["days"][0]["weekday"], "Monday")
            self.assertEqual(week["days"][6]["weekday"], "Sunday")
            # Sunday has no events but is still listed.
            self.assertEqual(week["days"][6]["events"], [])
            self.assertEqual(week["event_count"], 10)
            # NVDA (17 Nov) is outside this week; the 13F event
            # (16 Nov) is outside it too.
            items = week["items"]
            self.assertEqual(len(items), 10)
            self.assertTrue(all(
                set(item) == {"item", "date"} for item in items))
            self.assertEqual(items[0],
                             {"item": "KSPI — KSPI earnings",
                              "date": "2026-11-09"})
            self.assertIn("21:30 Asia/Singapore",
                          [item["item"] for item in items
                           if "CPI" in item["item"]][0])
            # The items are exactly what Step 32 consumes.
            from agents import daily_summary as ds
            self.assertEqual(ds._upcoming_items(items), items)

    def test_seed_twice_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(et.seed_week_ahead(tmp)), 12)
            self.assertEqual(et.seed_week_ahead(tmp), [])
            self.assertEqual(len(et.list_events(tmp)), 12)

    def test_seed_week_starting_2026_11_16(self):
        with tempfile.TemporaryDirectory() as tmp:
            et.seed_week_ahead(tmp)
            week = et.week_ahead(tmp, start_date="2026-11-16")
            self.assertEqual(week["event_count"], 2)
            self.assertEqual(
                [e["title"] for e in week["days"][0]["events"]],
                ["Pabrai / Ackman Q3 13F filings due"])
            self.assertEqual(
                [e["title"] for e in week["days"][1]["events"]],
                ["NVDA earnings"])


class TestTimezones(unittest.TestCase):
    def test_event_datetime_and_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(
                tmp, ticker=None, event_type="economic",
                date="2026-11-10", time="21:30",
                timezone="Asia/Singapore", title="US Oct CPI")
            self.assertEqual(
                et.event_time_in_timezone(event, "UTC"),
                "13:30 UTC")
            self.assertEqual(
                et.event_time_in_timezone(event, "America/New_York"),
                "08:30 America/New_York")
            self.assertEqual(
                et.event_time_in_timezone(event, "Asia/Singapore"),
                "21:30 Asia/Singapore")
            self.assertEqual(
                et.event_datetime(event).isoformat(),
                "2026-11-10T21:30:00+08:00")

    def test_date_only_event_has_no_datetime(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(tmp, **_sample_event())
            self.assertIsNone(et.event_datetime(event))
            self.assertIsNone(
                et.event_time_in_timezone(event, "UTC"))
            self.assertEqual(
                et.event_display(event),
                "2026-11-12: MSFT — MSFT earnings")

    def test_event_display_with_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(
                tmp, ticker=None, event_type="economic",
                date="2026-11-10", time="21:30",
                timezone="Asia/Singapore", title="US Oct CPI")
            self.assertEqual(
                et.event_display(event),
                "2026-11-10 21:30 Asia/Singapore: US Oct CPI")


class TestOutcomes(unittest.TestCase):
    def test_record_outcome_with_surprise(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(tmp, **_sample_event())
            updated = et.record_outcome(event, {
                "reported_eps": 1.32, "expected_eps": 1.20,
                "reported_revenue": 100.0, "expected_revenue": 95.0,
                "notes": "Beat on both."}, events_dir=tmp)
            outcome = updated["outcome"]
            self.assertAlmostEqual(outcome["eps_surprise_pct"], 0.10)
            self.assertAlmostEqual(
                outcome["revenue_surprise_pct"], 0.0526315789)
            self.assertIsNotNone(updated["outcome_recorded_at"])
            reloaded = et.load_event(updated["file_path"])
            self.assertEqual(reloaded["outcome"], outcome)

    def test_record_outcome_rejects_empty_and_bad_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = et.add_event(tmp, **_sample_event())
            with self.assertRaises(ValueError):
                et.record_outcome(event, {}, events_dir=tmp)
            with self.assertRaises(ValueError):
                et.record_outcome(event, "beat", events_dir=tmp)
            # A non-numeric reported value is stored, not crashed
            # on: surprise is simply not computed.
            updated = et.record_outcome(
                event, {"reported_eps": "high", "expected_eps": 1.20},
                events_dir=tmp)
            self.assertNotIn("eps_surprise_pct", updated["outcome"])


class TestPostEventCheck(unittest.TestCase):
    def test_msft_unchanged_before_and_after(self):
        db_path = _temp_db_copy()
        try:
            conn = sqlite3.connect(db_path)
            event = _sample_event()
            before = mos.margin_of_safety_summary(conn, "MSFT")
            before["health_verdict"] = "strong"
            result = et.post_event_check(conn, event,
                                         before_summary=before)
            self.assertEqual(result["after_tier"], "overvalued")
            self.assertEqual(result["after"]["quality_rating"],
                             "excellent")
            self.assertEqual(result["after"]["health_verdict"],
                             "strong")
            self.assertEqual(result["changes"], [])
            self.assertIsNone(result["after_error"])
        finally:
            conn.close()
            os.unlink(db_path)

    def test_tier_change_is_reported(self):
        rows = tbq._company_rows("FIXA", tbq.MID_SPEC)
        universe = [tms._universe_row("FIXA")]
        conn = tud._fixture_conn(rows, universe)
        event = _sample_event(ticker="FIXA", title="FIXA earnings",
                              date="2026-11-12")
        before = mos.margin_of_safety_summary(
            conn, "FIXA", price_input={"price": 5.0},
            starting_fcf_basis="annual")
        self.assertEqual(before["tier"]["tier"], "strongest_alert")
        # The valuation range does not depend on the price, so
        # only the tier changes when the price moves; the health
        # verdict is supplied here so it does not show up as a
        # spurious change (the fixture's health verdict is
        # "strong", as the Step 27 probe showed).
        before["health_verdict"] = "strong"
        # The same company at $25 is fairly valued, not an alert.
        result = et.post_event_check(conn, event,
                                     before_summary=before,
                                     price_input=25.0)
        self.assertEqual(result["after_tier"], "fairly_valued")
        self.assertEqual(
            [c["field"] for c in result["changes"]],
            ["tier"])
        self.assertEqual(result["changes"][0]["before"],
                         "strongest_alert")

    def test_nvda_insufficient_data_never_crashes(self):
        db_path = _temp_db_copy()
        try:
            conn = sqlite3.connect(db_path)
            event = _sample_event(ticker="NVDA", title="NVDA earnings",
                                  date="2026-11-17")
            result = et.post_event_check(conn, event)
            self.assertEqual(result["after_tier"], "insufficient_data")
            self.assertIsNone(result["after_error"])
            self.assertEqual(result["changes"], [])
        finally:
            conn.close()
            os.unlink(db_path)

    def test_market_event_has_no_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(":memory:")
            event = et.add_event(
                tmp, ticker=None, event_type="economic",
                date="2026-11-10", time="21:30",
                timezone="Asia/Singapore", title="US Oct CPI")
            result = et.post_event_check(conn, event)
            self.assertIsNone(result["after"])
            self.assertIn("no ticker", result["after_error"])
            conn.close()


class TestRealDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before_hash = content_fingerprint(DB_PATH)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(DB_PATH) == cls.before_hash, (
            "production DB changed during Step 33 tests")

    def test_no_events_in_production_db(self):
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertNotIn("events", tables)
            self.assertNotIn("calendar_events", tables)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM financial_statements "
                    "WHERE ticker='MSFT'").fetchone()[0], 242)
        finally:
            conn.close()

    def test_production_hash_unchanged(self):
        self.assertEqual(content_fingerprint(DB_PATH), self.before_hash)


if __name__ == "__main__":
    unittest.main()
