"""Step 36 — Portfolio performance and risk analysis: tests.

Fixture portfolios and price histories only; no network, no
production writes.  Every test cleans up after itself via
TemporaryDirectory (the /tmp housekeeping gap noted in Steps
27/33b: older _temp_db_copy helpers never delete their
copies).  The production DB is copied once per test class
into the temp directory and the copy is removed in
tearDownClass; the production hash is asserted unchanged.
"""

import hashlib
import json
import math
import shutil
import sqlite3
import statistics
import tempfile
import unittest
from datetime import date
from pathlib import Path

from agents import portfolio_performance as pp

PROJECT_ROOT = Path(__file__).resolve().parent
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"


def _db_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _monthly(start, months, first, last):
    """Linear monthly price series from ``first`` to ``last``."""
    year, month = int(start[:4]), int(start[5:7])
    points = []
    for i in range(months):
        y = year + (month - 1 + i) // 12
        m = (month - 1 + i) % 12 + 1
        price = first + (last - first) * i / (months - 1)
        points.append((f"{y:04d}-{m:02d}-01", round(price, 4)))
    return points


# Fixture portfolio: three positions, $1,000 cost each.
# ALPHA 100 @ $10 -> $15 (+50%), BETA 50 @ $20 -> $18 (-10%),
# GAMMA 200 @ $5 -> $8 (+60%).  Total cost $3,000, value
# $4,000, total return +33.333%.  Dividends $40 + $60 + $0.
FIXTURE_HOLDINGS = [
    {"ticker": "ALPHA", "shares": 100, "cost_basis": 10.0,
     "purchase_date": "2024-11-01", "dividends": 40.0,
     "sector": "Technology", "country": "USA"},
    {"ticker": "BETA", "shares": 50, "cost_basis": 20.0,
     "purchase_date": "2024-11-01", "dividends": 60.0,
     "sector": "Technology", "country": "USA"},
    {"ticker": "GAMMA", "shares": 200, "cost_basis": 5.0,
     "purchase_date": "2025-06-01", "dividends": 0.0,
     "sector": "Healthcare", "country": "Singapore"},
]

FIXTURE_HISTORY = {
    "ALPHA": _monthly("2024-11-01", 24, 10.0, 15.0),
    "BETA": _monthly("2024-11-01", 24, 20.0, 18.0),
    # GAMMA starts 2025-06-01 (17 monthly points, $5 -> $8).
    "GAMMA": _monthly("2025-06-01", 17, 5.0, 8.0),
}

FIXTURE_BENCHMARK = _monthly("2024-11-01", 24, 1000.0, 1300.0)


class _TempDbTestCase(unittest.TestCase):
    """A temp DB copy shared by the class; removed afterwards."""

    db_path = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="step36-")
        cls.db_path = Path(cls.tmp.name) / "copy.db"
        shutil.copy(PROD_DB, cls.db_path)
        cls.hash_before = _db_hash(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        assert _db_hash(PROD_DB) == PROD_HASH, (
            "production DB hash changed during Step 36 tests")
        cls.tmp.cleanup()

    def setUp(self):
        self.store = tempfile.TemporaryDirectory(prefix="step36-store-")
        self.store_dir = Path(self.store.name)

    def tearDown(self):
        self.store.cleanup()


class TestPositionPerformance(_TempDbTestCase):

    def test_position_return_and_income(self):
        perf = pp.position_performance(
            FIXTURE_HOLDINGS[0], FIXTURE_HISTORY)
        self.assertEqual(perf["status"], "ok")
        self.assertEqual(perf["total_cost"], 1000.0)
        self.assertEqual(perf["current_value"], 1500.0)
        self.assertEqual(perf["unrealised_gain"], 500.0)
        self.assertAlmostEqual(perf["total_return"], 0.50)
        self.assertEqual(perf["dividends"], 40.0)
        self.assertAlmostEqual(perf["yield_on_cost"], 0.04)
        self.assertAlmostEqual(perf["yield_on_value"], 40.0 / 1500.0)

    def test_position_loss(self):
        perf = pp.position_performance(
            FIXTURE_HOLDINGS[1], FIXTURE_HISTORY)
        self.assertEqual(perf["current_value"], 900.0)
        self.assertEqual(perf["unrealised_gain"], -100.0)
        self.assertAlmostEqual(perf["total_return"], -0.10)

    def test_position_missing_price(self):
        perf = pp.position_performance(
            FIXTURE_HOLDINGS[0], {"ALPHA": []})
        self.assertEqual(perf["status"], "missing")
        self.assertIn("price history", perf["reason"])
        self.assertIsNone(perf["current_value"])
        self.assertIsNone(perf["total_return"])

    def test_position_missing_cost_basis(self):
        perf = pp.position_performance(
            {"ticker": "ALPHA", "shares": 10},
            FIXTURE_HISTORY)
        # No cost basis: valued (price exists) but return is None.
        self.assertEqual(perf["status"], "ok")
        self.assertEqual(perf["total_cost"], 0.0)
        self.assertEqual(perf["current_value"], 150.0)
        self.assertIsNone(perf["total_return"])

    def test_realised_and_unrealised_split(self):
        holding = {"ticker": "ALPHA", "shares": 60,
                   "cost_basis": 10.0,
                   "sold": [{"shares": 40, "price": 12.0,
                             "date": "2025-06-01"}]}
        perf = pp.position_performance(holding, FIXTURE_HISTORY)
        self.assertEqual(perf["sold_shares"], 40.0)
        self.assertEqual(perf["realised_gain"], 80.0)  # 40 x $2
        self.assertEqual(perf["unrealised_gain"], 300.0)  # 60 x $5
        self.assertEqual(perf["total_gain"], 380.0)
        self.assertEqual(perf["total_cost"], 1000.0)
        self.assertAlmostEqual(perf["total_return"], 0.38)

    def test_current_price_override(self):
        perf = pp.position_performance(
            FIXTURE_HOLDINGS[0], FIXTURE_HISTORY,
            current_prices={"ALPHA": 20.0})
        self.assertEqual(perf["current_value"], 2000.0)
        self.assertIn("caller-supplied", perf["price_source"])


class TestReturns(_TempDbTestCase):

    def test_time_weighted_return_hand_computed(self):
        # 1000 -> 1100 (+10%), -> 990 (-10%), -> 1210 (+22.22%):
        # 1.1 x 0.9 x 1.2222... = 1.21 exactly.
        values = [(date(2025, 1, 1), 1000.0),
                  (date(2025, 2, 1), 1100.0),
                  (date(2025, 3, 1), 990.0),
                  (date(2025, 4, 1), 1210.0)]
        result = pp.time_weighted_return(values)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["value"], 0.21, places=6)

    def test_time_weighted_return_needs_two_points(self):
        result = pp.time_weighted_return(
            [(date(2025, 1, 1), 1000.0)])
        self.assertEqual(result["status"], "missing")
        self.assertIn("two", result["reason"])

    def test_time_weighted_return_zero_start(self):
        result = pp.time_weighted_return(
            [(date(2025, 1, 1), 0.0),
             (date(2025, 2, 1), 100.0)])
        self.assertEqual(result["status"], "missing")

    def test_money_weighted_return_solves_npv(self):
        flows = [(date(2024, 11, 1), -1000.0),
                 (date(2025, 11, 1), 100.0),
                 (date(2026, 11, 1), 1200.0)]
        result = pp.money_weighted_return(flows)
        self.assertEqual(result["status"], "ok")
        # Hand estimate ~14.7% (exact 1y/2y spacing); verify by
        # re-solving NPV at the returned rate, independently.
        rate = result["value"]
        self.assertTrue(0.14 < rate < 0.15)
        base = flows[0][0]
        npv = sum(amount / ((1.0 + rate) ** ((when - base).days / 365.25))
                  for when, amount in flows)
        self.assertAlmostEqual(npv, 0.0, places=4)

    def test_money_weighted_return_no_solution(self):
        # Only money in, never out: no IRR exists.
        result = pp.money_weighted_return(
            [(date(2024, 11, 1), -1000.0),
             (date(2025, 11, 1), -500.0)])
        self.assertEqual(result["status"], "missing")

    def test_money_weighted_return_needs_two_dates(self):
        result = pp.money_weighted_return(
            [(date(2024, 11, 1), -1000.0),
             (date(2024, 11, 1), 1200.0)])
        self.assertEqual(result["status"], "missing")


class TestRisk(_TempDbTestCase):

    def test_volatility_hand_computed(self):
        # Returns [0.10, -0.05, 0.20, 0.05]: mean 0.075,
        # squared deviations sum 0.0325, /3 = 0.0108333,
        # sqrt = 0.1040833.
        result = pp.volatility([0.10, -0.05, 0.20, 0.05])
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["value"], 0.1040833, places=6)
        self.assertIsNone(result["annualised"])
        self.assertIn("periods per year", result["annualised_reason"])

    def test_volatility_annualised(self):
        result = pp.volatility([0.10, -0.05, 0.20, 0.05],
                               periods_per_year=12)
        self.assertAlmostEqual(result["annualised"],
                               0.1040833 * math.sqrt(12), places=6)
        self.assertEqual(result["periods_per_year"], 12)

    def test_volatility_matches_stdlib(self):
        returns = [0.03, -0.02, 0.05, 0.01, -0.04, 0.02]
        result = pp.volatility(returns)
        self.assertAlmostEqual(result["value"],
                               statistics.stdev(returns))

    def test_volatility_needs_two_returns(self):
        result = pp.volatility([0.05])
        self.assertEqual(result["status"], "missing")

    def test_max_drawdown_hand_computed(self):
        # 100 -> 110 -> 99 -> 121: worst fall 110 -> 99 = -10%.
        values = [(date(2025, 1, 1), 100.0),
                  (date(2025, 2, 1), 110.0),
                  (date(2025, 3, 1), 99.0),
                  (date(2025, 4, 1), 121.0)]
        result = pp.max_drawdown(values)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["value"], -0.10)
        self.assertEqual(result["peak_date"], "2025-02-01")
        self.assertEqual(result["trough_date"], "2025-03-01")

    def test_max_drawdown_rising_series_is_zero(self):
        values = [(date(2025, 1, 1), 100.0),
                  (date(2025, 2, 1), 110.0),
                  (date(2025, 3, 1), 120.0)]
        result = pp.max_drawdown(values)
        self.assertEqual(result["value"], 0.0)

    def test_max_drawdown_needs_two_points(self):
        result = pp.max_drawdown([(date(2025, 1, 1), 100.0)])
        self.assertEqual(result["status"], "missing")

    def test_beta_hand_computed(self):
        # Asset returns [0.10, -0.05, 0.20], benchmark
        # [0.08, -0.04, 0.15]: cov 0.0080556, var 0.0061556,
        # beta 1.3087 (hand-computed, see step36-report.md).
        asset = [("2025-01-01", 100.0), ("2025-02-01", 110.0),
                 ("2025-03-01", 104.5), ("2025-04-01", 125.4)]
        bench = [("2025-01-01", 1000.0), ("2025-02-01", 1080.0),
                 ("2025-03-01", 1036.8), ("2025-04-01", 1192.32)]
        result = pp.beta(asset, bench)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["value"], 1.3087, places=3)
        self.assertEqual(result["overlapping_dates"], 3)

    def test_beta_needs_overlap(self):
        asset = [("2025-01-01", 100.0), ("2025-02-01", 110.0)]
        bench = [("2026-01-01", 1000.0), ("2026-02-01", 1080.0)]
        result = pp.beta(asset, bench)
        self.assertEqual(result["status"], "missing")
        self.assertIn("shared", result["reason"])

    def test_beta_flat_benchmark_undefined(self):
        asset = [("2025-01-01", 100.0), ("2025-02-01", 110.0),
                 ("2025-03-01", 99.0)]
        bench = [("2025-01-01", 1000.0), ("2025-02-01", 1000.0),
                 ("2025-03-01", 1000.0)]
        result = pp.beta(asset, bench)
        self.assertEqual(result["status"], "missing")
        self.assertIn("do not vary", result["reason"])


class TestConcentration(_TempDbTestCase):

    def test_concentration_flags(self):
        # Values 1500 / 900 / 1600 of 4000: GAMMA 40% (single
        # name), Technology 60% (sector).  USA 60% is not above
        # the 60% country threshold, so no country warning.
        result = pp.concentration(FIXTURE_HOLDINGS, FIXTURE_HISTORY)
        self.assertEqual(result["total_value"], 4000.0)
        self.assertAlmostEqual(result["max_single_name_weight"], 0.40)
        self.assertEqual(result["max_single_name_ticker"], "GAMMA")
        self.assertEqual(result["max_sector"]["sector"], "Technology")
        self.assertAlmostEqual(result["max_sector"]["weight"], 0.60)
        warnings = " ".join(result["warnings"])
        self.assertIn("concentrated in one name", warnings)
        self.assertIn("concentrated in one sector", warnings)
        self.assertNotIn("concentrated in one country", warnings)
        self.assertIn("current value", result["basis"])

    def test_concentration_unvalued_excluded(self):
        result = pp.concentration(
            FIXTURE_HOLDINGS + [{"ticker": "NOPRICE", "shares": 10,
                                 "cost_basis": 1.0}],
            FIXTURE_HISTORY)
        self.assertEqual(len(result["unvalued"]), 1)
        self.assertEqual(result["unvalued"][0]["ticker"], "NOPRICE")
        self.assertEqual(result["total_value"], 4000.0)

    def test_concentration_empty(self):
        result = pp.concentration([], {})
        self.assertEqual(result["total_value"], 0.0)
        self.assertIsNone(result["max_single_name_weight"])


class TestPortfolioSummary(_TempDbTestCase):

    def test_fixture_portfolio_totals(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY,
            benchmark_history=FIXTURE_BENCHMARK,
            periods_per_year=12, currency="USD")
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["total_cost"], 3000.0)
        self.assertEqual(summary["total_value"], 4000.0)
        self.assertEqual(summary["total_gain"], 1000.0)
        self.assertAlmostEqual(summary["total_return"], 1 / 3)
        self.assertEqual(summary["realised_gain"], 0.0)
        self.assertEqual(summary["unrealised_gain"], 1000.0)
        self.assertEqual(summary["dividends_received"], 100.0)
        self.assertAlmostEqual(summary["yield_on_cost"], 100.0 / 3000.0)
        self.assertAlmostEqual(summary["yield_on_value"], 100.0 / 4000.0)

    def test_fixture_contributors_and_detractors(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        self.assertEqual(
            [c["ticker"] for c in summary["contributors"]],
            ["GAMMA", "ALPHA"])
        self.assertEqual(summary["contributors"][0]["gain"], 600.0)
        self.assertEqual(
            [d["ticker"] for d in summary["detractors"]], ["BETA"])
        self.assertEqual(summary["detractors"][0]["gain"], -100.0)

    def test_fixture_time_weighted_return(self):
        # Value series starts 2025-06-01 (first date GAMMA has
        # a price): ALPHA 1152.17 + BETA 969.57 + GAMMA 1000.00
        # = 3121.74, ending 4000.00 on 2026-10-01.  No cash
        # flows in between, so TWR = 4000/3121.74 - 1.
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        twr = summary["time_weighted_return"]
        self.assertEqual(twr["status"], "ok")
        self.assertAlmostEqual(twr["value"], 4000.0 / 3121.74 - 1,
                               places=4)
        self.assertEqual(twr["period"]["from"], "2025-06-01")
        self.assertEqual(twr["period"]["to"], "2026-10-01")

    def test_fixture_drawdown_is_zero(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        dd = summary["max_drawdown"]
        self.assertEqual(dd["status"], "ok")
        self.assertEqual(dd["value"], 0.0)

    def test_fixture_volatility_annualised(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY,
            periods_per_year=12, currency="USD")
        vol = summary["volatility"]
        self.assertEqual(vol["status"], "ok")
        self.assertEqual(vol["periods_per_year"], 12)
        self.assertGreater(vol["annualised"], vol["value"])

    def test_fixture_beta_is_value_weighted(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY,
            benchmark_history=FIXTURE_BENCHMARK, currency="USD")
        beta = summary["beta"]
        self.assertEqual(beta["status"], "ok")
        self.assertEqual(beta["positions_with_beta"], 3)
        # Beta is a weighted mean of three position betas, so
        # it must lie between the smallest and largest.
        individual = [pp.beta(FIXTURE_HISTORY[t],
                              FIXTURE_BENCHMARK)["value"]
                      for t in ("ALPHA", "BETA", "GAMMA")]
        self.assertTrue(min(individual) <= beta["value"]
                        <= max(individual))

    def test_beta_missing_without_benchmark(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        self.assertEqual(summary["beta"]["status"], "missing")
        self.assertIn("benchmark", summary["beta"]["reason"])

    def test_money_weighted_missing_without_flows(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        self.assertEqual(
            summary["money_weighted_return"]["status"], "missing")

    def test_currency_warning(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY)
        self.assertIn("currency not supplied",
                      " ".join(summary["risk_flags"]))

    def test_empty_portfolio(self):
        summary = pp.portfolio_summary([], {}, currency="USD")
        self.assertEqual(summary["status"], "insufficient_data")
        self.assertEqual(summary["total_value"], 0.0)
        self.assertIsNone(summary["total_return"])

    def test_summary_lines(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        lines = pp.summary_lines(summary)
        self.assertTrue(any("4000.00" in line for line in lines))
        self.assertTrue(any("+33.3%" in line for line in lines))
        self.assertTrue(any("Risk:" in line for line in lines))

    def test_snapshot_save_load_round_trip(self):
        summary = pp.portfolio_summary(
            FIXTURE_HOLDINGS, FIXTURE_HISTORY, currency="USD")
        path = self.store_dir / "snapshot.json"
        pp.save_snapshot(summary, path)
        loaded = pp.load_snapshot(path)
        self.assertEqual(loaded["total_value"], 4000.0)
        self.assertEqual(loaded["total_cost"], 3000.0)
        self.assertEqual(loaded["positions_total"], 3)


class TestMsftOnRealData(_TempDbTestCase):
    """MSFT from the temp DB copy: only one price point exists."""

    def _msft_history(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT snapshot_date, financial_data "
            "FROM financial_snapshots WHERE ticker = 'MSFT' "
            "ORDER BY snapshot_date").fetchall()
        conn.close()
        history = []
        for snapshot_date, raw in rows:
            data = json.loads(raw)
            history.append((snapshot_date[:10],
                            float(data["current_price"])))
        return history

    def test_msft_snapshot_history_is_one_price_point(self):
        history = self._msft_history()
        self.assertEqual(len(history), 3)
        dates = {d for d, _ in history}
        self.assertEqual(dates, {"2026-11-03"})
        prices = {p for _, p in history}
        self.assertEqual(prices, {507.12})

    def test_msft_simple_return_is_available(self):
        # One price point plus a cost basis still gives a
        # simple return — what is unavailable is every
        # history-dependent metric (volatility, drawdown, TWR,
        # beta), not the return itself.
        holding = [{"ticker": "MSFT", "shares": 10,
                    "cost_basis": 400.0}]
        summary = pp.portfolio_summary(
            holding, {"MSFT": self._msft_history()}, currency="USD")
        self.assertEqual(summary["total_value"], 5071.20)
        self.assertAlmostEqual(summary["total_return"], 0.2678)

    def test_msft_history_metrics_unavailable(self):
        holding = [{"ticker": "MSFT", "shares": 10,
                    "cost_basis": 400.0}]
        summary = pp.portfolio_summary(
            holding, {"MSFT": self._msft_history()},
            benchmark_history=[("2026-11-01", 1000.0),
                               ("2026-11-03", 1010.0)],
            currency="USD")
        for key in ("time_weighted_return", "volatility",
                    "max_drawdown", "beta"):
            self.assertEqual(summary[key]["status"], "missing",
                             key)
        self.assertEqual(
            summary["money_weighted_return"]["status"], "missing")
        # Concentration still works on the single current value.
        self.assertEqual(
            summary["concentration"]["total_value"], 5071.20)


if __name__ == "__main__":
    unittest.main()
