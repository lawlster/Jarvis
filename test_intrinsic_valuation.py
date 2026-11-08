"""Tests for Step 21 — intrinsic valuation engine.

Fixture rows and temp DB copies only; no network requests and no
production-database writes.  The MSFT test uses a temp copy of the
real database and hand-checked values from the DB (not from any
earlier report).
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import intrinsic_valuation as iv

BASE_DIR = Path(__file__).resolve().parent
PRODUCTION_DB = BASE_DIR / "database" / "assistant.db"


def temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copyfile(PRODUCTION_DB, tmp.name)
    return tmp.name


class DcfArithmeticTests(unittest.TestCase):
    """The wrapper must reproduce calculate_dcf, checked by hand."""

    def test_base_case_matches_hand_computation(self):
        # FCF $59.957bn, g=10%, WACC 11%, terminal 2.5%, 10y,
        # net debt $15.2bn, shares 7.446bn — computed independently
        # in the Step 21 verification (PV flows $570.7bn, PV terminal
        # $660.5bn, per share $163.30).
        result = iv.dcf_value(59.957e9, 0.10, 0.11, 0.025, 15.2e9,
                              7.446e9, 10)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 163.30, places=1)
        self.assertAlmostEqual(
            result["present_value_of_cash_flows"] / 1e9, 570.7, places=0)
        self.assertAlmostEqual(
            result["present_terminal_value"] / 1e9, 660.5, places=0)

    def test_zero_fcf_is_missing_not_an_error(self):
        result = iv.dcf_value(0, 0.10, 0.11, 0.025, 0, 1e9, 10)
        self.assertFalse(result["present"])
        self.assertIn("Free cash flow", result["reason"])

    def test_negative_fcf_is_missing(self):
        result = iv.dcf_value(-100, 0.10, 0.11, 0.025, 0, 1e9, 10)
        self.assertFalse(result["present"])

    def test_discount_not_above_terminal_growth_is_missing(self):
        result = iv.dcf_value(1e9, 0.10, 0.02, 0.025, 0, 1e9, 10)
        self.assertFalse(result["present"])

    def test_missing_starting_fcf_is_missing(self):
        result = iv.dcf_value(None, 0.10, 0.11, 0.025, 0, 1e9, 10)
        self.assertFalse(result["present"])
        self.assertIn("starting_fcf", result["reason"])

    def test_missing_shares_is_missing(self):
        result = iv.dcf_value(1e9, 0.10, 0.11, 0.025, 0, None, 10)
        self.assertFalse(result["present"])
        self.assertIn("shares_outstanding", result["reason"])

    def test_net_cash_increases_value(self):
        # Same FCF, one company with net debt, one with net cash:
        # the net-cash company must be worth more per share.
        indebted = iv.dcf_value(1e9, 0.05, 0.11, 0.025, 5e9, 1e9, 10)
        cash_rich = iv.dcf_value(1e9, 0.05, 0.11, 0.025, -5e9, 1e9, 10)
        self.assertGreater(cash_rich["value"], indebted["value"])
        self.assertAlmostEqual(
            cash_rich["value"] - indebted["value"], 10.0, places=6)

    def test_higher_wacc_lowers_value(self):
        low = iv.dcf_value(1e9, 0.05, 0.10, 0.025, 0, 1e9, 10)
        high = iv.dcf_value(1e9, 0.05, 0.12, 0.025, 0, 1e9, 10)
        self.assertGreater(low["value"], high["value"])


class ScenarioTests(unittest.TestCase):

    def test_scenario_ordering(self):
        scenarios = iv.dcf_scenarios(1e9, 0, 1e9)
        self.assertLess(scenarios["bear"]["value"],
                        scenarios["base"]["value"])
        self.assertLess(scenarios["base"]["value"],
                        scenarios["optimistic"]["value"])

    def test_scenario_growth_rates_are_the_documented_anchors(self):
        self.assertEqual(iv.SCENARIO_GROWTH,
                         {"bear": 0.03, "base": 0.10, "optimistic": 0.15})

    def test_wacc_sensitivity_is_monotonic(self):
        sens = iv.wacc_sensitivity(1e9, 0.10, 0, 1e9)
        values = [sens[w]["value"] for w in (0.10, 0.11, 0.12)]
        self.assertGreater(values[0], values[1])
        self.assertGreater(values[1], values[2])

    def test_implied_growth_round_trips(self):
        # A price set exactly at the base DCF value must imply the
        # base growth rate.
        base = iv.dcf_value(1e9, 0.10, 0.11, 0.025, 0, 1e9, 10)
        implied = iv.implied_growth(base["value"], 1e9, 0.11, 0.025,
                                    0, 1e9, 10)
        self.assertIsNotNone(implied)
        self.assertAlmostEqual(implied, 0.10, places=4)

    def test_implied_growth_rises_with_price(self):
        low = iv.implied_growth(50, 1e9, 0.11, 0.025, 0, 1e9, 10)
        high = iv.implied_growth(200, 1e9, 0.11, 0.025, 0, 1e9, 10)
        self.assertLess(low, high)

    def test_implied_growth_none_for_absurd_price(self):
        # A price no growth rate in [-90%, 200%] can produce.
        self.assertIsNone(
            iv.implied_growth(1e9, 1e9, 0.11, 0.025, 0, 1e9, 10))
        self.assertIsNone(iv.implied_growth(None, 1e9, 0.11, 0.025,
                                            0, 1e9, 10))
        self.assertIsNone(iv.implied_growth(100, None, 0.11, 0.025,
                                            0, 1e9, 10))


class UnavailableMethodTests(unittest.TestCase):

    def test_owner_earnings_is_missing_with_exact_reasons(self):
        result = iv.owner_earnings_valuation()
        self.assertFalse(result["present"])
        self.assertIn("depreciation", result["reason"].lower())
        self.assertIn("maintenance", result["reason"].lower())

    def test_ev_ebitda_is_missing_with_reason(self):
        result = iv.ev_ebitda_valuation()
        self.assertFalse(result["present"])
        self.assertIn("EBITDA", result["reason"])

    def test_historical_pe_missing_without_price_history(self):
        eps = {"2024-06-30": 11.86, "2025-06-30": 13.64,
               "2026-06-30": 17.95}
        result = iv.historical_pe_valuation(eps, {})
        self.assertFalse(result["present"])
        self.assertIn("historical price", result["reason"])

    def test_historical_pe_with_supplied_history(self):
        # Median P/E of 20, 25, 30 is 25; latest EPS 10 -> value 250.
        eps = {"2024": 10.0, "2025": 10.0, "2026": 10.0}
        prices = {"2024": 200.0, "2025": 250.0, "2026": 300.0}
        result = iv.historical_pe_valuation(eps, prices,
                                            period="2026",
                                            period_type="annual")
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 250.0, places=6)
        self.assertAlmostEqual(result["median_pe"], 25.0, places=6)

    def test_historical_pe_skips_non_positive_eps(self):
        eps = {"2024": -5.0, "2025": 10.0, "2026": 10.0}
        prices = {"2024": 100.0, "2025": 200.0, "2026": 300.0}
        result = iv.historical_pe_valuation(eps, prices)
        self.assertTrue(result["present"])
        # Usable P/Es are 20 and 30; median 25; latest EPS 10.
        self.assertAlmostEqual(result["value"], 250.0, places=6)


class MsftValuationTests(unittest.TestCase):
    """MSFT on a temp copy of the real DB, hand-checked inputs."""

    def setUp(self):
        self.db_path = temp_db_copy()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_inputs_match_the_db(self):
        inputs, _ = iv.valuation_inputs(self.conn, "MSFT")
        self.assertEqual(inputs["ttm_free_cash_flow"], 59_957_000_000)
        self.assertEqual(inputs["annual_free_cash_flow"], 66_987_000_000)
        self.assertEqual(inputs["net_debt"], 15_200_000_000)
        self.assertEqual(inputs["shares_outstanding_diluted"],
                         7_446_000_000)
        self.assertEqual(inputs["price_input"]["price"], 507.12)
        self.assertEqual(inputs["price_input"]["as_of_date"], "2026-11-03")
        self.assertIsNone(inputs["valuation_profile"])

    def test_msft_dcf_scenarios(self):
        summary = iv.valuation_summary(self.conn, "MSFT")
        scenarios = summary["dcf_scenarios"]
        self.assertAlmostEqual(scenarios["bear"]["value"], 98.52,
                               places=1)
        self.assertAlmostEqual(scenarios["base"]["value"], 163.30,
                               places=1)
        self.assertAlmostEqual(scenarios["optimistic"]["value"], 234.64,
                               places=1)
        annual = summary["dcf_scenarios_annual_fcf"]
        self.assertAlmostEqual(annual["base"]["value"], 182.68, places=1)

    def test_msft_implied_growth_is_high(self):
        # At $507.12 on TTM FCF the price implies ~26% FCF growth a
        # year for 10 years — the capex-depressed TTM starting point
        # is doing heavy lifting.  Annual-FCF basis is ~24%.
        summary = iv.valuation_summary(self.conn, "MSFT")
        self.assertAlmostEqual(summary["implied_growth_at_price"], 0.258,
                               places=2)
        annual_implied = iv.implied_growth(
            507.12, 66_987_000_000, 0.11, 0.025, 15_200_000_000,
            7_446_000_000)
        self.assertAlmostEqual(annual_implied, 0.242, places=2)

    def test_msft_unavailable_methods_stay_missing(self):
        summary = iv.valuation_summary(self.conn, "MSFT")
        self.assertFalse(summary["owner_earnings"]["present"])
        self.assertFalse(summary["ev_ebitda"]["present"])
        self.assertFalse(summary["historical_pe"]["present"])

    def test_msft_fair_value_range_and_margin_of_safety(self):
        summary = iv.valuation_summary(self.conn, "MSFT")
        rng = summary["fair_value_range"]
        self.assertLess(rng["conservative"], rng["base"])
        self.assertLess(rng["base"], rng["optimistic"])
        # Price $507.12 is far above the base value: margin of
        # safety vs base is deeply negative.  A number only — no call.
        self.assertAlmostEqual(summary["margin_of_safety_vs_base"],
                               -0.678, places=2)
        self.assertEqual(summary["price_as_of"], "2026-11-03")

    def test_saved_profile_overrides_base_assumptions(self):
        # A profile row in the temp DB must drive the base scenario.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS valuation_profiles ("
            "ticker TEXT PRIMARY KEY, growth_rate REAL NOT NULL, "
            "discount_rate REAL NOT NULL, terminal_growth_rate REAL "
            "NOT NULL, margin_of_safety REAL NOT NULL, "
            "forecast_years INTEGER NOT NULL, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        self.conn.execute(
            "INSERT INTO valuation_profiles VALUES "
            "('MSFT', 0.05, 0.11, 0.025, 0.25, 10, CURRENT_TIMESTAMP)")
        self.conn.commit()
        summary = iv.valuation_summary(self.conn, "MSFT")
        self.assertEqual(summary["assumptions"]["assumption_source"],
                         "saved valuation_profiles row")
        self.assertAlmostEqual(
            summary["assumptions"]["base_growth_rate"], 0.05)
        # Base at 5% growth on TTM FCF is below the 10% default base.
        self.assertLess(summary["dcf_scenarios"]["base"]["value"], 163.30)

    def test_unknown_ticker_returns_missing_everything(self):
        summary = iv.valuation_summary(self.conn, "ZZZZ")
        self.assertIsNone(summary["starting_fcf"])
        self.assertFalse(
            summary["dcf_scenarios"]["base"]["present"])
        self.assertIsNone(summary["fair_value_range"]["base"])


if __name__ == "__main__":
    unittest.main()
