"""Tests for Step 42 — general-purpose web research.

Stubbed fetchers only: the suite never touches the network.
The one live smoke test is behind JARVIS_LIVE_WEB_RESEARCH=1
and is skipped by default, so `unittest discover` stays
offline.
"""

import json
import os
import tempfile
import unittest

from agent import web_research as wr


def _stub_text(results, error=None):
    def fetcher(query, max_results=5):
        return list(results)[:max_results], error
    return fetcher


def _stub_news(results, error=None):
    def fetcher(query, max_results=5):
        return list(results)[:max_results], error
    return fetcher


TEXT_RESULTS = [
    {"title": "US CPI rose 3.0% in September",
     "url": "https://www.bls.gov/news/cpi.htm",
     "date": "2026-10-24",
     "description": "The consumer price index rose 3.0 percent over the year."},
    {"title": "Inflation cools to 2.9%",
     "url": "https://example.com/inflation-cools",
     "date": "2026-10-25",
     "description": "Annual inflation eased to 2.9 percent, below forecasts."},
]

NEWS_RESULTS = [
    {"title": "Microsoft shares rise after earnings beat",
     "url": "https://www.reuters.com/msft-earnings",
     "date": "2026-11-06",
     "description": "Microsoft reported quarterly revenue above expectations.",
     "source": "Reuters"},
    {"title": "Microsoft cloud growth slows",
     "url": "https://example.com/msft-cloud",
     "date": "2026-11-05",
     "description": "Azure growth came in below the prior quarter."},
]


class TestClassifyAndPlan(unittest.TestCase):
    def test_news_question_plans_news_then_text(self):
        plan = wr.plan_research("latest news on MSFT")
        self.assertEqual(plan["kind"], "news")
        self.assertEqual(plan["topic"], "MSFT")
        self.assertEqual([s["kind"] for s in plan["searches"]],
                         ["news", "text"])
        self.assertTrue(all(s["query"] for s in plan["searches"]))

    def test_fact_question_plans_two_text_searches(self):
        plan = wr.plan_research("what is the current US CPI?")
        self.assertEqual(plan["kind"], "fact")
        self.assertEqual([s["kind"] for s in plan["searches"]],
                         ["text", "text"])
        self.assertIn("latest", plan["searches"][1]["query"])

    def test_explainer_question_plans_one_text_search(self):
        plan = wr.plan_research("explain how a DCF works")
        self.assertEqual(plan["kind"], "explainer")
        self.assertEqual(len(plan["searches"]), 1)

    def test_general_question_plans_one_text_search(self):
        plan = wr.plan_research("who won the 2026 World Series?")
        self.assertEqual(plan["kind"], "general")
        self.assertEqual(len(plan["searches"]), 1)

    def test_empty_question_is_general(self):
        self.assertEqual(wr.classify_question(""), "general")


class TestResearchWithStubs(unittest.TestCase):
    def test_fact_research_returns_sourced_claims(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(len(result["claims"]), 2)
        for claim in result["claims"]:
            self.assertTrue(claim["url"])
            self.assertTrue(claim["source"])
            self.assertIn(str(claim["source_number"]), result["answer_text"])
        self.assertEqual(result["requests_made"], 2)

    def test_news_research_uses_news_fetcher_first(self):
        seen = []

        def news_fetcher(query, max_results=5):
            seen.append(("news", query))
            return list(NEWS_RESULTS), None

        def text_fetcher(query, max_results=5):
            seen.append(("text", query))
            return [], None

        result = wr.research("latest news on MSFT",
                             text_fetcher=text_fetcher,
                             news_fetcher=news_fetcher,
                             rate_limit_seconds=0)
        self.assertEqual(seen[0][0], "news")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["findings"][0]["source"], "Reuters")

    def test_per_query_cap_is_enforced(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS * 3),
            news_fetcher=_stub_news([]),
            per_query_cap=2, rate_limit_seconds=0)
        self.assertTrue(all(r["results_returned"] <= 2
                            for r in result["searches"]))
        self.assertEqual(len(result["findings"]), 2)

    def test_session_cap_truncates_plan(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            session_cap=1, rate_limit_seconds=0)
        self.assertEqual(result["requests_made"], 1)
        self.assertEqual(result["truncated_searches"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("truncated" in lim
                            for lim in result["limitations"]))

    def test_rate_limit_delay_is_recorded(self):
        sleeps = []

        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=2.5,
            sleep_fn=lambda seconds: sleeps.append(seconds))
        self.assertEqual(sleeps, [2.5])
        self.assertEqual(result["sleeps_between_requests"], 1)
        self.assertEqual(result["rate_limit_seconds"], 2.5)

    def test_dedupe_by_url(self):
        dup = [dict(TEXT_RESULTS[0]), dict(TEXT_RESULTS[0])]
        dup[1]["url"] = "https://www.bls.gov/news/cpi.htm?x=1"
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(dup),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        # Both searches return the same article; one copy kept.
        self.assertEqual(len(result["findings"]), 1)

    def test_allowlist_filters_and_counts(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            allowed_sources=["bls.gov"],
            rate_limit_seconds=0)
        self.assertEqual(len(result["findings"]), 1)
        # The stub returns the same two results for both planned
        # searches, so the allowlist drops the example.com copy twice.
        self.assertEqual(result["dropped_by_allowlist"], 2)
        self.assertTrue(any("allowlist" in lim
                            for lim in result["limitations"]))

    def test_no_sources_means_no_answer(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text([]),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(result["status"], "no_sources")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["answer_text"], "")
        self.assertTrue(any("no answer" in lim or "unanswered" in lim
                            for lim in result["limitations"]))

    def test_failed_search_is_recorded_not_raised(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text([], error="Search error: boom"),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(r["status"] == "failed"
                            for r in result["searches"]))
        self.assertTrue(any("failed" in lim
                            for lim in result["limitations"]))

    def test_fetcher_exception_is_recorded_not_raised(self):
        def broken(query, max_results=5):
            raise RuntimeError("network down")

        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=broken,
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(r["status"] == "failed"
                            for r in result["searches"]))

    def test_finding_without_url_is_dropped(self):
        bad = [{"title": "No URL here", "description": "cannot be cited"}]
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(bad),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "no_sources")

    def test_disagreement_flagged_for_fact_question(self):
        # 3.0% vs 2.9% for the same unit: flagged, labelled heuristic.
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(len(result["disagreements"]), 1)
        item = result["disagreements"][0]
        self.assertEqual(item["unit"], "%")
        self.assertEqual(sorted(v["value"] for v in item["values"]),
                         [2.9, 3.0])
        self.assertIn("heuristic", item["note"])
        self.assertTrue(any("disagree" in lim
                            for lim in result["limitations"]))

    def test_no_disagreement_when_sources_agree(self):
        agree = [dict(TEXT_RESULTS[0]), dict(TEXT_RESULTS[0])]
        agree[1]["url"] = "https://example.com/cpi-same"
        agree[1]["description"] = "The index rose 3.0 percent."
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(agree),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertEqual(result["disagreements"], [])

    def test_disagreement_not_checked_for_news_questions(self):
        result = wr.research(
            "latest news on MSFT",
            text_fetcher=_stub_text([]),
            news_fetcher=_stub_news(NEWS_RESULTS),
            rate_limit_seconds=0)
        self.assertEqual(result["disagreements"], [])

    def test_freshness_limitation_uses_published_dates(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        self.assertTrue(any("2026-10-25" in lim
                            for lim in result["limitations"]))

    def test_markdown_renders_sources_and_limitations(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        md = wr.research_to_markdown(result)
        self.assertIn("## Sources", md)
        self.assertIn("https://www.bls.gov/news/cpi.htm", md)
        self.assertIn("## Limitations", md)
        self.assertIn("[1]", md)
        self.assertIn("## Disagreements", md)

    def test_module_adds_no_recommendation_words(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        md = wr.research_to_markdown(result).lower()
        self.assertNotIn("you should buy", md)
        self.assertNotIn("you should sell", md)

    def test_save_round_trip(self):
        result = wr.research(
            "what is the current US CPI?",
            text_fetcher=_stub_text(TEXT_RESULTS),
            news_fetcher=_stub_news([]),
            rate_limit_seconds=0)
        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = wr.save_research(result, research_dir=tmp)
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(md_path))
            with open(json_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["question"], result["question"])
            self.assertEqual(len(loaded["findings"]), 2)


class TestNormalizeFinding(unittest.TestCase):
    def test_source_defaults_to_domain(self):
        finding = wr.normalize_finding(
            {"title": "T", "url": "https://www.bls.gov/x",
             "description": "d"})
        # Step 31's source_from_url strips a leading "www.".
        self.assertEqual(finding["source"], "bls.gov")

    def test_missing_fields_become_empty(self):
        finding = wr.normalize_finding({})
        self.assertEqual(finding["title"], "")
        self.assertEqual(finding["url"], "")
        self.assertIsNone(finding["published_date_iso"])


@unittest.skipUnless(
    os.environ.get("JARVIS_LIVE_WEB_RESEARCH") == "1",
    "live smoke test: set JARVIS_LIVE_WEB_RESEARCH=1 to run")
class TestLiveSmoke(unittest.TestCase):
    def test_live_news_search_returns_sourced_findings(self):
        result = wr.research("latest news on MSFT", rate_limit_seconds=1.0)
        self.assertIn(result["status"], ("ok", "partial"))
        if result["status"] == "ok":
            self.assertTrue(result["findings"])
            for finding in result["findings"]:
                self.assertTrue(finding["url"])
                self.assertTrue(finding["source"])


if __name__ == "__main__":
    unittest.main()
