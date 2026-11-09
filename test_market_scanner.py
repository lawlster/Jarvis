"""Tests for Step 25 — agents/market_scanner.py.

Fixture rows + temporary database copies only; no network
requests, no production database writes.

Fixture expectations (verified by probe against the real Step 23
summary before these tests were written):
- MID_SPEC (test_business_quality) at $5.00, annual FCF basis:
  conservative value $13.39, margin vs conservative 62.6%,
  quality "good", no deterioration -> strongest_alert.
- CHEAP_WEAK_SPEC below at $0.30, annual FCF basis: conservative
  value $0.50, margin vs conservative 39.7%, quality "weak"
  (mean score 0.0), health "weak" (total debt > 5x OCF) ->
  strongest alert BLOCKED, tier reasonable_price.
- WEAK_SPEC (test_business_quality) at $1.00: latest FCF is
  negative, so no conservative value exists -> insufficient_data,
  quality "weak".
- MSFT on a temp copy of the real DB: tier overvalued, quality
  excellent, snapshot price $507.12 as of 2026-11-03.
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
from agents import market_scanner as ms

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_fingerprint import content_fingerprint  # noqa: E402
DB_PATH = BASE_DIR / "database" / "assistant.db"

# Cheap but weak: declining revenue, thin volatile margins, a loss
# year, low returns, dividends cut, diluting shares, debt > 5x OCF
# — but latest-year FCF is positive and the price is far below the
# conservative value, so the margin alone would qualify for a
# strongest alert.  Quality (weak) and deterioration block it.
CHEAP_WEAK_SPEC = {
    "revenue": [1100, 1080, 1050, 1020, 1000, 990],
    "cost_of_revenue": [990, 980, 960, 940, 920, 915],
    "gross_profit": [110, 100, 90, 80, 80, 75],
    "research_and_development": [20, 20, 20, 20, 20, 20],
    "selling_general_administrative": [70, 68, 66, 64, 62, 45],
    "operating_income": [20, 12, 4, -4, -2, 10],
    "income_before_tax": [15, 7, -1, 1, 3, 5],
    "income_tax": [3, 1, 0, 0, 1, 1],
    "net_income": [12, 6, -1, 1, 2, 4],
    "eps_basic": [0.12, 0.06, -0.01, 0.01, 0.02, 0.04],
    "eps_diluted": [0.12, 0.06, -0.01, 0.01, 0.02, 0.04],
    "shares_outstanding_basic": [95, 96, 97, 98, 99, 100],
    "shares_outstanding_diluted": [95, 96, 97, 98, 99, 100],
    "total_assets": [2000, 2000, 2000, 2000, 2000, 2000],
    "total_liabilities": [1200, 1200, 1200, 1200, 1200, 1200],
    "total_equity": [800, 800, 800, 800, 800, 800],
    "cash_and_equivalents": [100, 100, 100, 100, 100, 100],
    "short_term_debt": [100, 100, 100, 100, 100, 100],
    "long_term_debt": [200, 200, 200, 200, 200, 200],
    "current_assets": [600, 600, 600, 600, 600, 600],
    "current_liabilities": [500, 500, 500, 500, 500, 500],
    "inventory": [200, 200, 200, 200, 200, 200],
    "accounts_receivable": [150, 150, 150, 150, 150, 150],
    "accounts_payable": [100, 100, 100, 100, 100, 100],
    "short_term_investments": [50, 50, 50, 50, 50, 50],
    "operating_cash_flow": [60, 55, 50, 45, 42, 40],
    "capital_expenditure": [30, 30, 28, 25, 22, 20],
    "dividends_paid": [10, 10, 5, 0, 0, 0],
    "share_repurchases": [0, 0, 0, 0, 0, 0],
    "share_based_compensation": [5, 5, 5, 5, 5, 5],
    "investing_cash_flow": [-30, -30, -28, -25, -22, -20],
    "financing_cash_flow": [-10, -10, -5, 0, 0, 0],
}


def _fixture_conn(rows, universe_rows=()):
    """In-memory DB with statement rows and a market_universe table."""
    conn = tbq._fixture_conn(rows)
    conn.execute(
        "CREATE TABLE market_universe ("
        "id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
        "exchange TEXT, country TEXT, currency TEXT, sector TEXT, "
        "industry TEXT, security_type TEXT, market_cap REAL, "
        "is_active INTEGER, sec_cik TEXT)")
    for row in universe_rows:
        conn.execute(
            "INSERT INTO market_universe (ticker, company_name, "
            "exchange, country, currency, sector, industry, "
            "security_type, market_cap, is_active, sec_cik) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    return conn


def _universe_row(ticker, country="USA", sector=None,
                  security_type="common_stock"):
    return (ticker, ticker, "NASDAQ", country, "USD", sector, None,
            security_type, None, 1, None)


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _candidate(ticker, tier, margin=None, quality=None):
    return {"ticker": ticker, "tier": tier,
            "margin_of_safety_vs_conservative": margin,
            "quality_rating": quality, "seen_recently": False,
            "rank": None}


class TestPriceNormalisation(unittest.TestCase):
    def test_plain_number(self):
        price = ms.normalise_price_input(12.5)
        self.assertEqual(price["price"], 12.5)
        self.assertEqual(price["source"], "caller-supplied price")

    def test_dict_passthrough(self):
        price = ms.normalise_price_input(
            {"price": 12.5, "as_of_date": "2026-11-06",
             "source": "Yahoo"})
        self.assertEqual(price["as_of_date"], "2026-11-06")
        self.assertEqual(price["source"], "Yahoo")

    def test_invalid(self):
        self.assertIsNone(ms.normalise_price_input(None))
        self.assertIsNone(ms.normalise_price_input(0))
        self.assertIsNone(ms.normalise_price_input(-3))
        self.assertIsNone(ms.normalise_price_input("cheap"))
        self.assertIsNone(ms.normalise_price_input({}))
        self.assertIsNone(ms.normalise_price_input({"price": None}))


class TestRanking(unittest.TestCase):
    def test_tier_order(self):
        results = ms.rank_results([
            _candidate("OV", "overvalued", 0.1, "excellent"),
            _candidate("IN", "insufficient_data", None, None),
            _candidate("AL", "strongest_alert", 0.25, "good"),
            _candidate("FV", "fairly_valued", -0.05, "good"),
            _candidate("RP", "reasonable_price", 0.05, "adequate"),
        ])
        self.assertEqual([r["ticker"] for r in results],
                         ["AL", "RP", "FV", "OV", "IN"])
        self.assertEqual([r["rank"] for r in results], [1, 2, 3, 4, 5])

    def test_margin_descending_within_tier(self):
        results = ms.rank_results([
            _candidate("LOW", "strongest_alert", 0.21, "good"),
            _candidate("HIGH", "strongest_alert", 0.60, "good"),
            _candidate("NOMARGIN", "strongest_alert", None, "good"),
        ])
        self.assertEqual([r["ticker"] for r in results],
                         ["HIGH", "LOW", "NOMARGIN"])

    def test_rotation_demotes_surfed_within_tier(self):
        results = ms.rank_results([
            _candidate("FIXA", "strongest_alert", 0.60, "good"),
            _candidate("FIXB", "strongest_alert", 0.60, "good"),
        ], already_surfed=["FIXA"])
        # Same tier, same margin: FIXA would win on ticker order,
        # but it was surfaced recently, so FIXB goes first.
        self.assertEqual([r["ticker"] for r in results],
                         ["FIXB", "FIXA"])
        self.assertTrue(results[1]["seen_recently"])
        self.assertFalse(results[0]["seen_recently"])

    def test_rotation_never_promotes_across_tiers(self):
        results = ms.rank_results([
            _candidate("AL", "strongest_alert", 0.21, "good"),
            _candidate("RP", "reasonable_price", 0.50, "excellent"),
        ], already_surfed=["AL"])
        self.assertEqual([r["ticker"] for r in results], ["AL", "RP"])

    def test_quality_breaks_margin_ties(self):
        results = ms.rank_results([
            _candidate("WEAKQ", "reasonable_price", 0.10, "weak"),
            _candidate("GOODQ", "reasonable_price", 0.10, "good"),
        ])
        self.assertEqual([r["ticker"] for r in results],
                         ["GOODQ", "WEAKQ"])


class TestScanTicker(unittest.TestCase):
    def test_cheap_good_business_reaches_strongest_alert(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        result = ms.scan_ticker(conn, "FIX", price_input=5.0)
        self.assertTrue(result["scannable"])
        self.assertEqual(result["tier"], "strongest_alert")
        self.assertEqual(result["quality_rating"], "good")
        self.assertAlmostEqual(
            result["fair_value_range"]["conservative"], 13.39,
            places=1)
        self.assertGreaterEqual(
            result["margin_of_safety_vs_conservative"], 0.20)
        self.assertEqual(result["starting_fcf_basis"], "annual")
        self.assertEqual(result["price"], 5.0)
        self.assertEqual(result["price_source"],
                         "caller-supplied price")
        self.assertEqual(result["missing_inputs"], [])

    def test_cheap_weak_business_does_not_reach_alert(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIXW", CHEAP_WEAK_SPEC))
        result = ms.scan_ticker(conn, "FIXW", price_input=0.30)
        self.assertTrue(result["scannable"])
        self.assertNotEqual(result["tier"], "strongest_alert")
        self.assertEqual(result["tier"], "reasonable_price")
        self.assertEqual(result["quality_rating"], "weak")
        # The margin alone would qualify — the block is the point.
        self.assertGreaterEqual(
            result["margin_of_safety_vs_conservative"], 0.20)
        self.assertTrue(any("BLOCKED" in reason
                            for reason in result["tier_reasons"]))

    def test_weak_business_with_no_value_is_insufficient(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIXW", tbq.WEAK_SPEC))
        result = ms.scan_ticker(conn, "FIXW", price_input=1.0)
        self.assertEqual(result["tier"], "insufficient_data")
        self.assertEqual(result["quality_rating"], "weak")
        self.assertIsNone(
            result["fair_value_range"]["conservative"])

    def test_no_statements_is_insufficient_not_skipped(self):
        conn = tbq._fixture_conn([])
        result = ms.scan_ticker(conn, "NONE")
        self.assertEqual(result["tier"], "insufficient_data")
        self.assertFalse(result["scannable"])
        self.assertIsNone(result["detail"])
        self.assertIn(ms.MISSING_NO_STATEMENTS,
                      result["missing_inputs"])
        self.assertIsNone(result["price"])

    def test_statements_but_no_price_is_insufficient(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        result = ms.scan_ticker(conn, "FIX")
        self.assertEqual(result["tier"], "insufficient_data")
        self.assertFalse(result["scannable"])
        self.assertIn(ms.MISSING_NO_PRICE, result["missing_inputs"])
        # The valuation range is still computed and shown.
        self.assertIsNotNone(
            result["fair_value_range"]["conservative"])

    def test_cache_avoids_rescan(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        cache = {}
        first = ms.scan_ticker(conn, "FIX", price_input=5.0,
                               cache=cache)
        second = ms.scan_ticker(conn, "FIX", price_input=5.0,
                                cache=cache)
        self.assertIs(first, second)
        self.assertEqual(len(cache), 1)


class TestScanMarket(unittest.TestCase):
    def _market_conn(self):
        rows = (tbq._company_rows("FIXA", tbq.MID_SPEC)
                + tbq._company_rows("FIXB", tbq.MID_SPEC)
                + tbq._company_rows("FIXW", CHEAP_WEAK_SPEC))
        universe = [_universe_row("FIXA"), _universe_row("FIXB"),
                    _universe_row("FIXW"), _universe_row("NONE")]
        return _fixture_conn(rows, universe)

    def test_mixed_market_ranking_and_coverage(self):
        conn = self._market_conn()
        scan = ms.scan_market(
            conn, prices={"FIXA": 5.0, "FIXB": 5.0, "FIXW": 0.30})
        results = scan["results"]
        self.assertEqual([r["ticker"] for r in results],
                         ["FIXA", "FIXB", "FIXW", "NONE"])
        self.assertEqual([r["tier"] for r in results],
                         ["strongest_alert", "strongest_alert",
                          "reasonable_price", "insufficient_data"])
        coverage = scan["coverage"]
        self.assertEqual(coverage["selected"], 4)
        self.assertEqual(coverage["with_statements"], 3)
        self.assertEqual(coverage["with_price"], 3)
        self.assertEqual(coverage["scannable"], 3)
        self.assertEqual(coverage["pipeline_run"], 3)
        self.assertEqual(coverage["by_tier"],
                         {"strongest_alert": 2,
                          "reasonable_price": 1,
                          "insufficient_data": 1})
        self.assertEqual(coverage["by_country"]["USA"]["selected"], 4)
        self.assertEqual(coverage["by_country"]["USA"]["scannable"], 3)

    def test_rotation_in_market_scan(self):
        conn = self._market_conn()
        scan = ms.scan_market(
            conn, prices={"FIXA": 5.0, "FIXB": 5.0, "FIXW": 0.30},
            already_surfed=["FIXA"])
        self.assertEqual([r["ticker"] for r in scan["results"]],
                         ["FIXB", "FIXA", "FIXW", "NONE"])

    def test_explicit_ticker_list_without_universe_table(self):
        conn = tbq._fixture_conn(
            tbq._company_rows("FIX", tbq.MID_SPEC))
        scan = ms.scan_market(conn, tickers=["FIX"],
                              prices={"FIX": 5.0})
        self.assertEqual(len(scan["results"]), 1)
        self.assertEqual(scan["results"][0]["tier"],
                         "strongest_alert")

    def test_country_filter(self):
        conn = self._market_conn()
        scan = ms.scan_market(conn, country="Singapore",
                              prices={"FIXA": 5.0})
        self.assertEqual(scan["coverage"]["selected"], 0)
        scan = ms.scan_market(conn, country="USA",
                              prices={"FIXA": 5.0, "FIXB": 5.0,
                                      "FIXW": 0.30})
        self.assertEqual(scan["coverage"]["selected"], 4)

    def test_price_for_unknown_ticker_is_warned_not_crash(self):
        conn = self._market_conn()
        scan = ms.scan_market(conn, prices={"NOPE": 10.0})
        self.assertTrue(any("NOPE" in w for w in scan["warnings"]))
        self.assertEqual(scan["coverage"]["selected"], 4)

    def test_same_ticker_two_exchanges_are_two_candidates(self):
        # Regression: AWX/BHK/BMA/BSL/HMN/OGS are listed in both the
        # USA and Singapore.  Each market_universe row is its own
        # candidate; the per-run cache must not collapse them into
        # one (the first version did, and 6 USA rows were reported
        # under Singapore).
        rows = []
        universe = [_universe_row("DUAL", country="USA"),
                    _universe_row("DUAL", country="Singapore")]
        universe[0] = ("DUAL", "DUAL", "NASDAQ", "USA", "USD", None,
                       None, "common_stock", None, 1, None)
        universe[1] = ("DUAL", "DUAL", "SGX", "Singapore", "SGD",
                       None, None, "common_stock", None, 1, None)
        conn = _fixture_conn(rows, universe)
        scan = ms.scan_market(conn)
        self.assertEqual(scan["coverage"]["selected"], 2)
        self.assertEqual(
            sorted(r["country"] for r in scan["results"]),
            ["Singapore", "USA"])
        self.assertEqual(
            scan["coverage"]["by_country"]["USA"]["selected"], 1)
        self.assertEqual(
            scan["coverage"]["by_country"]["Singapore"]["selected"],
            1)

    def test_snapshot_prices_helper(self):
        conn = tbq._fixture_conn([])
        conn.execute(
            "INSERT INTO financial_snapshots "
            "(ticker, snapshot_date, financial_data) VALUES "
            "(?, ?, ?)",
            ("AAA", "2026-11-03 12:00:00",
             json.dumps({"current_price": 42.0,
                         "market_cap": 4200})))
        conn.execute(
            "INSERT INTO financial_snapshots "
            "(ticker, snapshot_date, financial_data) VALUES "
            "(?, ?, ?)",
            ("AAA", "2026-11-01 12:00:00",
             json.dumps({"current_price": 40.0})))
        prices = ms.snapshot_prices(conn)
        self.assertEqual(prices["AAA"]["price"], 42.0)
        self.assertEqual(prices["AAA"]["as_of_date"], "2026-11-03")


class TestRealData(unittest.TestCase):
    """Temp copies of the production DB only."""

    @classmethod
    def setUpClass(cls):
        cls._prod_hash = content_fingerprint(DB_PATH)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(DB_PATH) == cls._prod_hash, (
            "production database changed during Step 25 tests")

    def test_msft_is_overvalued_excellent(self):
        path = _temp_db_copy()
        try:
            scan = ms.run_scan(path, tickers=["MSFT"])
            result = scan["results"][0]
            self.assertEqual(result["tier"], "overvalued")
            self.assertEqual(result["quality_rating"], "excellent")
            self.assertTrue(result["scannable"])
            self.assertAlmostEqual(result["price"], 507.12, places=2)
            self.assertEqual(result["price_as_of"], "2026-11-03")
            self.assertEqual(result["starting_fcf_basis"], "ttm")
            self.assertAlmostEqual(
                result["fair_value_range"]["base"], 163.30, places=1)
        finally:
            os.unlink(path)

    def test_msft_caller_price_overrides_snapshot(self):
        path = _temp_db_copy()
        try:
            scan = ms.run_scan(path, tickers=["MSFT"],
                               prices={"MSFT": 530.61})
            result = scan["results"][0]
            self.assertEqual(result["price"], 530.61)
            self.assertEqual(result["price_source"],
                             "caller-supplied price")
            self.assertEqual(result["tier"], "overvalued")
        finally:
            os.unlink(path)

    def test_d05_is_insufficient_data(self):
        path = _temp_db_copy()
        try:
            scan = ms.run_scan(path, tickers=["D05"])
            result = scan["results"][0]
            self.assertEqual(result["tier"], "insufficient_data")
            self.assertFalse(result["scannable"])
            self.assertIsNone(
                result["fair_value_range"]["conservative"])
        finally:
            os.unlink(path)

    def test_full_universe_coverage(self):
        path = _temp_db_copy()
        try:
            scan = ms.run_scan(path)
            coverage = scan["coverage"]
            self.assertEqual(coverage["selected"], 10619)
            self.assertEqual(coverage["with_statements"], 2)
            self.assertEqual(coverage["scannable"], 1)
            self.assertEqual(coverage["pipeline_run"], 2)
            self.assertEqual(coverage["by_tier"],
                             {"overvalued": 1,
                              "insufficient_data": 10618})
            self.assertEqual(
                coverage["by_country"]["USA"]["selected"], 7625)
            self.assertEqual(
                coverage["by_country"]["USA"]["scannable"], 1)
            self.assertEqual(
                coverage["by_country"]["Singapore"]["selected"], 201)
            self.assertEqual(
                coverage["by_country"]["Singapore"]["scannable"], 0)
            self.assertEqual(
                coverage["by_country"]["Hong Kong"]["selected"], 2793)
            self.assertEqual(
                coverage["by_country"]["Hong Kong"]["scannable"], 0)
            # Only MSFT is scannable, so it is the only ranked
            # candidate above the insufficient-data block.
            self.assertEqual(scan["results"][0]["ticker"], "MSFT")
            self.assertEqual(scan["results"][0]["tier"], "overvalued")
            self.assertLess(scan["run_seconds"], 60)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
