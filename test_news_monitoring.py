"""Step 34 — Company news and event monitoring: tests.

Fixture news/events and temp directories only; no network, no
production writes. Every test cleans up after itself via
TemporaryDirectory (the /tmp housekeeping gap noted in Steps
27/33b: older _temp_db_copy helpers never delete their copies).
The production DB is copied once per test class into the temp
directory and the copy is removed in tearDownClass; the
production hash is asserted unchanged.
"""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import event_tracking as et
from agents import news_monitoring as nm
from agents import thesis_tracking as tt

PROJECT_ROOT = Path(__file__).resolve().parent
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"


def _db_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _article(title, url, iso=None, fetched_at="2026-11-08T10:00:00Z",
             source="Fixture News", tickers=("MSFT",)):
    return {
        "title": title,
        "source": source,
        "published_date": iso or "",
        "published_date_iso": iso,
        "url": url,
        "snippet": "fixture snippet",
        "tickers": list(tickers),
        "fetched_at": fetched_at,
        "query": "MSFT stock news",
    }


def _write_run(news_dir, run_id, articles):
    path = Path(news_dir) / f"{run_id}.json"
    path.write_text(json.dumps({
        "run_id": run_id, "started_at": "2026-11-08T10:00:00Z",
        "tickers": ["MSFT"], "articles": articles,
        "article_count": len(articles),
    }), encoding="utf-8")
    return path


class _TempDbTestCase(unittest.TestCase):
    """A temp DB copy shared by the class; removed afterwards."""

    db_path = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="step34-")
        cls.db_path = Path(cls.tmp.name) / "assistant.db"
        shutil.copy(PROD_DB, cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        assert _db_hash(PROD_DB) == PROD_HASH, \
            "production DB changed during tests"

    def _connect(self):
        return sqlite3.connect(self.db_path)


class TestArticleNewness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="step34-news-")
        self.news_dir = Path(self.tmp.name) / "news"
        self.news_dir.mkdir()
        self.articles = [
            _article("Old story", "https://example.com/a",
                     iso="2026-11-05", fetched_at="2026-11-06T09:00:00Z"),
            _article("New story", "https://example.com/b",
                     iso="2026-11-08", fetched_at="2026-11-08T10:00:00Z"),
        ]
        _write_run(self.news_dir, "20261108_100000_MSFT", self.articles)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_run_reports_everything(self):
        fresh, all_articles = nm.new_articles("MSFT", None,
                                              news_dir=self.news_dir)
        self.assertEqual(len(all_articles), 2)
        self.assertEqual(len(fresh), 2)

    def test_seen_articles_are_not_new_again(self):
        state = {"seen_article_keys":
                 [nm.article_key(a) for a in self.articles],
                 "last_checked_at": None}
        fresh, _ = nm.new_articles("MSFT", state, news_dir=self.news_dir)
        self.assertEqual(fresh, [])

    def test_only_unseen_article_is_new(self):
        state = {"seen_article_keys":
                 [nm.article_key(self.articles[0])],
                 "last_checked_at": None}
        fresh, _ = nm.new_articles("MSFT", state, news_dir=self.news_dir)
        self.assertEqual([a["title"] for a in fresh], ["New story"])

    def test_first_run_with_last_checked_filters_by_age(self):
        state = {"seen_article_keys": [],
                 "last_checked_at": "2026-11-07T00:00:00Z"}
        fresh, _ = nm.new_articles("MSFT", state, news_dir=self.news_dir)
        self.assertEqual([a["title"] for a in fresh], ["New story"])

    def test_article_key_is_stable_across_url_spelling(self):
        first = _article("T", "https://Example.com/A/")
        second = _article("T", "https://example.com/A")
        self.assertEqual(nm.article_key(first), nm.article_key(second))

    def test_no_stored_news_is_empty_not_an_error(self):
        fresh, all_articles = nm.new_articles(
            "ZZZZ", None, news_dir=self.news_dir)
        self.assertEqual(fresh, [])
        self.assertEqual(all_articles, [])


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="step34-ev-")
        self.events_dir = Path(self.tmp.name) / "events"
        et.add_event(events_dir=self.events_dir, ticker="MSFT",
                     event_type="earnings", date="2026-11-12",
                     title="MSFT earnings",
                     source="fixture")
        et.add_event(events_dir=self.events_dir, ticker="MSFT",
                     event_type="dividend", date="2026-11-05",
                     title="MSFT dividend",
                     source="fixture")
        et.add_event(events_dir=self.events_dir, ticker=None,
                     event_type="economic", date="2026-11-10",
                     title="US Oct CPI",
                     source="fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def test_upcoming_events_for_ticker(self):
        upcoming = nm.upcoming_events_for_ticker(
            "MSFT", self.events_dir, as_of="2026-11-09", days=7)
        self.assertEqual([e["title"] for e in upcoming],
                         ["MSFT earnings"])

    def test_upcoming_excludes_market_wide_events(self):
        upcoming = nm.upcoming_events_for_ticker(
            "MSFT", self.events_dir, as_of="2026-11-09", days=7)
        self.assertNotIn("US Oct CPI",
                         [e["title"] for e in upcoming])

    def test_upcoming_market_events(self):
        market = nm.upcoming_market_events(
            self.events_dir, as_of="2026-11-09", days=7)
        self.assertEqual([e["title"] for e in market], ["US Oct CPI"])

    def test_past_events_most_recent_first_with_outcome_flag(self):
        past = list(nm.past_events_for_ticker(
            "MSFT", self.events_dir, as_of="2026-11-09"))
        self.assertEqual([e["title"] for e in past], ["MSFT dividend"])
        self.assertFalse(past[0]["outcome_recorded"])

    def test_past_event_with_outcome_recorded(self):
        event = et.list_events(self.events_dir, ticker="MSFT")[0]
        et.record_outcome(event, {"reported_eps": 3.0,
                                  "expected_eps": 2.9},
                          events_dir=self.events_dir)
        past = list(nm.past_events_for_ticker(
            "MSFT", self.events_dir, as_of="2026-11-09"))
        self.assertTrue(past[0]["outcome_recorded"])
        self.assertAlmostEqual(
            past[0]["outcome"]["eps_surprise_pct"], 0.1 / 2.9)


class TestSnapshotComparison(unittest.TestCase):
    def test_compare_snapshots_reports_field_changes(self):
        old = {"price": 507.12, "tier": "overvalued",
               "quality_rating": "excellent",
               "health_verdict": "strong",
               "material_deterioration": False,
               "deterioration_verdict": "warning",
               "strongest_alert_threshold_price": 78.82,
               "fair_value_range": {"conservative": 98.52,
                                    "base": 163.30,
                                    "optimistic": 234.64},
               "key_figures": {"revenue": 331839000000,
                               "net_income": 133749000000,
                               "free_cash_flow": 66987000000}}
        new = dict(old)
        new["price"] = 70.0
        new["tier"] = "strongest_alert"
        changes = nm.compare_snapshots(old, new)
        fields = {c["field"] for c in changes}
        self.assertEqual(fields, {"price", "tier"})
        price_change = next(c for c in changes
                            if c["field"] == "price")
        self.assertEqual((price_change["before"],
                          price_change["after"]), (507.12, 70.0))

    def test_compare_snapshots_reports_range_and_figure_changes(self):
        old = {"fair_value_range": {"conservative": 98.52,
                                    "base": 163.30,
                                    "optimistic": 234.64},
               "key_figures": {"revenue": 100, "net_income": 10,
                               "free_cash_flow": 5}}
        new = {"fair_value_range": {"conservative": 110.0,
                                    "base": 163.30,
                                    "optimistic": 234.64},
               "key_figures": {"revenue": 100, "net_income": 12,
                               "free_cash_flow": 5}}
        fields = {c["field"]
                  for c in nm.compare_snapshots(old, new)}
        self.assertEqual(fields, {"fair_value_range.conservative",
                                  "key_figures.net_income"})

    def test_compare_snapshots_empty_without_old_or_new(self):
        self.assertEqual(nm.compare_snapshots(None, {"price": 1}), [])
        self.assertEqual(nm.compare_snapshots({"price": 1}, None), [])
        self.assertEqual(nm.compare_snapshots({"price": 1},
                                              {"price": 1}), [])

    def test_price_vs_alert(self):
        below = nm.price_vs_alert({"price": 70.0,
                                   "strongest_alert_threshold_price": 78.82})
        self.assertTrue(below["below_alert"])
        self.assertAlmostEqual(below["distance_pct"],
                               (70.0 - 78.82) / 78.82)
        above = nm.price_vs_alert(
            {"price": 507.12,
             "strongest_alert_threshold_price": 78.82})
        self.assertFalse(above["below_alert"])
        missing = nm.price_vs_alert({"price": None,
                                     "strongest_alert_threshold_price": 78.82})
        self.assertIsNone(missing["below_alert"])
        self.assertIsNone(missing["distance_pct"])


class TestAlerts(unittest.TestCase):
    def test_strongest_alert_candidate_raises_alert(self):
        detection = {"candidates": [{
            "ticker": "FIXA", "detection_band": "strongest_alert",
            "tier": "strongest_alert", "price": 5.0,
            "price_as_of": "2026-11-08",
            "fair_value_range": {"conservative": 13.39},
            "margin_of_safety_vs_conservative": 0.626,
            "quality_rating": "good", "health_verdict": "strong",
            "qualifies_reasons": ["margin >= 20%"],
        }]}
        alerts = nm.build_alerts(detection=detection)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["alert_type"], "strongest_alert")
        self.assertEqual(alerts[0]["ticker"], "FIXA")
        self.assertEqual(alerts[0]["alert_id"], "strongest_alert:FIXA")

    def test_watch_band_candidate_is_not_an_alert(self):
        detection = {"candidates": [{
            "ticker": "FIXB", "detection_band": "watch_discount",
            "tier": "reasonable_price", "price": 12.0,
        }]}
        self.assertEqual(nm.build_alerts(detection=detection), [])

    def test_thesis_invalidated_and_confirmed_raise_alerts(self):
        reviews = [
            {"ticker": "MSFT", "thesis_id": "MSFT_2026-11-08_01",
             "suggested_status": "invalidated",
             "suggested_reason": "material deterioration",
             "confirm_met": 0, "confirm_total": 3,
             "invalidate_met": 1, "invalidate_total": 2,
             "changes": []},
            {"ticker": "NVDA", "thesis_id": "NVDA_2026-11-08_01",
             "suggested_status": "confirmed",
             "suggested_reason": "all confirm conditions met",
             "confirm_met": 2, "confirm_total": 2,
             "invalidate_met": 0, "invalidate_total": 1,
             "changes": []},
            {"ticker": "GOOGL", "thesis_id": "GOOGL_2026-11-08_01",
             "suggested_status": "active",
             "suggested_reason": "no condition met",
             "confirm_met": 0, "confirm_total": 2,
             "invalidate_met": 0, "invalidate_total": 1,
             "changes": []},
        ]
        alerts = nm.build_alerts(thesis_reviews=reviews)
        self.assertEqual(
            [(a["alert_type"], a["ticker"]) for a in alerts],
            [("thesis_invalidated", "MSFT"),
             ("thesis_confirmed", "NVDA")])

    def test_build_alerts_with_no_inputs(self):
        self.assertEqual(nm.build_alerts(), [])


class TestState(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="step34-state-") as tmp:
            path = Path(tmp) / "state.json"
            state = nm.new_state(["MSFT", "NVDA"])
            state["tickers"]["MSFT"] = {
                "last_checked_at": "2026-11-09T00:00:00Z",
                "seen_article_keys": ["url:https://example.com/a"],
                "seen_event_ids": ["2026-11-12_MSFT_01"],
                "last_snapshot": {"ticker": "MSFT", "tier": "overvalued"},
            }
            state["alerted_keys"] = ["strongest_alert:FIXA"]
            nm.save_state(state, path)
            loaded = nm.load_state(path)
            self.assertEqual(loaded, state)

    def test_load_missing_state_is_a_first_run(self):
        with tempfile.TemporaryDirectory(prefix="step34-state-") as tmp:
            state = nm.load_state(Path(tmp) / "missing.json")
            self.assertEqual(state["tickers"], {})
            self.assertEqual(state["alerted_keys"], [])

    def test_load_corrupt_state_raises(self):
        with tempfile.TemporaryDirectory(prefix="step34-state-") as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                nm.load_state(path)


class TestRunMonitor(_TempDbTestCase):
    def setUp(self):
        self.tmp2 = tempfile.TemporaryDirectory(prefix="step34-run-")
        self.news_dir = Path(self.tmp2.name) / "news"
        self.events_dir = Path(self.tmp2.name) / "events"
        self.state_path = Path(self.tmp2.name) / "state.json"
        self.news_dir.mkdir()
        _write_run(self.news_dir, "20261108_100000_MSFT", [
            _article("Story one", "https://example.com/one",
                     iso="2026-11-07"),
            _article("Story two", "https://example.com/two",
                     iso="2026-11-08"),
        ])
        et.add_event(events_dir=self.events_dir, ticker="MSFT",
                     event_type="earnings", date="2026-11-12",
                     title="MSFT earnings", source="fixture")
        et.add_event(events_dir=self.events_dir, ticker=None,
                     event_type="economic", date="2026-11-10",
                     title="US Oct CPI", source="fixture")

    def tearDown(self):
        self.tmp2.cleanup()

    def test_first_run_reports_new_articles_and_events(self):
        conn = self._connect()
        try:
            record = nm.run_monitor(
                conn, ["MSFT"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09")
        finally:
            conn.close()
        msft = record["tickers"]["MSFT"]
        self.assertEqual(msft["new_article_count"], 2)
        self.assertEqual(msft["upcoming_event_count"], 1)
        self.assertEqual(record["summary"]["market_events"], 1)
        self.assertEqual(msft["snapshot_status"], "complete")
        self.assertEqual(msft["price_vs_alert"]["below_alert"], False)

    def test_second_run_reports_nothing_new(self):
        conn = self._connect()
        try:
            nm.run_monitor(conn, ["MSFT"], state_path=self.state_path,
                           news_dir=self.news_dir,
                           events_dir=self.events_dir, as_of="2026-11-09")
            record = nm.run_monitor(
                conn, ["MSFT"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09")
        finally:
            conn.close()
        msft = record["tickers"]["MSFT"]
        self.assertEqual(msft["new_article_count"], 0)
        self.assertEqual(msft["change_count"], 0)
        self.assertEqual(record["alert_count"], 0)

    def test_second_run_with_new_article_reports_only_it(self):
        conn = self._connect()
        try:
            nm.run_monitor(conn, ["MSFT"], state_path=self.state_path,
                           news_dir=self.news_dir,
                           events_dir=self.events_dir, as_of="2026-11-09")
            _write_run(self.news_dir, "20261109_100000_MSFT", [
                _article("Story three", "https://example.com/three",
                         iso="2026-11-09"),
            ])
            record = nm.run_monitor(
                conn, ["MSFT"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09")
        finally:
            conn.close()
        msft = record["tickers"]["MSFT"]
        self.assertEqual(msft["new_article_count"], 1)
        self.assertEqual(msft["new_articles"][0]["title"], "Story three")

    def test_price_fall_is_detected_as_a_change(self):
        conn = self._connect()
        try:
            nm.run_monitor(conn, ["MSFT"], state_path=self.state_path,
                           news_dir=self.news_dir,
                           events_dir=self.events_dir, as_of="2026-11-09")
            record = nm.run_monitor(
                conn, ["MSFT"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09", prices={"MSFT": 70.0})
        finally:
            conn.close()
        msft = record["tickers"]["MSFT"]
        fields = {c["field"] for c in msft["changes"]}
        self.assertIn("price", fields)
        self.assertIn("tier", fields)
        self.assertTrue(msft["price_vs_alert"]["below_alert"])

    def test_alert_raised_once_then_seen(self):
        detection = {"candidates": [{
            "ticker": "FIXA", "detection_band": "strongest_alert",
            "tier": "strongest_alert", "price": 5.0,
            "price_as_of": "2026-11-08",
            "fair_value_range": {"conservative": 13.39},
            "margin_of_safety_vs_conservative": 0.626,
            "quality_rating": "good", "health_verdict": "strong",
            "qualifies_reasons": ["margin >= 20%"],
        }]}
        conn = self._connect()
        try:
            first = nm.run_monitor(
                conn, ["MSFT"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09", detection=detection)
            second = nm.run_monitor(
                conn, ["MSFT"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09", detection=detection)
        finally:
            conn.close()
        self.assertEqual(first["alert_count"], 1)
        self.assertEqual(first["alerts"][0]["ticker"], "FIXA")
        self.assertEqual(second["alert_count"], 0)
        self.assertEqual(second["alerts_already_seen"], 1)

    def test_graceful_degradation_for_unknown_ticker(self):
        conn = self._connect()
        try:
            record = nm.run_monitor(
                conn, ["ZZZZ"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09")
        finally:
            conn.close()
        zzzz = record["tickers"]["ZZZZ"]
        self.assertEqual(zzzz["new_article_count"], 0)
        self.assertEqual(zzzz["upcoming_event_count"], 0)
        self.assertEqual(zzzz["snapshot_status"], "insufficient_data")
        self.assertTrue(zzzz["reasons"])

    def test_check_without_connection_still_reports_news(self):
        record = nm.check_ticker(
            None, "MSFT", ticker_state=None, news_dir=self.news_dir,
            events_dir=self.events_dir, as_of="2026-11-09")
        self.assertEqual(record["new_article_count"], 2)
        self.assertEqual(record["snapshot_status"], "not_checked")

    def test_briefing_lines(self):
        conn = self._connect()
        try:
            record = nm.run_monitor(
                conn, ["MSFT", "ZZZZ"], state_path=self.state_path,
                news_dir=self.news_dir, events_dir=self.events_dir,
                as_of="2026-11-09")
        finally:
            conn.close()
        lines = nm.briefing_lines(record)
        self.assertTrue(any(line.startswith("MSFT")
                            and "2 new article(s)" in line
                            for line in lines))
        self.assertTrue(any(line.startswith("ZZZZ")
                            and "nothing new" in line for line in lines))


class TestRealStoredData(_TempDbTestCase):
    """The real stored news/events: MSFT only, 10 articles."""

    def test_msft_real_stored_news_and_events(self):
        conn = self._connect()
        try:
            with tempfile.TemporaryDirectory(prefix="step34-real-") as tmp:
                record = nm.run_monitor(
                    conn, ["MSFT"],
                    state_path=Path(tmp) / "state.json",
                    news_dir=PROJECT_ROOT / "data" / "news",
                    events_dir=PROJECT_ROOT / "data" / "events",
                    as_of="2026-11-09")
                # The state assertions must run inside the
                # TemporaryDirectory: the state file lives in it
                # and is deleted when the block exits.
                state = nm.load_state(record["state_file"])
        finally:
            conn.close()
        msft = record["tickers"]["MSFT"]
        self.assertEqual(msft["total_articles"], 10)
        self.assertEqual(msft["new_article_count"], 10)
        # No MSFT event in the seeded week; NVDA's is not MSFT's.
        self.assertEqual(msft["upcoming_event_count"], 0)
        self.assertEqual(msft["past_event_count"], 0)
        # Market-wide events in the week: CPI 10 Nov, bond closed
        # 11 Nov, PPI 13 Nov, 13F due 16 Nov is outside 7 days.
        market_titles = [e["title"] for e in record["market_events"]]
        self.assertIn("US Oct CPI", market_titles)
        self.assertIn("US PPI", market_titles)
        self.assertEqual(msft["snapshot_status"], "complete")
        self.assertAlmostEqual(
            msft["price_vs_alert"]["alert_price"], 78.82, places=2)
        self.assertFalse(msft["price_vs_alert"]["below_alert"])
        self.assertEqual(len(state["tickers"]["MSFT"]
                             ["seen_article_keys"]), 10)
        self.assertIsNotNone(
            state["tickers"]["MSFT"]["last_snapshot"])

    def test_second_real_run_reports_nothing_new(self):
        conn = self._connect()
        try:
            with tempfile.TemporaryDirectory(prefix="step34-real-") as tmp:
                state_path = Path(tmp) / "state.json"
                kwargs = dict(
                    news_dir=PROJECT_ROOT / "data" / "news",
                    events_dir=PROJECT_ROOT / "data" / "events",
                    as_of="2026-11-09")
                nm.run_monitor(conn, ["MSFT"], state_path=state_path,
                               **kwargs)
                record = nm.run_monitor(conn, ["MSFT"],
                                        state_path=state_path, **kwargs)
        finally:
            conn.close()
        self.assertEqual(record["tickers"]["MSFT"]["new_article_count"], 0)
        self.assertEqual(record["tickers"]["MSFT"]["change_count"], 0)


if __name__ == "__main__":
    unittest.main()
