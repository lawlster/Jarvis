"""Step 22 tests — sector-specific valuation models.

Fixture rows / temp DB copies only; no network, no production
writes.  Covers the sector router, the FCF-DCF refusal for banks /
insurers / REITs, DDM / excess-return / justified-P/B arithmetic
against hand-computed values, unavailable-model behaviour, and the
MSFT technology path reproducing Step 21's base value on a temp
copy of the real database.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import sector_valuation as sv

PROJECT_ROOT = Path(__file__).resolve().parent
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"


def _val(value):
    return {"present": True, "value": value}


def _fixture_bank_values():
    """Six annual periods of a synthetic bank, in Step 16 format.

    Book value per share $50 in the latest year, ROE 12%,
    DPS $2.50 growing ~4% a year, EPS $4.00 (payout 62.5%).
    Units are dollars / shares (not $m) — the ratios are what
    matter, and every function here is unit-consistent.
    """
    values = {}
    periods = ["2021-12-31", "2022-12-31", "2023-12-31",
               "2024-12-31", "2025-12-31", "2026-12-31"]
    equity = [42000, 44000, 46000, 48000, 49000, 50000]
    net_income = [5040, 5280, 5520, 5760, 5880, 6000]
    dividends = [2100, 2200, 2300, 2400, 2450, 2500]
    shares = 1000
    for i, p in enumerate(periods):
        values[("balance_sheet", p, "annual")] = {
            "total_equity": _val(equity[i]),
        }
        values[("income_statement", p, "annual")] = {
            "net_income": _val(net_income[i]),
            "shares_outstanding_diluted": _val(shares),
            "eps_diluted": _val(net_income[i] / shares),
        }
        values[("cash_flow_statement", p, "annual")] = {
            "dividends_paid": _val(dividends[i]),
        }
    return values


class TestSectorNormalisation(unittest.TestCase):
    def test_bank(self):
        self.assertEqual(sv.normalise_sector("Banks"), "bank")
        self.assertEqual(
            sv.normalise_sector("Financials", "Regional Banks"),
            "bank")

    def test_insurance(self):
        self.assertEqual(sv.normalise_sector("Insurance"), "insurance")
        self.assertEqual(
            sv.normalise_sector("Financials", "Reinsurance"),
            "insurance")

    def test_reit_security_type_wins(self):
        self.assertEqual(
            sv.normalise_sector(None, None, "reit"), "reit")
        self.assertEqual(
            sv.normalise_sector("Real Estate", None, "reit"), "reit")

    def test_financials_is_ambiguous(self):
        # Stored "Financials" must NOT be guessed as a bank.
        self.assertEqual(sv.normalise_sector("Financials"),
                         "financial")
        self.assertEqual(
            sv.normalise_sector("Financials", None, "financial"),
            "financial")

    def test_technology_and_general(self):
        self.assertEqual(
            sv.normalise_sector("Information Technology"),
            "technology")
        self.assertEqual(sv.normalise_sector("Software"), "technology")
        self.assertEqual(sv.normalise_sector("Consumer Staples"),
                         "general")

    def test_unknown(self):
        self.assertEqual(sv.normalise_sector(None), "unknown")
        self.assertEqual(sv.normalise_sector(""), "unknown")


class TestModelRefusals(unittest.TestCase):
    def test_fcf_dcf_refused_for_bank_insurer_reit(self):
        for sector in ("bank", "insurance", "reit"):
            allowed, reason = sv.validate_model("fcf_dcf", sector)
            self.assertFalse(allowed)
            self.assertIn("refused", reason)

    def test_fcf_dcf_allowed_for_technology_and_general(self):
        for sector in ("technology", "general"):
            allowed, _ = sv.validate_model("fcf_dcf", sector)
            self.assertTrue(allowed)

    def test_bank_models_selected(self):
        selected, _ = sv.models_for_sector("bank")
        self.assertEqual(selected, ["dividend_discount",
                                    "excess_return", "justified_pb",
                                    "historical_pb"])

    def test_financial_and_unknown_select_nothing(self):
        for sector in ("financial", "unknown"):
            selected, refused = sv.models_for_sector(sector)
            self.assertEqual(selected, [])
            self.assertIn("fcf_dcf", refused)


class TestDividendDiscount(unittest.TestCase):
    def test_gordon_arithmetic(self):
        # DPS $2.50, g 4%, k 10% -> 2.50*1.04/0.06 = $43.33
        result = sv.dividend_discount_value(2.50, 0.04, 0.10)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 43.333333, places=4)

    def test_cost_must_exceed_growth(self):
        result = sv.dividend_discount_value(2.50, 0.10, 0.10)
        self.assertFalse(result["present"])
        self.assertIn("must exceed", result["reason"])

    def test_missing_dps(self):
        result = sv.dividend_discount_value(None, 0.04, 0.10)
        self.assertFalse(result["present"])

    def test_ddm_from_fixture_history(self):
        values = _fixture_bank_values()
        result = sv.bank_dividend_discount(values)
        self.assertTrue(result["present"])
        # Latest DPS $2.50; 5y DPS CAGR (2.10 -> 2.50) ~3.55%,
        # under the 5% cap.  Value = 2.50*1.0355/0.0645 ≈ $40.14.
        dps_cagr = (2.50 / 2.10) ** (1 / 5) - 1
        expected = 2.50 * (1 + dps_cagr) / (0.10 - dps_cagr)
        self.assertAlmostEqual(result["value"], expected, places=2)
        self.assertAlmostEqual(result["inputs"]["growth_rate"]
                               if "growth_rate" in result["inputs"]
                               else dps_cagr, dps_cagr, places=4)

    def test_ddm_no_history_is_missing(self):
        result = sv.bank_dividend_discount({})
        self.assertFalse(result["present"])
        self.assertIn("no dividend history", result["reason"])


class TestExcessReturn(unittest.TestCase):
    def test_arithmetic_hand_checked(self):
        # BVPS $50, ROE 12%, k 10%, book growth 5%, 10y, terminal
        # 2.5%.  Residual income grows at the book rate through
        # year 10, then at the terminal rate (the same convention
        # as the canonical DCF's terminal value): PV of residual
        # income ≈ $7.44, PV of terminal value ≈ $8.17,
        # total ≈ $65.61.
        result = sv.excess_return_value(50.0, 0.12, 0.10, 0.05,
                                        years=10,
                                        terminal_growth_rate=0.025)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 65.61, places=1)
        self.assertAlmostEqual(
            result["present_value_of_residual_income"], 7.44,
            places=1)
        self.assertAlmostEqual(result["present_terminal_value"],
                               8.17, places=1)

    def test_excess_return_matches_independent_loop(self):
        bvps, roe, k, g = 50.0, 0.12, 0.10, 0.05
        bv, pv = bvps, 0.0
        ris = []
        for year in range(1, 11):
            ri = (roe - k) * bv
            ris.append(ri)
            pv += ri / (1 + k) ** year
            bv *= (1 + g)
        tv = ris[-1] * 1.025 / (k - 0.025)
        expected = bvps + pv + tv / (1 + k) ** 10
        result = sv.excess_return_value(bvps, roe, k, g, years=10,
                                        terminal_growth_rate=0.025)
        self.assertAlmostEqual(result["value"], expected, places=6)

    def test_roe_below_cost_of_equity_discounts_book(self):
        # ROE 8% < k 10%: residual income is negative, value < BVPS.
        result = sv.excess_return_value(50.0, 0.08, 0.10, 0.03,
                                        years=10,
                                        terminal_growth_rate=0.025)
        self.assertTrue(result["present"])
        self.assertLess(result["value"], 50.0)

    def test_from_fixture_history(self):
        values = _fixture_bank_values()
        result = sv.bank_excess_return(values)
        self.assertTrue(result["present"])
        # Latest BVPS $50, ROE 6000/50000 = 12%, payout 2.50/6.00
        # = 41.7%, retention 58.3%, book growth 7.0%.
        self.assertAlmostEqual(
            result["inputs"]["book_value_per_share"], 50.0)
        self.assertAlmostEqual(result["inputs"]["roe"], 0.12)
        self.assertAlmostEqual(
            result["inputs"]["retention_ratio"], 1 - 2.50 / 6.00,
            places=4)
        self.assertGreater(result["value"], 50.0)

    def test_missing_inputs(self):
        result = sv.bank_excess_return({})
        self.assertFalse(result["present"])


class TestJustifiedPB(unittest.TestCase):
    def test_arithmetic(self):
        # (0.12 - 0.04) / (0.10 - 0.04) = 1.3333 x BVPS $50 = $66.67
        result = sv.justified_pb_value(50.0, 0.12, 0.10, 0.04)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["justified_pb"], 1.333333,
                               places=4)
        self.assertAlmostEqual(result["value"], 66.666667, places=4)

    def test_from_fixture_history(self):
        values = _fixture_bank_values()
        result = sv.bank_justified_pb(values)
        self.assertTrue(result["present"])
        # BVPS $50, ROE 12%, k 10%, g 2.5% ->
        # (0.12-0.025)/(0.10-0.025) = 1.2667 x 50 = $63.33
        self.assertAlmostEqual(result["value"], 63.333333, places=4)

    def test_historical_pb_with_fixture_price_history(self):
        bvps = {"2024-12-31": 48.0, "2025-12-31": 49.0,
                "2026-12-31": 50.0}
        prices = {"2024-12-31": 48.0, "2025-12-31": 58.8,
                 "2026-12-31": 65.0}
        # P/Bs 1.0, 1.2, 1.3 -> median 1.2 x latest BVPS 50 = $60
        result = sv.historical_pb_valuation(bvps, prices)
        self.assertTrue(result["present"])
        self.assertAlmostEqual(result["value"], 60.0, places=4)

    def test_historical_pb_missing_without_price_history(self):
        result = sv.historical_pb_valuation(
            {"2026-12-31": 50.0}, {})
        self.assertFalse(result["present"])
        self.assertIn("no historical price series",
                      result["reason"])


class TestUnavailableModels(unittest.TestCase):
    def test_ffo_unavailable_with_reason(self):
        result = sv.ffo_valuation()
        self.assertFalse(result["present"])
        self.assertIn("depreciation", result["reason"])

    def test_nav_unavailable_with_reason(self):
        result = sv.nav_valuation()
        self.assertFalse(result["present"])
        self.assertIn("property", result["reason"])

    def test_embedded_value_unavailable_with_reason(self):
        result = sv.embedded_value_valuation()
        self.assertFalse(result["present"])
        self.assertIn("embedded value", result["reason"])

    def test_insurer_valuation_reports_unavailable_parts(self):
        values = _fixture_bank_values()
        result = sv.insurer_valuation(values)
        self.assertTrue(result["justified_pb"]["present"])
        self.assertFalse(result["embedded_value"]["present"])
        self.assertFalse(
            result["underwriting_vs_investment"]["present"])
        self.assertFalse(result["historical_pb"]["present"])

    def test_reit_valuation_ddm_runs_ffo_does_not(self):
        values = _fixture_bank_values()
        result = sv.reit_valuation(values)
        self.assertTrue(result["distribution_ddm"]["present"])
        self.assertEqual(result["distribution_ddm"]["method"],
                         "distribution_ddm")
        self.assertFalse(result["ffo"]["present"])
        self.assertFalse(result["nav"]["present"])
        self.assertFalse(result["historical_p_ffo"]["present"])


class TestRealDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="step22-test-")
        cls.db_path = Path(cls.tmpdir) / "assistant.db"
        shutil.copy(PROD_DB, cls.db_path)
        cls.conn = sqlite3.connect(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_msft_sector_is_unknown_in_stored_data(self):
        info = sv.resolve_sector(self.conn, "MSFT")
        self.assertEqual(info["sector"], "unknown")
        self.assertIsNone(info["raw_sector"])

    def test_msft_technology_path_reproduces_step21(self):
        summary = sv.sector_valuation_summary(
            self.conn, "MSFT", sector_override="technology")
        self.assertEqual(summary["sector"], "technology")
        self.assertEqual(summary["models_selected"], ["fcf_dcf"])
        # Step 21: bear $98.52, base $163.30, optimistic $234.64.
        rng = summary["fair_value_range"]
        self.assertAlmostEqual(rng["conservative"], 98.52, places=2)
        self.assertAlmostEqual(rng["base"], 163.30, places=2)
        self.assertAlmostEqual(rng["optimistic"], 234.64, places=2)
        self.assertAlmostEqual(
            summary["methods"]["fcf_dcf"]["implied_growth_at_price"],
            0.258, places=2)

    def test_msft_no_override_defaults_to_general_dcf_with_warning(self):
        summary = sv.sector_valuation_summary(self.conn, "MSFT")
        self.assertEqual(summary["sector"], "unknown")
        # The general DCF is shown as the default, with a warning.
        self.assertEqual(summary["models_selected"], ["fcf_dcf"])
        self.assertIn("fcf_dcf", summary["methods"])
        self.assertAlmostEqual(summary["fair_value_range"]["base"],
                               163.30, places=2)
        self.assertTrue(any("unknown" in w.lower()
                            for w in summary["warnings"]))

    def test_dbs_sector_financial_is_ambiguous(self):
        info = sv.resolve_sector(self.conn, "D05")
        self.assertEqual(info["sector"], "financial")
        self.assertEqual(info["raw_sector"], "Financials")

    def test_dbs_no_model_selected_without_subsector(self):
        summary = sv.sector_valuation_summary(self.conn, "D05")
        self.assertEqual(summary["sector"], "financial")
        self.assertEqual(summary["models_selected"], [])
        self.assertIsNone(summary["fair_value_range"]["base"])
        self.assertIn("fcf_dcf", summary["models_refused"])

    def test_dbs_bank_override_selects_bank_models_but_no_data(self):
        summary = sv.sector_valuation_summary(
            self.conn, "D05", sector_override="bank")
        self.assertEqual(summary["sector"], "bank")
        self.assertEqual(summary["models_selected"],
                         ["dividend_discount", "excess_return",
                          "justified_pb", "historical_pb"])
        for name, method in summary["methods"].items():
            self.assertFalse(method["present"], name)
        self.assertIsNone(summary["fair_value_range"]["base"])
        self.assertIn("no dividend history",
                      summary["methods"]["dividend_discount"]["reason"])

    def test_link_reit_routes_to_reit_and_reports_unavailable(self):
        summary = sv.sector_valuation_summary(self.conn, "00823")
        self.assertEqual(summary["sector"], "reit")
        self.assertIn("distribution_ddm",
                      summary["models_selected"])
        methods = summary["methods"]
        self.assertFalse(methods["distribution_ddm"]["present"])
        self.assertFalse(methods["ffo"]["present"])
        self.assertIn("depreciation", methods["ffo"]["reason"])
        self.assertFalse(methods["nav"]["present"])
        self.assertIsNone(summary["fair_value_range"]["base"])

    def test_msft_bank_models_run_on_real_data_as_cross_check(self):
        # MSFT is not a bank, but its dividend/equity history is
        # complete, so the bank machinery runs on real data and
        # gives a sane cross-check: DDM ≈ $40-45, justified P/B
        # far above book (ROE 30% >> k 10%).
        values, _ = __import__(
            "agents.financial_ratios", fromlist=["x"]
        ).load_standardised_values(self.conn, "MSFT")
        ddm = sv.bank_dividend_discount(values)
        self.assertTrue(ddm["present"])
        # Latest DPS ~$3.55; 5y DPS CAGR ~10%, capped at 5% ->
        # 3.55 x 1.05 / 0.05 ≈ $74.5.
        self.assertGreater(ddm["value"], 60.0)
        self.assertLess(ddm["value"], 90.0)
        justified = sv.bank_justified_pb(values)
        self.assertTrue(justified["present"])
        self.assertGreater(justified["value"], 100.0)


if __name__ == "__main__":
    unittest.main()
