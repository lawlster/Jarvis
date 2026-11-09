"""Tests for Step 26 — agents/undervalued_detector.py.

Fixture rows + temporary database copies only; no network
requests, no production database writes.

Fixture expectations (verified by probe against the real Step 23
summary before these tests were written):
- MID_SPEC (test_business_quality) range: conservative $13.39,
  base $23.04, optimistic $33.67; quality "good", no
  deterioration.  At $5.00 -> strongest_alert (margin 62.6%).
  At $12.00 -> watch_discount (margin 10.4%).  At $14.00 ->
  watch_below_base (margin vs conservative -4.6%, vs base
  39.2%).  At $25.00 -> fairly_valued.  At $40.00 -> overvalued.
- CHEAP_WEAK_SPEC (test_market_scanner) at $0.30: conservative
  value $0.50, margin vs conservative 39.7%, quality "weak",
  health "weak", material deterioration -> value trap, tier
  reasonable_price with the alert blocked.
- WEAK_SPEC (test_business_quality) at $1.00: latest FCF is
  negative, so no conservative value exists -> insufficient_data.
- MSFT on a temp copy of the real DB: tier overvalued, quality
  excellent -> not a candidate, not a trap.
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
from agents import market_scanner as ms
from agents import undervalued_detector as ud

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_fingerprint import content_fingerprint  # noqa: E402
DB_PATH = BASE_DIR / "database" / "assistant.db"


def _fixture_conn(rows, universe_rows=()):
    return tms._fixture_conn(rows, universe_rows)


def _market_conn():
    rows = (tbq._company_rows("FIXA", tbq.MID_SPEC)
            + tbq._company_rows("FIXB", tbq.MID_SPEC)
            + tbq._company_rows("FIXW", tms.CHEAP_WEAK_SPEC))
    universe = [tms._universe_row("FIXA"), tms._universe_row("FIXB"),
                tms._universe_row("FIXW"), tms._universe_row("NONE")]
    return _fixture_conn(rows, universe)


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _hand_candidate(ticker="HAND", tier="reasonable_price",
                    margin_cons=0.25, margin_base=0.5,
                    margin_opt=0.7, quality="good",
                    deteriorated=False, unknown=False,
                    blocked=False, price=10.0,
                    conservative=13.39):
    """Hand-built scanner candidate dict (no DB) for explain tests."""
    reasons = []
    if blocked:
        reasons.append(
            "margin vs conservative value is 25.0% (>= 20%) but a "
            "strongest alert is BLOCKED")
    return {
        "ticker": ticker, "company_name": ticker, "country": "USA",
        "exchange": "NASDAQ", "sector": "technology",
        "security_type": "common_stock", "price": price,
        "price_as_of": "2026-11-03", "price_source": "test",
        "scannable": True, "tier": tier,
        "tier_reasons": reasons,
        "conditions_to_change": ["a price rise would remove it"],
        "margin_of_safety_vs_conservative": margin_cons,
        "margin_of_safety_vs_base": margin_base,
        "margin_of_safety_vs_optimistic": margin_opt,
        "fair_value_range": {"conservative": conservative,
                             "base": 23.04, "optimistic": 33.67},
        "strongest_alert_threshold_price": 10.71,
        "quality_rating": quality, "quality_mean_score": 1.5,
        "health_verdict": "strong", "health_reasons": [],
        "starting_fcf_basis": "annual", "seen_recently": False,
        "rank": 1, "missing_inputs": [], "warnings": [],
        "detail": {
            "deterioration": {"deteriorated": deteriorated,
                              "unknown": unknown,
                              "signals": ["health verdict is 'weak'"]
                              if deteriorated else [],
                              "cautions": []},
            "margins_of_safety": {
                "conservative": {"margin_vs_price": 0.34},
                "base": {"margin_vs_price": 1.30},
                "optimistic": {"margin_vs_price": 2.37}},
        },
    }


class TestStrongestAlertDetection(unittest.TestCase):
    def test_cheap_good_business_is_strongest_alert_candidate(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        result = ud.detect_undervalued(conn, tickers=["FIX"],
                                       prices={"FIX": 5.0})
        self.assertEqual(len(result["candidates"]), 1)
        record = result["candidates"][0]
        self.assertEqual(record["detection_band"], "strongest_alert")
        self.assertEqual(record["tier"], "strongest_alert")
        self.assertEqual(record["rank"], 1)
        self.assertAlmostEqual(record["fair_value_range"]["base"],
                               23.04, places=1)
        self.assertAlmostEqual(
            record["margin_of_safety_vs_conservative"], 0.626,
            places=2)
        # Both margin conventions, labelled, on the record.
        self.assertAlmostEqual(
            record["margin_vs_price_vs_conservative"], 1.678,
            places=2)
        self.assertIn("Graham",
                      record["margin_convention"])
        self.assertEqual(record["quality_rating"], "good")
        self.assertIs(record["deteriorated"], False)
        self.assertEqual(record["price"], 5.0)
        self.assertEqual(result["value_traps"], [])
        self.assertEqual(result["summary"]["strongest_alerts"], 1)
        self.assertEqual(result["summary"]["watch"], 0)

    def test_just_above_20_percent_margin_qualifies(self):
        # Price 79.9% of the actual conservative value (read from
        # a first scan, so the test does not assume a rounded
        # value).  A price at exactly 80% does NOT qualify here:
        # floating-point makes the computed margin
        # 0.19999999999999993, just below Step 23's >= 20% test —
        # documented in the Step 26 report, not hidden.
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        first = ms.scan_market(conn, tickers=["FIX"],
                               prices={"FIX": 5.0})
        conservative = first["results"][0]["fair_value_range"][
            "conservative"]
        result = ud.detect_undervalued(
            conn, tickers=["FIX"],
            prices={"FIX": conservative * 0.799})
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["detection_band"],
                         "strongest_alert")

    def test_just_below_20_percent_is_watch_not_alert(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        first = ms.scan_market(conn, tickers=["FIX"],
                               prices={"FIX": 5.0})
        conservative = first["results"][0]["fair_value_range"][
            "conservative"]
        result = ud.detect_undervalued(
            conn, tickers=["FIX"],
            prices={"FIX": conservative * 0.81})
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["detection_band"],
                         "watch_discount")
        self.assertEqual(result["summary"]["strongest_alerts"], 0)


class TestWatchBands(unittest.TestCase):
    def test_watch_discount_band(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        result = ud.detect_undervalued(conn, tickers=["FIX"],
                                       prices={"FIX": 12.0})
        self.assertEqual(len(result["candidates"]), 1)
        record = result["candidates"][0]
        self.assertEqual(record["detection_band"], "watch_discount")
        self.assertEqual(record["tier"], "reasonable_price")
        self.assertAlmostEqual(
            record["margin_of_safety_vs_conservative"], 0.104,
            places=2)
        # The record says why it is not stronger.
        self.assertTrue(any("does not qualify for strongest_alert"
                            in r for r in
                            record["not_stronger_reasons"]))
        self.assertEqual(result["summary"]["watch"], 1)

    def test_watch_below_base_band(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        result = ud.detect_undervalued(conn, tickers=["FIX"],
                                       prices={"FIX": 14.0})
        self.assertEqual(len(result["candidates"]), 1)
        record = result["candidates"][0]
        self.assertEqual(record["detection_band"],
                         "watch_below_base")
        self.assertLess(
            record["margin_of_safety_vs_conservative"], 0)
        self.assertGreaterEqual(
            record["margin_of_safety_vs_base"], 0)

    def test_watch_requires_adequate_quality(self):
        # CHEAP_WEAK at a price in the watch band is a trap, not a
        # watch candidate: margin vs conservative is positive but
        # quality is weak and it is deteriorating.
        conn = tbq._fixture_conn(
            tbq._company_rows("FIXW", tms.CHEAP_WEAK_SPEC))
        result = ud.detect_undervalued(conn, tickers=["FIXW"],
                                       prices={"FIXW": 0.45})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["value_traps"]), 1)


class TestValueTraps(unittest.TestCase):
    def test_cheap_weak_business_is_a_trap_not_a_candidate(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIXW", tms.CHEAP_WEAK_SPEC))
        result = ud.detect_undervalued(conn, tickers=["FIXW"],
                                       prices={"FIXW": 0.30})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["value_traps"]), 1)
        trap = result["value_traps"][0]
        self.assertEqual(trap["detection_band"], "value_trap")
        self.assertEqual(trap["quality_rating"], "weak")
        self.assertEqual(trap["health_verdict"], "weak")
        self.assertIs(trap["deteriorated"], True)
        self.assertGreaterEqual(
            trap["margin_of_safety_vs_conservative"], 0.20)
        self.assertIn("weak", trap["why_not_a_candidate"])
        self.assertEqual(result["summary"]["value_traps"], 1)
        self.assertEqual(result["summary"]["by_category"],
                         {"value_trap": 1})

    def test_weak_business_with_no_value_is_not_a_trap(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIXW", tbq.WEAK_SPEC))
        result = ud.detect_undervalued(conn, tickers=["FIXW"],
                                       prices={"FIXW": 1.0})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["value_traps"], [])
        self.assertEqual(result["summary"]["by_category"],
                         {"insufficient_data": 1})


class TestExplainTicker(unittest.TestCase):
    def test_overvalued_explanation(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        scan = ms.scan_market(conn, tickers=["FIX"],
                              prices={"FIX": 40.0})
        explanation = ud.explain_ticker(scan["results"][0])
        self.assertFalse(explanation["qualifies"])
        self.assertEqual(explanation["category"], "overvalued")
        self.assertTrue(any("above the optimistic" in r
                            for r in explanation["reasons"]))

    def test_fairly_valued_explanation(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        scan = ms.scan_market(conn, tickers=["FIX"],
                              prices={"FIX": 25.0})
        explanation = ud.explain_ticker(scan["results"][0])
        self.assertFalse(explanation["qualifies"])
        self.assertEqual(explanation["category"], "fairly_valued")

    def test_insufficient_data_explanation(self):
        conn = tbq._fixture_conn([])
        scan = ms.scan_market(conn, tickers=["NONE"])
        explanation = ud.explain_ticker(scan["results"][0])
        self.assertFalse(explanation["qualifies"])
        self.assertEqual(explanation["category"], "insufficient_data")
        self.assertTrue(explanation["reasons"])

    def test_blocked_alert_without_trap_evidence(self):
        # Margin qualifies but deterioration is UNKNOWN: blocked,
        # and neither weak quality nor deterioration makes it a
        # trap, so the category is blocked_alert.
        candidate = _hand_candidate(deteriorated=None, unknown=True,
                                    blocked=True)
        explanation = ud.explain_ticker(candidate)
        self.assertFalse(explanation["qualifies"])
        self.assertEqual(explanation["category"], "blocked_alert")

    def test_reasonable_price_weak_quality_is_not_watch(self):
        candidate = _hand_candidate(
            margin_cons=-0.05, margin_base=0.39, quality="weak")
        explanation = ud.explain_ticker(candidate)
        self.assertFalse(explanation["qualifies"])
        self.assertEqual(explanation["category"], "reasonable_price")
        self.assertTrue(any("weak" in r
                            for r in explanation["reasons"]))


class TestMixedMarket(unittest.TestCase):
    def test_candidates_traps_and_insufficient_together(self):
        conn = _market_conn()
        result = ud.detect_undervalued(
            conn, prices={"FIXA": 5.0, "FIXB": 12.0, "FIXW": 0.30})
        self.assertEqual(
            [r["ticker"] for r in result["candidates"]],
            ["FIXA", "FIXB"])
        self.assertEqual(
            [r["detection_band"] for r in result["candidates"]],
            ["strongest_alert", "watch_discount"])
        self.assertEqual(
            [r["ticker"] for r in result["value_traps"]], ["FIXW"])
        self.assertEqual(result["summary"]["candidates"], 2)
        self.assertEqual(result["summary"]["by_band"],
                         {"strongest_alert": 1, "watch_discount": 1})
        self.assertEqual(result["summary"]["by_category"],
                         {"strongest_alert": 1, "watch": 1,
                          "value_trap": 1, "insufficient_data": 1})
        self.assertEqual(result["summary"]["selected"], 4)
        self.assertEqual(result["summary"]["scannable"], 3)

    def test_briefing_output(self):
        conn = _market_conn()
        result = ud.detect_undervalued(
            conn, prices={"FIXA": 5.0, "FIXB": 12.0, "FIXW": 0.30})
        briefing = ud.briefing_summary(result)
        self.assertIn("2 candidate(s)", briefing["headline"])
        self.assertIn("1 strongest alert", briefing["headline"])
        self.assertIn("1 value trap(s)", briefing["headline"])
        self.assertEqual(len(briefing["lines"]), 2)
        self.assertIn("FIXA", briefing["lines"][0])
        self.assertIn("STRONGEST ALERT", briefing["lines"][0])
        self.assertIn("not a buy recommendation",
                      briefing["lines"][0])
        self.assertIn("FIXB", briefing["lines"][1])
        self.assertEqual(len(briefing["trap_lines"]), 1)
        self.assertIn("FIXW", briefing["trap_lines"][0])
        self.assertIn("VALUE TRAP", briefing["trap_lines"][0])

    def test_rotation_passes_through_to_scanner(self):
        conn = _market_conn()
        result = ud.detect_undervalued(
            conn, prices={"FIXA": 5.0, "FIXB": 5.0, "FIXW": 0.30},
            already_surfed=["FIXA"])
        # Both FIXA and FIXB are strongest alerts; FIXA was
        # surfaced recently, so FIXB ranks first in the scan — and
        # the detector keeps that order.
        self.assertEqual(
            [r["ticker"] for r in result["candidates"]],
            ["FIXB", "FIXA"])


class TestRunLog(unittest.TestCase):
    def test_log_written_and_read_back(self):
        conn = _market_conn()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "runs.jsonl")
            result = ud.detect_undervalued(
                conn, prices={"FIXA": 5.0, "FIXB": 12.0,
                              "FIXW": 0.30},
                log_path=log_path)
            records = ud.read_run_log(log_path)
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["selected"], 4)
            self.assertEqual(record["scannable"], 3)
            self.assertEqual(record["candidates"], 2)
            self.assertEqual(record["strongest_alerts"], 1)
            self.assertEqual(record["watch"], 1)
            self.assertEqual(record["value_traps"], 1)
            self.assertEqual(record["candidate_tickers"],
                             ["FIXA", "FIXB"])
            self.assertEqual(record["trap_tickers"], ["FIXW"])
            self.assertIn("run_at_utc", record)
            # A second run appends, it does not overwrite.
            ud.detect_undervalued(
                conn, prices={"FIXA": 5.0},
                log_path=log_path)
            self.assertEqual(len(ud.read_run_log(log_path)), 2)

    def test_no_log_when_path_is_none(self):
        conn = _market_conn()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "runs.jsonl")
            ud.detect_undervalued(
                conn, prices={"FIXA": 5.0}, log_path=None)
            self.assertFalse(os.path.exists(log_path))

    def test_read_missing_log_is_empty(self):
        self.assertEqual(
            ud.read_run_log("/tmp/does-not-exist-runs.jsonl"), [])


class TestRealData(unittest.TestCase):
    """Temp copies of the production DB only."""

    @classmethod
    def setUpClass(cls):
        cls._prod_hash = content_fingerprint(DB_PATH)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(DB_PATH) == cls._prod_hash, (
            "production database changed during Step 26 tests")

    def test_msft_is_not_a_candidate(self):
        path = _temp_db_copy()
        try:
            result = ud.run_detection(path, log_path=None,
                                      tickers=["MSFT"])
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["value_traps"], [])
            scan = result["scan"]["results"][0]
            self.assertEqual(scan["tier"], "overvalued")
            explanation = ud.explain_ticker(scan)
            self.assertFalse(explanation["qualifies"])
            self.assertEqual(explanation["category"], "overvalued")
        finally:
            os.unlink(path)

    def test_d05_is_insufficient_not_a_candidate(self):
        path = _temp_db_copy()
        try:
            result = ud.run_detection(path, log_path=None,
                                      tickers=["D05"])
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["value_traps"], [])
            explanation = ud.explain_ticker(
                result["scan"]["results"][0])
            self.assertEqual(explanation["category"],
                             "insufficient_data")
        finally:
            os.unlink(path)

    def test_full_universe_no_candidates_no_traps(self):
        path = _temp_db_copy()
        try:
            result = ud.run_detection(path, log_path=None)
            summary = result["summary"]
            self.assertEqual(summary["candidates"], 0)
            self.assertEqual(summary["value_traps"], 0)
            self.assertEqual(summary["selected"], 10619)
            self.assertEqual(summary["scannable"], 1)
            self.assertEqual(summary["by_category"],
                             {"overvalued": 1,
                              "insufficient_data": 10618})
            briefing = ud.briefing_summary(result)
            self.assertIn("0 candidate(s)", briefing["headline"])
            self.assertEqual(briefing["lines"], [])
            self.assertEqual(briefing["trap_lines"], [])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
