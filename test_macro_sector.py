"""Tests for Step 33b — agents/macro_sector.py.

Fixture data + temporary directories / temporary database copies
only; no network requests, no production database writes.

Fixture expectations (verified by probe against the real
Step 28b profiles before these tests were written):
- Three-company fixture (MID $5.00, BIG $20.00, WEAK $0.30):
  revenue median 1220, net margin median 0.127, ROIC median
  0.152, P/E median 4.474, DCF base median 23.04, D/E median
  0.316; quality {good: 2, weak: 1}; tier {strongest_alert: 2,
  reasonable_price: 1}.
- Sector [MID, BIG] vs market [MID, BIG, WEAK]: net margin
  sector median 0.155 vs market 0.127 (difference +0.028,
  +22.1%); revenue 1830 vs 1220 (difference +610, +50.0%).
- MSFT on a temp copy of the real DB: every aggregate is a
  single-company figure (revenue $331,839,000,000, net margin
  0.403, ROIC 0.306, P/E 28.25, DCF base $163.30), quality
  excellent, tier overvalued; selection by stored sector is
  empty (sector NULL for every US/HK row).
"""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import test_business_quality as tbq
import test_company_comparison as tcc
import test_market_scanner as tms
from agents import macro_sector as mcs

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def _db_hash():
    return hashlib.sha256(DB_PATH.read_bytes()).hexdigest()


def _fixture_conn():
    return tcc._fixture_conn()


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


class TestMacroStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.macro_dir = Path(self.tmp.name) / "macro"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_list_round_trip(self):
        point = mcs.add_macro_point(
            self.macro_dir, indicator="VIX", value=28.18,
            unit="index", date="2026-11-05", source="owner research")
        self.assertEqual(point["indicator"], "VIX")
        self.assertEqual(point["value"], 28.18)
        self.assertEqual(point["date"], "2026-11-05")
        self.assertTrue(point["macro_id"].startswith("2026-11-05_vix_"))
        self.assertTrue(Path(point["file_path"]).exists())
        listed = mcs.list_macro_points(self.macro_dir)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["value"], 28.18)

    def test_add_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            mcs.add_macro_point(self.macro_dir, indicator="  ",
                                value=1.0, date="2026-11-05")
        with self.assertRaises(ValueError):
            mcs.add_macro_point(self.macro_dir, indicator="CPI",
                                value="high", date="2026-11-05")
        with self.assertRaises(ValueError):
            mcs.add_macro_point(self.macro_dir, indicator="CPI",
                                value=True, date="2026-11-05")
        with self.assertRaises(ValueError):
            mcs.add_macro_point(self.macro_dir, indicator="CPI",
                                value=1.0, date="2026-13-40")
        with self.assertRaises(ValueError):
            mcs.add_macro_point(self.macro_dir, indicator="CPI",
                                value=1.0, date="2026-11-05",
                                unit=5)

    def test_list_filters(self):
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=20.0,
                            date="2026-11-03", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=28.18,
                            date="2026-11-05", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="CPI", value=3.1,
                            unit="%", date="2026-11-04",
                            source="owner research")
        self.assertEqual(
            len(mcs.list_macro_points(self.macro_dir)), 3)
        self.assertEqual(
            len(mcs.list_macro_points(self.macro_dir, indicator="vix")), 2)
        self.assertEqual(
            len(mcs.list_macro_points(self.macro_dir, since="2026-11-04")),
            2)
        self.assertEqual(
            len(mcs.list_macro_points(self.macro_dir, until="2026-11-03")),
            1)
        # Newest first.
        self.assertEqual(
            mcs.list_macro_points(self.macro_dir)[0]["date"], "2026-11-05")

    def test_corrupt_file_skipped(self):
        self.macro_dir.mkdir(parents=True)
        (self.macro_dir / "2026-11-05_vix_01.json").write_text(
            "{not json", encoding="utf-8")
        (self.macro_dir / "2026-11-05_cpi_01.json").write_text(
            json.dumps({"indicator": "CPI", "value": 3.1,
                        "date": "2026-11-05"}), encoding="utf-8")
        listed = mcs.list_macro_points(self.macro_dir)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["indicator"], "CPI")

    def test_snapshot_latest_per_indicator(self):
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=20.0,
                            date="2026-11-03", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=28.18,
                            date="2026-11-05", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=30.0,
                            date="2026-11-07", source="owner research")
        snap = mcs.macro_snapshot(self.macro_dir, as_of="2026-11-05")
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["indicators"]["vix"]["value"], 28.18)
        # The 2026-11-07 point must not leak into the earlier date.
        snap2 = mcs.macro_snapshot(self.macro_dir, as_of="2026-11-04")
        self.assertEqual(snap2["indicators"]["vix"]["value"], 20.0)

    def test_what_changed(self):
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=20.0,
                            date="2026-11-03", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=28.18,
                            date="2026-11-05", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="CPI", value=3.1,
                            date="2026-11-03", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="CPI", value=3.1,
                            date="2026-11-05", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="PPI", value=2.0,
                            date="2026-11-05", source="owner research")
        result = mcs.what_changed(self.macro_dir, earlier="2026-11-03",
                                  later="2026-11-05")
        self.assertEqual(len(result["changes"]), 1)
        change = result["changes"][0]
        self.assertEqual(change["indicator"], "VIX")
        self.assertAlmostEqual(change["change"], 8.18, places=2)
        self.assertAlmostEqual(change["change_pct"], 40.9, places=1)
        self.assertEqual(change["direction"], "up")
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(result["added"][0]["indicator"], "PPI")
        self.assertEqual(len(result["unchanged"]), 1)
        self.assertEqual(result["unchanged"][0]["indicator"], "CPI")
        self.assertEqual(result["dropped"], [])

    def test_what_changed_rejects_reversed_dates(self):
        with self.assertRaises(ValueError):
            mcs.what_changed(self.macro_dir, earlier="2026-11-05",
                             later="2026-11-03")

    def test_what_changed_zero_base_has_no_pct(self):
        mcs.add_macro_point(self.macro_dir, indicator="X", value=0.0,
                            date="2026-11-03", source="owner research")
        mcs.add_macro_point(self.macro_dir, indicator="X", value=1.5,
                            date="2026-11-05", source="owner research")
        result = mcs.what_changed(self.macro_dir, earlier="2026-11-03",
                                  later="2026-11-05")
        self.assertIsNone(result["changes"][0]["change_pct"])
        self.assertEqual(result["changes"][0]["direction"], "up")

    def test_relevant_macro_needs_caller_associations(self):
        mcs.add_macro_point(self.macro_dir, indicator="Policy rate",
                            value=4.0, unit="%", date="2026-11-05",
                            source="owner research")
        # No associations supplied: nothing is relevant by default.
        result = mcs.relevant_macro_points("banks", self.macro_dir)
        self.assertEqual(result["points"], {})
        self.assertIn("none supplied", result["associations_source"])
        # Caller-supplied associations: the rate is relevant.
        result = mcs.relevant_macro_points(
            "banks", self.macro_dir,
            associations={"banks": ["Policy rate", "CPI"]})
        self.assertEqual(
            result["points"]["Policy rate"]["value"], 4.0)
        self.assertIsNone(result["points"]["CPI"])
        self.assertEqual(result["missing"], ["CPI"])
        self.assertEqual(result["associations_source"], "caller-supplied")


class TestSectorAnalysis(unittest.TestCase):
    def setUp(self):
        self.conn = _fixture_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.macro_dir = Path(self.tmp.name) / "macro"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_three_company_aggregates(self):
        res = mcs.analyse_sector(
            self.conn, sector="technology",
            tickers=["MID", "BIG", "WEAK"], prices=tcc.PRICES)
        self.assertEqual(res["coverage"]["selected"], 3)
        self.assertEqual(res["coverage"]["with_statements"], 3)
        self.assertEqual(res["coverage"]["scannable"], 3)
        self.assertEqual(res["quality_distribution"],
                         {"good": 2, "weak": 1})
        self.assertEqual(res["tier_distribution"],
                         {"strongest_alert": 2, "reasonable_price": 1})
        self.assertEqual(res["aggregates"]["revenue"]["median"], 1220)
        self.assertEqual(res["aggregates"]["revenue"]["count"], 3)
        self.assertFalse(res["aggregates"]["revenue"]["single_company"])
        self.assertAlmostEqual(
            res["aggregates"]["net_margin"]["median"], 0.127, places=3)
        self.assertAlmostEqual(
            res["aggregates"]["roic"]["median"], 0.152, places=3)
        self.assertAlmostEqual(
            res["aggregates"]["pe_ratio"]["median"], 4.474, places=3)
        self.assertAlmostEqual(
            res["aggregates"]["dcf_base"]["median"], 23.04, places=1)
        self.assertAlmostEqual(
            res["aggregates"]["debt_to_equity"]["median"], 0.316,
            places=3)
        self.assertEqual(res["warnings"], [])

    def test_single_ticker_labelled_not_sector(self):
        res = mcs.analyse_sector(
            self.conn, sector="technology", tickers=["MID"],
            prices={"MID": 5.00})
        agg = res["aggregates"]["net_margin"]
        self.assertTrue(agg["single_company"])
        self.assertEqual(agg["count"], 1)
        self.assertIn("single-company figure", agg["reason"])
        self.assertTrue(any("single-company figure" in w
                            for w in res["warnings"]))

    def test_no_statements_means_no_aggregates(self):
        res = mcs.analyse_sector(
            self.conn, sector="technology", tickers=["ZZZZ"], prices={})
        self.assertEqual(res["coverage"]["with_statements"], 0)
        self.assertIsNone(res["aggregates"]["revenue"]["median"])
        self.assertEqual(res["aggregates"]["revenue"]["count"], 0)
        self.assertEqual(res["tier_distribution"],
                         {"insufficient_data": 1})
        self.assertTrue(any("no ticker in the set has statements" in w
                            for w in res["warnings"]))
        self.assertEqual(res["per_ticker"][0]["has_statements"], False)

    def test_stored_sector_selection_empty_on_fixture_db(self):
        # No market_universe table in the fixture DB: selection by
        # stored sector is empty and says so.
        res = mcs.analyse_sector(self.conn, sector="technology")
        self.assertEqual(res["coverage"]["selected"], 0)
        self.assertTrue(any("no market_universe rows" in w
                            for w in res["warnings"]))

    def test_requires_sector_or_tickers(self):
        with self.assertRaises(ValueError):
            mcs.analyse_sector(self.conn)

    def test_sector_vs_market_comparison(self):
        result = mcs.compare_sector_to_market(
            self.conn, "technology",
            market_tickers=["MID", "BIG", "WEAK"],
            prices=tcc.PRICES, market_prices=tcc.PRICES,
            sector_tickers=["MID", "BIG"])
        comp = result["comparison"]
        self.assertEqual(comp["sector_label"], "technology")
        self.assertEqual(comp["market_label"], "market")
        net = comp["metrics"]["net_margin"]
        self.assertAlmostEqual(net["sector_median"], 0.155, places=3)
        self.assertAlmostEqual(net["market_median"], 0.127, places=3)
        self.assertAlmostEqual(net["difference"], 0.028, places=3)
        self.assertAlmostEqual(net["difference_pct"], 22.1, places=1)
        self.assertTrue(net["comparable"])
        revenue = comp["metrics"]["revenue"]
        self.assertEqual(revenue["sector_median"], 1830)
        self.assertEqual(revenue["market_median"], 1220)
        self.assertEqual(revenue["difference"], 610)
        self.assertAlmostEqual(revenue["difference_pct"], 50.0)
        self.assertIn("descriptive only", comp["note"])
        self.assertNotIn("better", comp["note"].lower().replace(
            "which set is better", ""))

    def test_comparison_incomparable_when_sector_empty(self):
        result = mcs.compare_sector_to_market(
            self.conn, "technology",
            market_tickers=["MID", "BIG", "WEAK"],
            market_prices=tcc.PRICES)
        # Sector side selected by stored sector: empty on the
        # fixture DB, so nothing is comparable.
        self.assertFalse(
            result["comparison"]["metrics"]["revenue"]["comparable"])
        self.assertIsNone(
            result["comparison"]["metrics"]["revenue"]["sector_median"])

    def test_macro_context_in_sector_result(self):
        mcs.add_macro_point(self.macro_dir, indicator="VIX", value=28.18,
                            date="2026-11-05", source="owner research")
        res = mcs.analyse_sector(
            self.conn, sector="technology",
            tickers=["MID", "BIG", "WEAK"], prices=tcc.PRICES,
            macro_dir=self.macro_dir,
            associations={"technology": ["VIX", "CPI"]},
            as_of="2026-11-06")
        context = res["macro_context"]
        self.assertEqual(context["points"]["VIX"]["value"], 28.18)
        self.assertIsNone(context["points"]["CPI"])
        self.assertEqual(context["missing"], ["CPI"])


class TestRealDataMSFT(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before_hash = _db_hash()
        cls.db_path = _temp_db_copy()

    @classmethod
    def tearDownClass(cls):
        assert _db_hash() == cls.before_hash, \
            "production database changed during Step 33b tests"

    def test_msft_sector_is_single_company_figures(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            res = mcs.analyse_sector(
                conn, sector="technology", tickers=["MSFT"])
        finally:
            conn.close()
        self.assertEqual(res["coverage"]["selected"], 1)
        self.assertEqual(res["coverage"]["with_statements"], 1)
        self.assertEqual(res["coverage"]["scannable"], 1)
        self.assertEqual(res["quality_distribution"], {"excellent": 1})
        self.assertEqual(res["tier_distribution"], {"overvalued": 1})
        for metric in ("revenue", "net_margin", "roic", "pe_ratio",
                       "dcf_base"):
            agg = res["aggregates"][metric]
            self.assertTrue(agg["single_company"], metric)
            self.assertEqual(agg["count"], 1, metric)
        self.assertEqual(res["aggregates"]["revenue"]["median"],
                         331839000000)
        self.assertAlmostEqual(
            res["aggregates"]["net_margin"]["median"], 0.403, places=3)
        self.assertAlmostEqual(
            res["aggregates"]["roic"]["median"], 0.306, places=3)
        self.assertAlmostEqual(
            res["aggregates"]["pe_ratio"]["median"], 28.25, places=2)
        self.assertAlmostEqual(
            res["aggregates"]["dcf_base"]["median"], 163.30, places=1)
        self.assertTrue(any("single-company figure" in w
                            for w in res["warnings"]))

    def test_stored_sector_selection_empty_on_real_db(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            res = mcs.analyse_sector(conn, sector="technology")
        finally:
            conn.close()
        self.assertEqual(res["coverage"]["selected"], 0)
        self.assertTrue(any("sector is NULL" in w
                            for w in res["warnings"]))

    def test_msft_vs_whole_universe_market(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            result = mcs.compare_sector_to_market(
                conn, "technology", sector_tickers=["MSFT"])
        finally:
            conn.close()
        self.assertEqual(result["market"]["coverage"]["scannable"], 1)
        self.assertEqual(
            result["market"]["coverage"]["with_statements"], 2)
        # Both sides are MSFT-only figures, so the difference is 0.
        pe = result["comparison"]["metrics"]["pe_ratio"]
        self.assertTrue(pe["comparable"])
        self.assertAlmostEqual(pe["difference"], 0.0, places=6)
        self.assertEqual(pe["sector_count"], 1)
        self.assertEqual(pe["market_count"], 1)


if __name__ == "__main__":
    unittest.main()
