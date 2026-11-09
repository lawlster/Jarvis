"""Tests for agents/scheduled_scan.py (Step 59).

All live pulls are stubbed (no network): the fetcher is
injected and returns canned ``get_company_financials`` dicts,
or the CLI reads a canned-pull fixture file.  The production
database is never opened — the Step 57 content fingerprint
guard asserts its logical content is unchanged.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from db_fingerprint import content_fingerprint, temp_dir_with_db_cleanup, cleanup_temp_dir

from agent import orchestrator as orch
from agent import ui
from agents import scheduled_scan as ss
from agents import watchlist_alerts as wa
from agents.watchlist_foundation import save_watchlist

REPO_ROOT = Path(__file__).resolve().parent
PROD_DB = REPO_ROOT / "database" / "assistant.db"

TRADE_WORDS = re.compile(r"\b(buy|sell|order)\b", re.IGNORECASE)


def _live(symbol, price, fcf, debt=0.0, cash=0.0, shares=1e9,
          currency="USD", fcf_period="2026-06-30"):
    """A get_company_financials-shaped live pull (Step 54 shape)."""
    return {
        "ticker": symbol,
        "company_name": f"{symbol} Co",
        "sector": "Technology",
        "currency": currency,
        "data_source": "Yahoo Finance",
        "data_available": True,
        "data_freshness": {
            "retrieved_at_utc": "2026-11-09T03:00:00+00:00",
            "price_data_as_of_utc": "2026-11-06T21:00:00+00:00",
            "financial_statements_latest_period": {
                "income_statement": fcf_period,
                "cash_flow": fcf_period,
                "balance_sheet": fcf_period,
            },
        },
        "current_price": price,
        "free_cash_flow": {fcf_period: fcf} if fcf is not None else {},
        "total_debt": {fcf_period: debt},
        "cash": {fcf_period: cash},
        "shares_outstanding": shares,
    }


def _record(ticker, band=None, status="ok", price=200.0,
            conservative=134.0, margin=None):
    """A Step 54-shaped scan record (for diff fixtures)."""
    if margin is None:
        margin = ((conservative - price) / conservative
                  if price is not None and conservative is not None
                  else None)
    return {
        "ticker": ticker,
        "status": status,
        "detection_band": band,
        "price": price,
        "price_as_of": "2026-11-06",
        "fair_value_range": {
            "conservative": conservative, "base": 190.0,
            "optimistic": 270.0},
        "strongest_alert_price": 0.8 * conservative,
        "margin_of_safety_vs_conservative": margin,
        "margin_of_safety_vs_base": None,
        "margin_of_safety_vs_optimistic": None,
        "tier": "reasonable_price",
        "tier_reasons": [],
        "conditions_to_change": [],
        "provisional": True,
        "retrieved_at": "2026-11-09T03:00:00+00:00",
    }


def _scan(records, scanned_at="2026-11-10T05:30:00+00:00"):
    by_band = {}
    for record in records:
        if record.get("detection_band"):
            by_band[record["detection_band"]] = (
                by_band.get(record["detection_band"], 0) + 1)
    return {
        "results": records,
        "summary": {
            "selected": len(records),
            "scannable": sum(
                1 for r in records if r["status"] == "ok"),
            "candidates": sum(by_band.values()),
            "by_band": by_band,
            "strongest_alerts": by_band.get("strongest_alert", 0),
            "watch": by_band.get("watch_discount", 0)
                    + by_band.get("watch_below_base", 0),
            "provider_unavailable": sum(
                1 for r in records
                if r["status"] == "provider_unavailable"),
            "insufficient_data": sum(
                1 for r in records
                if r["status"] == "insufficient_data"),
        },
        "scanned_at": scanned_at,
        "method": "fixture",
        "limitations": [],
    }


class ScheduledScanTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prod_fp_before = content_fingerprint(PROD_DB)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, \
            "production DB changed"

    def setUp(self):
        self.tmp = temp_dir_with_db_cleanup()
        self.root = Path(self.tmp.name)
        self.scans_dir = self.root / "scans"
        self.store_dir = self.root / "watchlist"
        self.alert_state = str(self.root / "alerts" / "state.json")
        self.live_scan_path = str(self.root / "step54-live-scan.json")

    def tearDown(self):
        cleanup_temp_dir(self.tmp)

    # -- helpers ----------------------------------------------------

    def _seed(self, tickers):
        save_watchlist([
            {"ticker": t, "universe_ticker": None,
             "company_name": f"{t} Co", "sources": ["test"],
             "reason": "test", "notes": [],
             "date_added": "2026-11-09"}
            for t in tickers
        ], store_dir=str(self.store_dir))

    def _fetcher(self, pulls):
        def fetch(symbol):
            if symbol not in pulls:
                raise AssertionError(f"unexpected fetch: {symbol}")
            result = pulls[symbol]
            if isinstance(result, Exception):
                raise result
            return result
        return fetch

    def _run(self, pulls, tickers, as_of="2026-11-10T05:30:00+00:00"):
        self._seed(tickers)
        return ss.run_daily_scan(
            scans_dir=str(self.scans_dir),
            store_dir=str(self.store_dir),
            alert_state_path=self.alert_state,
            live_scan_path=self.live_scan_path,
            fetcher=self._fetcher(pulls),
            rate_limit_seconds=0, as_of=as_of)

    def _active_keys(self):
        state = wa.load_state(self.alert_state)
        return set(state.get("active_alerts", {}))

    # -- diff --------------------------------------------------------

    def test_diff_new_strongest_alert(self):
        previous = _scan([_record("BKE", band=None, price=200.0)])
        current = _scan([_record("BKE", band="strongest_alert",
                                 price=50.0)])
        diff = ss.diff_runs(previous, current)
        self.assertEqual(len(diff["new_strongest_alert"]), 1)
        entry = diff["new_strongest_alert"][0]
        self.assertEqual(entry["ticker"], "BKE")
        self.assertIsNone(entry["previous_band"])
        self.assertTrue(entry["provisional"])
        self.assertEqual(diff["new_watch"], [])
        self.assertEqual(diff["disappeared"], [])

    def test_diff_promotion_to_strongest_counts_as_new(self):
        previous = _scan([_record("BKE", band="watch_discount",
                                  price=125.0)])
        current = _scan([_record("BKE", band="strongest_alert",
                                 price=50.0)])
        diff = ss.diff_runs(previous, current)
        self.assertEqual(
            [e["ticker"] for e in diff["new_strongest_alert"]],
            ["BKE"])
        self.assertEqual(
            diff["new_strongest_alert"][0]["previous_band"],
            "watch_discount")

    def test_diff_disappeared_band(self):
        previous = _scan([_record("BKE", band="strongest_alert",
                                  price=50.0)])
        current = _scan([_record("BKE", band=None, price=200.0)])
        diff = ss.diff_runs(previous, current)
        self.assertEqual(diff["new_strongest_alert"], [])
        self.assertEqual(
            [e["ticker"] for e in diff["disappeared"]], ["BKE"])
        self.assertEqual(diff["disappeared"][0]["previous_band"],
                         "strongest_alert")

    def test_diff_unknown_is_not_disappeared(self):
        # A ticker whose pull failed is unknown, not gone: it
        # must be listed as newly unavailable, never as a
        # disappeared band.
        previous = _scan([_record("BKE", band="strongest_alert",
                                  price=50.0)])
        current = _scan([_record(
            "BKE", band=None, status="provider_unavailable",
            price=None, margin=None)])
        diff = ss.diff_runs(previous, current)
        self.assertEqual(diff["disappeared"], [])
        self.assertEqual(
            [e["ticker"] for e in diff["newly_unavailable"]],
            ["BKE"])
        self.assertEqual(
            diff["newly_unavailable"][0]["status"],
            "provider_unavailable")

    def test_diff_newly_scannable(self):
        previous = _scan([_record(
            "BKE", band=None, status="provider_unavailable",
            price=None, margin=None)])
        current = _scan([_record("BKE", band=None, price=200.0)])
        diff = ss.diff_runs(previous, current)
        self.assertEqual(
            [e["ticker"] for e in diff["newly_scannable"]],
            ["BKE"])

    def test_diff_no_previous_run(self):
        current = _scan([_record("BKE", band="strongest_alert",
                                 price=50.0)])
        diff = ss.diff_runs(None, current)
        self.assertIsNone(diff["previous_scanned_at"])
        self.assertEqual(
            [e["ticker"] for e in diff["new_strongest_alert"]],
            ["BKE"])

    # -- run storage -------------------------------------------------

    def test_missed_run_is_never_fabricated(self):
        run, _path = ss.latest_run(self.scans_dir)
        self.assertIsNone(run)
        self.assertEqual(ss.list_runs(self.scans_dir), [])
        self._run({"BKE": _live("BKE", 50.0, 10e9)},
                  ["BKE"], as_of="2026-11-10T05:30:00+00:00")
        # Exactly one run file, dated by the SGT date of the
        # run — nothing for any other date.
        self.assertEqual(
            [p.name for p in ss.list_runs(self.scans_dir)],
            ["2026-11-10.json"])

    def test_partial_run_stores_failures_explicitly(self):
        run = self._run({
            "BKE": _live("BKE", 50.0, 10e9),
            "DECK": RuntimeError("provider down"),
        }, ["BKE", "DECK"])
        self.assertEqual(run["summary"]["scannable"], 1)
        self.assertEqual(
            run["summary"]["provider_unavailable"], 1)
        by_ticker = {r["ticker"]: r for r in run["results"]}
        self.assertEqual(by_ticker["DECK"]["status"],
                         "provider_unavailable")
        # The partial run still completed and was stored.
        self.assertEqual(run["run_id"], "2026-11-10")
        self.assertTrue((self.scans_dir / "2026-11-10.json").exists())

    def test_first_run_diffs_against_stored_step54_scan(self):
        # The stored Step 54 run is the baseline: a band it
        # already held is not "new" on the first scheduled run.
        previous = _scan(
            [_record("BKE", band="strongest_alert", price=50.0)],
            scanned_at="2026-11-09T04:36:44+00:00")
        (self.root / "step54-live-scan.json").write_text(
            json.dumps(previous), encoding="utf-8")
        run = self._run({"BKE": _live("BKE", 50.0, 10e9)}, ["BKE"])
        self.assertEqual(run["diff"]["new_strongest_alert"], [])
        self.assertEqual(run["diff"]["unchanged_bands"], 1)
        self.assertEqual(
            run["diff"]["previous_scanned_at"],
            "2026-11-09T04:36:44+00:00")

    def test_run_updates_live_scan_pointer(self):
        run = self._run({"BKE": _live("BKE", 50.0, 10e9)}, ["BKE"])
        pointer = json.loads(
            Path(self.live_scan_path).read_text(encoding="utf-8"))
        self.assertEqual(pointer["scanned_at"],
                         run["scanned_at"])
        self.assertEqual(pointer["results"][0]["ticker"], "BKE")

    # -- alerts through the Step 38 seam -----------------------------

    def test_new_strongest_alert_listed_by_step38_engine(self):
        run = self._run({"BKE": _live("BKE", 50.0, 10e9),
                         "PLAIN": _live("PLAIN", 200.0, 10e9)},
                        ["BKE", "PLAIN"])
        self.assertIn("strongest_alert:BKE",
                      run["alerts"]["new_alerts"])
        self.assertIn("strongest_alert:BKE", self._active_keys())
        state = wa.load_state(self.alert_state)
        alert = state["active_alerts"]["strongest_alert:BKE"]
        self.assertIn("PROVISIONAL", alert["reason"])
        self.assertIn("NOT a confirmed", alert["reason"])
        self.assertNotIn("no material deterioration",
                         alert["reason"])
        self.assertIn("PROVISIONAL", alert["briefing_line"])
        self.assertIn("worth a closer look",
                      alert["briefing_line"])

    def test_second_run_raises_nothing_new(self):
        pulls = {"BKE": _live("BKE", 50.0, 10e9)}
        self._run(pulls, ["BKE"])
        run2 = self._run(pulls, ["BKE"],
                         as_of="2026-11-11T05:30:00+00:00")
        self.assertEqual(run2["alerts"]["new_alerts"], [])
        self.assertIn("strongest_alert:BKE",
                      run2["alerts"]["already_active"])
        self.assertIn("strongest_alert:BKE", self._active_keys())

    def test_disappeared_band_clears_its_alert(self):
        self._run({"BKE": _live("BKE", 50.0, 10e9)}, ["BKE"])
        self.assertIn("strongest_alert:BKE", self._active_keys())
        # Price 250 is above the base value (~205), so the band
        # is gone entirely — not merely demoted to a watch band
        # (price 200 would still be watch_below_base).
        run2 = self._run({"BKE": _live("BKE", 250.0, 10e9)},
                         ["BKE"], as_of="2026-11-11T05:30:00+00:00")
        self.assertEqual(
            [e["ticker"] for e in run2["diff"]["disappeared"]],
            ["BKE"])
        self.assertIn("strongest_alert:BKE",
                      run2["alerts"]["cleared_alerts"])
        self.assertNotIn("strongest_alert:BKE", self._active_keys())

    def test_failed_pull_keeps_active_alert(self):
        self._run({"BKE": _live("BKE", 50.0, 10e9)}, ["BKE"])
        run2 = self._run(
            {"BKE": RuntimeError("provider down")}, ["BKE"],
            as_of="2026-11-11T05:30:00+00:00")
        self.assertEqual(run2["diff"]["disappeared"], [])
        self.assertIn("strongest_alert:BKE", self._active_keys())

    def test_no_quality_change_alert_from_live_evidence(self):
        # A stored-data rating changing to "unknown (live pull)"
        # is a change in evidence, not in the business.
        state = wa.new_state()
        state["last_evidence"]["BKE"] = {
            "ticker": "BKE", "quality_rating": "excellent",
            "health_verdict": "strong", "deteriorated": False,
            "tier": "reasonable_price", "price": 50.0,
            "fair_value_range": {"conservative": 134.0},
            "as_of": "2026-11-09T04:36:44+00:00"}
        wa.save_state(state, self.alert_state)
        run = self._run({"BKE": _live("BKE", 50.0, 10e9)}, ["BKE"])
        self.assertNotIn("quality_changed:BKE",
                         run["alerts"]["new_alerts"])

    # -- no trade, ever ------------------------------------------------

    def test_no_trade_language_anywhere(self):
        run = self._run({"BKE": _live("BKE", 50.0, 10e9),
                         "PLAIN": _live("PLAIN", 200.0, 10e9)},
                        ["BKE", "PLAIN"])
        lines = ss.summary_lines(run)
        self.assertIn("worth a closer look", "\n".join(lines))
        for line in lines:
            self.assertIsNone(
                TRADE_WORDS.search(line), f"trade word in: {line}")
        state = wa.load_state(self.alert_state)
        for alert in state["active_alerts"].values():
            self.assertIsNone(TRADE_WORDS.search(
                alert["reason"]), alert["alert_key"])
            self.assertIsNone(TRADE_WORDS.search(
                alert["briefing_line"]), alert["alert_key"])

    # -- lock ----------------------------------------------------------

    def test_concurrent_runs_prevented(self):
        self._seed(["BKE"])
        lock_path, _stale = ss.acquire_lock(self.scans_dir)
        try:
            with self.assertRaises(ss.ScanAlreadyRunning):
                ss.run_daily_scan(
                    scans_dir=str(self.scans_dir),
                    store_dir=str(self.store_dir),
                    alert_state_path=self.alert_state,
                    live_scan_path=self.live_scan_path,
                    fetcher=self._fetcher(
                        {"BKE": _live("BKE", 50.0, 10e9)}),
                    rate_limit_seconds=0)
        finally:
            ss.release_lock(lock_path)
        # After the lock is released the run proceeds.
        run = ss.run_daily_scan(
            scans_dir=str(self.scans_dir),
            store_dir=str(self.store_dir),
            alert_state_path=self.alert_state,
            live_scan_path=self.live_scan_path,
            fetcher=self._fetcher({"BKE": _live("BKE", 50.0, 10e9)}),
            rate_limit_seconds=0,
            as_of="2026-11-10T05:30:00+00:00")
        self.assertEqual(run["run_id"], "2026-11-10")
        self.assertFalse(run["stale_lock_removed"])

    def test_stale_lock_is_taken_over_and_noted(self):
        self._seed(["BKE"])
        lock_path, _stale = ss.acquire_lock(self.scans_dir)
        # Fake a crash four hours ago.
        old = time.time() - 4 * 3600
        os.utime(lock_path, (old, old))
        run = ss.run_daily_scan(
            scans_dir=str(self.scans_dir),
            store_dir=str(self.store_dir),
            alert_state_path=self.alert_state,
            live_scan_path=self.live_scan_path,
            fetcher=self._fetcher({"BKE": _live("BKE", 50.0, 10e9)}),
            rate_limit_seconds=0)
        self.assertTrue(run["stale_lock_removed"])
        self.assertIn("stale lock",
                      "\n".join(ss.summary_lines(run)))

    # -- CLI -----------------------------------------------------------

    def test_cli_runs_on_fixtures_with_no_network_and_exits_zero(
            self):
        self._seed(["BKE", "PLAIN"])
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({
            "BKE": _live("BKE", 50.0, 10e9),
            "PLAIN": _live("PLAIN", 200.0, 10e9),
        }), encoding="utf-8")
        log_file = self.root / "daily-scan.log"
        proc = subprocess.run(
            [sys.executable, "-m", "agents.scheduled_scan", "run",
             "--scans-dir", str(self.scans_dir),
             "--store-dir", str(self.store_dir),
             "--alert-state-path", self.alert_state,
             "--live-scan-path", self.live_scan_path,
             "--fixture", str(fixture),
             "--as-of", "2026-11-10T05:30:00+00:00",
             "--rate-limit-seconds", "0",
             "--log-file", str(log_file)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BKE", proc.stdout)
        self.assertIsNone(TRADE_WORDS.search(proc.stdout))
        self.assertTrue((self.scans_dir / "2026-11-10.json").exists())
        self.assertIn("BKE", log_file.read_text(encoding="utf-8"))
        self.assertIn("strongest_alert:BKE", self._active_keys())

    def test_cli_refuses_second_concurrent_run(self):
        self._seed(["BKE"])
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({
            "BKE": _live("BKE", 50.0, 10e9),
        }), encoding="utf-8")
        lock_path, _stale = ss.acquire_lock(self.scans_dir)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "agents.scheduled_scan",
                 "run", "--scans-dir", str(self.scans_dir),
                 "--store-dir", str(self.store_dir),
                 "--alert-state-path", self.alert_state,
                 "--live-scan-path", self.live_scan_path,
                 "--fixture", str(fixture)],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                timeout=120)
        finally:
            ss.release_lock(lock_path)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already running", proc.stderr)

    # -- dashboard panel -----------------------------------------------

    def _dashboard_app(self, with_run):
        db_path = self.root / "assistant.db"
        shutil.copy(PROD_DB, db_path)
        data_dir = self.root / "data"
        if with_run:
            run = _scan([_record("BKE", band="strongest_alert",
                                 price=50.0),
                         _record("PLAIN", band=None, price=200.0)])
            run["run_id"] = "2026-11-10"
            run["scheduled"] = True
            run["scheduled_time_sgt"] = "05:30"
            run["run_seconds"] = 12.0
            run["diff"] = ss.diff_runs(
                _scan([_record("BKE", band=None, price=200.0)]), run)
            run["alerts"] = {"new_alerts": ["strongest_alert:BKE"],
                             "already_active": [],
                             "cleared_alerts": [],
                             "state_file": None}
            run["stale_lock_removed"] = False
            scans = data_dir / "scans"
            scans.mkdir(parents=True, exist_ok=True)
            (scans / "2026-11-10.json").write_text(
                json.dumps(run), encoding="utf-8")
        return ui.JarvisUI(
            db_path=db_path,
            store_dir=str(self.store_dir),
            news_dir=str(self.root / "news"),
            events_dir=str(self.root / "events"),
            alert_state_path=self.alert_state,
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(data_dir),
            live_scan_path=str(self.root / "no-scan.json"),
            readiness={"verdict": "not_ready_for_always_on",
                       "generated_at": "2026-11-09T00:00:00Z",
                       "blockers": ["fixture"]},
        )

    def test_dashboard_panel_with_stored_scan(self):
        app = self._dashboard_app(with_run=True)
        html = ui.dashboard_html(app)
        self.assertIn("Scheduled daily scan", html)
        self.assertIn("BKE", html)
        self.assertIn("New strongest-alert bands", html)
        self.assertIn("Scan history", html)
        self.assertIn("2026-11-10", html)
        self.assertNotIn("No scheduled scan has run yet", html)

    def test_dashboard_panel_without_stored_scan(self):
        app = self._dashboard_app(with_run=False)
        html = ui.dashboard_html(app)
        self.assertIn("Scheduled daily scan", html)
        self.assertIn("No scheduled scan has run yet", html)
        self.assertIn("05:30 SGT", html)


if __name__ == "__main__":
    unittest.main()
