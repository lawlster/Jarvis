"""Step 35 — Watchlist and financial tracker foundation: tests.

Fixture data, temp directories and temp DB copies only; no
network, no production writes.  Every test cleans up after
itself via TemporaryDirectory (the /tmp housekeeping gap noted
in Steps 27/33b: older _temp_db_copy helpers never delete their
copies).  The production DB is copied once per test class into
the temp directory and the copy is removed in tearDownClass;
the production content fingerprint is asserted unchanged.
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agents import watchlist_foundation as wf

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"


class _TempDbTestCase(unittest.TestCase):
    """A temp DB copy shared by the class; removed afterwards."""

    db_path = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        cls.db_path = Path(cls.tmp.name) / "copy.db"
        shutil.copy(PROD_DB, cls.db_path)
        cls.hash_before = content_fingerprint(cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)

    @classmethod
    def tearDownClass(cls):
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, (
            "production DB changed during Step 35 tests")

    def setUp(self):
        self.store = tempfile.TemporaryDirectory(prefix="step35-store-")
        self.store_dir = Path(self.store.name)

    def tearDown(self):
        self.store.cleanup()

    def _conn(self):
        return sqlite3.connect(self.db_path)


class TestWatchlistStore(_TempDbTestCase):

    def test_add_list_remove(self):
        entry, action = wf.add_ticker(
            "msft", "owner_core", reason="core watch",
            company_name="Microsoft Corporation",
            store_dir=self.store_dir)
        self.assertEqual(action, "added")
        self.assertEqual(entry["ticker"], "MSFT")
        self.assertEqual(entry["sources"], ["owner_core"])
        self.assertEqual(wf.list_tickers(self.store_dir), ["MSFT"])
        self.assertEqual(wf.get_entry("MSFT", self.store_dir)["reason"],
                         "core watch")
        self.assertTrue(wf.remove_ticker("MSFT", self.store_dir))
        self.assertEqual(wf.list_tickers(self.store_dir), [])
        self.assertFalse(wf.remove_ticker("MSFT", self.store_dir))

    def test_add_empty_ticker_rejected(self):
        with self.assertRaises(ValueError):
            wf.add_ticker("  ", "owner_core", store_dir=self.store_dir)

    def test_dedupe_merges_sources(self):
        wf.add_ticker("GOOGL", "owner_core", reason="core",
                      store_dir=self.store_dir)
        entry, action = wf.add_ticker(
            "googl", "ackman_13f_q2_2026", store_dir=self.store_dir)
        self.assertEqual(action, "updated")
        self.assertEqual(len(wf.load_watchlist(self.store_dir)), 1)
        self.assertEqual(entry["sources"],
                         ["owner_core", "ackman_13f_q2_2026"])
        self.assertEqual(entry["reason"], "core")

    def test_record_exit_removes_source_keeps_note(self):
        wf.add_ticker("GOOGL", "owner_core", store_dir=self.store_dir)
        wf.add_ticker("GOOGL", "ackman_13f_q2_2026",
                      store_dir=self.store_dir)
        entry = wf.record_exit(
            "GOOGL", "ackman_13f_q2_2026",
            wf.ACKMAN_GOOGL_EXIT_NOTE, store_dir=self.store_dir)
        self.assertEqual(entry["sources"], ["owner_core"])
        self.assertIn(wf.ACKMAN_GOOGL_EXIT_NOTE, entry["notes"])
        # Re-recording the same exit does not duplicate the note.
        wf.record_exit("GOOGL", "ackman_13f_q2_2026",
                       wf.ACKMAN_GOOGL_EXIT_NOTE,
                       store_dir=self.store_dir)
        self.assertEqual(
            wf.get_entry("GOOGL", self.store_dir)["notes"],
            [wf.ACKMAN_GOOGL_EXIT_NOTE])

    def test_add_note_unknown_ticker_rejected(self):
        with self.assertRaises(ValueError):
            wf.add_note("ZZZZ", "a note", store_dir=self.store_dir)

    def test_store_round_trip(self):
        wf.add_ticker("MSFT", "ackman_13f_q2_2026",
                      company_name="Microsoft Corporation",
                      store_dir=self.store_dir)
        raw = json.loads((self.store_dir / "watchlist.json").read_text())
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["entries"][0]["ticker"], "MSFT")
        self.assertEqual(wf.load_watchlist(self.store_dir)[0]["ticker"],
                         "MSFT")


class TestSeed(_TempDbTestCase):

    def test_seed_size_and_dedupe(self):
        entries = wf.seed_watchlist(store_dir=self.store_dir)
        # 38 unique tickers: GOOGL is one entry, not two.
        self.assertEqual(len(entries), 38)
        self.assertEqual(len({e["ticker"] for e in entries}), 38)

    def test_seed_googl_ackman_exit_note(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        googl = wf.get_entry("GOOGL", self.store_dir)
        self.assertEqual(googl["sources"], ["owner_core"])
        self.assertIn(wf.ACKMAN_GOOGL_EXIT_NOTE, googl["notes"])

    def test_seed_universe_ticker_aliases(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        self.assertEqual(
            wf.get_entry("BRK.B", self.store_dir)["universe_ticker"],
            "BRK-B")
        self.assertEqual(
            wf.get_entry("DBS", self.store_dir)["universe_ticker"], "D05")
        self.assertEqual(
            wf.get_entry("OCBC", self.store_dir)["universe_ticker"], "O39")
        self.assertEqual(
            wf.get_entry("UOB", self.store_dir)["universe_ticker"], "U11")
        self.assertIsNone(
            wf.get_entry("MSFT", self.store_dir)["universe_ticker"])

    def test_seed_sources_cover_every_group(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        sources = {s for e in wf.load_watchlist(self.store_dir)
                   for s in e["sources"]}
        self.assertEqual(sources, {
            "owner_core", "owner_semis", "owner_basket_1000",
            "owner_combined_screen", "pabrai_13f_q2_2026",
            "ackman_13f_q2_2026", "owner_sg_banks"})

    def test_seed_is_idempotent(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        again = wf.seed_watchlist(store_dir=self.store_dir)
        self.assertEqual(len(again), 38)
        self.assertEqual(
            wf.get_entry("GOOGL", self.store_dir)["notes"],
            [wf.ACKMAN_GOOGL_EXIT_NOTE])


class TestRefresh(_TempDbTestCase):

    def test_refresh_msft_only_scannable(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        with self._conn() as conn:
            result = wf.refresh_watchlist(
                conn, store_dir=self.store_dir,
                checked_at="2026-11-09T00:00:00+00:00")
        self.assertEqual(result["summary"]["tickers"], 38)
        self.assertEqual(result["summary"]["scannable"], 1)
        self.assertEqual(result["summary"]["insufficient_data"], 37)
        self.assertEqual(result["summary"]["tiers"],
                         {"overvalued": 1, "insufficient_data": 37})
        msft = next(r for r in result["records"] if r["ticker"] == "MSFT")
        self.assertEqual(msft["tier"], "overvalued")
        self.assertEqual(msft["quality_rating"], "excellent")
        self.assertEqual(msft["health_verdict"], "strong")
        self.assertEqual(msft["deterioration_overall_verdict"], "warning")
        self.assertFalse(msft["deterioration_material"])
        self.assertAlmostEqual(msft["price"], 507.12)
        self.assertEqual(msft["price_as_of"], "2026-11-03")
        self.assertAlmostEqual(
            msft["strongest_alert_threshold_price"], 78.82, places=2)
        self.assertAlmostEqual(msft["fair_value_range"]["base"],
                               163.30, places=2)
        self.assertIn("free cash flow softening", msft["risk_flags"])
        self.assertEqual(msft["last_checked"], "2026-11-09T00:00:00+00:00")

    def test_refresh_d05_insufficient_with_reason(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        with self._conn() as conn:
            result = wf.refresh_watchlist(conn, store_dir=self.store_dir)
        dbs = next(r for r in result["records"] if r["ticker"] == "DBS")
        self.assertFalse(dbs["scannable"])
        self.assertEqual(dbs["tier"], "insufficient_data")
        self.assertTrue(dbs["missing_inputs"])

    def test_refresh_writes_tracker_file(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        with self._conn() as conn:
            wf.refresh_watchlist(conn, store_dir=self.store_dir)
        raw = json.loads((self.store_dir / "tracker.json").read_text())
        self.assertEqual(len(raw["records"]), 38)
        self.assertEqual(
            wf.get_tracker_record("MSFT", self.store_dir)["tier"],
            "overvalued")

    def test_refresh_does_not_write_to_db(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        with self._conn() as conn:
            wf.refresh_watchlist(conn, store_dir=self.store_dir)
        self.assertEqual(content_fingerprint(self.db_path),
                         self.hash_before)

    def test_refresh_twice_replaces_records(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        with self._conn() as conn:
            wf.refresh_watchlist(conn, store_dir=self.store_dir,
                                 checked_at="2026-11-09T00:00:00+00:00")
            result = wf.refresh_watchlist(
                conn, store_dir=self.store_dir,
                checked_at="2026-11-10T00:00:00+00:00")
        self.assertEqual(len(result["records"]), 38)
        self.assertEqual(
            wf.get_tracker_record("MSFT", self.store_dir)["last_checked"],
            "2026-11-10T00:00:00+00:00")

    def test_refresh_with_caller_supplied_prices(self):
        wf.add_ticker("MSFT", "owner_core", store_dir=self.store_dir)
        with self._conn() as conn:
            result = wf.refresh_watchlist(
                conn, prices={"MSFT": 530.61}, store_dir=self.store_dir)
        msft = result["records"][0]
        self.assertAlmostEqual(msft["price"], 530.61)

    def test_refresh_empty_watchlist(self):
        with self._conn() as conn:
            result = wf.refresh_watchlist(conn, store_dir=self.store_dir)
        self.assertEqual(result["summary"], {
            "tickers": 0, "scannable": 0, "insufficient_data": 0,
            "tiers": {}, "last_checked": result["summary"]["last_checked"]})


class TestView(_TempDbTestCase):

    def _refreshed(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        with self._conn() as conn:
            wf.refresh_watchlist(conn, store_dir=self.store_dir,
                                 checked_at="2026-11-09T00:00:00+00:00")
        return wf.load_tracker(self.store_dir)

    def test_msft_view_line(self):
        tracker = self._refreshed()
        line = wf.view_line(tracker["MSFT"])
        self.assertEqual(line["ticker"], "MSFT")
        self.assertEqual(line["tier"], "overvalued")
        self.assertIn("overvalued", line["note"])
        self.assertIn("quality excellent", line["note"])
        self.assertIn("health strong", line["note"])
        self.assertIn("$507.12", line["note"])
        self.assertIn("alert price $78.82", line["note"])
        self.assertIn("deterioration warning", line["note"])
        self.assertIn("free cash flow softening", line["note"])
        self.assertIn("2026-11-09T00:00:00+00:00", line["note"])

    def test_insufficient_view_line(self):
        tracker = self._refreshed()
        line = wf.view_line(tracker["DBS"])
        self.assertIn("insufficient data", line["note"])
        self.assertEqual(line["tier"], "insufficient_data")

    def test_watchlist_view_covers_all(self):
        tracker = self._refreshed()
        view = wf.watchlist_view(store_dir=self.store_dir)
        self.assertEqual(len(view), 38)
        self.assertEqual([v["ticker"] for v in view],
                         sorted(tracker))

    def test_view_lines_carry_no_recommendation(self):
        tracker = self._refreshed()
        for line in wf.watchlist_view(store_dir=self.store_dir):
            for word in ("buy", "sell"):
                self.assertNotIn(f" {word} ", f" {line['note'].lower()} ")


class TestLegacyUntouched(_TempDbTestCase):

    def test_legacy_watchlist_still_three_rows(self):
        # The legacy DB table is not synced from the JSON store:
        # old modules keep their own list.
        self.assertEqual(wf.legacy_watchlist_count(str(self.db_path)), 3)

    def test_production_hash_unchanged(self):
        self.assertEqual(content_fingerprint(PROD_DB),
                         self._prod_fp_before)


if __name__ == "__main__":
    unittest.main()
