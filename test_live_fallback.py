"""Tests for agents/live_fallback.py (Step 58).

All live pulls are stubbed (no network): the fetcher is
injected and returns canned ``get_company_financials`` dicts.
The one live smoke test is behind JARVIS_LIVE_FALLBACK=1 and
treats a provider outage as a valid outcome (the reason must
be stated), so it never fails spuriously on network flakiness.
The production DB is copied once per test class into the temp
directory; the production content fingerprint is asserted
unchanged.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
from agent import orchestrator as orch  # noqa: E402
from agent import research_agents as ra  # noqa: E402
from agent.orchestrator import StubModelClient, handle  # noqa: E402
from agents import live_fallback as lfb  # noqa: E402
from agents import live_watchlist_scan as lws  # noqa: E402
from agents.watchlist_foundation import save_watchlist  # noqa: E402
from tools import finance as finance_mod  # noqa: E402

PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
REAL_SCAN = PROJECT_ROOT / "step54-live-scan.json"

# The real stored Step 54 NVDA values (run 2026-11-09 12:18 SGT,
# prices as of Fri 6 Nov close) — the fallback must reproduce
# them exactly from the stored scan.
NVDA_DCF = {"conservative": 57.55, "base": 82.19,
            "optimistic": 117.66}
NVDA_PRICE = 186.64


def _live(symbol, price, fcf, debt=0.0, cash=0.0, shares=1e9,
          currency="USD", financial_currency=None,
          sector="Technology", industry="Semiconductors",
          fcf_period="2026-06-30"):
    """A get_company_financials-shaped live pull."""
    return {
        "ticker": symbol,
        "company_name": f"{symbol} Co",
        "sector": sector,
        "industry": industry,
        "currency": currency,
        "financial_currency": financial_currency,
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


def _live_nvda():
    """A realistic NVDA pull matching the stored Step 54 values."""
    return _live(
        "NVDA", NVDA_PRICE, 96.676e9, debt=0.435e9, cash=0.0,
        shares=24.147e9, currency="USD", financial_currency="USD",
        sector="Technology", industry="Semiconductors",
        fcf_period="2026-01-31")


def _stored_nvda_record():
    """A Step 54 record for NVDA, as written by the live scan."""
    return {
        "ticker": "NVDA",
        "yahoo_symbol": "NVDA",
        "status": "ok",
        "detection_band": None,
        "provisional": False,
        "deterioration_unknown": True,
        "quality_unknown": True,
        "dcf_basis": lws.DCF_BASIS_TEXT,
        "company_name": "NVIDIA Corporation",
        "sector": "Technology",
        "currency": "USD",
        "price_as_of": "2026-11-06T21:00:00+00:00",
        "retrieved_at": "2026-11-09T04:36:44+00:00",
        "price": NVDA_PRICE,
        "dcf_notes": [],
        "fair_value_range": {
            "conservative": 57.5485963416729,
            "base": 82.18922267594483,
            "optimistic": 117.66391984005053,
        },
        "latest_fcf": 96676000000.0,
        "latest_fcf_period": "2026-01-31",
        "net_debt": 435000000.0,
        "shares_outstanding": 24147000000.0,
        "margins": {
            "conservative": -2.243172064387044,
            "base": -1.2708573450789655,
            "optimistic": -0.5862126661572542,
        },
        "margin_of_safety_vs_conservative": -2.243172064387044,
        "margin_of_safety_vs_base": -1.2708573450789655,
        "margin_of_safety_vs_optimistic": -0.5862126661572542,
        "strongest_alert_price": 46.03887707333832,
        "tier": "overvalued",
        "tier_reasons": ["price is above the optimistic value"],
        "strongest_alert_blocked": False,
        "conditions_to_change": [],
    }


def _write_scan_file(path, records, scanned_at="2026-11-09T04:36:44+00:00"):
    data = {
        "results": records,
        "summary": {"selected": len(records)},
        "scanned_at": scanned_at,
        "method": {"dcf_basis": lws.DCF_BASIS_TEXT},
        "limitations": [],
    }
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _seed_watchlist(store_dir, tickers):
    save_watchlist([
        {"ticker": t, "universe_ticker": None,
         "company_name": f"{t} Co", "sources": ["test"],
         "reason": "test", "notes": [],
         "date_added": "2026-11-09"}
        for t in tickers
    ], store_dir=str(store_dir))


class LiveFallbackTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        cls.db_path = Path(cls.tmp.name) / "assistant.db"
        shutil.copy(PROD_DB, cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)
        cls.store_dir = Path(cls.tmp.name) / "watchlist"
        _seed_watchlist(cls.store_dir, ["NVDA", "DBS", "KSPI"])
        cls.stub = StubModelClient(reply="Stub chat reply.")
        # A live-scan path that does not exist: the stored path
        # is skipped and the stub fetcher is used.
        cls.no_scan = str(Path(cls.tmp.name) / "no-scan.json")

    @classmethod
    def tearDownClass(cls):
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, \
            "production DB changed"

    def _handle(self, text, **kwargs):
        kwargs.setdefault("model_client", self.stub)
        kwargs.setdefault("db_path", str(self.db_path))
        kwargs.setdefault("log_dir", Path(self.tmp.name) / "turns")
        kwargs.setdefault("store_dir", str(self.store_dir))
        return handle(text, **kwargs)

    # -- stored-scan reuse ------------------------------------------

    def test_stored_scan_nvda_reproduces_step54_values(self):
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()])
        result = lfb.live_valuation(
            "NVDA", live_scan_path=scan_path, explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "stored_scan")
        rng = result["record"]["fair_value_range"]
        for name, expected in NVDA_DCF.items():
            self.assertAlmostEqual(rng[name], expected, places=2)
        self.assertEqual(result["record"]["price"], NVDA_PRICE)
        self.assertEqual(result["record"]["tier"], "overvalued")

    def test_stored_scan_used_without_fetch(self):
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()])

        def _boom(symbol):
            raise AssertionError("fetcher must not be called")

        result = lfb.live_valuation(
            "NVDA", live_scan_path=scan_path, fetcher=_boom,
            explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "stored_scan")

    def test_stale_stored_scan_falls_through_to_live_fetch(self):
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()],
            scanned_at="2026-10-01T04:36:44+00:00")
        calls = []
        result = lfb.live_valuation(
            "NVDA", live_scan_path=scan_path,
            fetcher=lambda s: calls.append(s) or _live_nvda(),
            explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "live_pull")
        self.assertEqual(calls, ["NVDA"])

    def test_force_live_ignores_stored_scan(self):
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()])
        result = lfb.live_valuation(
            "NVDA", live_scan_path=scan_path, force_live=True,
            fetcher=lambda s: _live_nvda(), explicitly_named=True)
        self.assertEqual(result["source"], "live_pull")

    def test_missing_stored_ticker_fetches_live(self):
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()])
        result = lfb.live_valuation(
            "GOOGL", live_scan_path=scan_path,
            fetcher=lambda s: _live("GOOGL", 284.78, 50e9),
            explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "live_pull")

    # -- live pull ----------------------------------------------------

    def test_live_pull_nvda_matches_stored_values(self):
        result = lfb.live_valuation(
            "NVDA", live_scan_path=self.no_scan,
            fetcher=lambda s: _live_nvda(), explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "live_pull")
        rng = result["record"]["fair_value_range"]
        for name, expected in NVDA_DCF.items():
            self.assertAlmostEqual(rng[name], expected, places=2)

    def test_live_reply_carries_every_label(self):
        result = lfb.live_valuation(
            "NVDA", live_scan_path=self.no_scan,
            fetcher=lambda s: _live_nvda(), explicitly_named=True)
        reply = lfb.format_live_valuation(result)
        for label in ("live pull", "as of 2026-11-06", "WACC 11%",
                      "terminal growth 2.5%", "provisional",
                      "unknown from a live pull",
                      "not a recommendation", "$57.55", "$82.19",
                      "$117.66", "$186.64", "$46.04"):
            self.assertIn(label, reply, label)

    def test_stored_reply_labels_the_scan_timestamp(self):
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()])
        result = lfb.live_valuation(
            "NVDA", live_scan_path=scan_path, explicitly_named=True)
        reply = lfb.format_live_valuation(result)
        self.assertIn("stored live scan run at", reply)
        self.assertIn("2026-11-09T04:36:44", reply)
        self.assertIn("not stored statements", reply)

    # -- exclusions ---------------------------------------------------

    def test_bank_never_gets_fcf_dcf(self):
        bank = _live("DBS", 69.72, 5e9, sector="Financial Services",
                     industry="Banks - Regional", currency="SGD",
                     financial_currency="SGD")
        result = lfb.live_valuation(
            "DBS", live_scan_path=self.no_scan,
            fetcher=lambda s: bank, explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("bank", result["reason"].lower())
        self.assertNotIn("fair_value_range",
                         str(result.get("record") or ""))

    def test_stored_bank_band_is_refused_not_repeated(self):
        stored_dbs = dict(_stored_nvda_record())
        stored_dbs.update({
            "ticker": "DBS", "sector": "Financial Services",
            "currency": "SGD", "price": 69.72,
            "fair_value_range": {
                "conservative": 79.35, "base": 108.0,
                "optimistic": 145.0},
            "margin_of_safety_vs_conservative": 0.12,
            "detection_band": "watch_discount",
        })
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json", [stored_dbs])
        result = lfb.live_valuation(
            "DBS", live_scan_path=scan_path, explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("bank", result["reason"].lower())

    def test_currency_mismatch_refused(self):
        kspi = _live("KSPI", 91.68, 491.098e9, shares=199e6,
                     currency="USD", financial_currency="KZT")
        result = lfb.live_valuation(
            "KSPI", live_scan_path=self.no_scan,
            fetcher=lambda s: kspi, explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("Currency mismatch", result["reason"])
        self.assertIn("KZT", result["reason"])
        self.assertIn("USD", result["reason"])

    def test_currency_mismatch_without_field_uses_magnitude_guard(self):
        # Stored scans written before Step 58 carry no reporting
        # currency — KSPI's 99.8% margin must still be refused.
        stored_kspi = dict(_stored_nvda_record())
        stored_kspi.update({
            "ticker": "KSPI", "sector": "Technology",
            "currency": "USD", "price": 91.68,
            "fair_value_range": {
                "conservative": 39436.0, "base": 55341.0,
                "optimistic": 78240.0},
            "margin_of_safety_vs_conservative": 0.9977,
            "detection_band": "strongest_alert",
        })
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json", [stored_kspi])
        result = lfb.live_valuation(
            "KSPI", live_scan_path=scan_path, explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("Suspected currency", result["reason"])

    def test_unknown_sector_allowed_with_warning(self):
        mystery = _live("MYST", 50.0, 10e9, sector=None,
                        industry=None)
        result = lfb.live_valuation(
            "MYST", live_scan_path=self.no_scan,
            fetcher=lambda s: mystery, explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any("sector is unknown" in w.lower()
                            for w in result["warnings"]))

    def test_negative_fcf_is_insufficient_data(self):
        result = lfb.live_valuation(
            "INTC", live_scan_path=self.no_scan,
            fetcher=lambda s: _live("INTC", 30.0, -5e9),
            explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("not positive", result["reason"])

    # -- provider failure ---------------------------------------------

    def test_fetch_exception_is_provider_unavailable(self):
        def _boom(symbol):
            raise RuntimeError("curl timeout")

        result = lfb.live_valuation(
            "NVDA", live_scan_path=self.no_scan, fetcher=_boom,
            explicitly_named=True)
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertIn("curl timeout", result["reason"])

    def test_provider_unavailable_flag_passes_through(self):
        def _down(symbol):
            return {"ticker": symbol, "data_available": False,
                    "provider_unavailable": True,
                    "provider_error": "crumb fetch timed out"}

        result = lfb.live_valuation(
            "NVDA", live_scan_path=self.no_scan, fetcher=_down,
            explicitly_named=True)
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertIn("crumb", result["reason"])

    def test_genuine_no_data_is_insufficient_not_unavailable(self):
        def _nodata(symbol):
            return {"ticker": symbol, "data_available": False}

        result = lfb.live_valuation(
            "ZZZQ", live_scan_path=self.no_scan, fetcher=_nodata,
            explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("no live financial data", result["reason"])

    # -- gate -----------------------------------------------------------

    def test_off_watchlist_unrequested_ticker_is_refused(self):
        calls = []

        def _boom(symbol):
            calls.append(symbol)
            raise AssertionError("fetcher must not be called")

        result = lfb.live_valuation(
            "GOOGL", store_dir=str(self.store_dir),
            live_scan_path=self.no_scan, fetcher=_boom,
            explicitly_named=False)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("not on the watchlist", result["reason"])
        self.assertEqual(calls, [])

    def test_cache_shared_within_a_session(self):
        calls = []
        cache = {}

        def _fetch(symbol):
            calls.append(symbol)
            return _live_nvda()

        first = lfb.live_valuation(
            "NVDA", live_scan_path=self.no_scan, fetcher=_fetch,
            explicitly_named=True, cache=cache)
        second = lfb.live_valuation(
            "NVDA", live_scan_path=self.no_scan, fetcher=_fetch,
            explicitly_named=True, cache=cache)
        self.assertEqual(first, second)
        self.assertEqual(calls, ["NVDA"])

    # -- provider pass-through (Step 58 field) ------------------------

    def test_financial_currency_passes_through_finance(self):
        old_manager = finance_mod.provider_manager

        class _StubManager:
            def get_company_profile(self, ticker,
                                    provider_name="yahoo"):
                return {"company_name": "KSPI Co",
                        "currency": "USD",
                        "financial_currency": "KZT"}

            def get_company_financials(self, ticker,
                                       provider_name="yahoo"):
                return {
                    "provider": "Yahoo Finance",
                    "retrieved_at_utc": "2026-11-09T00:00:00+00:00",
                    "price_data_as_of_utc":
                        "2026-11-06T21:00:00+00:00",
                    "financial_statements_latest_period": {},
                    "financials": {
                        "current_price": 91.68,
                        "free_cash_flow": {"2025-12-31": 491098e6},
                        "total_debt": {}, "cash": {},
                        "shares_outstanding": 199e6,
                    },
                    "data_available": True,
                }

        try:
            finance_mod.provider_manager = _StubManager()
            result = finance_mod.get_company_financials(
                "KSPI", sleep=lambda s: None)
        finally:
            finance_mod.provider_manager = old_manager
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["financial_currency"], "KZT")

    def test_scan_ticker_live_records_financial_currency(self):
        record = lws.scan_ticker_live(
            "KSPI", fetcher=lambda s: _live(
                "KSPI", 91.68, 491.098e9, shares=199e6,
                currency="USD", financial_currency="KZT"),
            yahoo_symbol_override="KSPI")
        self.assertEqual(record["financial_currency"], "KZT")

    # -- orchestrator ---------------------------------------------------

    def test_orchestrator_nvda_uses_live_fallback(self):
        result = self._handle(
            "What is the intrinsic value of NVDA?",
            live_fallback=True, live_fetcher=lambda s: _live_nvda(),
            live_scan_path=self.no_scan)
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(result["status"], "ok")
        for expected in ("$57.55", "$82.19", "$117.66", "$186.64",
                         "live", "provisional", "not a recommendation"):
            self.assertIn(expected, result["reply"], expected)
        self.assertEqual(result["modules_called"],
                         ["agents/live_fallback.py",
                          "agents/live_watchlist_scan.py"])
        self.assertTrue(any("Step 58" in s for s in result["sources"]))

    def test_orchestrator_nvda_without_fallback_is_unchanged(self):
        calls = []

        def _boom(symbol):
            calls.append(symbol)
            raise AssertionError("fetcher must not be called")

        result = self._handle(
            "What is the intrinsic value of NVDA?",
            live_fetcher=_boom)
        self.assertEqual(result["status"], "ok")
        self.assertIn("do not have enough data", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/margin_of_safety.py"])
        self.assertEqual(calls, [])

    def test_orchestrator_msft_still_uses_stored_data(self):
        result = self._handle(
            "What is the intrinsic value of MSFT?",
            live_fallback=True,
            live_fetcher=lambda s: (_ for _ in ()).throw(
                AssertionError("fetcher must not be called")),
            live_scan_path=self.no_scan)
        self.assertEqual(result["status"], "ok")
        self.assertIn("$98.52", result["reply"])
        self.assertIn("$163.30", result["reply"])
        self.assertIn("$234.64", result["reply"])
        self.assertNotIn("live-derived", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/margin_of_safety.py"])

    def test_orchestrator_bank_fallback_refuses_without_figures(self):
        # "DBS" is not a market_universe ticker (the SG listing
        # is D05.SI), so the question names the universe ticker.
        # The watchlist alias DBS -> D05 is not resolved by the
        # orchestrator's ticker extraction — a pre-existing
        # limitation, not a Step 58 concern.
        bank = _live("DBS", 69.72, 5e9,
                     sector="Financial Services",
                     industry="Banks - Regional", currency="SGD",
                     financial_currency="SGD")
        result = self._handle(
            "What is the intrinsic value of D05.SI?",
            live_fallback=True, live_fetcher=lambda s: bank,
            live_scan_path=self.no_scan)
        self.assertEqual(result["status"], "ok")
        self.assertIn("bank", result["reply"].lower())
        self.assertNotIn("$79.35", result["reply"])
        self.assertIn("live fallback also could not value it",
                      result["reply"])

    def test_orchestrator_provider_down_reports_it(self):
        def _down(symbol):
            return {"ticker": symbol, "data_available": False,
                    "provider_unavailable": True,
                    "provider_error": "crumb fetch timed out"}

        result = self._handle(
            "What is the intrinsic value of NVDA?",
            live_fallback=True, live_fetcher=_down,
            live_scan_path=self.no_scan)
        self.assertEqual(result["status"], "ok")
        self.assertIn("live fallback also could not value it",
                      result["reply"])
        self.assertIn("crumb", result["reply"])

    # -- research session ----------------------------------------------

    def test_research_nvda_uses_live_fallback(self):
        brief = ra.run_research_session(
            "Research NVDA", db_path=str(self.db_path),
            save_dir=False, live_fallback=True,
            live_fetcher=lambda s: _live_nvda(),
            live_scan_path=self.no_scan,
            store_dir=str(self.store_dir))
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["scan"]["status"], "ok")
        self.assertEqual(by_id["valuation"]["status"], "ok")
        # The full report needs stored statements, so the
        # session still stops there — with the live claims kept.
        self.assertEqual(by_id["report"]["status"],
                         "insufficient_data")
        self.assertTrue(any(
            "NVDA" in c["text"] and "live" in c["source"].lower()
            for c in brief["claims"]))
        self.assertTrue(any(
            "$57.55" in c["text"] or "57.55" in c["text"]
            for c in brief["claims"]))
        self.assertTrue(any(
            "report" in item.lower()
            and "financial statement" in item.lower()
            for item in brief["limitations"]))

    def test_research_nvda_without_fallback_stops_early(self):
        brief = ra.run_research_session(
            "Research NVDA", db_path=str(self.db_path),
            save_dir=False)
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["scan"]["status"], "insufficient_data")
        self.assertEqual(by_id["valuation"]["status"], "not_attempted")
        self.assertFalse(any(
            "live" in c["source"].lower() for c in brief["claims"]))

    def test_research_msft_unchanged_with_fallback(self):
        brief = ra.run_research_session(
            "Research MSFT", db_path=str(self.db_path),
            save_dir=False, live_fallback=True,
            live_fetcher=lambda s: (_ for _ in ()).throw(
                AssertionError("fetcher must not be called")),
            live_scan_path=self.no_scan,
            store_dir=str(self.store_dir))
        by_id = {s["id"]: s for s in brief["steps"]}
        self.assertEqual(by_id["scan"]["status"], "ok")
        self.assertEqual(by_id["valuation"]["status"], "ok")
        self.assertEqual(by_id["report"]["status"], "ok")
        self.assertFalse(any(
            "live" in c["source"].lower() for c in brief["claims"]))

    # -- real stored scan (repo file) -----------------------------------

    def test_real_stored_scan_nvda(self):
        self.assertTrue(REAL_SCAN.exists(),
                        "step54-live-scan.json must exist in the repo")
        result = lfb.live_valuation("NVDA", explicitly_named=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "stored_scan")
        rng = result["record"]["fair_value_range"]
        for name, expected in NVDA_DCF.items():
            self.assertAlmostEqual(rng[name], expected, places=2)

    def test_real_stored_scan_kspi_refused(self):
        result = lfb.live_valuation("KSPI", explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("Suspected currency", result["reason"])

    def test_real_stored_scan_dbs_refused(self):
        result = lfb.live_valuation("DBS", explicitly_named=True)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("bank", result["reason"].lower())


@unittest.skipUnless(
    os.environ.get("JARVIS_LIVE_FALLBACK") == "1",
    "live smoke test: set JARVIS_LIVE_FALLBACK=1 to run")
class LiveFallbackSmokeTestCase(unittest.TestCase):
    def test_live_nvda_fetch(self):
        # One real Yahoo fetch.  A provider outage is a valid
        # outcome here — the reason must be stated — because
        # Yahoo is flaky; only a returned value is checked
        # against the stored scan.
        result = lfb.live_valuation("NVDA", force_live=True,
                                    explicitly_named=True)
        if result["status"] == "provider_unavailable":
            self.assertTrue(result["reason"])
            return
        self.assertEqual(result["status"], "ok")
        rng = result["record"]["fair_value_range"]
        for name, expected in NVDA_DCF.items():
            self.assertAlmostEqual(rng[name], expected,
                                   delta=expected * 0.05)


if __name__ == "__main__":
    unittest.main()
