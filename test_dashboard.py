"""Tests for Step 49 — live dashboards and agent monitoring.

The snapshot is built from fixture logs in a temp directory —
no production files are read, no production writes happen, and
the dashboard page is exercised through the Step 48 test server
on localhost with a temp DB copy and a stubbed model client.
"""

import hashlib
import json
import re
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from agent import dashboard as dash
from agent import orchestrator as orch
from agent import ui

BASE = Path(__file__).resolve().parent
PROD_DB = BASE / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"

# A control whose name, action, or label suggests trading must
# never appear in the served HTML.
_FORBIDDEN_CONTROL_RE = re.compile(
    r"<(?:button|input|select|form)[^>]*(?:buy|sell|trade|order|"
    r"place_order|checkout)",
    re.IGNORECASE,
)

# Fixed "now" for the fixture: Mon 2026-11-09 12:00 UTC.
NOW = "2026-11-09T12:00:00+00:00"


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_jsonl(path, records, corrupt=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    if corrupt:
        lines.append("{not json")
        lines.append("[1, 2, 3]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _fixture_data(root):
    """A complete fixture data/ tree under ``root``."""
    data = Path(root) / "data"
    _write_jsonl(data / "detection_runs.jsonl", [
        {"run_at_utc": "2026-11-08T09:36:00Z", "selected": 10619,
         "scannable": 1, "candidates": 0, "strongest_alerts": 0,
         "watch": 0, "value_traps": 0},
        {"run_at_utc": "2026-11-09T10:00:00Z", "selected": 10619,
         "scannable": 1, "candidates": 0, "strongest_alerts": 0,
         "watch": 0, "value_traps": 0},
    ])
    _write_jsonl(data / "orchestration" / "turns.jsonl", [
        {"request": "What is the intrinsic value of MSFT?",
         "intent": "valuation", "intent_method": "keyword",
         "route": "valuation", "status": "ok",
         "checked_at": "2026-11-09T11:00:00+00:00"},
        {"request": "Buy 100 shares of MSFT at $500",
         "intent": "trade", "intent_method": "keyword",
         "route": "guardrail_trade", "status": "refused",
         "checked_at": "2026-11-09T11:30:00+00:00"},
    ])
    _write_jsonl(data / "scheduler" / "runs.jsonl", [
        {"job_id": "watchlist-check", "job_name": "Watchlist check",
         "kind": "watchlist_check",
         "started_at": "2026-11-08T09:36:00+00:00",
         "finished_at": "2026-11-08T09:36:05+00:00",
         "status": "ok", "summary": "1 ticker(s) checked",
         "error": None},
        {"job_id": "alert-digest", "job_name": "Alert digest",
         "kind": "alert_digest",
         "started_at": "2026-11-08T09:35:00+00:00",
         "finished_at": "2026-11-08T09:35:02+00:00",
         "status": "failed", "summary": "",
         "error": "delivery failed"},
    ])
    _write_json(data / "alerts" / "alert_state.json", {
        "version": 1,
        "active_alerts": {
            "FIXA:strongest_alert": {
                "alert_key": "FIXA:strongest_alert",
                "ticker": "FIXA", "rule": "strongest_alert",
                "severity": "strongest",
                "raised_at": "2026-11-09T09:00:00+00:00",
            },
        },
        "cleared_alerts": [{
            "alert_key": "FIXB:watch_discount",
            "ticker": "FIXB", "rule": "watch_discount",
            "severity": "watch",
            "raised_at": "2026-11-08T09:00:00+00:00",
            "cleared_at": "2026-11-08T12:00:00+00:00",
        }],
        "last_evidence": {},
    })
    _write_json(data / "monitoring" / "monitoring_state.json", {
        "version": 1,
        "watchlist": ["MSFT"],
        "last_checked_at": "2026-11-08T17:30:13.448710Z",
        "tickers": {
            "MSFT": {
                "last_checked_at": "2026-11-08T17:30:13.448710Z",
                "seen_article_keys": ["url:https://example.com/a",
                                      "url:https://example.com/b"],
                "seen_event_ids": [],
            },
        },
        "alerted_keys": [],
    })
    _write_json(data / "watchlist" / "watchlist.json", {
        "version": 1,
        "entries": [
            {"ticker": "MSFT", "sources": ["owner_core"]},
            {"ticker": "D05", "sources": ["owner_core"]},
        ],
    })
    _write_json(data / "watchlist" / "tracker.json", {
        "version": 1,
        "records": {
            "MSFT": {"ticker": "MSFT", "scannable": True,
                     "tier": "overvalued",
                     "last_checked": "2026-11-09T01:00:00+00:00"},
            "D05": {"ticker": "D05", "scannable": False,
                    "tier": "insufficient_data",
                    "last_checked": "2026-11-09T01:00:00+00:00"},
        },
    })
    _write_json(data / "memory" / "index.json", {
        "record_count": 1,
        "skipped_files": [],
        "records": [{
            "id": "fixture-voice",
            "type": "preference",
            "stated_at": "2026-11-03T00:00:00+00:00",
            "tags": ["voice", "preference"],
            "status": "active",
        }],
    })
    _write_json(data / "news" / "20261108_163835_MSFT.json", {
        "run_id": "fixture", "tickers": ["MSFT"],
        "articles": [{"title": "A"}, {"title": "B"}],
    })
    _write_json(data / "events" / "2026-11-09_KSPI_01.json", {
        "event_id": "2026-11-09_KSPI_01", "ticker": "KSPI",
        "event_type": "earnings", "date": "2026-11-09",
    })
    _write_json(data / "events" / "2026-11-17_NVDA_01.json", {
        "event_id": "2026-11-17_NVDA_01", "ticker": "NVDA",
        "event_type": "earnings", "date": "2026-11-17",
    })
    return data


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data_dir = _fixture_data(cls.tmp.name)
        cls.snapshot = dash.collect_status(
            data_dir=str(cls.data_dir), now=NOW)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        assert _hash(PROD_DB) == PROD_HASH, (
            "production DB hash changed during dashboard tests")

    # -- Snapshot ------------------------------------------------------

    def test_modules_present_and_shaped(self):
        modules = self.snapshot["modules"]
        for name in ("undervalued_detection", "orchestrator",
                     "scheduler", "watchlist_alerts",
                     "news_monitoring", "watchlist",
                     "long_term_memory", "news_collection",
                     "event_tracking", "cloud_models"):
            self.assertIn(name, modules)
            entry = modules[name]
            for key in ("status", "last_run_at", "records",
                        "freshness", "age_seconds", "age", "detail"):
                self.assertIn(key, entry, f"{name} missing {key}")

    def test_detection_ok_and_fresh(self):
        entry = self.snapshot["modules"]["undervalued_detection"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["records"], 2)
        self.assertEqual(entry["last_run_at"], "2026-11-09T10:00:00Z")
        self.assertEqual(entry["freshness"], "fresh")
        self.assertIn("corrupt line(s) skipped", entry["detail"])

    def test_orchestrator_ok_and_counts_turns(self):
        entry = self.snapshot["modules"]["orchestrator"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["records"], 2)
        self.assertIn("trade", entry["detail"])
        self.assertIn("refused", entry["detail"])

    def test_scheduler_stale_because_last_run_is_old(self):
        # Last run 2026-11-08 09:36 is 26h24m before NOW: stale
        # at the 24h default threshold.
        entry = self.snapshot["modules"]["scheduler"]
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["freshness"], "stale")
        self.assertEqual(entry["records"], 2)
        self.assertIn("failed", entry["detail"])

    def test_scheduler_fresh_with_a_longer_threshold(self):
        snap = dash.collect_status(
            data_dir=str(self.data_dir), now=NOW,
            stale_after_hours=48)
        entry = snap["modules"]["scheduler"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["freshness"], "fresh")

    def test_alerts_ok_with_active_and_cleared(self):
        entry = self.snapshot["modules"]["watchlist_alerts"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["records"], 2)
        self.assertIn("1 active alert(s)", entry["detail"])
        self.assertIn("1 cleared alert(s)", entry["detail"])

    def test_monitoring_ok_and_counts_seen_articles(self):
        entry = self.snapshot["modules"]["news_monitoring"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["records"], 1)
        self.assertIn("2 article(s) seen", entry["detail"])

    def test_watchlist_ok_and_scannable_count(self):
        entry = self.snapshot["modules"]["watchlist"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["records"], 2)
        self.assertIn("1 scannable", entry["detail"])

    def test_memory_news_events_counts(self):
        modules = self.snapshot["modules"]
        self.assertEqual(modules["long_term_memory"]["records"], 1)
        self.assertEqual(modules["news_collection"]["records"], 2)
        self.assertEqual(modules["event_tracking"]["records"], 2)
        self.assertEqual(modules["cloud_models"]["status"], "no_data")

    def test_recent_activity_newest_first_and_capped(self):
        activity = self.snapshot["recent_activity"]
        self.assertTrue(activity)
        stamps = [a["at"] for a in activity]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        kinds = [a["kind"] for a in activity]
        self.assertIn("orchestrator turn", kinds)
        self.assertIn("detection run", kinds)
        self.assertIn("scheduled run", kinds)
        self.assertIn("alert raised", kinds)
        self.assertIn("watchlist check", kinds)
        capped = dash.collect_status(
            data_dir=str(self.data_dir), now=NOW, recent_limit=2)
        self.assertEqual(len(capped["recent_activity"]), 2)

    def test_gaps_for_fixture(self):
        texts = [g["text"] for g in self.snapshot["gaps"]]
        joined = "\n".join(texts)
        self.assertIn("No cloud model configured", joined)
        self.assertIn("No live price feed", joined)
        self.assertIn("No holdings store exists", joined)
        self.assertIn("1 of 2 watchlist tickers are insufficient",
                      joined)
        self.assertIn("Voice is off by default", joined)
        # These did happen in the fixture, so they are not gaps.
        self.assertNotIn("No undervalued-detection run", joined)
        self.assertNotIn("No orchestrator turn", joined)
        self.assertNotIn("No scheduled job", joined)
        self.assertNotIn("No alert has ever fired", joined)
        for item in self.snapshot["gaps"]:
            self.assertIn("source", item)
            self.assertTrue(item["source"])

    def test_missing_everything_is_no_data_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            snap = dash.collect_status(data_dir=empty, now=NOW)
        for entry in snap["modules"].values():
            self.assertEqual(entry["status"], "no_data")
        self.assertEqual(snap["recent_activity"], [])
        texts = [g["text"] for g in snap["gaps"]]
        self.assertIn("No undervalued-detection run has ever been "
                      "logged.", texts)
        self.assertIn("No orchestrator turn has ever been logged — "
                      "no live Ollama turn has run on this machine.",
                      texts)
        # No alert state file at all still means no alert has
        # ever fired — the same fact as an empty state file.
        self.assertIn("No alert has ever fired on real data.", texts)

    def test_corrupt_state_file_is_corrupt_not_a_crash(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            (data / "alerts").mkdir(parents=True)
            (data / "alerts" / "alert_state.json").write_text(
                "{broken", encoding="utf-8")
            snap = dash.collect_status(data_dir=str(data), now=NOW)
        entry = snap["modules"]["watchlist_alerts"]
        self.assertEqual(entry["status"], "corrupt")
        self.assertIn("corrupt", entry["detail"])

    def test_snapshot_is_read_only(self):
        before = {}
        for path in sorted(Path(self.data_dir).rglob("*")):
            if path.is_file():
                before[str(path)] = _hash(path)
        dash.collect_status(data_dir=str(self.data_dir), now=NOW)
        after = {}
        for path in sorted(Path(self.data_dir).rglob("*")):
            if path.is_file():
                after[str(path)] = _hash(path)
        self.assertEqual(before, after)

    def test_summary_lines_and_markdown(self):
        lines = dash.summary_lines(self.snapshot)
        joined = "\n".join(lines)
        self.assertIn("undervalued_detection: ok", joined)
        self.assertIn("Gaps:", joined)
        markdown = dash.snapshot_to_markdown(self.snapshot)
        self.assertTrue(markdown.startswith("# Jarvis status"))
        self.assertIn("## Modules", markdown)
        self.assertIn("## Gaps", markdown)

    def test_parse_time_handles_z_naive_and_bad(self):
        self.assertIsNotNone(dash._parse_time("2026-11-09T10:00:00Z"))
        self.assertIsNotNone(dash._parse_time("2026-11-09 10:00:00"))
        self.assertIsNone(dash._parse_time("not a date"))
        self.assertIsNone(dash._parse_time(None))
        self.assertIsNone(dash._parse_time(42))


class TestDashboardPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.data_dir = _fixture_data(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)
        cls.app = ui.JarvisUI(
            db_path=cls.db_path,
            data_dir=str(cls.data_dir),
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
        )
        cls.server = ui.make_server(cls.app, port=0)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = (
            f"http://127.0.0.1:{cls.server.server_address[1]}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()
        assert _hash(PROD_DB) == PROD_HASH, (
            "production DB hash changed during dashboard UI tests")

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_dashboard_renders(self):
        status, body = self.get("/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("Dashboard", body)
        self.assertIn("undervalued_detection", body)
        self.assertIn("orchestrator", body)
        self.assertIn("watchlist_alerts", body)
        self.assertIn("FIXA", body)
        self.assertIn("No cloud model configured", body)
        self.assertIn("Recent activity", body)
        self.assertIn("Gaps", body)

    def test_dashboard_has_no_trading_controls(self):
        _, body = self.get("/dashboard")
        self.assertIsNone(_FORBIDDEN_CONTROL_RE.search(body))

    def test_home_links_to_dashboard(self):
        _, body = self.get("/")
        self.assertIn('href="/dashboard"', body)


if __name__ == "__main__":
    unittest.main()
