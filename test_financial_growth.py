"""Step 19 tests — growth analysis.

Fixture rows only, plus a temporary copy of the real database for
the MSFT hand-checked values.  No network requests, no production
database writes (the production hash is verified unchanged by the
report run, not by these tests).
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agents import financial_growth as fg

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    temp_dir_with_db_cleanup,
)
PRODUCTION_DB = PROJECT_ROOT / "database" / "assistant.db"


def _value_dict(value):
    return {"present": value is not None, "value": value}


def _values_for(concept, annual, quarterly=None):
    """Fake ``load_standardised_values`` output for one concept.

    ``annual`` / ``quarterly``: list of (period, value|None).
    """
    from agents import financial_standard as fs

    statement = fs.CONCEPT_STATEMENT.get(concept, "income_statement")
    values = {}
    for period, value in annual or []:
        values[(statement, period, "annual")] = {
            concept: _value_dict(value),
            "_source_id": 1, "_warnings": [], "_currency": "USD",
        }
    for period, value in quarterly or []:
        values[(statement, period, "quarterly")] = {
            concept: _value_dict(value),
            "_source_id": 1, "_warnings": [], "_currency": "USD",
        }
    return values


class YoyTests(unittest.TestCase):
    def test_simple_yoy(self):
        series = [("2024-06-30", 100.0), ("2025-06-30", 120.0),
                  ("2026-06-30", 108.0)]
        yoy = fg.yoy_growth(series)
        self.assertEqual(len(yoy), 2)
        self.assertAlmostEqual(yoy[0]["value"], 0.20)
        self.assertAlmostEqual(yoy[1]["value"], -0.10)
        self.assertTrue(all(g["present"] for g in yoy))

    def test_yoy_missing_current(self):
        series = [("2024-06-30", 100.0), ("2025-06-30", None)]
        yoy = fg.yoy_growth(series)
        self.assertFalse(yoy[0]["present"])
        self.assertIn("current value", yoy[0]["reason"])

    def test_yoy_missing_prior(self):
        series = [("2024-06-30", None), ("2025-06-30", 120.0)]
        yoy = fg.yoy_growth(series)
        self.assertFalse(yoy[0]["present"])
        self.assertIn("prior value", yoy[0]["reason"])

    def test_yoy_zero_prior_is_missing_not_infinite(self):
        series = [("2024-06-30", 0.0), ("2025-06-30", 50.0)]
        yoy = fg.yoy_growth(series)
        self.assertFalse(yoy[0]["present"])
        self.assertIn("zero", yoy[0]["reason"])

    def test_yoy_negative_prior_is_missing_not_nonsense(self):
        # -$10m to +$5m is not "-150% growth" in any usable sense.
        series = [("2024-06-30", -10.0), ("2025-06-30", 5.0)]
        yoy = fg.yoy_growth(series)
        self.assertFalse(yoy[0]["present"])
        self.assertIn("negative", yoy[0]["reason"])

    def test_yoy_negative_current_is_fine(self):
        # +$10m to -$5m is a genuine -150% change from a positive base.
        series = [("2024-06-30", 10.0), ("2025-06-30", -5.0)]
        yoy = fg.yoy_growth(series)
        self.assertTrue(yoy[0]["present"])
        self.assertAlmostEqual(yoy[0]["value"], -1.5)


class CagrTests(unittest.TestCase):
    def _series(self, start, end, years):
        # Annual periods ending 2026-06-30, ``years`` steps back.
        periods = [f"{2026 - i}-06-30" for i in range(years, -1, -1)]
        values = [None] * (years + 1)
        values[0] = start
        values[-1] = end
        return list(zip(periods, values))

    def test_cagr_doubling_in_5_years(self):
        result = fg.cagr(self._series(100.0, 200.0, 5), 5)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 2 ** 0.2 - 1)

    def test_cagr_missing_start_endpoint(self):
        result = fg.cagr(self._series(None, 200.0, 5), 5)
        self.assertFalse(result["present"])
        self.assertIn("missing endpoint", result["reason"])

    def test_cagr_missing_end_endpoint(self):
        result = fg.cagr(self._series(100.0, None, 5), 5)
        self.assertFalse(result["present"])
        self.assertIn("missing endpoint", result["reason"])

    def test_cagr_negative_start_is_undefined(self):
        result = fg.cagr(self._series(-100.0, 200.0, 5), 5)
        self.assertFalse(result["present"])
        self.assertIn("undefined", result["reason"])

    def test_cagr_zero_start_is_undefined(self):
        result = fg.cagr(self._series(0.0, 200.0, 5), 5)
        self.assertFalse(result["present"])
        self.assertIn("undefined", result["reason"])

    def test_cagr_negative_end_is_undefined(self):
        result = fg.cagr(self._series(100.0, -50.0, 5), 5)
        self.assertFalse(result["present"])
        self.assertIn("undefined", result["reason"])

    def test_cagr_zero_end_is_minus_100_percent(self):
        result = fg.cagr(self._series(100.0, 0.0, 5), 5)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], -1.0)

    def test_cagr_insufficient_history(self):
        series = [("2025-06-30", 100.0), ("2026-06-30", 120.0)]
        result = fg.cagr(series, 10)
        self.assertFalse(result["present"])
        self.assertIn("insufficient history", result["reason"])


class ConsistencyTests(unittest.TestCase):
    def test_consistency_metrics(self):
        yoy = [
            {"period": f"202{i}-06-30", "value": v, "present": True}
            for i, v in enumerate([0.10, -0.05, 0.20, 0.15, 0.05], start=2)
        ]
        m = fg.consistency_metrics(yoy)
        self.assertEqual(m["years_used"], 5)
        self.assertAlmostEqual(m["share_growing"], 0.8)
        self.assertAlmostEqual(m["mean_growth"], 0.09)
        self.assertAlmostEqual(m["median_growth"], 0.10)
        self.assertEqual(m["best_year"]["period"], "2024-06-30")
        self.assertEqual(m["worst_year"]["period"], "2023-06-30")

    def test_consistency_missing_years_excluded_not_zero(self):
        yoy = [
            {"period": "2024-06-30", "value": 0.10, "present": True},
            {"period": "2025-06-30", "value": None, "present": False,
             "reason": "missing input(s): current value"},
            {"period": "2026-06-30", "value": 0.20, "present": True},
        ]
        m = fg.consistency_metrics(yoy)
        self.assertEqual(m["years_used"], 2)
        self.assertEqual(m["years_missing"], 1)
        self.assertAlmostEqual(m["mean_growth"], 0.15)

    def test_consistency_no_usable_years(self):
        m = fg.consistency_metrics([])
        self.assertEqual(m["years_used"], 0)
        self.assertIsNotNone(m["reason"])


class DrawdownTests(unittest.TestCase):
    def test_max_drawdown(self):
        series = [("2020-06-30", 100.0), ("2021-06-30", 120.0),
                  ("2022-06-30", 90.0), ("2023-06-30", 150.0)]
        dd = fg.max_drawdown(series)
        self.assertAlmostEqual(dd["value"], 0.25)
        self.assertEqual(dd["peak_period"], "2021-06-30")
        self.assertEqual(dd["trough_period"], "2022-06-30")

    def test_max_drawdown_never_rising(self):
        series = [("2020-06-30", 100.0), ("2021-06-30", 110.0)]
        dd = fg.max_drawdown(series)
        self.assertIsNone(dd["value"])

    def test_max_drawdown_skips_missing_and_non_positive(self):
        series = [("2020-06-30", 100.0), ("2021-06-30", None),
                  ("2022-06-30", -5.0), ("2023-06-30", 80.0)]
        dd = fg.max_drawdown(series)
        self.assertAlmostEqual(dd["value"], 0.20)


class CyclicalityTests(unittest.TestCase):
    def test_steady_series(self):
        series = [(f"20{10 + i}-06-30", 100.0 * (1.10 ** i))
                  for i in range(11)]
        yoy = fg.yoy_growth(series)
        verdict = fg.assess_cyclicality(series, yoy)
        self.assertEqual(verdict["verdict"], "steady")

    def test_volatile_series(self):
        series = [(f"20{10 + i}-06-30", v) for i, v in enumerate(
            [100.0, 130.0, 90.0, 140.0, 80.0, 150.0, 70.0, 160.0,
             60.0, 170.0, 50.0])]
        yoy = fg.yoy_growth(series)
        verdict = fg.assess_cyclicality(series, yoy)
        self.assertEqual(verdict["verdict"], "volatile")

    def test_insufficient_data(self):
        verdict = fg.assess_cyclicality(
            [("2026-06-30", 100.0)], [])
        self.assertEqual(verdict["verdict"], "insufficient data")


class QuarterlyYoyTests(unittest.TestCase):
    def test_quarterly_yoy_same_quarter_last_year(self):
        values = _values_for(
            "revenue", annual=[],
            quarterly=[("2025-09-30", 100.0), ("2026-09-30", 118.0)])
        result = fg.quarterly_yoy(values, "revenue")
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 0.18)
        self.assertEqual(result["prior_period"], "2025-09-30")

    def test_quarterly_yoy_missing_prior_quarter(self):
        values = _values_for(
            "revenue", annual=[],
            quarterly=[("2026-09-30", 118.0)])
        result = fg.quarterly_yoy(values, "revenue")
        self.assertFalse(result["present"])
        self.assertIn("2025-09-30", result["reason"])

    def test_quarterly_yoy_no_quarterly_rows(self):
        result = fg.quarterly_yoy({}, "revenue")
        self.assertFalse(result["present"])


class RealDbTests(unittest.TestCase):
    """Hand-checked MSFT values from the real database (temp copy)."""

    def setUp(self):
        self.tmp = temp_dir_with_db_cleanup()
        self.db_path = Path(self.tmp.name) / "test.db"
        shutil.copy(PRODUCTION_DB, self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        cleanup_temp_dir(self.tmp)

    def test_msft_revenue_yoy_and_cagrs(self):
        result = fg.analyse_growth(self.conn, "MSFT")
        rev = result["series"]["revenue"]
        # 331,839 / 281,724 - 1
        self.assertAlmostEqual(rev["latest_yoy"]["value"], 0.1779, places=3)
        self.assertEqual(rev["latest_yoy"]["period"], "2026-06-30")
        self.assertAlmostEqual(rev["cagr"][10]["value"], 0.1379, places=3)
        self.assertAlmostEqual(rev["cagr"][5]["value"], 0.1457, places=3)
        self.assertAlmostEqual(rev["cagr"][3]["value"], 0.1612, places=3)
        self.assertEqual(rev["cagr"][10]["start_period"], "2016-06-30")

    def test_msft_net_income_and_eps_yoy(self):
        result = fg.analyse_growth(self.conn, "MSFT")
        # 133,749 / 101,832 - 1
        self.assertAlmostEqual(
            result["series"]["net_income"]["latest_yoy"]["value"],
            0.3134, places=3)
        # 17.95 / 13.64 - 1
        self.assertAlmostEqual(
            result["series"]["eps_diluted"]["latest_yoy"]["value"],
            0.316, places=3)

    def test_msft_fcf_fell_in_fy2026(self):
        result = fg.analyse_growth(self.conn, "MSFT")
        fcf = result["series"]["free_cash_flow"]
        # 66,987 / 71,611 - 1 (capex rose faster than OCF)
        self.assertAlmostEqual(fcf["latest_yoy"]["value"], -0.0646, places=3)
        self.assertAlmostEqual(fcf["cagr"][10]["value"], 0.1037, places=3)

    def test_msft_revenue_grew_every_year_in_10y(self):
        result = fg.analyse_growth(self.conn, "MSFT")
        rev = result["series"]["revenue"]
        self.assertAlmostEqual(
            rev["consistency_10y"]["share_growing"], 1.0)
        self.assertEqual(rev["cyclicality"]["verdict"], "steady")

    def test_msft_net_income_is_volatile(self):
        # FY2018 tax year and the 2011-2015 drawdown (Nokia write-down)
        result = fg.analyse_growth(self.conn, "MSFT")
        ni = result["series"]["net_income"]
        self.assertEqual(ni["cyclicality"]["verdict"], "volatile")
        self.assertAlmostEqual(
            ni["cyclicality"]["max_drawdown"], 0.4733, places=3)
        self.assertEqual(
            ni["cyclicality"]["drawdown_peak_period"], "2011-06-30")
        self.assertEqual(
            ni["cyclicality"]["drawdown_trough_period"], "2015-06-30")

    def test_msft_quarterly_yoy(self):
        result = fg.analyse_growth(self.conn, "MSFT")
        q = result["series"]["revenue"]["quarterly_yoy_latest"]
        self.assertEqual(q["period"], "2026-09-30")
        self.assertAlmostEqual(q["value"], 0.188, places=3)

    def test_msft_operating_cash_flow_yoy(self):
        result = fg.analyse_growth(self.conn, "MSFT")
        ocf = result["series"]["operating_cash_flow"]
        self.assertAlmostEqual(ocf["latest_yoy"]["value"], 0.3435, places=3)

    def test_unknown_ticker_returns_empty_not_exception(self):
        result = fg.analyse_growth(self.conn, "NOPE")
        self.assertEqual(result["series"], {})
        self.assertTrue(result["warnings"])

    def test_production_db_untouched_by_analysis(self):
        before = PRODUCTION_DB.read_bytes()
        fg.analyse_growth(self.conn, "MSFT")
        self.assertEqual(PRODUCTION_DB.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
