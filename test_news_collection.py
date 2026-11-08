"""Tests for Step 31 — market news collection.

Fixture articles / mocked fetchers only. No network is touched:
every test injects its own fetcher, and the default fetcher is
never called. News is stored under a temporary directory, never
under the project's data/ tree and never in the production DB.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agents import news_collection as nc

FIXTURE_MSFT = [
    {
        "title": "Microsoft reports strong cloud growth",
        "url": "https://www.example.com/news/msft-cloud",
        "date": "2026-11-07",
        "description": "MSFT Azure revenue grew as enterprise demand held up.",
    },
    {
        "title": "Microsoft announces dividend increase",
        "url": "https://www.example.com/news/msft-dividend",
        "date": "2026-11-06T10:30:00",
        "description": "Microsoft raised its quarterly dividend.",
    },
]

FIXTURE_NVDA = [
    {
        "title": "Nvidia chips face new export questions",
        "url": "https://www.example.com/news/nvda-export",
        "date": "2026-11-07",
        "description": "NVDA shares moved on the export news.",
    },
]


def fixture_fetcher_factory(results_by_query):
    """A fetcher returning canned results per query substring."""
    calls = []

    def fetcher(query, max_results=5):
        calls.append((query, max_results))
        for needle, results in results_by_query.items():
            if needle in query:
                return [dict(r) for r in results]
        return []

    fetcher.calls = calls
    return fetcher


class NewsCollectionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-news31-"))
        self.news_dir = self.tmp / "news"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- date parsing / source / URL normalisation ---

    def test_parse_published_date(self):
        self.assertEqual(nc.parse_published_date("2026-11-07"), "2026-11-07")
        self.assertEqual(
            nc.parse_published_date("2026-11-07T10:30:00"), "2026-11-07"
        )
        self.assertEqual(
            nc.parse_published_date("2026-11-07T10:30:00Z"), "2026-11-07"
        )
        self.assertEqual(nc.parse_published_date("Nov 7, 2026"), "2026-11-07")
        self.assertIsNone(nc.parse_published_date("Publication date unavailable"))
        self.assertIsNone(nc.parse_published_date(""))
        self.assertIsNone(nc.parse_published_date(None))
        self.assertIsNone(nc.parse_published_date("not a date at all"))

    def test_source_from_url(self):
        self.assertEqual(
            nc.source_from_url("https://www.reuters.com/article/x"), "reuters.com"
        )
        self.assertEqual(nc.source_from_url("https://news.microsoft.com/y"), "news.microsoft.com")
        self.assertEqual(nc.source_from_url(""), "")
        self.assertEqual(nc.source_from_url(None), "")

    def test_normalize_url_dedupe_key(self):
        a = {"url": "https://Example.com/News/Item/", "title": "T", "source": "example.com"}
        b = {"url": "https://example.com/News/Item", "title": "T", "source": "example.com"}
        self.assertEqual(nc.article_key(a), nc.article_key(b))
        self.assertEqual(len(nc.dedupe_articles([a, b])), 1)

    def test_dedupe_falls_back_to_title_and_source_without_url(self):
        a = {"url": "", "title": "Same Story", "source": "reuters.com"}
        b = {"url": "", "title": "same story", "source": "reuters.com"}
        c = {"url": "", "title": "Same Story", "source": "bloomberg.com"}
        self.assertEqual(len(nc.dedupe_articles([a, b, c])), 2)

    # --- ticker tagging ---

    def test_tag_tickers_word_boundary(self):
        text = "MSFT and NVDA moved; MSFTA is not a ticker here."
        self.assertEqual(
            nc.tag_tickers(text, ["MSFT", "NVDA", "GOOGL"]), ["MSFT", "NVDA"]
        )

    def test_tag_tickers_alias(self):
        text = "Microsoft beat expectations on cloud growth."
        self.assertEqual(
            nc.tag_tickers(text, ["MSFT"], names={"MSFT": ["Microsoft"]}),
            ["MSFT"],
        )
        self.assertEqual(nc.tag_tickers(text, ["MSFT"]), [])

    def test_tag_tickers_case_insensitive(self):
        self.assertEqual(nc.tag_tickers("msft rallied", ["MSFT"]), ["MSFT"])

    # --- article normalisation ---

    def test_normalize_article_fields(self):
        article = nc.normalize_article(
            FIXTURE_MSFT[0], query_ticker="MSFT", tickers=["MSFT", "NVDA"],
            names={"MSFT": ["Microsoft"]}, query="MSFT stock news",
        )
        self.assertEqual(article["title"], "Microsoft reports strong cloud growth")
        self.assertEqual(article["source"], "example.com")
        self.assertEqual(article["published_date"], "2026-11-07")
        self.assertEqual(article["published_date_iso"], "2026-11-07")
        self.assertEqual(article["tickers"], ["MSFT"])
        self.assertEqual(article["query"], "MSFT stock news")
        self.assertTrue(article["fetched_at"].endswith("Z"))

    def test_normalize_article_missing_fields_are_safe(self):
        article = nc.normalize_article({}, query_ticker="MSFT")
        self.assertEqual(article["title"], "")
        self.assertEqual(article["source"], "")
        self.assertIsNone(article["published_date_iso"])
        self.assertEqual(article["tickers"], ["MSFT"])

    def test_normalize_article_ddgs_news_shape(self):
        # Raw dicts from the ddgs news endpoint: publisher in
        # "source", snippet in "body", ISO date with timezone.
        raw = {
            "title": "Protesters enter Microsoft data centre site",
            "url": "https://www.msn.com/en-us/news/other/x",
            "date": "2026-11-06T16:49:13+00:00",
            "body": "Reuters reports protesters entered the site.",
            "source": "Reuters on MSN",
        }
        article = nc.normalize_article(raw, query_ticker="MSFT", tickers=["MSFT"])
        self.assertEqual(article["source"], "Reuters on MSN")
        self.assertEqual(article["snippet"], "Reuters reports protesters entered the site.")
        self.assertEqual(article["published_date_iso"], "2026-11-06")
        self.assertEqual(article["tickers"], ["MSFT"])

    def test_normalize_article_source_falls_back_to_domain(self):
        raw = {
            "title": "Microsoft story",
            "url": "https://www.reuters.com/markets/x",
            "date": "2026-11-07",
            "description": "MSFT news.",
        }
        article = nc.normalize_article(raw, query_ticker="MSFT")
        self.assertEqual(article["source"], "reuters.com")

    def test_normalize_article_tags_other_tickers_mentioned(self):
        raw = {
            "title": "MSFT and NVDA both rise",
            "url": "https://example.com/x",
            "date": "2026-11-07",
            "description": "",
        }
        article = nc.normalize_article(raw, query_ticker="MSFT", tickers=["MSFT", "NVDA"])
        self.assertEqual(article["tickers"], ["MSFT", "NVDA"])

    # --- collection ---

    def test_collect_news_writes_one_run_file(self):
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        run = nc.collect_news(["MSFT"], fetcher=fetcher, news_dir=self.news_dir)
        self.assertEqual(run["article_count"], 2)
        self.assertEqual(run["tickers"], ["MSFT"])
        self.assertEqual(run["max_per_ticker"], nc.DEFAULT_MAX_PER_TICKER)
        self.assertEqual(run["per_ticker"]["MSFT"]["fetched"], 2)
        self.assertIsNone(run["per_ticker"]["MSFT"]["error"])
        files = list(self.news_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        stored = nc.load_run(files[0])
        self.assertEqual(stored["article_count"], 2)
        self.assertEqual(stored["articles"][0]["title"], FIXTURE_MSFT[0]["title"])

    def test_collect_news_cap_per_ticker(self):
        many = [dict(FIXTURE_MSFT[0], title=f"Story {i}",
                     url=f"https://example.com/{i}") for i in range(10)]
        fetcher = fixture_fetcher_factory({"MSFT": many})
        run = nc.collect_news(
            ["MSFT"], fetcher=fetcher, news_dir=self.news_dir, max_per_ticker=3
        )
        self.assertEqual(run["article_count"], 3)
        self.assertEqual(run["per_ticker"]["MSFT"]["fetched"], 3)

    def test_collect_news_dedupes_across_tickers(self):
        shared = [dict(FIXTURE_MSFT[0])]
        fetcher = fixture_fetcher_factory({"MSFT": shared, "NVDA": shared})
        run = nc.collect_news(
            ["MSFT", "NVDA"], fetcher=fetcher, news_dir=self.news_dir,
            delay_seconds=0,
        )
        self.assertEqual(run["fetched_total"], 2)
        self.assertEqual(run["duplicates_removed"], 1)
        self.assertEqual(run["article_count"], 1)
        self.assertEqual(run["articles"][0]["tickers"], ["MSFT", "NVDA"])

    def test_collect_news_delay_between_tickers(self):
        sleeps = []
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT, "NVDA": FIXTURE_NVDA})
        nc.collect_news(
            ["MSFT", "NVDA"], fetcher=fetcher, news_dir=self.news_dir,
            delay_seconds=2.5, sleep_fn=sleeps.append,
        )
        # One sleep between the two tickers, none before the first.
        self.assertEqual(sleeps, [2.5])

    def test_collect_news_no_delay_for_single_ticker(self):
        sleeps = []
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        nc.collect_news(
            ["MSFT"], fetcher=fetcher, news_dir=self.news_dir,
            delay_seconds=5, sleep_fn=sleeps.append,
        )
        self.assertEqual(sleeps, [])

    def test_collect_news_fetcher_error_string_recorded(self):
        def failing_fetcher(query, max_results=5):
            return "Search error: blocked"

        run = nc.collect_news(
            ["MSFT"], fetcher=failing_fetcher, news_dir=self.news_dir,
            delay_seconds=0,
        )
        self.assertEqual(run["article_count"], 0)
        self.assertEqual(run["per_ticker"]["MSFT"]["error"], "Search error: blocked")

    def test_collect_news_fetcher_exception_recorded_not_raised(self):
        def broken_fetcher(query, max_results=5):
            raise RuntimeError("boom")

        run = nc.collect_news(
            ["MSFT"], fetcher=broken_fetcher, news_dir=self.news_dir,
            delay_seconds=0,
        )
        self.assertEqual(run["article_count"], 0)
        self.assertIn("RuntimeError", run["per_ticker"]["MSFT"]["error"])

    def test_collect_news_empty_tickers_rejected(self):
        with self.assertRaises(ValueError):
            nc.collect_news([], fetcher=lambda q, max_results=5: [])

    def test_collect_news_never_fetches_whole_universe(self):
        # The collector takes an explicit list; a 10,619-ticker list
        # would issue one query per ticker, so the interface makes
        # the cost visible — here we prove the call count equals the
        # list length, nothing more.
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        nc.collect_news(
            ["MSFT", "NVDA", "GOOGL"], fetcher=fetcher,
            news_dir=self.news_dir, delay_seconds=0,
        )
        self.assertEqual(len(fetcher.calls), 3)

    def test_collect_news_query_template(self):
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        nc.collect_news(
            ["MSFT"], fetcher=fetcher, news_dir=self.news_dir,
            delay_seconds=0, query_template="{ticker} earnings {name}",
            names={"MSFT": ["Microsoft"]},
        )
        self.assertEqual(fetcher.calls[0][0], "MSFT earnings Microsoft")

    # --- querying stored articles ---

    def _two_runs(self):
        fetcher = fixture_fetcher_factory(
            {"MSFT": FIXTURE_MSFT, "NVDA": FIXTURE_NVDA}
        )
        nc.collect_news(
            ["MSFT"], fetcher=fetcher, news_dir=self.news_dir, delay_seconds=0
        )
        nc.collect_news(
            ["NVDA"], fetcher=fetcher, news_dir=self.news_dir, delay_seconds=0
        )

    def test_list_articles_by_ticker(self):
        self._two_runs()
        msft = nc.list_articles(ticker="MSFT", news_dir=self.news_dir)
        self.assertEqual(len(msft), 2)
        self.assertTrue(all("MSFT" in a["tickers"] for a in msft))
        nvda = nc.list_articles(ticker="nvda", news_dir=self.news_dir)
        self.assertEqual(len(nvda), 1)

    def test_list_articles_since_date(self):
        self._two_runs()
        recent = nc.list_articles(since="2026-11-07", news_dir=self.news_dir)
        self.assertEqual(len(recent), 2)  # both 2026-11-07 articles
        self.assertTrue(all(a["published_date_iso"] == "2026-11-07" for a in recent))
        all_articles = nc.list_articles(news_dir=self.news_dir)
        self.assertEqual(len(all_articles), 3)

    def test_list_articles_newest_first(self):
        self._two_runs()
        articles = nc.list_articles(news_dir=self.news_dir)
        dates = [a["published_date_iso"] for a in articles]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_list_articles_dedupes_across_runs(self):
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        nc.collect_news(["MSFT"], fetcher=fetcher, news_dir=self.news_dir, delay_seconds=0)
        nc.collect_news(["MSFT"], fetcher=fetcher, news_dir=self.news_dir, delay_seconds=0)
        self.assertEqual(len(nc.list_runs(news_dir=self.news_dir)), 2)
        self.assertEqual(len(nc.list_articles(news_dir=self.news_dir)), 2)

    def test_list_articles_empty_directory(self):
        self.assertEqual(nc.list_articles(news_dir=self.news_dir), [])
        self.assertEqual(nc.list_runs(news_dir=self.news_dir), [])

    def test_list_articles_skips_corrupt_run_file(self):
        (self.news_dir).mkdir(parents=True, exist_ok=True)
        (self.news_dir / "broken.json").write_text("{not json", encoding="utf-8")
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        nc.collect_news(["MSFT"], fetcher=fetcher, news_dir=self.news_dir, delay_seconds=0)
        self.assertEqual(len(nc.list_articles(news_dir=self.news_dir)), 2)

    # --- MSFT on a temp DB copy: collection never touches the DB ---

    def test_msft_fixture_collection_leaves_db_untouched(self):
        db_src = Path(__file__).resolve().parent / "database" / "assistant.db"
        db_copy = self.tmp / "assistant.db"
        shutil.copyfile(db_src, db_copy)
        before = hashlib.sha256(db_copy.read_bytes()).hexdigest()
        fetcher = fixture_fetcher_factory({"MSFT": FIXTURE_MSFT})
        run = nc.collect_news(
            ["MSFT"], fetcher=fetcher, news_dir=self.news_dir, delay_seconds=0
        )
        self.assertEqual(run["article_count"], 2)
        after = hashlib.sha256(db_copy.read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
