import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agents import us_issuer_enrichment as mod


def metadata(cik, retrieved_at="2026-11-07T00:00:00+00:00"):
    return {
        "cik": cik,
        "retrieved_at_utc": retrieved_at,
        "sec_company_name": "Issuer",
        "sic": "1",
        "sic_description": "Test",
        "entity_type": "",
        "tickers": [],
        "exchanges": [],
        "fiscal_year_end": "",
        "state_of_incorporation": "",
        "state_of_incorporation_description": "",
        "former_names": [],
        "filing_history": {"recent": {}, "files": []},
    }


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "production.sqlite"
        self.production = self.root / "production"
        self.production.mkdir()
        self.ciks = [f"{value:010d}" for value in range(1, 22)]
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE market_universe "
            "(ticker TEXT, company_name TEXT, exchange TEXT, sec_cik TEXT, country TEXT)"
        )
        connection.executemany(
            "INSERT INTO market_universe VALUES (?, ?, ?, ?, 'USA')",
            [
                (f"T{index}", f"Company {index}", "TEST", cik)
                for index, cik in enumerate(self.ciks, 1)
            ],
        )
        connection.commit()
        connection.close()

        self.production_cache = self.production / "sec_issuer_enrichment_cache.json"
        self.production_cache.write_bytes(
            json.dumps({self.ciks[20]: metadata(self.ciks[20])}).encode("utf-8")
        )
        self.database_before = self.database.read_bytes()
        self.cache_before = self.production_cache.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def production_path_patches(self):
        return (
            patch.object(mod, "OUTPUT_DIR", self.production),
            patch.object(mod, "CACHE_PATH", self.production_cache),
            patch.object(mod, "JOURNAL_DIR", self.production / "sec_issuer_enrichment_journal"),
            patch.object(mod, "CSV_PATH", self.production / "sec_issuer_enrichment.csv"),
            patch.object(mod, "REPORT_PATH", self.production / "sec_issuer_enrichment_report.txt"),
            patch.object(mod, "COVERAGE_PATH", self.production / "sec_issuer_enrichment_coverage.json"),
        )

    def test_cik_validation(self):
        with patch.object(mod, "DATABASE_PATH", self.database):
            with self.assertRaises(ValueError):
                mod.validate_pilot_ciks(self.ciks[:19])
            with self.assertRaises(ValueError):
                mod.validate_pilot_ciks(self.ciks)
            with self.assertRaises(ValueError):
                mod.validate_pilot_ciks(self.ciks[:19] + [self.ciks[0]])
            with self.assertRaises(ValueError):
                mod.validate_pilot_ciks(self.ciks[:19] + ["9999999999"])
            selected = mod.validate_pilot_ciks(self.ciks[:20])
        self.assertEqual(selected, set(self.ciks[:20]))

    def test_actual_path_resolver_accepts_isolated_and_rejects_overlap(self):
        pilot_dir = self.root / "pilot"
        with ExitStack() as stack:
            stack.enter_context(patch.object(mod, "DATABASE_PATH", self.database))
            for path_patch in self.production_path_patches():
                stack.enter_context(path_patch)
            paths = mod.pilot_paths(pilot_dir)
            self.assertEqual(paths["output"], pilot_dir.resolve())
            for overlapping in (self.production, self.production / "nested", self.root):
                with self.subTest(path=overlapping), self.assertRaises(ValueError):
                    mod.pilot_paths(overlapping)

    def test_mocked_pilot_requests_exact_allowlist_and_preserves_production(self):
        pilot_ciks = set(self.ciks[:20])
        pilot_dir = self.root / "pilot"
        original_connect = sqlite3.connect
        connect_calls = []
        requested_ciks = []

        def observed_connect(*args, **kwargs):
            connect_calls.append((args, kwargs))
            return original_connect(*args, **kwargs)

        def mocked_sec(cik, session):
            requested_ciks.append(cik)
            return {"success": True, "data": {"name": "Mock issuer", "filings": {}}}

        with ExitStack() as stack:
            stack.enter_context(patch.object(mod, "DATABASE_PATH", self.database))
            for path_patch in self.production_path_patches():
                stack.enter_context(path_patch)
            stack.enter_context(patch.object(mod, "SEC_USER_AGENT", "test-agent"))
            stack.enter_context(
                patch.object(mod.sqlite3, "connect", side_effect=observed_connect)
            )
            stack.enter_context(
                patch.object(mod, "fetch_sec_submission", side_effect=mocked_sec)
            )
            stack.enter_context(patch.object(mod.time, "sleep"))
            paths = mod.pilot_paths(pilot_dir)
            paths["output"].mkdir(parents=True)
            # Out-of-pilot cache and journal records are present in the pilot
            # workspace to prove they cannot enter replay, coverage, or exports.
            paths["cache"].write_text(
                json.dumps({self.ciks[20]: metadata(self.ciks[20])}), encoding="utf-8"
            )
            paths["journal"].mkdir(parents=True)
            outside_journal = metadata(self.ciks[20])
            outside_journal.update(
                database_tickers=["T21"],
                database_company_names=["Company 21"],
                database_exchanges=["TEST"],
            )
            (paths["journal"] / f"{self.ciks[20]}.json").write_text(
                json.dumps(outside_journal), encoding="utf-8"
            )

            mod.run_enrichment(pilot_ciks, pilot_dir)

        self.assertEqual(set(requested_ciks), pilot_ciks)
        self.assertEqual(len(requested_ciks), 20)
        self.assertEqual(len(connect_calls), 1)
        database_uri = connect_calls[0][0][0]
        self.assertIn("mode=ro", database_uri)
        self.assertTrue(connect_calls[0][1].get("uri"))

        with paths["csv"].open("r", encoding="utf-8-sig", newline="") as csv_file:
            exported_ciks = {row["cik"] for row in csv.DictReader(csv_file)}
        self.assertEqual(exported_ciks, pilot_ciks)
        coverage = json.loads(paths["coverage"].read_text(encoding="utf-8"))
        self.assertEqual(coverage["database_unique_ciks"], 20)
        self.assertEqual(coverage["cache_record_count"], 20)
        self.assertEqual(coverage["extra_cache_ciks"], [])
        report = paths["report"].read_text(encoding="utf-8")
        self.assertNotIn(self.ciks[20], report)
        self.assertEqual(self.database.read_bytes(), self.database_before)
        self.assertEqual(self.production_cache.read_bytes(), self.cache_before)
        self.assertFalse((self.production / "sec_issuer_enrichment.csv").exists())


if __name__ == "__main__":
    unittest.main()
