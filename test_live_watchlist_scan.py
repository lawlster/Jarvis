"""Tests for agents/live_watchlist_scan.py (Step 54).

All live pulls are stubbed (no network): the fetcher is
injected and returns canned ``get_company_financials`` dicts.
"""

import tempfile
import unittest
from pathlib import Path

from agents import live_watchlist_scan as lws
from agents.watchlist_foundation import save_watchlist
from tools.valuation import calculate_dcf


def _live(symbol, price, fcf, debt=0.0, cash=0.0, shares=1e9,
          currency="USD", fcf_period="2026-06-30"):
    """A get_company_financials-shaped live pull."""
    return {
        "ticker": symbol,
        "company_name": f"{symbol} Co",
        "sector": "Technology",
        "currency": currency,
        "data_source": "Yahoo Finance",
        "data_available": True,
        "data_freshness": {
            "retrieved_at_utc": "2026-11-09T03:00:00+00:00",
            "price_data_as_of_utc": "2026-11-06T21:00:00+00:00",
            "financial_statements_latest_period": {
                "income_statement": fcf_period,
                "cash_flow": fcf_period,
                "balance_sheet": fcf_period,
            },
        },
        "current_price": price,
        "free_cash_flow": {fcf_period: fcf} if fcf is not None else {},
        "total_debt": {fcf_period: debt},
        "cash": {fcf_period: cash},
        "shares_outstanding": shares,
    }


def _dcf(fcf, growth, net_debt=0.0, shares=1e9):
    return calculate_dcf(
        fcf, growth, 0.11, 0.025, 10, net_debt, shares
    )["intrinsic_value_per_share"]


class LiveScanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self.tmp.name) / "watchlist"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, tickers):
        save_watchlist([
            {"ticker": t, "universe_ticker": None,
             "company_name": f"{t} Co", "sources": ["test"],
             "reason": "test", "notes": [],
             "date_added": "2026-11-09"}
            for t in tickers
        ], store_dir=str(self.store_dir))

    # -- symbol mapping -------------------------------------------

    def test_yahoo_symbol_mapping(self):
        self.assertEqual(
            lws.yahoo_symbol({"ticker": "BRK.B",
                              "universe_ticker": "BRK-B"}), "BRK-B")
        self.assertEqual(
            lws.yahoo_symbol({"ticker": "DBS",
                              "universe_ticker": "D05"}), "D05.SI")
        self.assertEqual(
            lws.yahoo_symbol({"ticker": "OCBC",
                              "universe_ticker": "O39"}), "O39.SI")
        self.assertEqual(
            lws.yahoo_symbol({"ticker": "UOB",
                              "universe_ticker": "U11"}), "U11.SI")
        self.assertEqual(
            lws.yahoo_symbol({"ticker": "GOOGL",
                              "universe_ticker": None}), "GOOGL")
        self.assertEqual(lws.yahoo_symbol("MSFT"), "MSFT")

    # -- DCF range --------------------------------------------------

    def test_dcf_range_matches_calculate_dcf(self):
        live = _live("CHEAP", 50.0, 10e9)
        fair_value_range, notes = lws.dcf_range(live)
        self.assertEqual(notes, [])
        for name, growth in lws.GROWTH_SCENARIOS.items():
            self.assertAlmostEqual(
                fair_value_range[name], _dcf(10e9, growth), places=6)
        self.assertLess(fair_value_range["conservative"],
                        fair_value_range["base"])
        self.assertLess(fair_value_range["base"],
                        fair_value_range["optimistic"])

    def test_dcf_range_net_debt(self):
        live = _live("DEBTY", 50.0, 10e9, debt=5e9, cash=1e9)
        fair_value_range, _ = lws.dcf_range(live)
        self.assertAlmostEqual(
            fair_value_range["base"], _dcf(10e9, 0.10, net_debt=4e9),
            places=6)

    def test_dcf_range_no_fcf(self):
        live = _live("NOF CF".replace(" ", ""), 50.0, None)
        fair_value_range, notes = lws.dcf_range(live)
        self.assertIsNone(fair_value_range)
        self.assertIn("free cash flow", notes[0])

    def test_dcf_range_negative_fcf(self):
        live = _live("NEG", 50.0, -5e9)
        fair_value_range, notes = lws.dcf_range(live)
        self.assertIsNone(fair_value_range)
        self.assertIn("not positive", notes[0])

    # -- single ticker ----------------------------------------------

    def _fetcher(self, pulls):
        def fetch(symbol):
            if symbol not in pulls:
                raise AssertionError(f"unexpected fetch: {symbol}")
            result = pulls[symbol]
            if isinstance(result, Exception):
                raise result
            return result
        return fetch

    def test_cheap_ticker_strongest_alert_band_provisional(self):
        record = lws.scan_ticker_live(
            "CHEAP", fetcher=self._fetcher(
                {"CHEAP": _live("CHEAP", 50.0, 10e9)}))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["detection_band"], "strongest_alert")
        self.assertTrue(record["provisional"])
        self.assertTrue(record["deterioration_unknown"])
        self.assertTrue(record["quality_unknown"])
        # Step 23 tier blocks the alert: deterioration unknown.
        self.assertEqual(record["tier"], "reasonable_price")
        self.assertTrue(record["strongest_alert_blocked"])
        self.assertGreaterEqual(
            record["margin_of_safety_vs_conservative"], 0.20)
        self.assertAlmostEqual(
            record["strongest_alert_price"],
            0.8 * record["fair_value_range"]["conservative"], places=6)

    def test_watch_discount_band(self):
        conservative = _dcf(10e9, 0.05)
        price = 0.9 * conservative  # 10% below conservative
        record = lws.scan_ticker_live(
            "WATCHD", fetcher=self._fetcher(
                {"WATCHD": _live("WATCHD", price, 10e9)}))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["detection_band"], "watch_discount")
        self.assertTrue(record["provisional"])

    def test_watch_below_base_band(self):
        conservative = _dcf(10e9, 0.05)
        base = _dcf(10e9, 0.10)
        price = (conservative + base) / 2
        record = lws.scan_ticker_live(
            "BELOWB", fetcher=self._fetcher(
                {"BELOWB": _live("BELOWB", price, 10e9)}))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["detection_band"], "watch_below_base")

    def test_overvalued_no_band(self):
        optimistic = _dcf(10e9, 0.15)
        record = lws.scan_ticker_live(
            "OVER", fetcher=self._fetcher(
                {"OVER": _live("OVER", 2 * optimistic, 10e9)}))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["tier"], "overvalued")
        self.assertIsNone(record["detection_band"])
        self.assertFalse(record["provisional"])

    def test_no_data_is_insufficient_not_provider_failure(self):
        pull = _live("NODATA", 50.0, 10e9)
        pull["data_available"] = False
        record = lws.scan_ticker_live(
            "NODATA", fetcher=self._fetcher({"NODATA": pull}))
        self.assertEqual(record["status"], "insufficient_data")
        self.assertIn("no live financial data", record["reason"])

    def test_provider_unavailable_recorded(self):
        pull = {"ticker": "UNREACH", "data_available": False,
                "provider_unavailable": True,
                "provider_error": "Cookie/crumb fetch failed (Timeout)"}
        record = lws.scan_ticker_live(
            "UNREACH", fetcher=self._fetcher({"UNREACH": pull}))
        self.assertEqual(record["status"], "provider_unavailable")
        self.assertIn("crumb", record["reason"])

    def test_fetcher_exception_never_aborts(self):
        record = lws.scan_ticker_live(
            "BOOM", fetcher=self._fetcher(
                {"BOOM": RuntimeError("connection reset")}))
        self.assertEqual(record["status"], "provider_unavailable")
        self.assertIn("connection reset", record["reason"])

    def test_missing_price_is_insufficient(self):
        pull = _live("NOPRICE", None, 10e9)
        record = lws.scan_ticker_live(
            "NOPRICE", fetcher=self._fetcher({"NOPRICE": pull}))
        self.assertEqual(record["status"], "insufficient_data")
        self.assertIn("price", record["reason"])

    # -- whole watchlist --------------------------------------------

    def test_watchlist_scan_mixed_summary_and_rate_limit(self):
        self._seed(["CHEAP", "OVER", "NODATA", "UNREACH"])
        optimistic = _dcf(10e9, 0.15)
        pulls = {
            "CHEAP": _live("CHEAP", 50.0, 10e9),
            "OVER": _live("OVER", 2 * optimistic, 10e9),
            "NODATA": dict(
                _live("NODATA", 50.0, 10e9), data_available=False),
            "UNREACH": {"ticker": "UNREACH", "data_available": False,
                        "provider_unavailable": True,
                        "provider_error": "timeout"},
        }
        sleeps = []
        result = lws.scan_watchlist_live(
            store_dir=str(self.store_dir),
            fetcher=self._fetcher(pulls),
            rate_limit_seconds=2.0, sleep=sleeps.append,
            as_of="2026-11-09T03:00:00+00:00")
        self.assertEqual(result["summary"]["selected"], 4)
        self.assertEqual(result["summary"]["scannable"], 2)
        self.assertEqual(result["summary"]["candidates"], 1)
        self.assertEqual(result["summary"]["strongest_alerts"], 1)
        self.assertEqual(result["summary"]["watch"], 0)
        self.assertEqual(result["summary"]["provider_unavailable"], 1)
        self.assertEqual(result["summary"]["insufficient_data"], 1)
        self.assertEqual(result["summary"]["by_band"],
                         {"strongest_alert": 1})
        # Rate-limit pause between fetches only: 3 pauses for 4.
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])
        self.assertEqual(result["scanned_at"],
                         "2026-11-09T03:00:00+00:00")
        lines = lws.summary_lines(result)
        self.assertIn("1 candidate(s)", lines[0])
        self.assertIn("CHEAP", "\n".join(lines))

    def test_watchlist_scan_subset_and_empty_store(self):
        self._seed(["CHEAP", "OVER"])
        pulls = {"CHEAP": _live("CHEAP", 50.0, 10e9)}
        result = lws.scan_watchlist_live(
            store_dir=str(self.store_dir), tickers=["CHEAP"],
            fetcher=self._fetcher(pulls), rate_limit_seconds=0,
            sleep=lambda seconds: None)
        self.assertEqual(result["summary"]["selected"], 1)
        empty = lws.scan_watchlist_live(
            store_dir=str(self.tmp.name + "/empty"),
            fetcher=self._fetcher({}), rate_limit_seconds=0,
            sleep=lambda seconds: None)
        self.assertEqual(empty["summary"]["selected"], 0)

    def test_one_ticker_failure_does_not_abort_run(self):
        self._seed(["BOOM", "CHEAP"])
        pulls = {
            "BOOM": RuntimeError("connection reset"),
            "CHEAP": _live("CHEAP", 50.0, 10e9),
        }
        result = lws.scan_watchlist_live(
            store_dir=str(self.store_dir),
            fetcher=self._fetcher(pulls), rate_limit_seconds=0,
            sleep=lambda seconds: None)
        by_ticker = {r["ticker"]: r for r in result["results"]}
        self.assertEqual(by_ticker["BOOM"]["status"],
                         "provider_unavailable")
        self.assertEqual(by_ticker["CHEAP"]["status"], "ok")
        self.assertEqual(result["summary"]["candidates"], 1)


if __name__ == "__main__":
    unittest.main()
