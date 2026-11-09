"""Tests for Step 43 — long-term contextual memory.

Temp directories only, no network, no production writes.  The
production DB content fingerprint is asserted unchanged in tearDownClass as a
guard: this module must never touch the database.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import agent.long_term_memory as ltm

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import content_fingerprint  # noqa: E402
DB_PATH = PROJECT_ROOT / "database" / "assistant.db"
class LongTermMemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self.tmp.name) / "memory"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, records):
        saved = []
        for kwargs in records:
            record = ltm.new_record(**kwargs)
            ltm.save_record(record, self.store_dir)
            saved.append(record)
        return saved

    # -- record creation -----------------------------------------------

    def test_new_record_all_types(self):
        for record_type in ltm.RECORD_TYPES:
            record = ltm.new_record(
                record_type, "Some durable text.",
                stated_by="owner", stated_at="2026-11-09T00:00:00+00:00",
                source="turn-1", source_kind="conversation",
                tags=["MSFT", "valuation"])
            self.assertEqual(record["type"], record_type)
            self.assertEqual(record["status"], "active")
            self.assertIsNone(record["superseded_by"])
            self.assertTrue(record["id"])
            self.assertEqual(record["stated_by"], "owner")
            self.assertEqual(record["source"], "turn-1")
            self.assertEqual(record["tags"], ["MSFT", "valuation"])

    def test_new_record_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            ltm.new_record("rumour", "Something.")

    def test_new_record_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            ltm.new_record("fact", "   ")

    def test_new_record_defaults_stated_at(self):
        record = ltm.new_record("fact", "Text.")
        self.assertTrue(record["stated_at"].endswith("+00:00"))

    def test_new_record_tags_normalised(self):
        record = ltm.new_record("fact", "Text.",
                                tags=[" MSFT ", "msft", "Valuation", None])
        self.assertEqual(record["tags"], ["MSFT", "Valuation"])

    # -- storage / round-trip ------------------------------------------

    def test_save_load_round_trip(self):
        record = ltm.new_record(
            "preference", "Prefer text replies.",
            stated_by="owner", source="turn-2",
            tags=["preferences"])
        path = ltm.save_record(record, self.store_dir)
        self.assertTrue(Path(path).exists())
        loaded = ltm.load_record(path)
        self.assertEqual(loaded, record)
        by_id = ltm.load_record_by_id(record["id"], self.store_dir)
        self.assertEqual(by_id["text"], "Prefer text replies.")

    def test_load_record_by_id_missing(self):
        self.assertIsNone(
            ltm.load_record_by_id("no-such-id", self.store_dir))

    def test_index_rebuilt_on_load(self):
        self._seed([
            {"record_type": "fact", "text": "First.",
             "stated_at": "2026-11-08T00:00:00+00:00"},
            {"record_type": "fact", "text": "Second.",
             "stated_at": "2026-11-09T00:00:00+00:00"},
        ])
        index_path = self.store_dir / "index.json"
        self.assertTrue(index_path.exists())
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["record_count"], 2)
        self.assertEqual(index["skipped_files"], [])
        # Newest first in the index.
        self.assertEqual(index["records"][0]["stated_at"],
                         "2026-11-09T00:00:00+00:00")

    def test_empty_store(self):
        store = ltm.load_store(self.store_dir)
        self.assertEqual(store["record_count"], 0)
        self.assertEqual(store["records"], [])
        self.assertEqual(store["skipped"], [])
        self.assertEqual(ltm.list_records(self.store_dir), [])
        self.assertEqual(ltm.recall(self.store_dir), [])
        context = ltm.build_context("MSFT valuation",
                                    memory_dir=self.store_dir)
        self.assertEqual(context["block"], "")
        self.assertEqual(context["record_count"], 0)

    def test_corrupt_record_file_skipped(self):
        self._seed([{"record_type": "fact", "text": "Good record."}])
        (self.store_dir / "broken.json").write_text(
            "{not json", encoding="utf-8")
        (self.store_dir / "wrong-shape.json").write_text(
            json.dumps(["a", "list"]), encoding="utf-8")
        store = ltm.load_store(self.store_dir)
        self.assertEqual(store["record_count"], 1)
        self.assertEqual(sorted(store["skipped"]),
                         ["broken.json", "wrong-shape.json"])
        self.assertEqual(len(ltm.list_records(self.store_dir)), 1)

    # -- recall --------------------------------------------------------

    def test_recall_by_tag(self):
        self._seed([
            {"record_type": "fact", "text": "MSFT is overvalued.",
             "tags": ["MSFT"]},
            {"record_type": "fact", "text": "NVDA is a growth bet.",
             "tags": ["NVDA"]},
        ])
        found = ltm.recall(self.store_dir, tag="msft")
        self.assertEqual(len(found), 1)
        self.assertIn("MSFT", found[0]["text"])

    def test_recall_by_type(self):
        self._seed([
            {"record_type": "fact", "text": "A fact."},
            {"record_type": "preference", "text": "A preference."},
            {"record_type": "decision", "text": "A decision."},
        ])
        found = ltm.recall(self.store_dir, record_type="preference")
        self.assertEqual([r["text"] for r in found], ["A preference."])
        with self.assertRaises(ValueError):
            ltm.recall(self.store_dir, record_type="rumour")

    def test_recall_by_date_range(self):
        self._seed([
            {"record_type": "fact", "text": "Early.",
             "stated_at": "2026-11-01T00:00:00+00:00"},
            {"record_type": "fact", "text": "Middle.",
             "stated_at": "2026-11-05T00:00:00+00:00"},
            {"record_type": "fact", "text": "Late.",
             "stated_at": "2026-11-09T00:00:00+00:00"},
        ])
        found = ltm.recall(self.store_dir, since="2026-11-03",
                           until="2026-11-08")
        self.assertEqual([r["text"] for r in found], ["Middle."])

    def test_recall_by_keyword(self):
        self._seed([
            {"record_type": "fact",
             "text": "The strongest-alert rule needs a 20% discount."},
            {"record_type": "fact", "text": "Unrelated note."},
        ])
        found = ltm.recall(self.store_dir, keyword="STRONGEST-ALERT")
        self.assertEqual(len(found), 1)
        self.assertIn("20%", found[0]["text"])

    def test_recall_newest_first_and_limit(self):
        self._seed([
            {"record_type": "fact", "text": f"Note {n}.",
             "stated_at": f"2026-11-0{n}T00:00:00+00:00"}
            for n in range(1, 6)
        ])
        found = ltm.recall(self.store_dir, limit=2)
        self.assertEqual([r["text"] for r in found],
                         ["Note 5.", "Note 4."])

    def test_recall_filters_combine(self):
        self._seed([
            {"record_type": "fact", "text": "MSFT fact.",
             "tags": ["MSFT"]},
            {"record_type": "preference", "text": "MSFT preference.",
             "tags": ["MSFT"]},
            {"record_type": "fact", "text": "NVDA fact.",
             "tags": ["NVDA"]},
        ])
        found = ltm.recall(self.store_dir, tag="MSFT",
                           record_type="fact")
        self.assertEqual([r["text"] for r in found], ["MSFT fact."])

    # -- supersede -------------------------------------------------------

    def test_supersede_keeps_old_record(self):
        old = self._seed([{
            "record_type": "fact",
            "text": "The alert threshold is 15% below value.",
            "stated_at": "2026-11-01T00:00:00+00:00",
        }])[0]
        replacement = ltm.new_record(
            "fact", "The alert threshold is 20% below value.",
            stated_at="2026-11-05T00:00:00+00:00")
        old_after, new_after = ltm.supersede_record(
            old["id"], replacement, self.store_dir)
        self.assertEqual(old_after["status"], "superseded")
        self.assertEqual(old_after["superseded_by"], new_after["id"])
        # The old file is still on disk and still readable.
        kept = ltm.load_record_by_id(old["id"], self.store_dir)
        self.assertEqual(kept["status"], "superseded")
        self.assertEqual(kept["text"],
                         "The alert threshold is 15% below value.")
        # Default recall excludes the superseded record.
        found = ltm.recall(self.store_dir)
        self.assertEqual([r["text"] for r in found],
                         ["The alert threshold is 20% below value."])
        # Explicit opt-in still finds it.
        found_all = ltm.recall(self.store_dir,
                               include_superseded=True)
        self.assertEqual(len(found_all), 2)

    def test_supersede_unknown_id(self):
        replacement = ltm.new_record("fact", "New text.")
        with self.assertRaises(KeyError):
            ltm.supersede_record("no-such-id", replacement,
                                 self.store_dir)

    def test_supersede_already_superseded(self):
        old = self._seed([{"record_type": "fact",
                           "text": "First."}])[0]
        first = ltm.new_record("fact", "Second.")
        ltm.supersede_record(old["id"], first, self.store_dir)
        third = ltm.new_record("fact", "Third.")
        with self.assertRaises(ValueError):
            ltm.supersede_record(old["id"], third, self.store_dir)

    # -- context blocks --------------------------------------------------

    def test_context_for_ticker(self):
        self._seed([
            {"record_type": "fact",
             "text": "FIXTURE: MSFT tier overvalued at $507.12.",
             "tags": ["MSFT"], "source": "fixture",
             "stated_at": "2026-11-09T00:00:00+00:00"},
            {"record_type": "fact",
             "text": "FIXTURE: NVDA is a growth bet.",
             "tags": ["NVDA"], "source": "fixture",
             "stated_at": "2026-11-09T00:00:00+00:00"},
            {"record_type": "preference",
             "text": "FIXTURE: prefer text replies.",
             "tags": ["preferences"], "source": "fixture",
             "stated_at": "2026-11-09T00:00:00+00:00"},
        ])
        context = ltm.build_context(ticker="MSFT",
                                    memory_dir=self.store_dir)
        self.assertEqual(context["record_count"], 1)
        self.assertIn("MSFT tier overvalued", context["block"])
        self.assertIn("[fact]", context["block"])
        self.assertIn("source: fixture", context["block"])
        self.assertNotIn("NVDA", context["block"])
        self.assertNotIn("text replies", context["block"])

    def test_context_for_question(self):
        self._seed([
            {"record_type": "fact",
             "text": "FIXTURE: the alert rule needs 20% below "
                     "conservative value.",
             "tags": ["alerts"], "source": "fixture",
             "stated_at": "2026-11-09T00:00:00+00:00"},
            {"record_type": "fact",
             "text": "FIXTURE: MSFT tier overvalued.",
             "tags": ["MSFT"], "source": "fixture",
             "stated_at": "2026-11-08T00:00:00+00:00"},
        ])
        context = ltm.build_context(
            "When does a strongest alert fire?",
            memory_dir=self.store_dir)
        self.assertEqual(context["record_count"], 1)
        self.assertIn("20% below conservative value", context["block"])

    def test_context_excludes_superseded(self):
        old = self._seed([{
            "record_type": "fact",
            "text": "FIXTURE: old MSFT note.",
            "tags": ["MSFT"],
            "stated_at": "2026-11-01T00:00:00+00:00",
        }])[0]
        replacement = ltm.new_record(
            "fact", "FIXTURE: new MSFT note.", tags=["MSFT"],
            stated_at="2026-11-09T00:00:00+00:00")
        ltm.supersede_record(old["id"], replacement, self.store_dir)
        context = ltm.build_context(ticker="MSFT",
                                    memory_dir=self.store_dir)
        self.assertEqual(context["record_count"], 1)
        self.assertIn("new MSFT note", context["block"])
        self.assertNotIn("old MSFT note", context["block"])

    def test_context_caps_enforced(self):
        self._seed([
            {"record_type": "fact",
             "text": f"FIXTURE: MSFT note {n}.",
             "tags": ["MSFT"],
             "stated_at": f"2026-11-0{n}T00:00:00+00:00"}
            for n in range(1, 8)
        ])
        context = ltm.build_context(
            ticker="MSFT", memory_dir=self.store_dir,
            max_records=3, max_chars=60)
        self.assertLessEqual(context["record_count"], 3)
        self.assertLessEqual(len(context["block"]), 60)
        self.assertTrue(context["truncated"])

    def test_context_no_query(self):
        self._seed([{"record_type": "fact", "text": "Something.",
                     "tags": ["MSFT"]}])
        context = ltm.build_context(memory_dir=self.store_dir)
        self.assertEqual(context["block"], "")
        self.assertEqual(context["record_count"], 0)

    def test_context_no_substring_match(self):
        # Regression: with substring matching, the token "we" in the
        # question matched inside the word "weak" in an unrelated
        # record.  Matching is whole-word only, so this record must
        # not be pulled in.
        self._seed([{"record_type": "fact",
                     "text": "FIXTURE: the alert rule is weak.",
                     "tags": ["alerts"], "source": "fixture"}])
        context = ltm.build_context("MSFT we",
                                    memory_dir=self.store_dir)
        self.assertEqual(context["record_count"], 0)
        self.assertEqual(context["block"], "")

    def test_context_block_text_only(self):
        self._seed([{"record_type": "fact",
                     "text": "FIXTURE: MSFT note.",
                     "tags": ["MSFT"], "source": "fixture"}])
        block = ltm.context_block(ticker="MSFT",
                                  memory_dir=self.store_dir)
        self.assertIn("FIXTURE: MSFT note.", block)

    # -- store class -----------------------------------------------------

    def test_memory_store_class(self):
        store = ltm.MemoryStore(self.store_dir)
        record = store.add("commitment", "FIXTURE: review MSFT "
                           "after earnings.", tags=["MSFT"],
                           source="fixture")
        self.assertEqual(store.recall(tag="MSFT")[0]["id"],
                         record["id"])
        self.assertEqual(len(store.list()), 1)
        context = store.context(ticker="MSFT")
        self.assertIn("review MSFT after earnings", context["block"])
        self.assertEqual(store.load_store()["record_count"], 1)

    # -- production DB untouched ------------------------------------------

    @classmethod
    def setUpClass(cls):
        cls._prod_fp_before = content_fingerprint(DB_PATH)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(DB_PATH) == cls._prod_fp_before, (
            "production DB changed during Step 43 tests")


if __name__ == "__main__":
    unittest.main()
