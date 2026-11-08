"""Tests for Step 18 — agents/financial_health.py.

Fixture rows only, plus temporary database copies for the real-data
tests.  No network requests, no production database writes.

Hand-checked expectations use the MSFT figures verified against the
real database (Step 17 report; all $m): FY2026 revenue 331,839,
net income 133,749, operating income 155,237, operating cash flow
182,935, capex 115,948, free cash flow 66,987, dividends paid 26,445,
total assets 758,376, total equity 442,387, total debt 40,294
(short 9,227 + long 31,067), cash 20,935, current assets 207,710,
current liabilities 168,825; FY2025 total assets 619,003, equity
343,479, debt 43,151, cash 30,242, current assets 191,131, current
liabilities 141,218, operating cash flow 136,162, free cash flow
71,611, dividends paid 24,082.
"""

import json
import shutil
import sqlite3
import statistics
import tempfile
import unittest
from pathlib import Path

from agents import financial_health as fh
from agents import financial_ratios as fr

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "assistant.db"

PRICE = {"price": 507.12, "market_cap": 3765971714048,
         "as_of_date": "2026-11-03",
         "source": "financial_snapshots (latest row)"}


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _row(stmt, period, period_type, data):
    return {
        "id": 1, "ticker": "MSFT", "company_name": "Microsoft",
        "exchange": "NASDAQ", "country": "USA",
        "statement_type": stmt, "reporting_period": period,
        "period_type": period_type, "fiscal_year": None,
        "fiscal_quarter": None, "currency": "USD",
        "source": "SEC EDGAR Company Facts",
        "source_document": "10-K", "data_json": data,
        "collected_at": "2026-11-03",
    }


def _entry(value, unit="USD"):
    return {"value": value, "unit": unit, "tag": "Test",
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
            (row["ticker"], row["company_name"],
             row["exchange"], row["country"], row["statement_type"],
             row["reporting_period"], row["period_type"],
             row["fiscal_year"], row["fiscal_quarter"],
             row["currency"], row["source"], row["source_document"],
             row["data_json"], row["collected_at"]))
    conn.commit()
    return conn


def _msft_rows(period, *, revenue, net, opinc, ocf, capex, fcf,
               div, assets, equity, debt, cash, ca, cl, inventory=0,
               tax=0, before_tax=0):
    income = {
        "revenue": _entry(revenue), "net_income": _entry(net),
        "operating_income": _entry(opinc),
        "income_before_tax": _entry(before_tax),
        "income_tax": _entry(tax),
        "gross_profit": _entry(revenue // 2),
        "eps_diluted": _entry(1), "diluted_shares": _entry(1000),
    }
    balance = {
        # Raw US key is "shareholders_equity" (Step 16 maps it to
        # the total_equity concept); "total_equity" is not a raw key.
        "total_assets": _entry(assets),
        "shareholders_equity": _entry(equity),
        "total_liabilities": _entry(assets - equity),
        "cash_and_equivalents": _entry(cash),
        "short_term_debt": _entry(debt // 4),
        "long_term_debt": _entry(debt - debt // 4),
        "current_assets": _entry(ca), "current_liabilities": _entry(cl),
        "inventory": _entry(inventory),
    }
    cashflow = {
        "operating_cash_flow": _entry(ocf),
        "capital_expenditure": _entry(capex),
        "free_cash_flow": _entry(fcf), "dividends_paid": _entry(div),
    }
    return [
        _row("income_statement", period, "annual", json.dumps(income)),
        _row("balance_sheet", period, "annual", json.dumps(balance)),
        _row("cash_flow_statement", period, "annual",
             json.dumps(cashflow)),
    ]


class RealDataTests(unittest.TestCase):
    def setUp(self):
        self.db_path = _temp_db_copy()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_fy2026_balance_sheet_strength(self):
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2026-06-30")
        bs = result["sections"]["balance_sheet_strength"]
        self.assertAlmostEqual(bs["equity_ratio"]["value"],
                               442387 / 758376, places=4)
        self.assertEqual(bs["equity_ratio"]["status"], "pass")
        self.assertAlmostEqual(bs["debt_to_equity"]["value"],
                               40294 / 442387, places=4)
        self.assertEqual(bs["debt_to_equity"]["status"], "pass")
        self.assertEqual(bs["net_debt"]["value"], (40294 - 20935) * 1000000)
        self.assertEqual(bs["net_debt_position"], "net debt")
        self.assertAlmostEqual(bs["cash_cover_of_debt"]["value"],
                               20935 / 40294, places=4)

    def test_fy2026_liquidity(self):
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2026-06-30")
        liq = result["sections"]["liquidity"]
        self.assertAlmostEqual(liq["current_ratio"]["value"],
                               207710 / 168825, places=4)
        self.assertAlmostEqual(liq["quick_ratio"]["value"],
                               (207710 - 1397) / 168825, places=4)
        self.assertEqual(liq["working_capital"]["value"],
                         (207710 - 168825) * 1000000)
        # Working capital fell from FY2025 (49,913) to FY2026 (38,885).
        self.assertLess(liq["working_capital_trend"]["change_3y"], 0)

    def test_fy2026_solvency(self):
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2026-06-30")
        solv = result["sections"]["solvency"]
        self.assertAlmostEqual(
            solv["debt_to_operating_cash_flow"]["value"],
            40294 / 182935, places=4)
        self.assertAlmostEqual(
            solv["debt_to_free_cash_flow"]["value"],
            40294 / 66987, places=4)
        self.assertEqual(
            solv["free_cash_flow_after_dividends"]["value"],
            (66987 - 26445) * 1000000)
        # Net debt / EBITDA is unavailable, never estimated.
        self.assertFalse(solv["net_debt_to_ebitda"]["present"])
        self.assertIn("EBITDA", solv["net_debt_to_ebitda"]["reason"])
        # Debt fell over 3 and 5 years (2021: 58,146; 2023: 47,237).
        self.assertLess(solv["debt_trend"]["change_3y"], 0)
        self.assertLess(solv["debt_trend"]["change_5y"], 0)

    def test_fy2026_verdict_strong(self):
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2026-06-30")
        self.assertEqual(result["verdict"], "strong")
        self.assertTrue(result["verdict_reasons"])
        # 5-year window: all profitable, low margin CV.
        metrics = result["sections"]["profitability_consistency"][
            "window_5y"]["metrics"]
        self.assertEqual(metrics["profitable_years"]["profitable"], 5)
        self.assertEqual(metrics["profitable_years"]["checked"], 5)
        margins = [72738 / 198270, 72361 / 211915, 88136 / 245122,
                   101832 / 281724, 133749 / 331839]
        self.assertAlmostEqual(metrics["net_margin"]["mean"],
                               statistics.fmean(margins), places=4)
        self.assertAlmostEqual(
            metrics["net_margin"]["cv"],
            statistics.stdev(margins) / statistics.fmean(margins),
            places=4)
        self.assertLess(metrics["net_margin"]["cv"], 0.20)
        self.assertGreater(metrics["roe"]["mean"], 0.15)

    def test_fy2025_verdict_strong(self):
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2025-06-30")
        self.assertEqual(result["verdict"], "strong")
        bs = result["sections"]["balance_sheet_strength"]
        self.assertAlmostEqual(bs["equity_ratio"]["value"],
                               343479 / 619003, places=4)
        self.assertEqual(bs["net_debt"]["value"], (43151 - 30242) * 1000000)
        solv = result["sections"]["solvency"]
        self.assertAlmostEqual(
            solv["debt_to_operating_cash_flow"]["value"],
            43151 / 136162, places=4)
        self.assertEqual(
            solv["free_cash_flow_after_dividends"]["value"],
            (71611 - 24082) * 1000000)

    def test_altman_no_z_score_without_retained_earnings(self):
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2026-06-30",
                                  price_input=PRICE)
        altman = result["distress"]["altman"]
        self.assertIsNone(altman["z_score"])
        self.assertIn("retained_earnings",
                      altman["reason"])
        comps = altman["components"]
        self.assertFalse(comps["retained_earnings_to_assets"]["present"])
        # The components that do exist are still reported.
        self.assertAlmostEqual(
            comps["working_capital_to_assets"]["value"],
            (207710 - 168825) / 758376, places=4)
        self.assertAlmostEqual(comps["ebit_to_assets"]["value"],
                               155237 / 758376, places=4)
        self.assertAlmostEqual(comps["sales_to_assets"]["value"],
                               331839 / 758376, places=4)
        # Market-value component uses the labelled stale price.
        self.assertAlmostEqual(
            comps["market_value_equity_to_liabilities"]["value"],
            3765971714048 / 315989000000, places=3)
        self.assertEqual(
            comps["market_value_equity_to_liabilities"]["inputs"]
            ["price_as_of"], "2026-11-03")
        self.assertEqual(result["distress"]["hard_flags"], [])

    def test_sparse_early_year_degrades_gracefully(self):
        # FY2008: total_assets and current assets/liabilities missing.
        result = fh.assess_health(self.conn, "MSFT",
                                  period="2008-06-30")
        self.assertEqual(result["verdict"], "insufficient data")
        self.assertTrue(result["verdict_reasons"])
        self.assertFalse(
            result["sections"]["balance_sheet_strength"]
            ["equity_ratio"]["present"])
        self.assertFalse(
            result["sections"]["liquidity"]["current_ratio"]["present"])

    def test_assess_all_periods(self):
        results = fh.assess_all_periods(self.conn, "MSFT",
                                        price_input=PRICE)
        self.assertEqual(len(results), 19)  # FY2008-FY2026
        verdicts = {r["reporting_period"]: r["verdict"]
                    for r in results}
        self.assertEqual(verdicts["2026-06-30"], "strong")
        self.assertEqual(verdicts["2008-06-30"], "insufficient data")
        # Every result carries a verdict and reasons.
        for r in results:
            self.assertIn(r["verdict"],
                          ("strong", "adequate", "weak",
                           "insufficient data"))
            self.assertTrue(r["verdict_reasons"])


class FixtureTests(unittest.TestCase):
    def test_weak_verdict_on_distress_flags(self):
        # A weak company: negative net income, current ratio < 1,
        # debt > equity, negative free cash flow.
        rows = []
        for i, period in enumerate(("2022-06-30", "2023-06-30",
                                    "2024-06-30")):
            rows += _msft_rows(
                period, revenue=100000, net=-5000, opinc=-2000,
                ocf=-1000, capex=2000, fcf=-3000, div=0,
                assets=100000, equity=20000, debt=60000, cash=5000,
                ca=10000, cl=30000, inventory=1000,
                tax=0, before_tax=-5000)
        conn = _fixture_conn(rows)
        result = fh.assess_health(conn, "MSFT", period="2024-06-30")
        self.assertEqual(result["verdict"], "weak")
        self.assertIn("negative net income",
                      "; ".join(result["verdict_reasons"]))
        self.assertIn("current ratio below 1",
                      result["distress"]["hard_flags"])
        conn.close()

    def test_missing_inputs_are_missing_not_zero(self):
        # A period with only an income row: balance concepts missing.
        income = {"revenue": _entry(50000), "net_income": _entry(5000),
                  "operating_income": _entry(8000),
                  "income_before_tax": _entry(6000),
                  "income_tax": _entry(1000),
                  "gross_profit": _entry(25000),
                  "eps_diluted": _entry(1), "diluted_shares": _entry(1000)}
        conn = _fixture_conn([
            _row("income_statement", "2024-06-30", "annual",
                 json.dumps(income))])
        result = fh.assess_health(conn, "MSFT", period="2024-06-30")
        self.assertEqual(result["verdict"], "insufficient data")
        bs = result["sections"]["balance_sheet_strength"]
        self.assertFalse(bs["equity_ratio"]["present"])
        self.assertFalse(bs["net_debt"]["present"])
        conn.close()

    def test_consistency_metrics(self):
        # Five years of ratios: margins 0.10, 0.12, 0.11, 0.13, 0.12.
        fake = {}
        for i, period in enumerate(("2020-06-30", "2021-06-30",
                                    "2022-06-30", "2023-06-30",
                                    "2024-06-30")):
            margin = (0.10, 0.12, 0.11, 0.13, 0.12)[i]
            fake[period] = {"ratios": {
                "net_margin": {"present": True, "value": margin},
                "roe": {"present": True, "value": 0.20},
                "roic": {"present": True, "value": 0.18}}}
        metrics = fh.consistency_metrics(fake, list(fake))
        margins = [0.10, 0.12, 0.11, 0.13, 0.12]
        self.assertAlmostEqual(metrics["net_margin"]["mean"],
                               statistics.fmean(margins))
        self.assertAlmostEqual(
            metrics["net_margin"]["cv"],
            statistics.stdev(margins) / statistics.fmean(margins))
        self.assertEqual(metrics["profitable_years"]["share"], 1.0)


if __name__ == "__main__":
    unittest.main()
