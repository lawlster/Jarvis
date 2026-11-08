"""Step 16 tests — agents/financial_standard.py.

Uses fixture rows and temporary database copies only.  No live network
requests, no production database writes.  Run from the project root:

    python -m unittest test_financial_standard -v
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import financial_standard as std

BASE_DIR = Path(__file__).resolve().parent


def us_entry(value, tag="NetIncomeLoss", unit="USD", filed="2024-08-01",
             form="10-K", fy=2024, fp="FY"):
    return {
        "value": value, "unit": unit, "tag": tag,
        "start": "2023-07-01", "end": "2024-06-30",
        "filed": filed, "form": form,
        "accession_number": "0000000000-24-000001",
        "fiscal_year": fy, "fiscal_period": fp,
    }


def raw_row(**overrides):
    row = {
        "id": 1,
        "ticker": "MSFT",
        "company_name": "MICROSOFT CORP",
        "exchange": "NASDAQ",
        "country": "USA",
        "statement_type": "income_statement",
        "reporting_period": "2024-06-30",
        "period_type": "annual",
        "fiscal_year": 2024,
        "fiscal_quarter": None,
        "currency": "USD",
        "source": "SEC EDGAR Company Facts",
        "source_document": "0000000000-24-000001",
        "data_json": json.dumps({}),
        "collected_at": "2026-11-08T00:00:00",
    }
    row.update(overrides)
    return row


class USMappingTests(unittest.TestCase):
    def test_income_statement_maps_all_core_concepts_with_lineage(self):
        data = {
            "revenue": us_entry(1000, tag="Revenues"),
            "cost_of_revenue": us_entry(400, tag="CostOfRevenue"),
            "gross_profit": us_entry(600, tag="GrossProfit"),
            "operating_income": us_entry(300, tag="OperatingIncomeLoss"),
            "net_income": us_entry(200),
            "basic_eps": us_entry(2.5, unit="USD/shares",
                                  tag="EarningsPerShareBasic"),
            "diluted_eps": us_entry(2.4, unit="USD/shares",
                                    tag="EarningsPerShareDiluted"),
            "basic_shares": us_entry(8000, unit="shares",
                                     tag="WeightedAverageNumberOfSharesOutstandingBasic"),
            "diluted_shares": us_entry(8300, unit="shares",
                                       tag="WeightedAverageNumberOfDilutedSharesOutstanding"),
        }
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertEqual(rec["values"]["revenue"]["value"], 1000)
        self.assertTrue(rec["values"]["revenue"]["present"])
        self.assertEqual(rec["values"]["revenue"]["source_key"], "revenue")
        self.assertEqual(rec["values"]["revenue"]["source_tag"], "Revenues")
        self.assertEqual(rec["values"]["revenue"]["unit"], "USD")
        self.assertEqual(rec["values"]["revenue"]["filed_date"], "2024-08-01")
        self.assertEqual(rec["values"]["eps_basic"]["value"], 2.5)
        self.assertEqual(rec["values"]["eps_basic"]["unit"], "USD/shares")
        self.assertEqual(rec["values"]["shares_outstanding_basic"]["value"], 8000)
        self.assertEqual(rec["values"]["shares_outstanding_basic"]["unit"], "shares")
        # Balance-sheet concepts do not belong on an income row.
        self.assertNotIn("total_assets", rec["values"])
        self.assertEqual(rec["currency"], "USD")
        self.assertEqual(rec["period_type"], "annual")

    def test_balance_sheet_maps_and_derives_total_debt(self):
        data = {
            "cash_and_equivalents": us_entry(500, tag="CashAndCashEquivalentsAtCarryingValue"),
            "total_assets": us_entry(5000, tag="Assets"),
            "total_liabilities": us_entry(3000, tag="Liabilities"),
            "shareholders_equity": us_entry(2000, tag="StockholdersEquity"),
            "short_term_debt": us_entry(100, tag="LongTermDebtCurrent"),
            "long_term_debt": us_entry(400, tag="LongTermDebtNoncurrent"),
        }
        rec = std.standardise_row(raw_row(
            statement_type="balance_sheet", data_json=json.dumps(data)))
        self.assertEqual(rec["values"]["total_assets"]["value"], 5000)
        self.assertEqual(rec["values"]["total_equity"]["value"], 2000)
        self.assertEqual(rec["values"]["total_equity"]["source_key"],
                         "shareholders_equity")
        debt = rec["values"]["total_debt"]
        self.assertTrue(debt["present"])
        self.assertTrue(debt["derived"])
        self.assertEqual(debt["value"], 500)
        self.assertIn("short_term_debt + long_term_debt", debt["derivation"])
        self.assertNotIn("partial", debt["derivation"].lower())

    def test_total_debt_partial_when_one_component_missing(self):
        data = {
            "total_assets": us_entry(5000, tag="Assets"),
            "long_term_debt": us_entry(400, tag="LongTermDebtNoncurrent"),
        }
        rec = std.standardise_row(raw_row(
            statement_type="balance_sheet", data_json=json.dumps(data)))
        debt = rec["values"]["total_debt"]
        self.assertTrue(debt["present"])
        self.assertEqual(debt["value"], 400)
        self.assertIn("partial", debt["derivation"].lower())
        self.assertIn("short_term_debt", debt["derivation"])

    def test_cash_flow_maps_and_derives_free_cash_flow(self):
        data = {
            "operating_cash_flow": us_entry(
                900, tag="NetCashProvidedByUsedInOperatingActivities"),
            "capital_expenditure": us_entry(
                150, tag="PaymentsToAcquirePropertyPlantAndEquipment"),
        }
        rec = std.standardise_row(raw_row(
            statement_type="cash_flow_statement",
            data_json=json.dumps(data)))
        fcf = rec["values"]["free_cash_flow"]
        self.assertTrue(fcf["present"])
        self.assertTrue(fcf["derived"])
        self.assertEqual(fcf["value"], 750)
        self.assertIn("operating_cash_flow - capital_expenditure",
                      fcf["derivation"])

    def test_free_cash_flow_missing_when_a_component_missing(self):
        data = {"operating_cash_flow": us_entry(
            900, tag="NetCashProvidedByUsedInOperatingActivities")}
        rec = std.standardise_row(raw_row(
            statement_type="cash_flow_statement",
            data_json=json.dumps(data)))
        fcf = rec["values"]["free_cash_flow"]
        self.assertFalse(fcf["present"])
        self.assertIsNone(fcf["value"])
        self.assertIn("capital_expenditure", fcf["derivation"])

    def test_raw_xbrl_tag_keys_are_mapped(self):
        # A row keyed by XBRL tag rather than the collector's field name.
        data = {"NetIncomeLoss": us_entry(200)}
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertEqual(rec["values"]["net_income"]["value"], 200)
        self.assertEqual(rec["values"]["net_income"]["source_key"],
                         "NetIncomeLoss")

    def test_quarterly_period_and_ttm_pass_through(self):
        rec = std.standardise_row(raw_row(period_type="quarterly",
                                          fiscal_quarter=2))
        self.assertEqual(rec["period_type"], "quarterly")
        self.assertEqual(rec["fiscal_quarter"], 2)
        rec = std.standardise_row(raw_row(period_type="ttm"))
        self.assertEqual(rec["period_type"], "ttm")

    def test_cash_flow_alias_normalised(self):
        # Singapore spelling "cash_flow" normalises to cash_flow_statement.
        rec = std.standardise_row(raw_row(
            country="Singapore", statement_type="cash_flow",
            source="SGX Announcements",
            data_json=json.dumps({"announcement_id": "X1",
                                  "extraction_status": "raw_extraction"})))
        self.assertEqual(rec["statement_type"], "cash_flow_statement")
        self.assertFalse(rec["values"]["operating_cash_flow"]["present"])


class MissingVsZeroTests(unittest.TestCase):
    def test_zero_is_present_not_missing(self):
        data = {"net_income": us_entry(0), "revenue": us_entry(1000)}
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertTrue(rec["values"]["net_income"]["present"])
        self.assertEqual(rec["values"]["net_income"]["value"], 0)

    def test_absent_key_is_missing(self):
        data = {"revenue": us_entry(1000)}
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertFalse(rec["values"]["net_income"]["present"])
        self.assertIsNone(rec["values"]["net_income"]["value"])
        self.assertIsNone(rec["values"]["net_income"]["source_key"])

    def test_null_value_is_missing(self):
        data = {"net_income": us_entry(None)}
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertFalse(rec["values"]["net_income"]["present"])

    def test_non_numeric_value_is_missing_with_warning(self):
        data = {"net_income": us_entry("not a number")}
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertFalse(rec["values"]["net_income"]["present"])
        self.assertTrue(any("non-numeric" in w for w in rec["warnings"]))

    def test_currency_is_preserved_not_converted(self):
        data = {"revenue": us_entry(1000, unit="USD")}
        rec = std.standardise_row(raw_row(data_json=json.dumps(data)))
        self.assertEqual(rec["currency"], "USD")
        self.assertEqual(rec["values"]["revenue"]["unit"], "USD")
        self.assertEqual(rec["values"]["revenue"]["value"], 1000)

    def test_invalid_json_gives_missing_concepts_with_warning(self):
        rec = std.standardise_row(raw_row(data_json="{not json"))
        self.assertFalse(rec["values"]["revenue"]["present"])
        self.assertTrue(any("not valid JSON" in w for w in rec["warnings"]))


class SingaporeTests(unittest.TestCase):
    def test_sg_metadata_only_row_invents_nothing(self):
        data = {
            "announcement_id": "U6RBLH1JFNDV1QZT",
            "announcement_title": "Annual Report",
            "pdf_url": "https://links.sgx.com/1.0.0/corporate-announcements/x.pdf",
            "unit": "Not yet standardised",
            "extraction_status": "statement_heading_confirmed_raw_values",
            "verification_note": "Statement page heading confirmed. "
                                 "Numerical rows remain raw.",
            "pages": [{"pdf_page": 112, "text": "Consolidated income statement",
                       "rows": [{"label": "Total income", "values": [12345]}]}],
        }
        rec = std.standardise_row(raw_row(
            country="Singapore", statement_type="income_statement",
            currency="SGD", source="SGX Announcements",
            data_json=json.dumps(data)))
        for concept, value in rec["values"].items():
            self.assertFalse(value["present"], concept)
            self.assertIsNone(value["value"], concept)
        self.assertEqual(rec["currency"], "SGD")
        self.assertTrue(any("metadata only" in w for w in rec["warnings"]))
        # Raw PDF rows are NOT parsed into values.
        self.assertNotIn(12345,
                         [v["value"] for v in rec["values"].values()])

    def test_sg_label_map_is_documented_unvalidated(self):
        self.assertIn("total income", std.SG_LABEL_MAP)
        # Bank total income must not be silently equated with revenue.
        self.assertFalse(std.SG_LABEL_MAP["total income"]["equivalent"])
        self.assertEqual(std.SG_LABEL_MAP["total income"]["concept"], "revenue")

    def test_hk_label_map_is_proposed_only(self):
        self.assertIn("turnover", std.HK_LABEL_MAP)
        self.assertIn("unvalidated", std.HK_LABEL_MAP["turnover"]["note"].lower())
        rec = std.standardise_row(raw_row(
            country="Hong Kong", statement_type="income_statement",
            currency="HKD", source="HKEX",
            data_json=json.dumps({})))
        self.assertFalse(rec["values"]["revenue"]["present"])
        self.assertTrue(any("HKEX" in w for w in rec["warnings"]))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "copy.db"
        shutil.copy2(BASE_DIR / "database" / "assistant.db", self.db_path)
        self.before_hash = self._hash()
        self.conn = sqlite3.connect(str(self.db_path))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _hash(self):
        import hashlib
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def test_production_db_untouched_by_standardise_all(self):
        # standardise_all only reads; the source table must be unchanged.
        records = std.standardise_all(self.conn)
        self.assertEqual(len(records), 321)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM financial_statements").fetchone()[0],
            321)
        self.assertFalse(
            self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='standardized_statements'").fetchone())

    def test_write_standardised_round_trips_and_is_idempotent(self):
        records = std.standardise_all(self.conn)
        inserted, skipped = std.write_standardised(self.conn, records)
        self.assertEqual(skipped, 0)
        self.assertGreater(inserted, 0)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM standardized_statements").fetchone()[0]
        self.assertEqual(n, inserted)
        # Re-running replaces, never duplicates (unique key enforced).
        inserted2, skipped2 = std.write_standardised(self.conn, records)
        self.assertEqual(skipped2, 0)
        n2 = self.conn.execute(
            "SELECT COUNT(*) FROM standardized_statements").fetchone()[0]
        self.assertEqual(n2, n)
        # A present value and a missing value are stored distinctly.
        row = self.conn.execute(
            "SELECT value, present FROM standardized_statements "
            "WHERE ticker='MSFT' AND concept='revenue' AND present=1 "
            "LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
        row = self.conn.execute(
            "SELECT value, present FROM standardized_statements "
            "WHERE ticker='D05' AND concept='revenue' LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(tuple(row), (None, 0))

    def test_write_with_on_conflict_error_skips_duplicates(self):
        records = std.standardise_all(self.conn)
        std.write_standardised(self.conn, records)
        _, skipped = std.write_standardised(self.conn, records,
                                            on_conflict="error")
        total = sum(len(r["values"]) for r in records)
        self.assertEqual(skipped, total)

    def test_migration_sql_file_matches_module(self):
        sql_path = (BASE_DIR / "docs" / "migrations"
                    / "step16_standardized_statements.sql")
        self.assertTrue(sql_path.exists())
        self.assertIn("standardized_statements",
                      sql_path.read_text(encoding="utf-8"))
        self.assertIn("CREATE TABLE IF NOT EXISTS standardized_statements",
                      std.MIGRATION_SQL)


if __name__ == "__main__":
    unittest.main()
