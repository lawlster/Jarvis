"""Tests for Step 24 — agents/business_quality.py.

Fixture rows only, plus temporary database copies for the real-data
tests.  No network requests, no production database writes.

Hand-checked expectations use the MSFT figures verified against the
real database (Step 24 probe; all ratios from Step 17 annual
ratios): 5y ROIC mean 33.86% (min 30.59%, CV 0.075), 10y ROIC mean
28.34%; 5y gross margin mean 68.77% (CV 0.010), net margin mean
36.65% (CV 0.062); revenue steady, 10/10 years growing, max
drawdown 3.3%; 5y mean OCF/net income 1.30, FCF/net income 0.75,
10/10 profitable years; dividends grew every year 2008-2026
(19 years, no cuts); shares outstanding -2.0% over 5y; incremental
ROIC 5y 23.5%, 10y 30.2%; cash conversion cycle -51.8 days; Step 18
health verdict strong.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import business_quality as bq
from agents import margin_of_safety as mos

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "assistant.db"

PRICE = {"price": 507.12, "market_cap": 3765971714048,
         "as_of_date": "2026-11-03",
         "source": "financial_snapshots (latest row)"}

PERIODS_5Y = ["2021-06-30", "2022-06-30", "2023-06-30",
              "2024-06-30", "2025-06-30", "2026-06-30"]

INCOME_CONCEPTS = (
    "revenue", "cost_of_revenue", "gross_profit",
    "research_and_development", "selling_general_administrative",
    "operating_income", "income_before_tax", "income_tax",
    "net_income", "eps_basic", "eps_diluted",
    "shares_outstanding_basic", "shares_outstanding_diluted",
)
BALANCE_CONCEPTS = (
    "total_assets", "total_liabilities", "total_equity",
    "cash_and_equivalents", "short_term_debt", "long_term_debt",
    "current_assets", "current_liabilities", "inventory",
    "accounts_receivable", "accounts_payable",
    "short_term_investments",
)
CASHFLOW_CONCEPTS = (
    "operating_cash_flow", "capital_expenditure", "dividends_paid",
    "share_repurchases", "share_based_compensation",
    "investing_cash_flow", "financing_cash_flow",
)

# A weak business: low/negative returns, volatile margins, losses,
# dividend cuts, diluting shares, cash tied up in working capital.
WEAK_SPEC = {
    "revenue": [1050, 1000, 900, 1100, 950, 800],
    "cost_of_revenue": [720, 700, 650, 800, 720, 640],
    "gross_profit": [330, 300, 250, 300, 230, 160],
    "research_and_development": [50, 50, 50, 55, 50, 45],
    "selling_general_administrative": [180, 170, 220, 185, 170, 165],
    "operating_income": [100, 80, -20, 60, 10, -50],
    "income_before_tax": [80, 60, -35, 50, 8, -70],
    "income_tax": [15, 10, -5, 10, 3, -10],
    "net_income": [65, 50, -30, 40, 5, -60],
    "eps_basic": [0.65, 0.50, -0.30, 0.40, 0.05, -0.60],
    "eps_diluted": [0.65, 0.50, -0.30, 0.40, 0.05, -0.60],
    "shares_outstanding_basic": [98, 100, 102, 104, 106, 108],
    "shares_outstanding_diluted": [98, 100, 102, 104, 106, 108],
    "total_assets": [2050, 2000, 1900, 2100, 2000, 1800],
    "total_liabilities": [1250, 1200, 1150, 1320, 1240, 1150],
    "total_equity": [800, 800, 750, 780, 760, 650],
    "cash_and_equivalents": [110, 100, 80, 90, 70, 50],
    "short_term_debt": [200, 200, 200, 200, 200, 200],
    "long_term_debt": [600, 600, 600, 600, 600, 600],
    "current_assets": [620, 600, 550, 620, 580, 500],
    "current_liabilities": [510, 500, 480, 520, 500, 480],
    "inventory": [210, 200, 190, 210, 200, 180],
    "accounts_receivable": [155, 150, 140, 160, 150, 130],
    "accounts_payable": [185, 180, 170, 190, 180, 160],
    "short_term_investments": [55, 50, 40, 50, 40, 30],
    "operating_cash_flow": [100, 90, 10, 70, 20, -10],
    "capital_expenditure": [55, 60, 50, 80, 40, 30],
    "dividends_paid": [20, 20, 20, 0, 10, 0],
    "share_repurchases": [0, 0, 0, 0, 0, 0],
    "share_based_compensation": [5, 5, 5, 5, 5, 5],
    "investing_cash_flow": [-55, -60, -50, -80, -40, -30],
    "financing_cash_flow": [-20, -20, -20, 0, -10, 0],
}

# A solid but unremarkable business: ~14% ROIC, ~12% net margin,
# steady 5% revenue growth, growing dividends, flat share count.
MID_SPEC = {
    "revenue": [950, 1000, 1050, 1100, 1160, 1220],
    "cost_of_revenue": [615, 650, 680, 715, 750, 790],
    "gross_profit": [335, 350, 370, 385, 410, 430],
    "research_and_development": [38, 40, 42, 44, 46, 48],
    "selling_general_administrative": [145, 150, 155, 160, 168, 175],
    "operating_income": [152, 160, 173, 181, 196, 207],
    "income_before_tax": [142, 150, 162, 170, 184, 194],
    "income_tax": [28, 30, 32, 34, 37, 39],
    "net_income": [114, 120, 130, 136, 147, 155],
    "eps_basic": [1.14, 1.20, 1.30, 1.36, 1.47, 1.55],
    "eps_diluted": [1.14, 1.20, 1.30, 1.36, 1.47, 1.55],
    "shares_outstanding_basic": [100, 100, 100, 100, 100, 100],
    "shares_outstanding_diluted": [100, 100, 100, 100, 100, 100],
    "total_assets": [1950, 2000, 2050, 2100, 2180, 2250],
    "total_liabilities": [1180, 1200, 1220, 1240, 1280, 1300],
    "total_equity": [770, 800, 830, 860, 900, 950],
    "cash_and_equivalents": [90, 100, 110, 120, 130, 140],
    "short_term_debt": [100, 100, 100, 100, 100, 100],
    "long_term_debt": [200, 200, 200, 200, 200, 200],
    "current_assets": [630, 650, 670, 690, 710, 730],
    "current_liabilities": [490, 500, 510, 520, 530, 540],
    "inventory": [115, 120, 125, 130, 135, 140],
    "accounts_receivable": [125, 130, 135, 140, 145, 150],
    "accounts_payable": [105, 110, 115, 120, 125, 130],
    "short_term_investments": [45, 50, 55, 60, 65, 70],
    "operating_cash_flow": [142, 150, 160, 170, 180, 190],
    "capital_expenditure": [58, 60, 62, 65, 68, 70],
    "dividends_paid": [36, 40, 44, 48, 52, 56],
    "share_repurchases": [10, 10, 10, 10, 10, 10],
    "share_based_compensation": [5, 5, 5, 5, 5, 5],
    "investing_cash_flow": [-58, -60, -62, -65, -68, -70],
    "financing_cash_flow": [-46, -50, -54, -58, -62, -66],
}


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _entry(value):
    return {"value": value, "unit": "USD", "tag": "Test",
            "filed": "2026-07-29", "form": "10-K"}


def _fixture_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE financial_statements ("
        "id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
        "exchange TEXT, country TEXT, statement_type TEXT, "
        "reporting_period TEXT, period_type TEXT, fiscal_year INTEGER, "
        "fiscal_quarter INTEGER, currency TEXT, source TEXT, "
        "source_document TEXT, data_json TEXT, collected_at TEXT)")
    conn.execute(
        "CREATE TABLE financial_snapshots (id INTEGER PRIMARY KEY, "
        "ticker TEXT, snapshot_date TEXT, financial_data TEXT)")
    for row in rows:
        conn.execute(
            "INSERT INTO financial_statements "
            "(ticker, company_name, exchange, country, "
            "statement_type, reporting_period, period_type, "
            "fiscal_year, fiscal_quarter, currency, source, "
            "source_document, data_json, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["ticker"], row["company_name"], row["exchange"],
             row["country"], row["statement_type"],
             row["reporting_period"], row["period_type"],
             row["fiscal_year"], row["fiscal_quarter"],
             row["currency"], row["source"], row["source_document"],
             row["data_json"], row["collected_at"]))
    return conn


# Step 16 US raw keys that differ from the concept name
# (agents/financial_standard.py US_KEY_MAP).
RAW_KEYS = {
    "eps_basic": "basic_eps",
    "eps_diluted": "diluted_eps",
    "shares_outstanding_basic": "basic_shares",
    "shares_outstanding_diluted": "diluted_shares",
    "total_equity": "shareholders_equity",
}


def _company_rows(ticker, spec, periods=PERIODS_5Y):
    """One income / balance / cash-flow row per period from a spec."""
    import json
    rows = []
    for i, period in enumerate(periods):
        for stmt, concepts in (
                ("income_statement", INCOME_CONCEPTS),
                ("balance_sheet", BALANCE_CONCEPTS),
                ("cash_flow_statement", CASHFLOW_CONCEPTS)):
            data = {RAW_KEYS.get(c, c): _entry(spec[c][i])
                    for c in concepts if c in spec}
            rows.append({
                "ticker": ticker, "company_name": ticker,
                "exchange": "NASDAQ", "country": "USA",
                "statement_type": stmt, "reporting_period": period,
                "period_type": "annual", "fiscal_year": None,
                "fiscal_quarter": None, "currency": "USD",
                "source": "SEC EDGAR Company Facts",
                "source_document": "10-K",
                "data_json": json.dumps(data),
                "collected_at": "2026-11-03",
            })
    return rows


def _no_deterioration():
    return {"deteriorated": False, "unknown": False, "signals": [],
            "cautions": [], "rule": "test", "source": "test"}


class TestBusinessQualityFixtures(unittest.TestCase):
    """Component scoring on synthetic weak and mid businesses."""

    def test_weak_business_scores_weak(self):
        conn = _fixture_conn(_company_rows("WEAK", WEAK_SPEC))
        result = bq.assess_business_quality(conn, "WEAK")
        self.assertEqual(result["rating"], "weak")
        comps = result["components"]
        self.assertEqual(comps["returns_on_capital"]["score"], 0)
        self.assertEqual(comps["margin_stability"]["score"], 0)
        self.assertEqual(comps["growth_consistency"]["score"], 0)
        self.assertEqual(comps["earnings_quality"]["score"], 0)
        self.assertEqual(comps["balance_sheet_strength"]["score"], 0)
        self.assertEqual(comps["capital_allocation"]["score"], 0)
        # ROIC 5y mean is ~0.8%: at or below the 11% cost of capital.
        self.assertAlmostEqual(
            comps["returns_on_capital"]["inputs"]["roic_5y_mean"],
            0.008, places=2)
        # Two dividend cuts in the 5y window.
        self.assertEqual(
            comps["capital_allocation"]["inputs"]["dividend_cuts_10y"], 2)
        # Incremental ROIC is missing, not nonsense: invested
        # capital shrank over the window.
        self.assertIsNone(
            comps["capital_allocation"]["inputs"]["incremental_roic_5y"])
        self.assertIn(
            "did not grow",
            comps["capital_allocation"]["inputs"]
            ["incremental_roic_5y_detail"])
        # Moat evidence is weak and qualified, not a moat claim.
        self.assertEqual(
            result["moat_evidence"]["assessment"],
            "weak financial evidence of a durable advantage")
        self.assertIn("caveat", result["moat_evidence"])
        conn.close()

    def test_mid_business_scores_good(self):
        conn = _fixture_conn(_company_rows("MID", MID_SPEC))
        result = bq.assess_business_quality(conn, "MID")
        self.assertEqual(result["rating"], "good")
        comps = result["components"]
        # ROIC 5y mean ~14.1%: adequate, above the 11% hurdle.
        self.assertEqual(comps["returns_on_capital"]["score"], 1)
        self.assertAlmostEqual(
            comps["returns_on_capital"]["inputs"]["roic_5y_mean"],
            0.141, places=2)
        self.assertEqual(comps["margin_stability"]["score"], 1)
        self.assertEqual(comps["growth_consistency"]["score"], 2)
        self.assertEqual(comps["earnings_quality"]["score"], 2)
        self.assertEqual(comps["balance_sheet_strength"]["score"], 2)
        self.assertEqual(comps["capital_allocation"]["score"], 1)
        # Dividends grew every year, no cuts; incremental ROIC 5y
        # is high (34%) but the share count is flat, so not excellent.
        self.assertEqual(
            comps["capital_allocation"]["inputs"]["dividend_cuts_10y"], 0)
        self.assertAlmostEqual(
            comps["capital_allocation"]["inputs"]["incremental_roic_5y"],
            0.33, places=2)
        self.assertAlmostEqual(result["mean_score"], 10 / 7, places=2)
        conn.close()

    def test_insufficient_data_few_periods(self):
        conn = _fixture_conn(
            _company_rows("SHORT", MID_SPEC, periods=PERIODS_5Y[:2]))
        result = bq.assess_business_quality(conn, "SHORT")
        self.assertEqual(result["rating"], "insufficient data")
        self.assertIsNone(result["mean_score"])
        conn.close()

    def test_insufficient_data_no_rows(self):
        conn = _fixture_conn([])
        result = bq.assess_business_quality(conn, "NONE")
        self.assertEqual(result["rating"], "insufficient data")
        self.assertEqual(result["components"], {})
        conn.close()

    def test_not_assessable_listed(self):
        conn = _fixture_conn(_company_rows("MID", MID_SPEC))
        result = bq.assess_business_quality(conn, "MID")
        items = [n["item"] for n in result["not_assessable"]]
        for expected in ("brand strength", "network effects",
                         "switching costs", "regulatory moats",
                         "management integrity and skill",
                         "competitive position vs peers",
                         "customer concentration"):
            self.assertIn(expected, items)
        conn.close()


class TestBusinessQualityMSFT(unittest.TestCase):
    """MSFT on a temp copy of the real database (hand-checked)."""

    def test_msft_is_excellent(self):
        path = _temp_db_copy()
        conn = sqlite3.connect(path)
        try:
            result = bq.assess_business_quality(conn, "MSFT")
        finally:
            conn.close()
        self.assertEqual(result["rating"], "excellent")
        self.assertAlmostEqual(result["mean_score"], 2.0, places=2)
        comps = result["components"]
        for name, comp in comps.items():
            self.assertEqual(comp["score"], 2, name)
        self.assertEqual(result["components_scored"], 7)

    def test_msft_component_values(self):
        path = _temp_db_copy()
        conn = sqlite3.connect(path)
        try:
            result = bq.assess_business_quality(conn, "MSFT")
        finally:
            conn.close()
        returns = result["components"]["returns_on_capital"]["inputs"]
        self.assertAlmostEqual(returns["roic_5y_mean"], 0.3386, places=3)
        self.assertAlmostEqual(returns["roic_5y_min"], 0.3059, places=3)
        self.assertAlmostEqual(returns["roic_10y_mean"], 0.2834, places=3)
        self.assertAlmostEqual(returns["roe_5y_mean"], 0.3809, places=3)
        margins = result["components"]["margin_stability"]["inputs"]
        self.assertAlmostEqual(
            margins["gross_margin"]["mean"], 0.6877, places=3)
        self.assertAlmostEqual(
            margins["gross_margin"]["cv"], 0.010, places=3)
        self.assertAlmostEqual(
            margins["net_margin"]["mean"], 0.3665, places=3)
        self.assertAlmostEqual(
            margins["net_margin"]["cv"], 0.062, places=3)
        growth = result["components"]["growth_consistency"]["inputs"]
        self.assertEqual(growth["cyclicality_verdict"], "steady")
        self.assertEqual(growth["share_growing_10y"], 1.0)
        self.assertAlmostEqual(growth["max_drawdown"], 0.0328, places=3)
        quality = result["components"]["earnings_quality"]["inputs"]
        self.assertAlmostEqual(
            quality["ocf_to_net_income_5y_mean"], 1.298, places=2)
        self.assertAlmostEqual(
            quality["fcf_to_net_income_5y_mean"], 0.752, places=2)
        self.assertEqual(quality["profitable_years"], 10)
        self.assertEqual(quality["profitable_years_checked"], 10)
        alloc = result["components"]["capital_allocation"]["inputs"]
        self.assertEqual(alloc["dividend_cuts_10y"], 0)
        self.assertGreaterEqual(
            alloc["dividend_growth_streak_years"], 10)
        self.assertAlmostEqual(alloc["shares_change_5y"], -0.0204,
                               places=3)
        self.assertAlmostEqual(
            alloc["incremental_roic_5y"], 0.235, places=2)
        self.assertAlmostEqual(
            alloc["incremental_roic_10y"], 0.302, places=2)
        efficiency = result["components"]["efficiency"]
        self.assertAlmostEqual(efficiency["value"], -51.79, places=1)
        self.assertEqual(
            result["moat_evidence"]["assessment"],
            "strong financial evidence of a durable advantage")

    def test_msft_all_periods(self):
        path = _temp_db_copy()
        conn = sqlite3.connect(path)
        try:
            results = bq.assess_all_periods(conn, "MSFT")
        finally:
            conn.close()
        self.assertEqual(len(results), 19)
        # The earliest years cannot be rated excellent: only 3
        # annual periods exist for the first assessment.
        self.assertEqual(results[0]["rating"], "insufficient data")

    def test_dbs_insufficient_data(self):
        path = _temp_db_copy()
        conn = sqlite3.connect(path)
        try:
            result = bq.assess_business_quality(conn, "DBS")
        finally:
            conn.close()
        self.assertEqual(result["rating"], "insufficient data")


class TestQualityGate(unittest.TestCase):
    """Step 24 rating gates the Step 23 strongest alert."""

    def test_weak_quality_blocks_alert(self):
        tier = mos.classify_tier(0.25, -0.10, -0.30,
                                 _no_deterioration(),
                                 quality={"rating": "weak"})
        self.assertNotEqual(tier["tier"], "strongest_alert")
        self.assertTrue(tier["strongest_alert_blocked"])
        self.assertEqual(tier["quality_rating"], "weak")
        self.assertIn("business quality",
                      " ".join(tier["reasons"]))

    def test_insufficient_quality_blocks_alert(self):
        tier = mos.classify_tier(0.25, -0.10, -0.30,
                                 _no_deterioration(),
                                 quality="insufficient data")
        self.assertNotEqual(tier["tier"], "strongest_alert")
        self.assertTrue(tier["strongest_alert_blocked"])

    def test_good_quality_allows_alert(self):
        tier = mos.classify_tier(0.25, -0.10, -0.30,
                                 _no_deterioration(),
                                 quality={"rating": "good"})
        self.assertEqual(tier["tier"], "strongest_alert")
        self.assertFalse(tier["strongest_alert_blocked"])
        self.assertEqual(tier["quality_rating"], "good")

    def test_no_quality_keeps_previous_behaviour(self):
        tier = mos.classify_tier(0.25, -0.10, -0.30,
                                 _no_deterioration())
        self.assertEqual(tier["tier"], "strongest_alert")
        self.assertIsNone(tier["quality_rating"])

    def test_weak_quality_falls_back_to_reasonable_price(self):
        # Margin vs base is positive, so a blocked alert lands on
        # reasonable_price rather than fairly_valued/overvalued.
        tier = mos.classify_tier(0.25, 0.10, -0.30,
                                 _no_deterioration(),
                                 quality={"rating": "weak"})
        self.assertEqual(tier["tier"], "reasonable_price")
        self.assertTrue(tier["strongest_alert_blocked"])

    def test_msft_summary_carries_quality(self):
        path = _temp_db_copy()
        conn = sqlite3.connect(path)
        try:
            summary = mos.margin_of_safety_summary(
                conn, "MSFT", price_input=PRICE)
        finally:
            conn.close()
        self.assertEqual(
            summary["business_quality"]["rating"], "excellent")
        self.assertIsNone(summary["business_quality_error"])
        self.assertEqual(summary["tier"]["quality_rating"], "excellent")
        # MSFT is still overvalued at $507.12; the gate changes
        # nothing there.
        self.assertEqual(summary["tier"]["tier"], "overvalued")


if __name__ == "__main__":
    unittest.main()
