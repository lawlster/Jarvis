import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import us_security_classifier as classifier
from agents import us_security_evidence as evidence
from agents import us_security_classification_phase3 as phase3


class USSecurityPhase3Tests(unittest.TestCase):
    def test_five_parser_artifacts_are_resolved_from_explicit_issue_descriptions(self):
        cases = {
            "BSBR": ("Banco Santander Brasil SA American Depositary Shares, each representing one unit", "adr_ads"),
            "RNP": ("Cohen & Steers REIT and Preferred and Income Fund, Inc. Common Shares", "common_share"),
            "VBF": ("Invesco Bond Fund Common Stock", "common_share"),
            "WDH": ("Waterdrop Inc. American Depositary Shares (each representing the right to receive 10 Class A Ordinary Shares)", "adr_ads"),
        }
        for ticker, (description, expected) in cases.items():
            with self.subTest(ticker=ticker):
                self.assertEqual(evidence.parse_issue_form_description(description)[0], expected)
        self.assertEqual(
            evidence.parse_issue_form_description("BANCO SANTANDER(BRAZIL)SA SPON ADR EA REPR 1 UNIT (COM+PRF)")[0],
            "adr_ads",
        )
        self.assertEqual(evidence.parse_issue_form_description("INVESCO BOND FUND")[0], "unknown")
        self.assertEqual(evidence.parse_share_class_description("WDH ADS EACH REPR 10 CL ORD SHS"), "")
        self.assertEqual(evidence.parse_share_class_description("WDH ADS EACH REPR 10 CL A ORD SHS"), "Class A")

    def test_tpc_and_tpc_case_are_distinct_listing_identities(self):
        self.assertEqual(classifier.listing_key({"ticker": "TPC", "exchange": "NYSE"}), ("TPC", "NYSE"))
        self.assertEqual(classifier.listing_key({"ticker": "TpC", "exchange": "NYSE"}), ("TpC", "NYSE"))
        self.assertNotEqual(
            classifier.listing_key({"ticker": "TPC", "exchange": "NYSE"}),
            classifier.listing_key({"ticker": "TpC", "exchange": "NYSE"}),
        )
        records = evidence.parse_nyse_records({
            "EQUITY": {"metadata": {"fetched_at_utc": "2026-11-08T00:00:00+00:00"}, "rows": [
                {"url": "https://www.nyse.com/quote/XNYS:TPC", "instrumentName": "TUTOR PERINI CORP"},
                {"url": "https://www.nyse.com/quote/XNYS:TpC", "instrumentName": "AT&T INC DEP SHS PERP PFD SER C"},
            ]},
        })
        self.assertEqual({row["ticker"] for row in records}, {"TPC", "TpC"})

    def test_three_authoritative_disagreements_remain_conflicting(self):
        cases = {
            "IPB": ("etf", "preferred_share"),
            "MHNC": ("debt_note", "preferred_share"),
            "TVC": ("common_share", "preferred_share"),
        }
        for ticker, (nasdaq_form, nyse_form) in cases.items():
            listing = {"id": ticker, "ticker": ticker, "exchange": "NYSE", "sec_cik": "0000000001"}
            rows = [
                classifier.normalize_evidence({
                    "ticker": ticker, "exchange": "NYSE", "sec_cik": "0000000001",
                    "issuer_structure": "etf" if ticker == "IPB" else "unknown",
                    "security_form": nasdaq_form if ticker != "IPB" else "exchange_traded_product",
                    "source_authority": "EXCHANGE", "evidence_source": "Nasdaq snapshot",
                    "evidence_reference": "https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs",
                    "evidence_date": "2026-11-08", "rationale": "Official Nasdaq field or description.",
                }),
                classifier.normalize_evidence({
                    "ticker": ticker, "exchange": "NYSE", "sec_cik": "0000000001",
                    "issuer_structure": "unknown", "security_form": nyse_form,
                    "source_authority": "EXCHANGE", "evidence_source": "NYSE snapshot",
                    "evidence_reference": "https://www.nyse.com/api/quotes/filter",
                    "evidence_date": "2026-11-08", "rationale": "NYSE preferred-stock category.",
                }),
            ]
            with self.subTest(ticker=ticker):
                result = classifier.classify_listing(listing, rows, "2026-11-08")
                self.assertEqual(result["classification_status"], "conflicting")
                self.assertEqual(result["manual_review_status"], "required")

    def test_end_to_end_rebuild_uses_local_hash_checked_snapshots_and_exact_ticker_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            source_specs = []
            nasdaq = (
                "Symbol|Security Name|Listing Exchange|ETF|Test Issue\n"
                "TPC|Tutor Perini Corporation Common Stock|N|N|N\n"
            ).encode()
            payloads = {"nasdaqtraded": ("text", nasdaq)}
            for category in evidence.NYSE_CATEGORIES:
                rows = []
                if category == "EQUITY":
                    rows = [
                        {"url": "https://www.nyse.com/quote/XNYS:TPC", "instrumentName": "TUTOR PERINI CORP COMMON STOCK"},
                        {"url": "https://www.nyse.com/quote/XNYS:TpC", "instrumentName": "AT&T INC DEP SHS PERP PFD SER C"},
                    ]
                if category == "PREFERRED_STOCK":
                    rows = [{"url": "https://www.nyse.com/quote/XNYS:TpC", "instrumentName": "AT&T INC DEP SHS PERP PFD SER C"}]
                source_id = "nyse_" + category.lower()
                payloads[source_id] = ("json", json.dumps(rows).encode())
            for source_id, (media_type, content) in payloads.items():
                snapshot_name = source_id + (".txt" if media_type == "text" else ".json")
                (cache / snapshot_name).write_bytes(content)
                source_specs.append({
                    "source_id": source_id, "snapshot_file": snapshot_name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "media_type": media_type, "fetched_at_utc": "2026-11-08T00:00:00+00:00",
                })
            prior_dir = root / "prior"
            prior_dir.mkdir()
            prior_rows = []
            for ticker in ("TPC", "TpC"):
                prior_rows.append({
                    "listing_id": "1" if ticker == "TPC" else "2", "ticker": ticker,
                    "exchange": "NYSE", "company_name": "Example", "classification_status": "unknown",
                    "issuer_structure": "unknown", "security_form": "unknown", "share_class": "",
                    "manual_review_status": "required", "manual_review_reason": "No evidence.",
                    "evidence_source": "", "classification_rationale": "No evidence.",
                })
            with (prior_dir / "us_security_classification_candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=classifier.CANDIDATE_FIELDS)
                writer.writeheader()
                writer.writerows(prior_rows)
            manifest_path = prior_dir / "source_manifest.json"
            manifest_path.write_text(json.dumps({"cache_directory": str(cache), "sources": source_specs}), encoding="utf-8")
            database = root / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE market_universe (id INTEGER, ticker TEXT, company_name TEXT, exchange TEXT, country TEXT, currency TEXT, security_type TEXT, is_active INTEGER, data_source TEXT, sec_cik TEXT)")
            connection.executemany(
                "INSERT INTO market_universe VALUES (?, ?, ?, 'NYSE', 'USA', 'USD', 'equity', 1, 'test', '0000000001')",
                [(1, "TPC", "Tutor Perini"), (2, "TpC", "AT&T preferred")],
            )
            connection.commit()
            connection.close()

            result = phase3.run_phase3(database, manifest_path, root / "results")
            with result["candidate_csv"].open(encoding="utf-8-sig", newline="") as handle:
                candidates = {row["ticker"]: row for row in csv.DictReader(handle)}
            self.assertEqual(set(candidates), {"TPC", "TpC"})
            self.assertEqual(candidates["TPC"]["security_form"], "common_share")
            self.assertEqual(candidates["TpC"]["security_form"], "preferred_share")
            self.assertTrue(result["manifest"].is_file())
            self.assertTrue(result["mic_reconciliation"].is_file())
            self.assertTrue((root / "results" / "v3_to_phase3_classification_changes.csv").is_file())


if __name__ == "__main__":
    unittest.main()
