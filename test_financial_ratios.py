"""Tests for Step 17 — agents/financial_ratios.py.

Fixture rows only, plus temporary database copies for the snapshot
test.  No network requests, no production database writes.

Hand-computed expectations use the MSFT FY2026 annual figures
verified against the real database (revenue $331,839m, gross profit
$225,465m, operating income $155,237m, net income $133,749m,
income before tax $165,934m, income tax $32,185m, diluted EPS $17.95,
diluted shares 7,453m; total assets $758,376m, total liabilities
$315,989m, total equity $442,387m, current assets $207,710m,
current liabilities $168,825m, inventory $1,397m, cash $20,935m,
short-term debt $9,227m, long-term debt $31,067m; operating cash
flow $182,935m, capex $115,948m) with the 2026-11-03 snapshot price
$507.12 and market cap $3,765,971,714,048.
"""

import gc
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_fingerprint import temp_dir_with_db_cleanup  # noqa: E402

from agents import financial_ratios as fr

PRICE = {"price": 507.12, "market_cap": 3765971714048,
         "as_of_date": "2026-11-03",
         "source": "financial_snapshots (latest row)"}


def _row(stmt, period, period_type, data, fy=None):
    return {
        "id": 1, "ticker": "MSFT", "company_name": "Microsoft",
        "exchange": "NASDAQ", "country": "USA",
        "statement_type": stmt, "reporting_period": period,
        "period_type": period_type, "fiscal_year": fy,
        "fiscal_quarter": None, "currency": "USD",
        "source": "SEC EDGAR Company Facts",
        "source_document": "10-K", "data_json": data,
        "collected_at": "2026-11-03",
    }


def _entry(value, unit="USD"):
    return {"value": value, "unit": unit, "tag": "Test",
            "filed": "2026-07-29", "form": "10-K"}


def msft_fy26_rows():
    income = {
        "revenue": _entry(331839000000), "cost_of_revenue": _entry(106374000000),
        "gross_profit": _entry(225465000000),
        "research_and_development": _entry(35562000000),
        "operating_income": _entry(155237000000),
        "income_before_tax": _entry(165934000000),
        "income_tax": _entry(32185000000),
        "net_income": _entry(133749000000),
        "basic_eps": _entry(18.0, "USD/shares"),
        "diluted_eps": _entry(17.95, "USD/shares"),
        "basic_shares": _entry(7429000000, "shares"),
        "diluted_shares": _entry(7453000000, "shares"),
    }
    balance = {
        "cash_and_equivalents": _entry(20935000000),
        "short_term_investments": _entry(55908000000),
        "accounts_receivable": _entry(80876000000),
        "inventory": _entry(1397000000),
        "current_assets": _entry(207710000000),
        "total_assets": _entry(758376000000),
        "accounts_payable": _entry(42416000000),
        "current_liabilities": _entry(168825000000),
        "short_term_debt": _entry(9227000000),
        "long_term_debt": _entry(31067000000),
        "total_liabilities": _entry(315989000000),
        "shareholders_equity": _entry(442387000000),
    }
    cashflow = {
        "operating_cash_flow": _entry(182935000000),
        "capital_expenditure": _entry(115948000000),
        "investing_cash_flow": _entry(-139500000000),
        "financing_cash_flow": _entry(-52546000000),
        "dividends_paid": _entry(26445000000),
        "share_repurchases": _entry(22271000000),
        "share_based_compensation": _entry(12405000000),
    }
    return [
        _row("income_statement", "2026-06-30", "annual", income, fy=2026),
        _row("balance_sheet", "2026-06-30", "annual", balance, fy=2026),
        _row("cash_flow_statement", "2026-06-30", "annual", cashflow, fy=2026),
    ]


def msft_fy25_rows():
    income = {
        "revenue": _entry(281724000000), "cost_of_revenue": _entry(87831000000),
        "gross_profit": _entry(193893000000),
        "research_and_development": _entry(32488000000),
        "operating_income": _entry(128528000000),
        "income_before_tax": _entry(123627000000),
        "income_tax": _entry(21795000000),
        "net_income": _entry(101832000000),
        "diluted_eps": _entry(13.64, "USD/shares"),
        "diluted_shares": _entry(7465000000, "shares"),
    }
    balance = {
        "cash_and_equivalents": _entry(30242000000),
        "short_term_investments": _entry(64323000000),
        "accounts_receivable": _entry(69905000000),
        "inventory": _entry(938000000),
        "current_assets": _entry(191131000000),
        "total_assets": _entry(619003000000),
        "accounts_payable": _entry(27724000000),
        "current_liabilities": _entry(141218000000),
        "short_term_debt": _entry(2999000000),
        "long_term_debt": _entry(40152000000),
        "total_liabilities": _entry(275524000000),
        "shareholders_equity": _entry(343479000000),
    }
    return [
        _row("income_statement", "2025-06-30", "annual", income, fy=2025),
        _row("balance_sheet", "2025-06-30", "annual", balance, fy=2025),
    ]


def msft_fy24_balance_row():
    balance = {
        "cash_and_equivalents": _entry(18315000000),
        "short_term_investments": _entry(0),
        "accounts_receivable": _entry(0),
        "inventory": _entry(1246000000),
        "current_assets": _entry(159734000000),
        "total_assets": _entry(512163000000),
        "accounts_payable": _entry(0),
        "current_liabilities": _entry(125286000000),
        "short_term_debt": _entry(2249000000),
        "long_term_debt": _entry(42688000000),
        "total_liabilities": _entry(243686000000),
        "shareholders_equity": _entry(268477000000),
    }
    return _row("balance_sheet", "2024-06-30", "annual", balance, fy=2024)


def values_for(rows):
    from agents import financial_standard as fs
    values = {}
    for row in rows:
        record = fs.standardise_row(row)
        key = (record["statement_type"], record["reporting_period"],
               record["period_type"])
        values[key] = dict(record["values"])
    return values


class TestMSFTFY2026(unittest.TestCase):
    def setUp(self):
        self.values = values_for(msft_fy26_rows() + msft_fy25_rows()
                                 + [msft_fy24_balance_row()])
        self.result = fr.compute_ratios(
            self.values, "MSFT", period="2026-06-30",
            period_type="annual", price_input=PRICE)
        self.r = self.result["ratios"]

    def test_pe_ratio(self):
        # 507.12 / 17.95
        self.assertAlmostEqual(self.r["pe_ratio"]["value"], 28.2518,
                               places=3)
        self.assertEqual(self.r["pe_ratio"]["price_as_of"], "2026-11-03")
        self.assertIn("price / diluted EPS",
                      self.r["pe_ratio"]["formula"])

    def test_pb_ratio(self):
        # 507.12 / (442,387,000,000 / 7,453,000,000)
        self.assertAlmostEqual(self.r["pb_ratio"]["value"], 8.5436,
                               places=3)

    def test_ps_ratio(self):
        # 3,765,971,714,048 / 331,839,000,000
        self.assertAlmostEqual(self.r["ps_ratio"]["value"], 11.3488,
                               places=3)

    def test_ev_ebit(self):
        # EV = 3,765,971,714,048 + 40,294,000,000 - 20,935,000,000
        ev = self.r["ev_ebit"]["inputs"]["enterprise_value"]
        self.assertEqual(ev, 3785330714048)
        self.assertAlmostEqual(self.r["ev_ebit"]["value"], 24.3842,
                               places=3)

    def test_margins(self):
        self.assertAlmostEqual(self.r["gross_margin"]["value"],
                               225465 / 331839, places=4)
        self.assertAlmostEqual(self.r["operating_margin"]["value"],
                               155237 / 331839, places=4)
        self.assertAlmostEqual(self.r["net_margin"]["value"],
                               133749 / 331839, places=4)

    def test_roe_uses_average_equity(self):
        # 133,749 / ((442,387 + 343,479) / 2) = 0.3404
        self.assertEqual(self.r["roe"].get("basis"), "average")
        self.assertAlmostEqual(self.r["roe"]["value"], 0.3404, places=3)

    def test_roa_uses_average_assets(self):
        # 133,749 / ((758,376 + 619,003) / 2) = 0.1942
        self.assertEqual(self.r["roa"].get("basis"), "average")
        self.assertAlmostEqual(self.r["roa"]["value"], 0.1942, places=3)

    def test_roic(self):
        # NOPAT = 155,237 * (1 - 32,185/165,934) = 125,126.8m
        # avg invested capital = (461,746 + 356,388) / 2 = 409,067m
        self.assertEqual(self.r["roic"].get("basis"), "average")
        self.assertAlmostEqual(self.r["roic"]["value"], 0.3059, places=3)
        self.assertAlmostEqual(
            self.r["roic"]["inputs"]["nopat"], 125126818572.44, delta=1.0)

    def test_leverage_and_liquidity(self):
        self.assertAlmostEqual(self.r["debt_to_equity"]["value"],
                               40294 / 442387, places=4)
        self.assertAlmostEqual(self.r["current_ratio"]["value"],
                               207710 / 168825, places=4)
        self.assertAlmostEqual(self.r["quick_ratio"]["value"],
                               (207710 - 1397) / 168825, places=4)

    def test_unavailable_ratios(self):
        self.assertFalse(self.r["ev_ebitda"]["present"])
        self.assertIn("EBITDA", self.r["ev_ebitda"]["reason"])
        self.assertFalse(self.r["interest_coverage"]["present"])
        self.assertIn("interest expense",
                      self.r["interest_coverage"]["reason"])


class TestMSFTFY2025(unittest.TestCase):
    def test_prior_year_fundamentals(self):
        values = values_for(msft_fy25_rows() + [msft_fy24_balance_row()])
        result = fr.compute_ratios(values, "MSFT", period="2025-06-30",
                                   period_type="annual")
        r = result["ratios"]
        # No price supplied: valuation ratios missing, fundamentals present.
        self.assertFalse(r["pe_ratio"]["present"])
        self.assertEqual(r["pe_ratio"]["reason"],
                         "no price input supplied")
        self.assertAlmostEqual(r["gross_margin"]["value"],
                               193893 / 281724, places=4)
        self.assertAlmostEqual(r["net_margin"]["value"],
                               101832 / 281724, places=4)
        # ROE on average equity: 101,832 / ((343,479+268,477)/2)
        self.assertAlmostEqual(r["roe"]["value"], 0.3328, places=3)
        # ROA on average assets: 101,832 / ((619,003+512,163)/2)
        self.assertAlmostEqual(r["roa"]["value"], 0.1800, places=3)
        # ROIC: NOPAT 128,528*(1-21,795/123,627)=105,869m;
        # avg IC (356,388+295,099)/2 = 325,743.5m -> 0.3250
        self.assertAlmostEqual(r["roic"]["value"], 0.3250, places=3)
        self.assertAlmostEqual(r["debt_to_equity"]["value"],
                               43151 / 343479, places=4)
        self.assertAlmostEqual(r["current_ratio"]["value"],
                               191131 / 141218, places=4)
        self.assertAlmostEqual(r["quick_ratio"]["value"],
                               (191131 - 938) / 141218, places=4)


class TestMissingAndZero(unittest.TestCase):
    def test_missing_balance_row(self):
        values = values_for([msft_fy26_rows()[0]])  # income only
        result = fr.compute_ratios(values, "MSFT", period="2026-06-30",
                                   period_type="annual",
                                   price_input=PRICE)
        r = result["ratios"]
        self.assertTrue(r["gross_margin"]["present"])
        for name in ("pb_ratio", "roe", "roa", "roic",
                     "debt_to_equity", "current_ratio", "quick_ratio"):
            self.assertFalse(r[name]["present"], name)
            self.assertIn("missing input", r[name]["reason"], name)
        self.assertFalse(r["ev_ebit"]["present"])

    def test_zero_revenue(self):
        rows = msft_fy26_rows()
        rows[0]["data_json"]["revenue"] = _entry(0)
        rows[0]["data_json"]["gross_profit"] = _entry(0)
        rows[0]["data_json"]["net_income"] = _entry(0)
        values = values_for(rows)
        result = fr.compute_ratios(values, "MSFT", period="2026-06-30",
                                   period_type="annual")
        r = result["ratios"]
        self.assertFalse(r["gross_margin"]["present"])
        self.assertEqual(r["gross_margin"]["reason"],
                         "denominator is zero")
        self.assertFalse(r["net_margin"]["present"])

    def test_zero_equity(self):
        rows = msft_fy26_rows()
        rows[1]["data_json"]["shareholders_equity"] = _entry(0)
        values = values_for(rows)
        result = fr.compute_ratios(values, "MSFT", period="2026-06-30",
                                   period_type="annual")
        self.assertFalse(result["ratios"]["roe"]["present"])
        self.assertEqual(result["ratios"]["roe"]["reason"],
                         "denominator is zero")

    def test_no_price_input(self):
        values = values_for(msft_fy26_rows())
        result = fr.compute_ratios(values, "MSFT", period="2026-06-30",
                                   period_type="annual")
        for name in ("pe_ratio", "pb_ratio", "ps_ratio", "ev_ebit"):
            self.assertFalse(result["ratios"][name]["present"], name)
        self.assertTrue(result["ratios"]["gross_margin"]["present"])

    def test_earliest_period_uses_period_end_basis(self):
        values = values_for(msft_fy26_rows())
        result = fr.compute_ratios(values, "MSFT", period="2026-06-30",
                                   period_type="annual")
        self.assertEqual(result["ratios"]["roe"].get("basis"),
                         "period_end")
        self.assertAlmostEqual(result["ratios"]["roe"]["value"],
                               133749 / 442387, places=4)


class TestTTM(unittest.TestCase):
    def _quarterly_income(self, period, revenue, operating_income,
                          net_income, ibt=None, tax=None, shares=7453000000):
        data = {
            "revenue": _entry(revenue),
            "operating_income": _entry(operating_income),
            "net_income": _entry(net_income),
            "diluted_shares": _entry(shares, "shares"),
        }
        if ibt is not None:
            data["income_before_tax"] = _entry(ibt)
        if tax is not None:
            data["income_tax"] = _entry(tax)
        return _row("income_statement", period, "quarterly", data)

    def test_ttm_annual_minus_prior_quarter(self):
        # MSFT has no quarterly row at a fiscal year end, so TTM is
        # annual FY26 + Q1 FY27 - Q1 FY26.
        rows = msft_fy26_rows() + msft_fy25_rows()
        rows.append(self._quarterly_income(
            "2026-09-30", 92279000000, 45915000000, 36629000000,
            ibt=50000000000, tax=10000000000))
        rows.append(self._quarterly_income(
            "2025-09-30", 77673000000, 37961000000, 27747000000,
            ibt=30000000000, tax=6000000000))
        rows.append(self._quarterly_income(
            "2025-12-31", 81273000000, 38275000000, 38458000000))
        rows.append(self._quarterly_income(
            "2026-03-31", 82886000000, 38398000000, 31778000000))
        # Quarterly balance row at 2026-09-30 for the TTM balances.
        bal = {
            "cash_and_equivalents": _entry(21000000000),
            "current_assets": _entry(210000000000),
            "total_assets": _entry(770000000000),
            "current_liabilities": _entry(170000000000),
            "inventory": _entry(1500000000),
            "short_term_debt": _entry(9000000000),
            "long_term_debt": _entry(30000000000),
            "total_liabilities": _entry(320000000000),
            "shareholders_equity": _entry(450000000000),
        }
        rows.append(_row("balance_sheet", "2026-09-30", "quarterly", bal))
        values = values_for(rows)
        result = fr.compute_ratios(values, "MSFT", period="2026-09-30",
                                   period_type="ttm",
                                   price_input=PRICE)
        r = result["ratios"]
        self.assertTrue(any("annual 2026-06-30 + quarter" in w
                            for w in result["warnings"]))
        # TTM revenue = 331,839 + 92,279 - 77,673 = 346,445m
        self.assertAlmostEqual(
            r["net_margin"]["inputs"]["revenue"], 346445000000)
        # TTM net income = 133,749 + 36,629 - 27,747 = 142,631m
        self.assertAlmostEqual(
            r["net_margin"]["inputs"]["net_income"], 142631000000)
        self.assertAlmostEqual(r["net_margin"]["value"],
                               142631 / 346445, places=4)
        # TTM P/S uses the snapshot market cap over TTM revenue.
        self.assertAlmostEqual(r["ps_ratio"]["value"],
                               3765971714048 / 346445000000, places=3)

    def test_ttm_missing_prior_quarter(self):
        rows = msft_fy26_rows()
        rows.append(self._quarterly_income(
            "2026-09-30", 92279000000, 45915000000, 36629000000))
        values = values_for(rows)
        result = fr.compute_ratios(values, "MSFT", period="2026-09-30",
                                   period_type="ttm")
        self.assertEqual(result["ratios"], {})
        self.assertTrue(any("cannot be built" in w
                            for w in result["warnings"]))

    def test_quarterly_single_period(self):
        rows = msft_fy26_rows()
        rows.append(self._quarterly_income(
            "2026-09-30", 92279000000, 45915000000, 36629000000,
            ibt=50000000000, tax=10000000000))
        bal = {
            "cash_and_equivalents": _entry(21000000000),
            "current_assets": _entry(210000000000),
            "total_assets": _entry(770000000000),
            "current_liabilities": _entry(170000000000),
            "inventory": _entry(1500000000),
            "short_term_debt": _entry(9000000000),
            "long_term_debt": _entry(30000000000),
            "total_liabilities": _entry(320000000000),
            "shareholders_equity": _entry(450000000000),
        }
        rows.append(_row("balance_sheet", "2026-09-30", "quarterly", bal))
        values = values_for(rows)
        result = fr.compute_ratios(values, "MSFT", period="2026-09-30",
                                   period_type="quarterly")
        r = result["ratios"]
        self.assertAlmostEqual(r["net_margin"]["value"],
                               36629 / 92279, places=4)
        self.assertAlmostEqual(r["current_ratio"]["value"],
                               210000 / 170000, places=4)


class TestSnapshotPrice(unittest.TestCase):
    def test_load_snapshot_price_from_temp_db(self):
        with temp_dir_with_db_cleanup() as tmp:
            db_path = Path(tmp) / "copy.db"
            import shutil
            shutil.copyfile(
                Path(__file__).resolve().parent / "database" / "assistant.db",
                db_path)
            conn = sqlite3.connect(str(db_path))
            price = fr.load_snapshot_price(conn, "MSFT")
            self.assertEqual(price["price"], 507.12)
            self.assertEqual(price["market_cap"], 3765971714048)
            self.assertEqual(price["as_of_date"], "2026-11-03")
            self.assertIsNone(fr.load_snapshot_price(conn, "AAPL"))
            conn.close()
            gc.collect()

    def test_market_cap_fallback(self):
        mcap, method = fr.market_cap_for(
            {"price": 100.0, "market_cap": None}, 1000)
        self.assertEqual(mcap, 100000.0)
        self.assertIn("price * shares", method)
        mcap, _ = fr.market_cap_for(None, 1000)
        self.assertIsNone(mcap)


if __name__ == "__main__":
    unittest.main()
