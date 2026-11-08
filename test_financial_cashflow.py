"""Step 20 tests — cash flow and owner earnings analysis.

Fixture rows only, plus a temporary copy of the real database for
the MSFT hand-checked values.  No network requests, no production
database writes.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import financial_cashflow as fc

PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_DB = PROJECT_ROOT / "database" / "assistant.db"


def _value_dict(value):
    return {"present": value is not None, "value": value}


def _values_for(concepts_by_period, period_type="annual"):
    """Fake ``load_standardised_values`` output.

    ``concepts_by_period``: {period: {concept: value|None}}.
    """
    from agents import financial_standard as fs

    values = {}
    for period, concepts in concepts_by_period.items():
        for concept, value in concepts.items():
            statement = fs.CONCEPT_STATEMENT.get(
                concept, "income_statement")
            row = values.setdefault(
                (statement, period, period_type),
                {"_source_id": 1, "_warnings": [], "_currency": "USD"})
            row[concept] = _value_dict(value)
    return values


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(PRODUCTION_DB, tmp.name)
    return tmp.name


# A small two-year fixture: OCF 100 -> 120, capex 20 -> 30, NI 80 -> 90,
# revenue 500 -> 600, cost of revenue 200 -> 240, dividends 10 -> 12,
# buybacks 5 -> 6, SBC 4 -> 5, CA 300 -> 320, CL 200 -> 210,
# AR 50 -> 60, AP 40 -> 48, inventory 10 -> 12.
FIXTURE = {
    "2025-06-30": {
        "revenue": 500.0, "cost_of_revenue": 200.0, "net_income": 80.0,
        "operating_income": 100.0, "operating_cash_flow": 100.0,
        "capital_expenditure": 20.0, "free_cash_flow": 80.0,
        "dividends_paid": 10.0, "share_repurchases": 5.0,
        "share_based_compensation": 4.0, "current_assets": 300.0,
        "current_liabilities": 200.0, "accounts_receivable": 50.0,
        "accounts_payable": 40.0, "inventory": 10.0,
    },
    "2026-06-30": {
        "revenue": 600.0, "cost_of_revenue": 240.0, "net_income": 90.0,
        "operating_income": 110.0, "operating_cash_flow": 120.0,
        "capital_expenditure": 30.0, "free_cash_flow": 90.0,
        "dividends_paid": 12.0, "share_repurchases": 6.0,
        "share_based_compensation": 5.0, "current_assets": 320.0,
        "current_liabilities": 210.0, "accounts_receivable": 60.0,
        "accounts_payable": 48.0, "inventory": 12.0,
    },
}


class FixtureAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.values = _values_for(FIXTURE)
        self.result = fc.assess_cashflow_from_values(
            fc._period_values(self.values, "2026-06-30", "annual"),
            fc._period_values(self.values, "2025-06-30", "annual"),
            self.values, "TEST", "2026-06-30", "annual")

    def test_ocf_quality(self):
        sec = self.result["sections"]["operating_cash_flow"]
        self.assertAlmostEqual(sec["ocf_to_net_income"]["value"], 120 / 90)
        self.assertAlmostEqual(
            sec["ocf_to_operating_income"]["value"], 120 / 110)

    def test_capex_ratios_and_yoy(self):
        sec = self.result["sections"]["capital_expenditure"]
        self.assertAlmostEqual(sec["capex_to_revenue"]["value"], 30 / 600)
        self.assertAlmostEqual(sec["capex_to_ocf"]["value"], 30 / 120)
        self.assertAlmostEqual(sec["capex_yoy"]["value"], 0.5)

    def test_fcf_after_dividends_and_buybacks(self):
        sec = self.result["sections"]["free_cash_flow"]
        self.assertAlmostEqual(sec["free_cash_flow"]["value"], 90.0)
        self.assertAlmostEqual(sec["fcf_after_dividends"]["value"], 78.0)
        self.assertAlmostEqual(
            sec["fcf_after_dividends_and_buybacks"]["value"], 72.0)
        self.assertAlmostEqual(sec["fcf_to_net_income"]["value"], 90 / 90)

    def test_sbc_adjusted_fcf(self):
        sec = self.result["sections"]["stock_based_compensation"]
        self.assertAlmostEqual(sec["sbc_to_ocf"]["value"], 5 / 120)
        self.assertAlmostEqual(sec["sbc_to_fcf"]["value"], 5 / 90)
        self.assertAlmostEqual(sec["sbc_adjusted_fcf"]["value"], 85.0)

    def test_working_capital_change(self):
        sec = self.result["sections"]["working_capital"]
        # WC 2026 = 320-210 = 110; WC 2025 = 300-200 = 100.
        self.assertAlmostEqual(sec["working_capital"]["value"], 110.0)
        self.assertAlmostEqual(
            sec["change_in_working_capital"]["value"], 10.0)
        # WC requirement = 10 / (600-500) = 0.10.
        self.assertAlmostEqual(
            sec["working_capital_requirement"]["value"], 0.10)

    def test_cash_conversion_cycle(self):
        sec = self.result["sections"]["working_capital"]
        ccc = sec["cash_conversion_cycle"]
        dio = 12 / 240 * 365
        dso = 60 / 600 * 365
        dpo = 48 / 240 * 365
        self.assertAlmostEqual(ccc["value"], dio + dso - dpo)
        self.assertAlmostEqual(
            ccc["days_inventory_outstanding"], dio)
        self.assertAlmostEqual(ccc["days_sales_outstanding"], dso)
        self.assertAlmostEqual(ccc["days_payable_outstanding"], dpo)


class OwnerEarningsTests(unittest.TestCase):
    def test_owner_earnings_unavailable_with_exact_reasons(self):
        values = _values_for(FIXTURE)
        result = fc.assess_cashflow_from_values(
            fc._period_values(values, "2026-06-30", "annual"),
            fc._period_values(values, "2025-06-30", "annual"),
            values, "TEST", "2026-06-30", "annual")
        oe = result["sections"]["owner_earnings"]["owner_earnings"]
        self.assertFalse(oe["present"])
        self.assertIsNone(oe["value"])
        self.assertIn("depreciation", oe["reason"])
        self.assertIn("maintenance capex", oe["reason"])
        self.assertIn("net income", oe["formula"])

    def test_fcf_is_not_labelled_owner_earnings(self):
        values = _values_for(FIXTURE)
        result = fc.assess_cashflow_from_values(
            fc._period_values(values, "2026-06-30", "annual"),
            fc._period_values(values, "2025-06-30", "annual"),
            values, "TEST", "2026-06-30", "annual")
        note = result["sections"]["owner_earnings"]["owner_earnings_note"]
        self.assertIn("NOT owner earnings", note)


class MissingInputTests(unittest.TestCase):
    def test_missing_ocf_makes_ratios_missing(self):
        concepts = {p: dict(v) for p, v in FIXTURE.items()}
        concepts["2026-06-30"]["operating_cash_flow"] = None
        concepts["2026-06-30"]["free_cash_flow"] = None
        values = _values_for(concepts)
        result = fc.assess_cashflow_from_values(
            fc._period_values(values, "2026-06-30", "annual"),
            fc._period_values(values, "2025-06-30", "annual"),
            values, "TEST", "2026-06-30", "annual")
        sec = result["sections"]["operating_cash_flow"]
        self.assertFalse(sec["ocf_to_net_income"]["present"])
        self.assertIn("missing input", sec["ocf_to_net_income"]["reason"])
        self.assertFalse(
            result["sections"]["free_cash_flow"]["free_cash_flow"]["present"])

    def test_zero_net_income_is_missing_not_infinite(self):
        concepts = {p: dict(v) for p, v in FIXTURE.items()}
        concepts["2026-06-30"]["net_income"] = 0.0
        values = _values_for(concepts)
        result = fc.assess_cashflow_from_values(
            fc._period_values(values, "2026-06-30", "annual"),
            fc._period_values(values, "2025-06-30", "annual"),
            values, "TEST", "2026-06-30", "annual")
        ratio = result["sections"]["operating_cash_flow"]["ocf_to_net_income"]
        self.assertFalse(ratio["present"])
        self.assertIn("zero", ratio["reason"])

    def test_missing_ccc_component_is_missing_not_estimated(self):
        concepts = {p: dict(v) for p, v in FIXTURE.items()}
        concepts["2026-06-30"]["inventory"] = None
        values = _values_for(concepts)
        result = fc.assess_cashflow_from_values(
            fc._period_values(values, "2026-06-30", "annual"),
            fc._period_values(values, "2025-06-30", "annual"),
            values, "TEST", "2026-06-30", "annual")
        ccc = result["sections"]["working_capital"]["cash_conversion_cycle"]
        self.assertFalse(ccc["present"])
        self.assertIn("inventory", ccc["reason"])

    def test_no_prior_period_means_yoy_and_wc_change_missing(self):
        values = _values_for(
            {"2026-06-30": FIXTURE["2026-06-30"]})
        result = fc.assess_cashflow_from_values(
            fc._period_values(values, "2026-06-30", "annual"),
            None, values, "TEST", "2026-06-30", "annual")
        self.assertFalse(
            result["sections"]["capital_expenditure"]["capex_yoy"]["present"])
        self.assertFalse(
            result["sections"]["working_capital"]
            ["change_in_working_capital"]["present"])


class MsftRealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _temp_db_copy()
        cls.conn = sqlite3.connect(cls.db_path)
        cls.result = fc.assess_cashflow(
            cls.conn, "MSFT", period="2026-06-30", period_type="annual")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        Path(cls.db_path).unlink(missing_ok=True)

    def test_fy2026_levels(self):
        sec = self.result["sections"]
        self.assertAlmostEqual(
            sec["operating_cash_flow"]["operating_cash_flow"]["value"],
            182935000000)
        self.assertAlmostEqual(
            sec["capital_expenditure"]["capital_expenditure"]["value"],
            115948000000)
        self.assertAlmostEqual(
            sec["free_cash_flow"]["free_cash_flow"]["value"], 66987000000)
        self.assertAlmostEqual(
            sec["free_cash_flow"]["fcf_after_dividends"]["value"],
            40542000000)
        self.assertAlmostEqual(
            sec["free_cash_flow"]["fcf_after_dividends_and_buybacks"]["value"],
            18271000000)

    def test_fy2026_cash_conversion(self):
        sec = self.result["sections"]
        self.assertAlmostEqual(
            sec["operating_cash_flow"]["ocf_to_net_income"]["value"],
            182935 / 133749, places=4)
        self.assertAlmostEqual(
            sec["free_cash_flow"]["fcf_to_net_income"]["value"],
            66987 / 133749, places=4)
        self.assertAlmostEqual(
            sec["capital_expenditure"]["capex_to_revenue"]["value"],
            115948 / 331839, places=4)
        self.assertAlmostEqual(
            sec["capital_expenditure"]["capex_yoy"]["value"],
            115948 / 64551 - 1, places=4)

    def test_fy2026_sbc(self):
        sec = self.result["sections"]["stock_based_compensation"]
        self.assertAlmostEqual(sec["sbc_to_ocf"]["value"], 12405 / 182935,
                               places=4)
        self.assertAlmostEqual(sec["sbc_to_fcf"]["value"], 12405 / 66987,
                               places=4)
        self.assertAlmostEqual(sec["sbc_adjusted_fcf"]["value"], 54582000000)

    def test_fy2026_working_capital_and_ccc(self):
        sec = self.result["sections"]["working_capital"]
        self.assertAlmostEqual(sec["working_capital"]["value"], 38885000000)
        self.assertAlmostEqual(
            sec["change_in_working_capital"]["value"], -11028000000)
        # Negative CCC: Microsoft is paid by customers long before it
        # pays suppliers (DIO ~4.8d, DSO ~89.0d, DPO ~145.5d).
        ccc = sec["cash_conversion_cycle"]
        self.assertTrue(ccc["present"])
        self.assertLess(ccc["value"], 0)
        self.assertAlmostEqual(ccc["value"], -51.79, places=1)
        self.assertAlmostEqual(
            ccc["days_payable_outstanding"], 145.54, places=1)

    def test_fy2026_owner_earnings_unavailable(self):
        oe = self.result["sections"]["owner_earnings"]["owner_earnings"]
        self.assertFalse(oe["present"])
        self.assertIn("depreciation", oe["reason"])

    def test_capex_surge_verdict(self):
        surge = self.result["sections"]["capex_surge"]
        self.assertAlmostEqual(surge["5y"]["capex_cagr"], 0.4125, places=3)
        self.assertAlmostEqual(surge["5y"]["ocf_cagr"], 0.1897, places=3)
        self.assertAlmostEqual(surge["5y"]["fcf_cagr"], 0.0360, places=3)
        self.assertIn("capex surge", surge["verdict"])
        self.assertIn("not because OCF is weak", surge["verdict"])

    def test_conversion_trends(self):
        trends = self.result["sections"]["cash_conversion_trends"]
        self.assertAlmostEqual(
            trends["5y"]["mean_ocf_to_net_income"], 1.2969, places=3)
        self.assertAlmostEqual(
            trends["5y"]["mean_capex_to_revenue"], 0.2026, places=3)

    def test_ttm_assessment(self):
        result = fc.assess_cashflow(
            self.conn, "MSFT", period="2026-09-30", period_type="ttm")
        sec = result["sections"]
        self.assertAlmostEqual(
            sec["free_cash_flow"]["free_cash_flow"]["value"], 59957000000)
        self.assertIn("TTM built by", result["warnings"][0])
        self.assertFalse(
            sec["capital_expenditure"]["capex_yoy"]["present"])

    def test_all_periods_count(self):
        results = fc.assess_all_periods(self.conn, "MSFT")
        # 19 annual periods (FY2008-FY2026) + 1 TTM.
        self.assertEqual(len(results), 20)
        self.assertEqual(results[-1]["period_type"], "ttm")


if __name__ == "__main__":
    unittest.main()
