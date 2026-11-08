"""Step 27 tests — financial risk and deterioration detection.

Fixture rows + temp DB copies only; no network requests, no
production writes.  Covers trend breaks (sustained decline vs
recovered one-off dip), margin erosion, leverage/liquidity drift,
cash-flow stress, the overall verdict rule, risk flags, MSFT on
a temp DB copy (expected: warning overall, not deteriorating),
a synthetic deteriorating company, insufficient data, and the
Step 23 wiring (MSFT tier unchanged).
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import deterioration as det
from agents import margin_of_safety as mos

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _entry(value, unit="USD"):
    return {"value": value, "unit": unit, "tag": "Test",
            "filed": "2026-07-29", "form": "10-K"}


def _row(ticker, stmt, period, period_type, data):
    return {
        "id": 1, "ticker": ticker, "company_name": "Test Co",
        "exchange": "NASDAQ", "country": "USA",
        "statement_type": stmt, "reporting_period": period,
        "period_type": period_type, "fiscal_year": None,
        "fiscal_quarter": None, "currency": "USD",
        "source": "SEC EDGAR Company Facts",
        "source_document": "10-K", "data_json": data,
        "collected_at": "2026-11-03",
    }


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


def _grow(base, rates):
    """Series from a base value and per-year growth rates."""
    vals = [base]
    for rate in rates:
        vals.append(vals[-1] * (1 + rate))
    return vals


def _year_rows(ticker, year, *, revenue, net, opinc, ocf, capex, div,
               bb, assets, equity, debt, cash, ca, cl, inventory=0,
               receivables=0, payables=0):
    """One annual year of fixture rows (all $m-scale values)."""
    period = f"{year}-06-30"
    before_tax = net / 0.8 if net > 0 else net
    tax = before_tax - net
    income = {
        "revenue": _entry(revenue), "net_income": _entry(net),
        "operating_income": _entry(opinc),
        "income_before_tax": _entry(before_tax),
        "income_tax": _entry(tax),
        "gross_profit": _entry(revenue * 0.6),
        "eps_diluted": _entry(net / 1000),
        "diluted_shares": _entry(1000),
    }
    balance = {
        "total_assets": _entry(assets),
        "shareholders_equity": _entry(equity),
        "total_liabilities": _entry(assets - equity),
        "cash_and_equivalents": _entry(cash),
        "short_term_debt": _entry(debt // 4),
        "long_term_debt": _entry(debt - debt // 4),
        "current_assets": _entry(ca),
        "current_liabilities": _entry(cl),
        "inventory": _entry(inventory),
        "accounts_receivable": _entry(receivables),
        "accounts_payable": _entry(payables),
    }
    cashflow = {
        "operating_cash_flow": _entry(ocf),
        "capital_expenditure": _entry(capex),
        "free_cash_flow": _entry(ocf - capex),
        "dividends_paid": _entry(div),
        "share_repurchases": _entry(bb),
        "share_based_compensation": _entry(0),
    }
    return [
        _row(ticker, "income_statement", period, "annual",
             json.dumps(income)),
        _row(ticker, "balance_sheet", period, "annual",
             json.dumps(balance)),
        _row(ticker, "cash_flow_statement", period, "annual",
             json.dumps(cashflow)),
    ]


def _company_rows(ticker, start_year, specs):
    """Rows for a company from a list of per-year spec dicts."""
    rows = []
    for i, spec in enumerate(specs):
        rows.extend(_year_rows(ticker, start_year + i, **spec))
    return rows


def _solid_specs(n_years=8):
    """Steady grower: revenue +10%, net +12%, OCF +12%, debt flat,
    working capital growing, margins stable-to-rising."""
    years = list(range(n_years))
    revenue = _grow(100_000, [0.10] * (n_years - 1))
    net = _grow(20_000, [0.14] * (n_years - 1))
    opinc = _grow(30_000, [0.11] * (n_years - 1))
    ocf = _grow(25_000, [0.12] * (n_years - 1))
    capex = _grow(5_000, [0.05] * (n_years - 1))
    div = _grow(5_000, [0.08] * (n_years - 1))
    assets = _grow(150_000, [0.08] * (n_years - 1))
    equity = _grow(90_000, [0.10] * (n_years - 1))
    cash = _grow(15_000, [0.10] * (n_years - 1))
    ca = _grow(60_000, [0.08] * (n_years - 1))
    cl = _grow(40_000, [0.06] * (n_years - 1))
    return [
        {"revenue": revenue[i], "net": net[i], "opinc": opinc[i],
         "ocf": ocf[i], "capex": capex[i], "div": div[i], "bb": 2_000,
         "assets": assets[i], "equity": equity[i], "debt": 20_000,
         "cash": cash[i], "ca": ca[i], "cl": cl[i], "inventory": 5_000,
         "receivables": 10_000, "payables": 8_000}
        for i in years
    ]


def _deteriorating_specs(n_years=8):
    """Three healthy years, then four years of decline: revenue
    -8%/yr, net -15%/yr, OCF -10%/yr, debt +20%/yr, current
    liabilities +25%/yr while current assets flat — current
    ratio below 1 and working capital negative by the last year."""
    revenue = _grow(100_000, [0.10, 0.10, 0.10,
                              -0.08, -0.08, -0.08, -0.08])
    net = _grow(20_000, [0.12, 0.12, 0.12,
                         -0.15, -0.15, -0.15, -0.15])
    opinc = _grow(30_000, [0.11, 0.11, 0.11,
                           -0.12, -0.12, -0.12, -0.12])
    ocf = _grow(25_000, [0.12, 0.12, 0.12,
                         -0.10, -0.10, -0.10, -0.10])
    capex = _grow(5_000, [0.05] * 7)
    div = _grow(5_000, [0.08, 0.08, 0.08, 0.0, 0.0, 0.0, 0.0])
    assets = _grow(150_000, [0.08, 0.08, 0.08, 0.0, 0.0, 0.0, 0.0])
    equity = _grow(90_000, [0.10, 0.10, 0.10,
                            -0.10, -0.10, -0.10, -0.10])
    cash = _grow(15_000, [0.10, 0.10, 0.10,
                          -0.10, -0.10, -0.10, -0.10])
    ca = _grow(60_000, [0.08, 0.08, 0.08, 0.0, 0.0, 0.0, 0.0])
    cl = _grow(40_000, [0.06, 0.06, 0.06,
                        0.25, 0.25, 0.25, 0.25])
    debt = _grow(20_000, [0.0, 0.0, 0.0, 0.20, 0.20, 0.20, 0.20])
    return [
        {"revenue": revenue[i], "net": net[i], "opinc": opinc[i],
         "ocf": ocf[i], "capex": capex[i], "div": div[i], "bb": 2_000,
         "assets": assets[i], "equity": equity[i], "debt": debt[i],
         "cash": cash[i], "ca": ca[i], "cl": cl[i], "inventory": 5_000,
         "receivables": 10_000, "payables": 8_000}
        for i in range(n_years)
    ]


class TestTrendBreaks(unittest.TestCase):
    def test_solid_grower_trend_improving(self):
        conn = _fixture_conn(_company_rows("SOLID", 2019,
                                           _solid_specs()))
        result = det.assess_deterioration(conn, "SOLID")
        self.assertEqual(result["areas"]["trend_breaks"]["verdict"],
                         "improving")

    def test_sustained_decline_is_deteriorating(self):
        conn = _fixture_conn(_company_rows("DECL", 2019,
                                           _deteriorating_specs()))
        result = det.assess_deterioration(conn, "DECL")
        self.assertEqual(result["areas"]["trend_breaks"]["verdict"],
                         "deteriorating")
        self.assertIn("sustained decline",
                      result["areas"]["trend_breaks"]["evidence"][0]
                      if result["areas"]["trend_breaks"]["evidence"]
                      else "")

    def test_one_off_dip_is_labelled_not_a_trend(self):
        specs = _solid_specs()
        # Year 4 (index 4): a one-off write-down halves net income
        # while revenue keeps growing; next year it recovers above
        # the pre-dip level.
        specs[4]["net"] = specs[3]["net"] * 0.5
        specs[4]["opinc"] = specs[3]["opinc"] * 0.6
        conn = _fixture_conn(_company_rows("ONEOFF", 2019, specs))
        result = det.assess_deterioration(conn, "ONEOFF")
        self.assertNotEqual(
            result["areas"]["trend_breaks"]["verdict"],
            "deteriorating")
        self.assertFalse(result["material_deterioration"])
        evidence = " ".join(
            result["areas"]["trend_breaks"]["evidence"])
        self.assertIn("one-off", evidence)
        self.assertIn("recovered", evidence)

    def test_msft_one_offs_are_in_the_evidence(self):
        db_path = _temp_db_copy()
        try:
            conn = sqlite3.connect(db_path)
            result = det.assess_deterioration(conn, "MSFT")
            evidence = " ".join(
                result["areas"]["trend_breaks"]["evidence"])
            # The FY2015 Nokia write-down and FY2018 tax charge
            # are recovered one-offs, not a trend.
            self.assertIn("2015-06-30", evidence)
            self.assertIn("2018-06-30", evidence)
            self.assertIn("recovered", evidence)
        finally:
            conn.close()
            Path(db_path).unlink(missing_ok=True)


class TestMarginErosion(unittest.TestCase):
    def test_collapsing_margins_are_deteriorating(self):
        specs = _solid_specs()
        # Net income falls 25%/yr for the last five years while
        # revenue keeps growing: net margin 20% -> ~3%.
        for i in range(3, 8):
            specs[i]["net"] = specs[i - 1]["net"] * 0.75
        conn = _fixture_conn(_company_rows("MARGIN", 2019, specs))
        result = det.assess_deterioration(conn, "MARGIN")
        self.assertEqual(
            result["areas"]["margin_erosion"]["verdict"],
            "deteriorating")

    def test_solid_margins_are_improving(self):
        conn = _fixture_conn(_company_rows("SOLID", 2019,
                                           _solid_specs()))
        result = det.assess_deterioration(conn, "SOLID")
        self.assertEqual(
            result["areas"]["margin_erosion"]["verdict"],
            "improving")


class TestLeverageLiquidity(unittest.TestCase):
    def test_working_capital_fall_is_warning_not_deterioration(self):
        specs = _solid_specs()
        # Current assets nearly flat, current liabilities +6%/yr:
        # working capital falls ~56% over 5y but the current ratio
        # stays above 1 — the MSFT pattern.
        for i in range(8):
            specs[i]["ca"] = 60_000 * (1.02 ** i)
            specs[i]["cl"] = 40_000 * (1.06 ** i)
        conn = _fixture_conn(_company_rows("WC", 2019, specs))
        result = det.assess_deterioration(conn, "WC")
        self.assertEqual(
            result["areas"]["leverage_liquidity"]["verdict"],
            "warning")
        self.assertEqual(result["overall_verdict"], "stable")
        self.assertFalse(result["material_deterioration"])
        flag_names = [f["name"] for f in result["risk_flags"]]
        self.assertIn("working capital falling", flag_names)

    def test_current_ratio_below_1_is_deteriorating(self):
        conn = _fixture_conn(_company_rows("DECL", 2019,
                                           _deteriorating_specs()))
        result = det.assess_deterioration(conn, "DECL")
        self.assertEqual(
            result["areas"]["leverage_liquidity"]["verdict"],
            "deteriorating")


class TestCashflowStress(unittest.TestCase):
    def test_payouts_exceeding_fcf_are_deteriorating(self):
        specs = _solid_specs()
        # A huge final-year dividend: dividends + buybacks exceed
        # free cash flow in the latest year.
        specs[-1]["div"] = 60_000
        conn = _fixture_conn(_company_rows("PAYOUT", 2019, specs))
        result = det.assess_deterioration(conn, "PAYOUT")
        self.assertEqual(
            result["areas"]["cashflow_stress"]["verdict"],
            "deteriorating")
        # One deteriorating area alone is a warning overall, not
        # material deterioration.
        self.assertEqual(result["overall_verdict"], "warning")
        self.assertFalse(result["material_deterioration"])

    def test_solid_cashflow_is_not_stressed(self):
        conn = _fixture_conn(_company_rows("SOLID", 2019,
                                           _solid_specs()))
        result = det.assess_deterioration(conn, "SOLID")
        self.assertIn(result["areas"]["cashflow_stress"]["verdict"],
                      ("stable", "improving"))


class TestOverallVerdict(unittest.TestCase):
    def test_solid_company_is_improving_not_deteriorating(self):
        conn = _fixture_conn(_company_rows("SOLID", 2019,
                                           _solid_specs()))
        result = det.assess_deterioration(conn, "SOLID")
        self.assertIn(result["overall_verdict"],
                      ("stable", "improving"))
        self.assertFalse(result["material_deterioration"])

    def test_deteriorating_company_is_material(self):
        conn = _fixture_conn(_company_rows("DECL", 2019,
                                           _deteriorating_specs()))
        result = det.assess_deterioration(conn, "DECL")
        self.assertEqual(result["overall_verdict"], "deteriorating")
        self.assertTrue(result["material_deterioration"])
        self.assertTrue(any(
            f["severity"] == "high" for f in result["risk_flags"]))

    def test_negative_net_income_is_hard_distress(self):
        specs = _solid_specs()
        specs[-1]["net"] = -5_000
        conn = _fixture_conn(_company_rows("LOSS", 2019, specs))
        result = det.assess_deterioration(conn, "LOSS")
        self.assertEqual(result["overall_verdict"], "deteriorating")
        self.assertTrue(result["material_deterioration"])

    def test_insufficient_data_with_no_rows(self):
        conn = _fixture_conn([])
        result = det.assess_deterioration(conn, "NONE")
        self.assertEqual(result["overall_verdict"],
                         "insufficient data")
        self.assertIsNone(result["material_deterioration"])

    def test_insufficient_data_with_two_periods(self):
        conn = _fixture_conn(_company_rows("SHORT", 2025,
                                           _solid_specs(2)))
        result = det.assess_deterioration(conn, "SHORT")
        self.assertEqual(result["overall_verdict"],
                         "insufficient data")
        self.assertIsNone(result["material_deterioration"])


class TestMsftRealData(unittest.TestCase):
    def setUp(self):
        self.db_path = _temp_db_copy()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_msft_overall_warning_not_deteriorating(self):
        result = det.assess_deterioration(self.conn, "MSFT")
        self.assertEqual(result["overall_verdict"], "warning")
        self.assertFalse(result["material_deterioration"])
        areas = {k: v["verdict"]
                 for k, v in result["areas"].items()}
        self.assertEqual(areas["trend_breaks"], "warning")
        self.assertEqual(areas["margin_erosion"], "improving")
        self.assertEqual(areas["leverage_liquidity"], "warning")
        self.assertEqual(areas["cashflow_stress"], "warning")

    def test_msft_risk_flags(self):
        result = det.assess_deterioration(self.conn, "MSFT")
        flags = {f["name"]: f for f in result["risk_flags"]}
        self.assertIn("working capital falling", flags)
        self.assertIn("capex surge compressing FCF", flags)
        self.assertIn("free cash flow softening", flags)
        for flag in result["risk_flags"]:
            self.assertEqual(flag["severity"], "medium")
            self.assertIn("evidence", flag)
            self.assertIn("trend", flag)

    def test_msft_working_capital_numbers(self):
        result = det.assess_deterioration(self.conn, "MSFT")
        leverage = result["areas"]["leverage_liquidity"]["details"]
        self.assertAlmostEqual(
            leverage["working_capital_change_5y"], -0.5939, places=3)
        self.assertAlmostEqual(leverage["current_ratio"], 1.23,
                               places=2)
        cashflow = result["areas"]["cashflow_stress"]["details"]
        self.assertAlmostEqual(
            cashflow["fcf_after_dividends_and_buybacks"],
            18_271_000_000, delta=1)

    def test_msft_history_flags_the_write_down_years(self):
        # Point-in-time: in FY2015/FY2016 the Nokia write-down
        # really was deterioration as of then (recovery was not
        # observable yet).  FY2026 is warning, not deteriorating.
        history = det.assess_all_periods(self.conn, "MSFT")
        self.assertEqual(len(history), 19)
        by_period = {h["reporting_period"]: h for h in history}
        self.assertEqual(
            by_period["2026-06-30"]["overall_verdict"], "warning")
        self.assertEqual(
            by_period["2015-06-30"]["overall_verdict"],
            "deteriorating")
        self.assertTrue(
            by_period["2015-06-30"]["material_deterioration"])

    def test_d05_is_insufficient_data(self):
        result = det.assess_deterioration(self.conn, "D05")
        self.assertEqual(result["overall_verdict"],
                         "insufficient data")
        self.assertIsNone(result["material_deterioration"])


class TestStep23Wiring(unittest.TestCase):
    def setUp(self):
        self.db_path = _temp_db_copy()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_msft_step23_result_unchanged(self):
        summary = mos.margin_of_safety_summary(self.conn, "MSFT")
        self.assertEqual(summary["tier"]["tier"], "overvalued")
        self.assertFalse(summary["deterioration"]["deteriorated"])
        self.assertFalse(summary["deterioration"]["unknown"])
        self.assertIsNotNone(summary["deterioration_27"])
        self.assertEqual(
            summary["deterioration_27"]["overall_verdict"],
            "warning")
        self.assertIn("Step 27",
                      summary["deterioration"]["source"])

    def test_step27_deteriorating_blocks_via_step23(self):
        conn = _fixture_conn(_company_rows("DECL", 2019,
                                           _deteriorating_specs()))
        det27 = det.assess_deterioration(conn, "DECL")
        self.assertTrue(det27["material_deterioration"])
        flag = mos.assess_deterioration(conn, "DECL",
                                        deterioration_27=det27)
        self.assertTrue(flag["deteriorated"])
        self.assertFalse(flag["unknown"])
        self.assertIn("Step 27", flag["source"])
        self.assertTrue(any("high" in str(s) or s for s in
                            flag["signals"]))

    def test_step27_insufficient_falls_back_to_old_rule(self):
        # Step 27 cannot assess the ticker, but the old Step
        # 18/19/20 rule still fires on a weak health verdict.
        conn = _fixture_conn(_company_rows("DECL", 2019,
                                           _deteriorating_specs()))
        fake_27 = {"overall_verdict": "insufficient data",
                   "material_deterioration": None,
                   "risk_flags": [], "cautions": [],
                   "areas": {}, "inputs": {}}
        flag = mos.assess_deterioration(conn, "DECL",
                                        deterioration_27=fake_27)
        self.assertTrue(flag["deteriorated"])
        self.assertIn("Steps 18/19/20", flag["source"])

    def test_step23_old_path_unchanged_without_step27(self):
        # Without a Step 27 assessment the old logic runs exactly
        # as before (MSFT: no deterioration).
        flag = mos.assess_deterioration(self.conn, "MSFT")
        self.assertFalse(flag["deteriorated"])
        self.assertFalse(flag["unknown"])


if __name__ == "__main__":
    unittest.main()
