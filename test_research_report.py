"""Tests for Step 29 — agents/research_report.py.

Fixture rows and temporary database copies only; no network
requests, no production database writes.

Hand-checked MSFT expectations use the figures verified against
the real database in Steps 17-24 and 27: FY2026 revenue
$331,839m, net income $133,749m, diluted EPS $17.95, OCF
$182,935m, FCF $66,987m; P/E 28.25, P/B 8.54, EV/EBIT 24.38;
health STRONG; quality excellent (7/7); deterioration warning,
no material deterioration; DCF bear $98.52, base $163.30,
optimistic $234.64; tier overvalued at $507.12 (2026-11-03
snapshot); strongest-alert price $78.82.
"""

import gc
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agents import business_quality as bq
from agents import deterioration as det
from agents import financial_health as fh
from agents import financial_ratios as fr
from agents import margin_of_safety as mos
from agents import research_report as rr

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_fingerprint import (  # noqa: E402
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
DB_PATH = BASE_DIR / "database" / "assistant.db"

PRICE = {"price": 507.12, "market_cap": 3765971714048,
         "as_of_date": "2026-11-03",
         "source": "financial_snapshots (latest row)"}

BUY_SELL = re.compile(r"\b(buy|sell)\b", re.IGNORECASE)

SECTION_NAMES = ("business_summary", "financials",
                 "health_and_quality", "valuation", "peer_comparison",
                 "risks", "conclusion")

PERIODS_6Y = ["2021-06-30", "2022-06-30", "2023-06-30",
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

# A solid synthetic business: steady growth, profitable, modest
# debt, dividends every year, no dilution.  Values in $m.
SOLID_SPEC = {
    "revenue": [100, 110, 121, 133, 146, 161],
    "cost_of_revenue": [40, 44, 48, 53, 58, 64],
    "gross_profit": [60, 66, 73, 80, 88, 97],
    "research_and_development": [10, 11, 12, 13, 14, 16],
    "selling_general_administrative": [20, 22, 24, 26, 29, 31],
    "operating_income": [30, 33, 37, 41, 45, 50],
    "income_before_tax": [28, 31, 35, 39, 43, 48],
    "income_tax": [6, 7, 8, 9, 10, 11],
    "net_income": [22, 24, 27, 30, 33, 37],
    "eps_basic": [2.2, 2.4, 2.7, 3.0, 3.3, 3.7],
    "eps_diluted": [2.2, 2.4, 2.7, 3.0, 3.3, 3.7],
    "shares_outstanding_basic": [10, 10, 10, 10, 10, 10],
    "shares_outstanding_diluted": [10, 10, 10, 10, 10, 10],
    "total_assets": [200, 210, 220, 235, 250, 270],
    "total_liabilities": [80, 82, 84, 88, 90, 95],
    "total_equity": [120, 128, 136, 147, 160, 175],
    "cash_and_equivalents": [20, 22, 24, 26, 28, 30],
    "short_term_debt": [5, 5, 5, 5, 5, 5],
    "long_term_debt": [30, 30, 30, 30, 30, 30],
    "current_assets": [60, 65, 70, 75, 80, 85],
    "current_liabilities": [40, 42, 44, 46, 48, 50],
    "inventory": [5, 5, 5, 5, 5, 5],
    "accounts_receivable": [15, 16, 18, 20, 22, 24],
    "accounts_payable": [20, 22, 24, 26, 28, 30],
    "short_term_investments": [0, 0, 0, 0, 0, 0],
    "operating_cash_flow": [28, 30, 33, 36, 40, 44],
    "capital_expenditure": [8, 9, 9, 10, 11, 12],
    "dividends_paid": [5, 5, 6, 6, 7, 7],
    "share_repurchases": [2, 2, 2, 2, 2, 2],
    "share_based_compensation": [1, 1, 1, 1, 1, 1],
    "investing_cash_flow": [-10, -11, -11, -12, -13, -14],
    "financing_cash_flow": [-8, -8, -9, -9, -10, -10],
}

RAW_KEYS = {
    "eps_basic": "basic_eps",
    "eps_diluted": "diluted_eps",
    "shares_outstanding_basic": "basic_shares",
    "shares_outstanding_diluted": "diluted_shares",
    "total_equity": "shareholders_equity",
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
    conn.execute(
        "CREATE TABLE market_universe ("
        "id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
        "exchange TEXT, country TEXT, currency TEXT, sector TEXT, "
        "industry TEXT, security_type TEXT, market_cap REAL, "
        "is_active INTEGER, sec_cik TEXT)")
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


def _company_rows(ticker, spec, periods=PERIODS_6Y):
    """One income / balance / cash-flow row per period from a spec."""
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


def _figure(report, label):
    for row in report["sections"]["financials"]["data"]["key_figures"]:
        if row["label"] == label:
            return row
    return None


def _ratio(report, label):
    for row in report["sections"]["financials"]["data"]["ratios_latest"]:
        if row["label"] == label:
            return row
    return None


class TestResearchReportRealData(unittest.TestCase):
    """MSFT / DBS / unknown ticker on temp copies of the real DB."""

    @classmethod
    def setUpClass(cls):
        cls._hash_before = content_fingerprint(DB_PATH)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(DB_PATH) == cls._hash_before, \
            "production DB changed during tests"

    def _conn(self):
        self._db = _temp_db_copy()
        self.addCleanup(lambda: Path(self._db).unlink(missing_ok=True))
        conn = sqlite3.connect(self._db)
        self.addCleanup(conn.close)
        return conn

    def test_msft_report_structure(self):
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE,
                                    report_date="2026-11-08")
        self.assertEqual(report["ticker"], "MSFT")
        self.assertEqual(report["company_name"], "MICROSOFT CORP")
        self.assertEqual(report["country"], "USA")
        self.assertEqual(report["price"], 507.12)
        self.assertEqual(report["price_as_of"], "2026-11-03")
        self.assertEqual(report["statement_rows"], 242)
        self.assertEqual(tuple(report["sections"].keys()), SECTION_NAMES)
        # Business summary is always partial: no description stored.
        self.assertEqual(
            report["sections"]["business_summary"]["status"], "partial")
        self.assertIn("no business description",
                      report["sections"]["business_summary"]["reason"])
        self.assertEqual(report["status"], "partial")

    def test_msft_key_figures_and_ratios(self):
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE)
        self.assertEqual(_figure(report, "Revenue")["value"], 331839000000)
        self.assertEqual(_figure(report, "Net income")["value"],
                         133749000000)
        self.assertEqual(_figure(report, "Operating cash flow")["value"],
                         182935000000)
        self.assertEqual(_figure(report, "Free cash flow")["value"],
                         66987000000)
        self.assertAlmostEqual(_ratio(report, "P/E")["value"], 28.25,
                               places=1)
        self.assertAlmostEqual(_ratio(report, "P/B")["value"], 8.54,
                               places=1)
        self.assertAlmostEqual(_ratio(report, "EV/EBIT")["value"], 24.38,
                               places=1)
        self.assertAlmostEqual(_ratio(report, "Net margin")["value"],
                               0.4031, places=3)
        growth = {row["label"]: row for row in
                  report["sections"]["financials"]["data"]["growth"]}
        self.assertAlmostEqual(growth["Revenue"]["latest_yoy"], 0.178,
                               places=2)
        self.assertAlmostEqual(growth["Free cash flow"]["latest_yoy"],
                               -0.065, places=2)
        self.assertAlmostEqual(growth["Revenue"]["cagr_10y"], 0.138,
                               places=2)

    def test_msft_report_matches_upstream_modules(self):
        """Every number in the report agrees with Steps 17-24/27."""
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE)
        health = fh.assess_health(conn, "MSFT")
        quality = bq.assess_business_quality(conn, "MSFT")
        deterioration = det.assess_deterioration(conn, "MSFT")
        summary = mos.margin_of_safety_summary(conn, "MSFT",
                                               price_input=PRICE)
        hq = report["sections"]["health_and_quality"]["data"]
        self.assertEqual(hq["health_verdict"], health["verdict"])
        self.assertEqual(hq["quality_rating"], quality["rating"])
        self.assertAlmostEqual(hq["quality_mean_score"],
                               quality["mean_score"])
        self.assertEqual(hq["deterioration_verdict"],
                         deterioration["overall_verdict"])
        self.assertEqual(hq["material_deterioration"],
                         deterioration["material_deterioration"])
        self.assertEqual(len(hq["quality_components"]), 7)
        self.assertEqual(
            [c["score"] for c in hq["quality_components"]],
            [c["score"] for c in quality["components"].values()])
        val = report["sections"]["valuation"]["data"]
        self.assertAlmostEqual(
            val["fair_value_range"]["conservative"],
            summary["fair_value_range"]["conservative"], places=2)
        self.assertAlmostEqual(
            val["fair_value_range"]["base"],
            summary["fair_value_range"]["base"], places=2)
        self.assertAlmostEqual(
            val["fair_value_range"]["optimistic"],
            summary["fair_value_range"]["optimistic"], places=2)
        self.assertEqual(val["tier"], summary["tier"]["tier"])
        self.assertAlmostEqual(
            val["strongest_alert_threshold_price"],
            summary["strongest_alert_threshold_price"], places=2)
        self.assertEqual(len(val["entry_price_ladder"]), 4)

    def test_msft_valuation_values(self):
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE)
        val = report["sections"]["valuation"]["data"]
        self.assertEqual(val["tier"], "overvalued")
        self.assertAlmostEqual(val["fair_value_range"]["conservative"],
                               98.52, places=1)
        self.assertAlmostEqual(val["fair_value_range"]["base"], 163.30,
                               places=1)
        self.assertAlmostEqual(val["fair_value_range"]["optimistic"],
                               234.64, places=1)
        self.assertAlmostEqual(
            val["strongest_alert_threshold_price"], 78.82, places=1)
        self.assertAlmostEqual(val["margins"]["base"]["margin_graham"],
                               -2.106, places=2)
        self.assertAlmostEqual(
            val["margins"]["base"]["margin_vs_price"], -0.678, places=2)
        ladder = val["entry_price_ladder"]
        self.assertAlmostEqual(ladder[0]["entry_price"], 98.52, places=1)
        self.assertAlmostEqual(ladder[1]["entry_price"], 78.82, places=1)
        self.assertAlmostEqual(ladder[1]["implied_fcf_growth"], -0.001,
                               places=2)

    def test_msft_risks_and_conclusion(self):
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE)
        risks = report["sections"]["risks"]["data"]["risks"]
        names = [r["risk"] for r in risks]
        self.assertIn("free cash flow softening", names)
        self.assertIn("working capital falling", names)
        self.assertIn("capex surge compressing FCF", names)
        conditions = report["sections"]["risks"]["data"][
            "what_would_change_the_view"]
        self.assertTrue(any("strongest alert" in c for c in conditions))
        text = report["sections"]["conclusion"]["data"]["text"]
        self.assertIn("overvalued", text)
        self.assertIn("excellent", text)
        self.assertIn("not a recommendation", text)
        self.assertIn("final decision is the reader's", text)
        self.assertIsNone(BUY_SELL.search(text))

    def test_dbs_report_insufficient(self):
        conn = self._conn()
        report = rr.generate_report(conn, "D05", report_date="2026-11-08")
        self.assertEqual(report["ticker"], "D05")
        self.assertEqual(report["company_name"],
                         "DBS Group Holdings Ltd")
        self.assertEqual(report["status"], "insufficient_data")
        # DBS has 79 statement rows but they are announcement
        # metadata only, so every section is insufficient for a
        # different, specific reason.
        for name in ("financials", "health_and_quality", "valuation"):
            section = report["sections"][name]
            self.assertEqual(section["status"], "insufficient_data")
            self.assertIsNotNone(section["reason"])
        self.assertIn("key figures missing",
                      report["sections"]["financials"]["reason"])
        self.assertIn("unavailable",
                      report["sections"]["health_and_quality"]["reason"])
        text = report["sections"]["conclusion"]["data"]["text"]
        self.assertIn("no intrinsic-value range could be computed", text)
        self.assertIsNone(BUY_SELL.search(text))

    def test_unknown_ticker_report(self):
        conn = self._conn()
        report = rr.generate_report(conn, "ZZZZ")
        self.assertEqual(report["status"], "insufficient_data")
        self.assertIsNone(report["company_name"])
        self.assertEqual(
            report["sections"]["financials"]["status"],
            "insufficient_data")
        self.assertEqual(
            report["sections"]["peer_comparison"]["status"],
            "insufficient_data")

    def test_msft_with_peers(self):
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE,
                                    peers=["NVDA", "AVGO"])
        peer = report["sections"]["peer_comparison"]
        self.assertEqual(peer["status"], "insufficient_data")
        self.assertIn("NVDA",
                      peer["data"]["not_comparable_peers"])
        self.assertIn("AVGO",
                      peer["data"]["not_comparable_peers"])
        self.assertIn("not comparable", peer["data"]["summary"])

    def test_msft_markdown_round_trip(self):
        conn = self._conn()
        report = rr.generate_report(conn, "MSFT", price_input=PRICE,
                                    report_date="2026-11-08")
        with tempfile.TemporaryDirectory() as tmp:
            path = rr.write_report(report, reports_dir=tmp)
            text = Path(path).read_text(encoding="utf-8")
        self.assertIn("# Research report: MICROSOFT CORP (MSFT)", text)
        self.assertIn("overvalued", text)
        self.assertIn("excellent", text)
        self.assertIn("## Financials", text)
        self.assertIn("## Valuation and margin of safety", text)
        self.assertIn("## Conclusion", text)
        self.assertIn("not a recommendation", text)
        self.assertIn("$98.52", text)
        self.assertIsNone(BUY_SELL.search(text))

    def test_generate_report_file_read_only(self):
        with temp_dir_with_db_cleanup() as tmp:
            db_copy = str(Path(tmp) / "copy.db")
            shutil.copy(DB_PATH, db_copy)
            before = hashlib.sha256(
                Path(db_copy).read_bytes()).hexdigest()
            report, path = rr.generate_report_file(
                db_path=db_copy, ticker="MSFT", price_input=PRICE,
                report_date="2026-11-08", output_path=str(
                    Path(tmp) / "msft.md"))
            self.assertEqual(report["ticker"], "MSFT")
            self.assertTrue(Path(path).exists())
            self.assertEqual(
                hashlib.sha256(Path(db_copy).read_bytes()).hexdigest(),
                before)
            gc.collect()


class TestResearchReportFixtures(unittest.TestCase):
    """A solid synthetic business on an in-memory fixture DB."""

    def _conn(self):
        rows = _company_rows("SOLID", SOLID_SPEC)
        conn = _fixture_conn(rows)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO market_universe (ticker, company_name, "
            "exchange, country, currency, sector, industry, "
            "security_type, market_cap, is_active, sec_cik) "
            "VALUES ('SOLID', 'Solid Corp', 'NASDAQ', 'USA', 'USD', "
            "'Technology', 'Software', 'common_stock', NULL, 1, NULL)")
        conn.execute(
            "INSERT INTO financial_snapshots (ticker, snapshot_date, "
            "financial_data) VALUES ('SOLID', '2026-11-03', ?)",
            (json.dumps({"current_price": 50.0,
                         "market_cap": 500.0}),))
        return conn

    def test_solid_report_sections(self):
        conn = self._conn()
        price = {"price": 50.0, "market_cap": 500.0,
                 "as_of_date": "2026-11-03", "source": "test fixture"}
        report = rr.generate_report(conn, "SOLID", price_input=price)
        self.assertEqual(report["company_name"], "Solid Corp")
        self.assertEqual(report["sections"]["financials"]["status"],
                         "complete")
        self.assertEqual(
            report["sections"]["health_and_quality"]["status"],
            "complete")
        self.assertEqual(report["sections"]["valuation"]["status"],
                         "complete")
        self.assertEqual(
            report["sections"]["peer_comparison"]["status"],
            "insufficient_data")
        self.assertEqual(report["sections"]["financials"]["data"]
                         ["key_figures"][0]["value"], 161)
        quality = report["sections"]["health_and_quality"]["data"]
        self.assertIn(quality["quality_rating"],
                      ("excellent", "good", "adequate"))
        self.assertEqual(quality["health_verdict"], "strong")
        val = report["sections"]["valuation"]["data"]
        self.assertIsNotNone(val["fair_value_range"]["conservative"])
        self.assertIn(val["tier"], ("strongest_alert",
                                    "reasonable_price", "fairly_valued",
                                    "overvalued"))
        self.assertEqual(len(val["entry_price_ladder"]), 4)

    def test_fixture_markdown_has_no_buy_sell_language(self):
        conn = self._conn()
        price = {"price": 50.0, "market_cap": 500.0,
                 "as_of_date": "2026-11-03", "source": "test fixture"}
        report = rr.generate_report(conn, "SOLID", price_input=price)
        text = rr.report_to_markdown(report)
        self.assertIn("## Business summary", text)
        self.assertIn("## Financials", text)
        self.assertIn("## Peer comparison", text)
        self.assertIn("## Risks and what would change the view", text)
        self.assertIn("## Conclusion", text)
        self.assertIsNone(BUY_SELL.search(text))

    def test_fixture_no_price(self):
        conn = self._conn()
        # No caller price and no snapshot row for this ticker.
        conn.execute("DELETE FROM financial_snapshots")
        report = rr.generate_report(conn, "SOLID")
        self.assertIsNone(report["price"])
        self.assertTrue(any("no price available" in m
                            for m in report["missing_inputs"]))
        val = report["sections"]["valuation"]["data"]
        self.assertIsNone(val["price"])
        # Price ratios are missing without a price.
        pe = _ratio(report, "P/E")
        self.assertIsNone(pe["value"])


if __name__ == "__main__":
    unittest.main()
