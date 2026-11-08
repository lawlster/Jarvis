import csv
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import us_security_classifier as classifier


def listing(listing_id, ticker, company, cik, exchange="NASDAQ", security_type="equity"):
    return {
        "id": listing_id,
        "ticker": ticker,
        "company_name": company,
        "exchange": exchange,
        "country": "USA",
        "currency": "USD",
        "sec_cik": cik,
        "is_active": 1,
        "security_type": security_type,
        "data_source": "SEC EDGAR company_tickers_exchange.json",
    }


def evidence(ticker, cik, form, structure="operating_company", authority="SEC", share_class=""):
    return classifier.normalize_evidence({
        "ticker": ticker,
        "exchange": "NASDAQ",
        "sec_cik": cik,
        "issuer_structure": structure,
        "security_form": form,
        "share_class": share_class,
        "source_authority": authority,
        "evidence_source": "SEC filing",
        "evidence_reference": "https://www.sec.gov/Archives/edgar/data/1/000000000026000001/",
        "evidence_date": "2026-11-08",
        "rationale": "The filing describes the exact listed class.",
    })


class USSecurityClassifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_listing_rows_are_independent_and_unverified_rows_review(self):
        rows = [
            listing(1, "ABC.A", "ABC Class A", "0000000001"),
            listing(2, "ABC.B", "ABC Class B", "0000000001"),
            listing(3, "ABC-WT", "ABC Warrant", "0000000001"),
        ]
        results = classifier.classify_listings(rows, classification_date="2026-11-08")
        self.assertEqual(len(results), 3)
        self.assertEqual({row["listing_id"] for row in results}, {1, 2, 3})
        self.assertEqual({row["sec_cik"] for row in results}, {"0000000001"})
        self.assertEqual({row["security_form"] for row in results}, {"unknown"})
        self.assertTrue(all(row["manual_review_status"] == "required" for row in results))
        self.assertTrue(all(row["classification_status"] == "unknown" for row in results))
        self.assertEqual(results[2]["candidate_hints"], "possible_warrant;possible_warrant_ticker_suffix")
        self.assertEqual(results[0]["classification_date"], "2026-11-08")

    def test_exact_primary_evidence_classifies_separate_share_classes(self):
        rows = [
            listing(1, "ABC.A", "ABC Class A", "0000000001"),
            listing(2, "ABC.B", "ABC Class B", "0000000001"),
            listing(3, "ABC-PR", "ABC Preferred", "0000000001"),
        ]
        source = [
            evidence("ABC.A", "0000000001", "common_share", share_class="Class A"),
            evidence("ABC.B", "0000000001", "common_share", share_class="Class B"),
            evidence("ABC-PR", "0000000001", "preferred_share", share_class="Series A"),
        ]
        results = classifier.classify_listings(rows, source, "2026-11-08")
        self.assertEqual([row["share_class"] for row in results], ["Class A", "Class B", "Series A"])
        self.assertEqual([row["security_form"] for row in results], ["common_share", "common_share", "preferred_share"])
        self.assertTrue(all(row["classification_status"] == "confirmed" for row in results))
        self.assertTrue(all(row["classification_confidence"] == "high" for row in results))
        self.assertTrue(all(row["manual_review_status"] == "not_required" for row in results))

    def test_secondary_evidence_alone_is_only_a_suggestion(self):
        row = listing(1, "FUND", "Sample Fund", "0000000001")
        source = [evidence("FUND", "0000000001", "fund_share", "etf", "SECONDARY")]
        result = classifier.classify_listing(row, source, "2026-11-08")
        self.assertEqual(result["issuer_structure"], "unknown")
        self.assertEqual(result["security_form"], "unknown")
        self.assertEqual(result["secondary_suggestions"], "etf;fund_share")
        self.assertEqual(result["manual_review_status"], "required")

    def test_conflicting_primary_evidence_fails_to_manual_review(self):
        row = listing(1, "ABC", "ABC Corporation", "0000000001")
        source = [
            evidence("ABC", "0000000001", "common_share"),
            evidence("ABC", "0000000001", "adr_ads"),
        ]
        result = classifier.classify_listing(row, source, "2026-11-08")
        self.assertEqual(result["issuer_structure"], "operating_company")
        self.assertEqual(result["security_form"], "unknown")
        self.assertEqual(result["classification_status"], "conflicting")
        self.assertIn("Conflicting primary security-form evidence.", result["manual_review_reason"])

    def test_market_universe_read_is_read_only_and_pipeline_never_overwrites(self):
        database = self.root / "market.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("""
            CREATE TABLE market_universe (
                id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, exchange TEXT,
                country TEXT, currency TEXT, security_type TEXT, is_active INTEGER,
                data_source TEXT, sec_cik TEXT
            )
        """)
        connection.executemany(
            "INSERT INTO market_universe VALUES (?, ?, ?, ?, 'USA', 'USD', 'equity', 1, ?, ?)",
            [
                (1, "ABC.A", "ABC Class A", "NASDAQ", "SEC source", "0000000001"),
                (2, "ABC.B", "ABC Class B", "NASDAQ", "SEC source", "0000000001"),
            ],
        )
        connection.commit()
        connection.close()
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        loaded = classifier.load_us_listings(database)
        self.assertEqual(len(loaded), 2)
        self.assertEqual({row["sec_cik"] for row in loaded}, {"0000000001"})

        output = self.root / "outputs"
        result = classifier.run_pipeline(database, self.root / "no_evidence.csv", output, "2026-11-08")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["manual_review_count"], 2)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        with result["candidate_csv"].open(encoding="utf-8-sig", newline="") as source:
            candidates = list(csv.DictReader(source))
        self.assertEqual([row["ticker"] for row in candidates], ["ABC.A", "ABC.B"])
        with self.assertRaises(FileExistsError):
            classifier.run_pipeline(database, self.root / "no_evidence.csv", output, "2026-11-08")

    def test_bad_primary_evidence_fails_closed(self):
        with self.assertRaises(ValueError):
            classifier.normalize_evidence({
                "ticker": "ABC",
                "exchange": "NASDAQ",
                "sec_cik": "0000000001",
                "issuer_structure": "operating_company",
                "security_form": "common_share",
                "source_authority": "SEC",
                "evidence_source": "SEC filing",
                "evidence_reference": "accession",
                "evidence_date": "2026-11-08",
                "rationale": "",
            })

        bad_source = {
            "ticker": "ABC",
            "exchange": "NASDAQ",
            "sec_cik": "0000000001",
            "issuer_structure": "operating_company",
            "security_form": "common_share",
            "source_authority": "SEC",
            "evidence_source": "Claimed SEC filing",
            "evidence_reference": "https://example.com/not-sec",
            "evidence_date": "2026-11-08",
            "rationale": "The filing describes the exact listed class.",
        }
        with self.assertRaisesRegex(ValueError, "sec.gov"):
            classifier.normalize_evidence(bad_source)

    def test_listing_evidence_requires_ticker_and_exchange_and_cik_is_optional(self):
        row = listing(1, "ABC", "Sample Corp", "0000000001")
        without_cik = classifier.normalize_evidence({
            "ticker": "ABC",
            "exchange": "NASDAQ",
            "sec_cik": "",
            "issuer_structure": "unknown",
            "security_form": "common_share",
            "source_authority": "EXCHANGE",
            "evidence_source": "Nasdaq Trader symbol directory",
            "evidence_reference": "https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs",
            "evidence_date": "2026-11-08",
            "rationale": "The issue description explicitly identifies common stock.",
        })
        same_listing = classifier.classify_listing(row, [without_cik], "2026-11-08")
        different_exchange = classifier.classify_listing(
            {**row, "exchange": "NYSE"}, [without_cik], "2026-11-08"
        )
        mismatched_cik = classifier.normalize_evidence({
            **without_cik,
            "sec_cik": "0000000002",
        })
        conflict = classifier.classify_listing(row, [mismatched_cik], "2026-11-08")
        self.assertEqual(same_listing["security_form"], "common_share")
        self.assertEqual(same_listing["classification_status"], "provisional")
        self.assertEqual(different_exchange["classification_status"], "unknown")
        self.assertEqual(conflict["classification_status"], "conflicting")

    def test_output_directory_cannot_overlap_production_or_pilot_paths(self):
        production = self.root / "market_data" / "us"
        pilot = self.root / "market_data" / "us_pilot_20"
        safe = self.root / "market_data" / "us_classification"
        self.assertEqual(classifier.validate_output_dir(safe, (production, pilot)), safe.resolve())
        for unsafe in (production, production / "nested", production.parent, pilot / "nested"):
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                classifier.validate_output_dir(unsafe, (production, pilot))


if __name__ == "__main__":
    unittest.main()
