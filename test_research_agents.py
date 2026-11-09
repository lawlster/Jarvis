"""Tests for Step 41 — integrate financial research agents.

Temp DB copies only; no network, no production writes. The
production database content fingerprint is asserted unchanged in tearDownClass.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agent import research_agents as ra

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
RAN_SOURCES = {
    ra.SOURCE_SCAN, ra.SOURCE_VALUATION, ra.SOURCE_REPORT,
    ra.SOURCE_COMPARISON, ra.SOURCE_NEWS, ra.SOURCE_EVENTS,
    ra.SOURCE_CHANGED,
}


def _copy_db(directory):
    path = Path(directory) / "copy.db"
    shutil.copy(PROD_DB, path)
    return str(path)


class ResearchAgentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = temp_dir_with_db_cleanup()
        cls.db_path = _copy_db(cls._tmp.name)
        cls._prod_fp_before = content_fingerprint(PROD_DB)
        cls.news_dir = str(PROJECT_ROOT / "data" / "news")
        cls.events_dir = str(PROJECT_ROOT / "data" / "events")

    @classmethod
    def tearDownClass(cls):
        cleanup_temp_dir(cls._tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, \
            "production DB changed!"

    # -- planning ---------------------------------------------------

    def test_plan_single(self):
        plan = ra.plan_research("Research MSFT", db_path=self.db_path)
        self.assertEqual(plan["kind"], "single")
        self.assertEqual(plan["target"], "MSFT")
        self.assertEqual([s["id"] for s in plan["steps"]],
                         ["scan", "valuation", "report", "news",
                          "events"])
        for step in plan["steps"]:
            self.assertIn("module", step)
            self.assertIn("question", step)
            self.assertIn(step["module"], RAN_SOURCES)

    def test_plan_comparison(self):
        plan = ra.plan_research("Compare MSFT with NVDA",
                                db_path=self.db_path)
        self.assertEqual(plan["kind"], "comparison")
        self.assertEqual(plan["target"], "MSFT")
        self.assertEqual(plan["peers"], ["NVDA"])
        self.assertEqual([s["id"] for s in plan["steps"]],
                         ["scan", "valuation", "comparison", "news",
                          "events"])

    def test_plan_what_changed(self):
        plan = ra.plan_research("What changed for MSFT this week?",
                                db_path=self.db_path)
        self.assertEqual(plan["kind"], "what_changed")
        self.assertEqual([s["id"] for s in plan["steps"]],
                         ["scan", "what_changed", "news", "events"])

    def test_plan_unknown_no_ticker(self):
        plan = ra.plan_research("Tell me about the market",
                                db_path=self.db_path)
        self.assertEqual(plan["kind"], "unknown")
        self.assertEqual(plan["steps"], [])

    def test_plan_empty(self):
        plan = ra.plan_research("", db_path=self.db_path)
        self.assertEqual(plan["kind"], "unknown")
        self.assertEqual(plan["steps"], [])

    # -- single-ticker session on real data -------------------------

    def test_msft_session(self):
        brief = ra.run_research_session(
            "Research MSFT", db_path=self.db_path,
            news_dir=self.news_dir, events_dir=self.events_dir,
            save_dir=False)
        self.assertEqual(brief["kind"], "single")
        # The Step 29 report is partial (no business description
        # stored), so the brief is partial, not complete.
        self.assertEqual(brief["status"], "partial")
        self.assertEqual([s["id"] for s in brief["steps"]],
                         ["scan", "valuation", "report", "news",
                          "events"])
        self.assertTrue(all(s["status"] == "ok"
                            for s in brief["steps"]))
        # Every claim names a module that actually ran.
        for claim in brief["claims"]:
            self.assertIn(claim["source"], RAN_SOURCES)
            self.assertIn(claim["source"],
                          [s["source"] for s in brief["steps"]])
        text = " ".join(c["text"] for c in brief["claims"])
        self.assertIn("tier overvalued", text)
        self.assertIn("quality excellent", text)
        self.assertIn("health strong", text)
        self.assertIn("$78.82", text)  # strongest-alert price
        self.assertIn("$163.30", text)  # DCF base value
        # What would change the view comes from Step 23.
        self.assertTrue(brief["what_would_change_view"])
        self.assertTrue(all(
            item["source"] in (ra.SOURCE_SCAN, ra.SOURCE_VALUATION)
            for item in brief["what_would_change_view"]))
        # The brief's own words contain no recommendation.
        own_words = " ".join(
            c["text"] for c in brief["claims"]
            if c["source"] != ra.SOURCE_NEWS)
        self.assertNotIn("buy", own_words.lower())
        self.assertNotIn("sell", own_words.lower())
        self.assertNotIn("recommend", own_words.lower())

    def test_msft_session_markdown(self):
        brief = ra.run_research_session(
            "Research MSFT", db_path=self.db_path,
            news_dir=self.news_dir, events_dir=self.events_dir,
            save_dir=False)
        md = ra.brief_to_markdown(brief)
        self.assertIn("Research brief: Research MSFT", md)
        self.assertIn("What the evidence says", md)
        self.assertIn("What would change the view", md)
        self.assertIn("Limitations", md)
        self.assertIn("not a recommendation", md)

    # -- early stop on insufficient data ----------------------------

    def test_dbs_session_stops_early(self):
        brief = ra.run_research_session(
            "Research D05", db_path=self.db_path,
            news_dir=self.news_dir, events_dir=self.events_dir,
            save_dir=False)
        self.assertEqual(brief["kind"], "single")
        self.assertEqual(brief["status"], "insufficient_data")
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["scan"]["status"], "insufficient_data")
        for step_id in ("valuation", "report", "news", "events"):
            self.assertEqual(by_id[step_id]["status"],
                             "not_attempted")
            self.assertIn("not attempted", by_id[step_id]["reason"])
        # Nothing is claimed for a ticker with no data.
        self.assertEqual(brief["claims"], [])
        self.assertTrue(brief["limitations"])
        self.assertTrue(brief["what_would_change_view"])

    def test_no_database(self):
        brief = ra.run_research_session(
            "Research MSFT", db_path=None, save_dir=False)
        self.assertEqual(brief["status"], "insufficient_data")
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["scan"]["status"], "insufficient_data")
        self.assertEqual(by_id["news"]["status"], "not_attempted")

    # -- comparison -------------------------------------------------

    def test_msft_vs_nvda_comparison(self):
        brief = ra.run_research_session(
            "Compare MSFT with NVDA", db_path=self.db_path,
            news_dir=self.news_dir, events_dir=self.events_dir,
            save_dir=False)
        self.assertEqual(brief["kind"], "comparison")
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["comparison"]["status"], "ok")
        comparison = by_id["comparison"]["output"]
        # NVDA has no statements: not comparable, never faked.
        self.assertIn("NVDA", comparison["not_comparable_peers"])
        self.assertFalse(comparison["ranking_meaningful"])
        text = " ".join(c["text"] for c in brief["claims"])
        self.assertIn("NVDA: not comparable", text)
        # The target scan still ran and is claimed.
        self.assertIn("MSFT: tier overvalued", text)

    # -- what changed ------------------------------------------------

    def test_msft_what_changed(self):
        brief = ra.run_research_session(
            "What changed for MSFT this week?", db_path=self.db_path,
            news_dir=self.news_dir, events_dir=self.events_dir,
            save_dir=False)
        self.assertEqual(brief["kind"], "what_changed")
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["what_changed"]["status"], "ok")
        changed = by_id["what_changed"]["output"]
        self.assertEqual(changed["snapshot_status"], "complete")
        # First check with no state: everything is new, no
        # changes against a previous snapshot.
        self.assertEqual(changed["change_count"], 0)
        self.assertEqual(changed["new_article_count"], 10)

    # -- guardrails and unknown --------------------------------------

    def test_trade_question_refused(self):
        brief = ra.run_research_session(
            "Buy 100 shares of MSFT at $500", db_path=self.db_path,
            save_dir=False)
        self.assertEqual(brief["status"], "refused")
        self.assertEqual(brief["steps"], [])
        self.assertEqual(brief["claims"], [])

    def test_unknown_question(self):
        brief = ra.run_research_session(
            "Tell me about the market", db_path=self.db_path,
            save_dir=False)
        self.assertEqual(brief["status"], "unknown")
        self.assertEqual(brief["steps"], [])
        self.assertEqual(brief["claims"], [])

    # -- saving -------------------------------------------------------

    def test_write_session_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = ra.run_research_session(
                "Research MSFT", db_path=self.db_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                save_dir=tmp)
            files = brief["files"]
            self.assertTrue(Path(files["json"]).exists())
            self.assertTrue(Path(files["markdown"]).exists())
            loaded = json.loads(Path(files["json"]).read_text())
            self.assertEqual(loaded["question"], "Research MSFT")
            self.assertEqual(loaded["status"], brief["status"])
            self.assertEqual(len(loaded["claims"]),
                             len(brief["claims"]))
            md = Path(files["markdown"]).read_text()
            self.assertIn("Research brief: Research MSFT", md)

    def test_brief_json_serialisable(self):
        brief = ra.run_research_session(
            "Research MSFT", db_path=self.db_path,
            news_dir=self.news_dir, events_dir=self.events_dir,
            save_dir=False)
        json.dumps(brief)  # must not raise


if __name__ == "__main__":
    unittest.main()
