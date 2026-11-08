"""Tests for Step 32 — agents/daily_summary.py.

Fixture data / temporary database copies only; no network
requests, no production database writes.

Fixture expectations (from Steps 25/26, probe-verified there):
- FIXA (MID_SPEC) at $5.00 -> strongest_alert candidate
  (conservative $13.39, margin 62.6%, quality good).
- FIXB (MID_SPEC) at $25.00 -> fairly_valued, not a candidate.
- FIXW (CHEAP_WEAK_SPEC) at $0.30 -> value trap (conservative
  $0.50, margin 39.7%, quality weak, health weak).
- MSFT on a temp copy of the real DB: tier overvalued, quality
  excellent, health strong; 2026-11-08 is a Sunday; the stored
  MSFT news files hold 10 items (5 hub pages with no date, 5
  dated articles 2026-04-22 to 2026-11-05), none published on
  2026-11-08.
"""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import test_business_quality as tbq
import test_market_scanner as tms
import test_undervalued_detector as tud
from agents import daily_summary as ds

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "assistant.db"
NEWS_DIR = BASE_DIR / "data" / "news"


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_news_dir():
    """Temp news dir with one run file of fixture articles."""
    directory = Path(tempfile.mkdtemp())
    run = {
        "run_id": "fixture",
        "started_at": "2026-11-08T00:00:00Z",
        "tickers": ["FIXA", "FIXB"],
        "articles": [
            {
                "title": "FIXA reports record quarter",
                "source": "Fixture News",
                "published_date": "2026-11-08",
                "published_date_iso": "2026-11-08",
                "url": "https://example.com/fixa-record",
                "snippet": "FIXA revenue up.",
                "tickers": ["FIXA"],
                "fetched_at": "2026-11-08T00:00:00Z",
                "query": "FIXA stock news",
            },
            {
                "title": "FIXB steady as expected",
                "source": "Fixture News",
                "published_date": "2026-11-07",
                "published_date_iso": "2026-11-07",
                "url": "https://example.com/fixb-steady",
                "snippet": "FIXB unchanged.",
                "tickers": ["FIXB"],
                "fetched_at": "2026-11-08T00:00:00Z",
                "query": "FIXB stock news",
            },
        ],
    }
    (directory / "20261108_fixture.json").write_text(
        json.dumps(run), encoding="utf-8")
    return directory


class TestFixtureSummary(unittest.TestCase):
    def test_headline_counts_and_candidate_lines(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA", "FIXB", "FIXW", "NONE"],
            prices={"FIXA": 5.0, "FIXB": 25.0, "FIXW": 0.30},
            news_dir=_fixture_news_dir())
        self.assertEqual(summary["coverage"]["selected"], 4)
        self.assertEqual(summary["coverage"]["scannable"], 3)
        self.assertEqual(len(summary["candidates"]), 1)
        self.assertEqual(summary["candidates"][0]["ticker"], "FIXA")
        self.assertEqual(
            summary["candidates"][0]["detection_band"],
            "strongest_alert")
        self.assertEqual(len(summary["value_traps"]), 1)
        self.assertEqual(summary["value_traps"][0]["ticker"], "FIXW")
        self.assertIn("1 candidate(s)", summary["headline"])
        self.assertIn("1 strongest alert", summary["headline"])
        self.assertIn("1 value trap(s)", summary["headline"])
        self.assertIn("FIXA", summary["candidate_lines"][0])
        self.assertIn("STRONGEST ALERT",
                      summary["candidate_lines"][0])
        self.assertIn("FIXW", summary["trap_lines"][0])
        # No buy/sell language in the generated summary.
        markdown = ds.summary_to_markdown(summary)
        self.assertNotIn("buy", markdown.lower())
        self.assertNotIn("sell", markdown.lower())

    def test_movers_ranked_and_news_reason_only_when_cited(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA", "FIXB", "FIXW"],
            prices={"FIXA": 5.0, "FIXB": 25.0, "FIXW": 0.30},
            previous_prices={"FIXA": 4.0, "FIXB": 30.0,
                             "FIXW": 0.30},
            news_dir=_fixture_news_dir())
        movers = summary["movers"]
        self.assertEqual([m["ticker"] for m in movers],
                         ["FIXA", "FIXB", "FIXW"])
        self.assertAlmostEqual(movers[0]["change_pct"], 0.25)
        self.assertAlmostEqual(movers[1]["change_pct"], -1 / 6)
        self.assertAlmostEqual(movers[2]["change_pct"], 0.0)
        # FIXA has a stored article: the reason cites it.
        self.assertIn("FIXA reports record quarter",
                      movers[0]["reason"])
        self.assertEqual(
            movers[0]["reason_article"]["url"],
            "https://example.com/fixa-record")
        # FIXB also has a stored article (dated 2026-11-07).
        self.assertIn("FIXB steady as expected",
                      movers[1]["reason"])
        markdown = ds.summary_to_markdown(summary)
        self.assertIn("FIXA: $4.00 -> $5.00 (25.0%)", markdown)
        self.assertIn("FIXB: $30.00 -> $25.00 (-16.7%)", markdown)

    def test_movers_without_previous_prices_say_so(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA"], prices={"FIXA": 5.0},
            news_dir=_fixture_news_dir())
        self.assertEqual(summary["movers"], [])
        self.assertFalse(summary["movers_available"])
        markdown = ds.summary_to_markdown(summary)
        self.assertIn("No price changes to compare", markdown)

    def test_mover_without_news_has_no_reason(self):
        conn = tud._market_conn()
        empty_news = Path(tempfile.mkdtemp())
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA"], prices={"FIXA": 5.0},
            previous_prices={"FIXA": 4.0}, news_dir=empty_news)
        self.assertEqual(len(summary["movers"]), 1)
        self.assertIsNone(summary["movers"][0]["reason"])
        markdown = ds.summary_to_markdown(summary)
        self.assertIn("no stored news article for this ticker",
                      markdown)

    def test_news_digest_groups_by_date_and_ticker(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA", "FIXB"], prices={"FIXA": 5.0},
            news_dir=_fixture_news_dir())
        digest = summary["news_digest"]
        self.assertEqual(digest["for_date_count"], 1)
        self.assertIn("FIXA", digest["for_date"])
        self.assertNotIn("FIXB", digest["for_date"])
        self.assertEqual(len(digest["latest_stored"]), 2)
        markdown = ds.summary_to_markdown(summary)
        self.assertIn("FIXA — published 2026-11-08", markdown)
        self.assertIn("FIXA reports record quarter", markdown)
        self.assertIn("Latest stored articles", markdown)

    def test_watchlist_notes_carry_tier_quality_deterioration(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA", "FIXB", "FIXW", "NONE"],
            prices={"FIXA": 5.0, "FIXB": 25.0, "FIXW": 0.30},
            watchlist=["FIXA", "FIXW", "NONE", "MISSING"],
            news_dir=_fixture_news_dir())
        notes = {n["ticker"]: n for n in summary["watchlist_notes"]}
        self.assertIn("tier strongest_alert", notes["FIXA"]["note"])
        self.assertIn("quality good", notes["FIXA"]["note"])
        self.assertIn("deterioration: improving",
                      notes["FIXA"]["note"])
        self.assertIn("insufficient data", notes["NONE"]["note"])
        self.assertIn("not in this summary's scan selection",
                      notes["MISSING"]["note"])

    def test_upcoming_items_listed_as_given(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08", tickers=["FIXA"],
            prices={"FIXA": 5.0},
            upcoming=[{"date": "2026-11-10",
                       "item": "US CPI release"},
                      "FIXA earnings (date not confirmed)"],
            news_dir=_fixture_news_dir())
        markdown = ds.summary_to_markdown(summary)
        self.assertIn("- 2026-11-10: US CPI release", markdown)
        self.assertIn("- FIXA earnings (date not confirmed)", markdown)

    def test_empty_selection_degrades_gracefully(self):
        conn = tms._fixture_conn([], [])
        summary = ds.generate_summary(
            conn, date="2026-11-09", tickers=[],
            news_dir=_fixture_news_dir())
        self.assertEqual(summary["coverage"]["selected"], 0)
        self.assertEqual(summary["candidates"], [])
        self.assertIn("0 tickers scanned", summary["headline"])
        markdown = ds.summary_to_markdown(summary)
        self.assertIn("No candidate was found", markdown)

    def test_markdown_write_read_round_trip(self):
        conn = tud._market_conn()
        summary = ds.generate_summary(
            conn, date="2026-11-08",
            tickers=["FIXA"], prices={"FIXA": 5.0},
            news_dir=_fixture_news_dir())
        directory = Path(tempfile.mkdtemp())
        path = ds.write_summary(summary, summaries_dir=directory)
        self.assertTrue(Path(path).exists())
        self.assertEqual(Path(path).read_text(encoding="utf-8"),
                         ds.summary_to_markdown(summary))
        # A second write for the same date gets a _2 suffix.
        path2 = ds.write_summary(summary, summaries_dir=directory)
        self.assertNotEqual(path, path2)
        self.assertTrue(path2.endswith("_2.md"))


class TestRealSummary(unittest.TestCase):
    def test_msft_2026_11_08(self):
        db_path = _temp_db_copy()
        try:
            before = _sha256(db_path)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                summary = ds.generate_summary(
                    conn, date="2026-11-08",
                    watchlist=["MSFT", "D05"],
                    news_dir=NEWS_DIR)
            finally:
                conn.close()
            self.assertEqual(_sha256(db_path), before)
            coverage = summary["coverage"]
            self.assertEqual(coverage["selected"], 10619)
            self.assertEqual(coverage["scannable"], 1)
            self.assertEqual(summary["candidates"], [])
            self.assertEqual(summary["value_traps"], [])
            self.assertIn("0 candidate(s)", summary["headline"])
            self.assertIn("0 value trap(s)", summary["headline"])
            self.assertEqual(summary["weekday"], "Sunday")
            self.assertIn("closed (weekend)",
                          summary["weekend_note"])
            notes = {n["ticker"]: n
                     for n in summary["watchlist_notes"]}
            self.assertIn("tier overvalued", notes["MSFT"]["note"])
            self.assertIn("quality excellent", notes["MSFT"]["note"])
            self.assertIn("deterioration: warning",
                          notes["MSFT"]["note"])
            self.assertIn("$507.12", notes["MSFT"]["note"])
            self.assertIn("insufficient data", notes["D05"]["note"])
            # No stored article was published on 2026-11-08; the
            # latest stored articles are shown instead, labelled.
            digest = summary["news_digest"]
            self.assertEqual(digest["for_date_count"], 0)
            self.assertEqual(len(digest["latest_stored"]), 5)
            self.assertEqual(
                digest["latest_stored"][0]["published_date_iso"],
                "2026-11-05")
            # No previous prices supplied: no movers.
            self.assertFalse(summary["movers_available"])
            markdown = ds.summary_to_markdown(summary)
            self.assertIn("No price changes to compare", markdown)
            self.assertIn("No stored article was published on "
                          "2026-11-08", markdown)
            self.assertIn("Latest stored articles", markdown)
            # No recommendation language in the summary's own
            # words (article titles are quoted verbatim and may
            # contain the word "Buy").
            self.assertNotIn("buy recommendation", markdown.lower())
            self.assertNotIn("sell", markdown.lower())
        finally:
            Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
