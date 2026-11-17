"""Tests for Step 60 — the chat rebuild (agent/ui.py, app.js).

The chat submits turns with fetch to POST /api/chat and appends
them to an on-screen conversation history instead of reloading
the whole page; the Step 58 live fallback is wired into the
chat, ticker and research pages; and the model notice reflects
the model client actually wired in. All live pulls are stubbed
(no network): the fetcher is injected and returns canned
``get_company_financials`` dicts, and the stored scan path
points at a file that does not exist unless a test writes one.
The database is a temp copy; the production content fingerprint
is asserted unchanged.
"""

import json
import re
import shutil
import sqlite3
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
from agent import orchestrator as orch  # noqa: E402
from agent import ui  # noqa: E402
from agents import live_watchlist_scan as lws  # noqa: E402
from agents.watchlist_foundation import save_watchlist  # noqa: E402

PROD_DB = PROJECT_ROOT / "database" / "assistant.db"

# A control whose name, action, or label suggests trading must
# never appear in the served HTML.
_FORBIDDEN_CONTROL_RE = re.compile(
    r"<(?:button|input|select|form)[^>]*(?:buy|sell|trade|order|"
    r"place_order|checkout)",
    re.IGNORECASE,
)

# The real stored Step 54 NVDA values (run 2026-11-09 12:18 SGT,
# prices as of Fri 6 Nov close) — the fallback must reproduce
# them exactly from the live pull.
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


def _live_dbs():
    """A DBS pull: a bank, so the FCF DCF must be refused."""
    return _live(
        "D05.SI", 69.72, 5.0e9, debt=0.0, cash=0.0,
        shares=2.6e9, currency="SGD", financial_currency="SGD",
        sector="Banks", industry="Banks",
        fcf_period="2025-12-31")


def _fixture_fetcher(symbol):
    """The injected live pull, by Yahoo symbol."""
    if symbol == "NVDA":
        return _live_nvda()
    if symbol == "D05.SI":
        return _live_dbs()
    raise AssertionError(f"unexpected live fetch for {symbol!r}")


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


def _write_scan_file(path, records,
                     scanned_at="2026-11-09T04:36:44+00:00"):
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


def _make_app(db_path, store_dir, root, **overrides):
    """A JarvisUI for tests: stub model, fallback on, fixture fetch."""
    kwargs = {
        "db_path": db_path,
        "store_dir": store_dir,
        "news_dir": str(root / "news"),
        "events_dir": str(root / "events"),
        "alert_state_path": str(root / "alerts" / "state.json"),
        "model_client": orch.StubModelClient(reply="Stub reply."),
        "log_dir": str(root / "logs"),
        "data_dir": str(root / "data"),
        "live_scan_path": str(root / "no-scan.json"),
        "live_fallback": True,
        "live_fetcher": _fixture_fetcher,
    }
    kwargs.update(overrides)
    return ui.JarvisUI(**kwargs)


def _serve(app):
    """(base_url, shutdown) for a localhost server on an ephemeral port."""
    server = ui.make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return base, lambda: (server.shutdown(), server.server_close(),
                          thread.join(timeout=5))


class _HttpHelpers:
    def get(self, base, path):
        with urllib.request.urlopen(base + path) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), \
                resp.read().decode("utf-8")

    def post_form(self, base, path, question):
        data = urllib.parse.urlencode({"q": question}).encode("utf-8")
        req = urllib.request.Request(base + path, data=data)
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")

    def post_json(self, base, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + path, data=data)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            return (resp.status,
                    resp.headers.get("Content-Type", ""),
                    json.loads(resp.read().decode("utf-8")))


class TestChatApi(_HttpHelpers, unittest.TestCase):
    """POST /api/chat — one turn as JSON, with the live fallback."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        root = Path(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)
        cls.store_dir = str(root / "watchlist")
        _seed_watchlist(cls.store_dir, ["NVDA", "MSFT", "D05"])
        cls.stub = orch.StubModelClient(reply="Stub reply.")
        cls.app = _make_app(cls.db_path, cls.store_dir, root,
                            model_client=cls.stub)
        cls.base, cls._stop = _serve(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, (
            "production DB changed during chat rebuild tests")

    def test_api_chat_nvda_returns_live_value(self):
        # The defect Step 60 fixes: the UI chat answered "I do
        # not have enough data" for NVDA even after Step 58.
        status, content_type, payload = self.post_json(
            self.base, "/api/chat",
            {"q": "What is the intrinsic value of NVDA?"})
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(payload["status"], "ok")
        reply = payload["reply"]
        self.assertIn("$57.55", reply)
        self.assertIn("$82.19", reply)
        self.assertIn("live-derived", reply)
        self.assertIn("provisional", reply)
        self.assertNotIn("I do not have enough data", reply)
        self.assertTrue(any("live_fallback" in source
                            for source in payload["sources"]))

    def test_api_chat_nvda_from_stored_scan(self):
        # A fresh stored scan is preferred over a live pull: the
        # same values, labelled as the stored scan, with no
        # fetcher call at all.
        scan_path = _write_scan_file(
            Path(self.tmp.name) / "scan.json",
            [_stored_nvda_record()])
        app = _make_app(self.db_path, self.store_dir,
                        Path(self.tmp.name),
                        live_scan_path=scan_path,
                        live_fetcher=lambda s: (_ for _ in ()).throw(
                            AssertionError("live pull must not run")))
        base, stop = _serve(app)
        try:
            _, _, payload = self.post_json(
                base, "/api/chat",
                {"q": "What is the intrinsic value of NVDA?"})
        finally:
            stop()
        self.assertIn("$57.55", payload["reply"])
        self.assertIn("stored live scan", payload["reply"])
        self.assertNotIn("I do not have enough data", payload["reply"])

    def test_api_chat_msft_still_uses_stored_data(self):
        _, _, payload = self.post_json(
            self.base, "/api/chat",
            {"q": "What is the intrinsic value of MSFT?"})
        self.assertEqual(payload["status"], "ok")
        self.assertIn("$163.30", payload["reply"])
        # The stored answer is never replaced by the fallback.
        self.assertNotIn("live-derived", payload["reply"])

    def test_api_chat_trade_refused_and_no_model_call(self):
        before = len(self.stub.calls)
        _, _, payload = self.post_json(
            self.base, "/api/chat", {"q": "Buy 100 shares of MSFT"})
        self.assertEqual(payload["status"], "refused")
        self.assertIn("will not place a trade", payload["reply"])
        self.assertEqual(len(self.stub.calls), before,
                         "a refused trade must not call the model")

    def test_api_chat_empty_question(self):
        _, _, payload = self.post_json(
            self.base, "/api/chat", {"q": ""})
        self.assertEqual(payload["status"], "unavailable")
        self.assertIn("did not receive a request", payload["reply"])

    def test_api_chat_unknown_route_404(self):
        req = urllib.request.Request(
            self.base + "/api/nope",
            data=json.dumps({"q": "hi"}).encode("utf-8"))
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


class TestChatHistory(_HttpHelpers, unittest.TestCase):
    """The conversation stays on screen across turns."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        root = Path(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)
        cls.store_dir = str(root / "watchlist")
        _seed_watchlist(cls.store_dir, ["NVDA", "MSFT", "D05"])
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, (
            "production DB changed during chat rebuild tests")

    def test_history_across_api_turns(self):
        # Two turns through the JSON endpoint, then a plain page
        # load: both turns are on screen, in order, and exactly
        # one reply carries the speakable id.
        app = _make_app(self.db_path, self.store_dir, self.root,
                        log_dir=str(self.root / "logs-history"))
        base, stop = _serve(app)
        try:
            self.post_json(
                base, "/api/chat",
                {"q": "What is the intrinsic value of MSFT?"})
            self.post_json(
                base, "/api/chat",
                {"q": "What is the intrinsic value of NVDA?"})
            _, _, body = self.get(base, "/chat")
        finally:
            stop()
        self.assertIn('id="chat-history"', body)
        self.assertIn("What is the intrinsic value of MSFT?", body)
        self.assertIn("What is the intrinsic value of NVDA?", body)
        self.assertLess(
            body.index("What is the intrinsic value of MSFT?"),
            body.index("What is the intrinsic value of NVDA?"))
        self.assertIn("$163.30", body)
        self.assertIn("$57.55", body)
        self.assertEqual(body.count('id="jarvis-reply-text"'), 1)

    def test_form_post_renders_history_without_js(self):
        # The no-JavaScript path: a plain form post reloads the
        # page and the turn is in the rendered history.
        app = _make_app(self.db_path, self.store_dir, self.root,
                        log_dir=str(self.root / "logs-form"))
        base, stop = _serve(app)
        try:
            status, body = self.post_form(
                base, "/chat", "What is the intrinsic value of MSFT?")
            self.assertEqual(status, 200)
            self.assertIn("What is the intrinsic value of MSFT?",
                          body)
            self.assertIn("$163.30", body)
            self.assertIn("Sources", body)
            self.assertEqual(body.count('id="jarvis-reply-text"'), 1)
        finally:
            stop()

    def test_history_renders_when_logging_is_off(self):
        # With log_dir=False there is no turn log to read, so the
        # turn just answered must still render from memory.
        app = _make_app(self.db_path, self.store_dir, self.root,
                        log_dir=False)
        base, stop = _serve(app)
        try:
            _, body = self.post_form(
                base, "/chat", "What is the intrinsic value of MSFT?")
            self.assertIn("What is the intrinsic value of MSFT?",
                          body)
            self.assertIn("$163.30", body)
            self.assertEqual(body.count('id="jarvis-reply-text"'), 1)
        finally:
            stop()


class TestModelNotice(unittest.TestCase):
    """The notice names the model client actually wired in."""

    def _notice(self, **kwargs):
        app = ui.JarvisUI(db_path=":memory:", **kwargs)
        return ui.chat_html(app)

    def test_stub_client_gets_stub_warning(self):
        body = self._notice(
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False)
        self.assertIn("What is missing here", body)
        self.assertIn("no live Ollama turn", body)
        self.assertNotIn("Live model connected", body)

    def test_live_client_gets_live_status(self):
        body = self._notice(
            model_client=orch.OllamaModelClient(), log_dir=False)
        self.assertIn("Live model connected", body)
        self.assertIn("Ollama", body)
        self.assertNotIn("no live Ollama turn", body)
        self.assertNotIn("stub model", body)

    def test_router_client_gets_live_status(self):
        from agent import cloud_models as cm
        router = cm.ModelRouter(
            local_client=orch.OllamaModelClient(), cloud_clients=[])
        body = self._notice(model_client=router, log_dir=False)
        self.assertIn("Live model connected", body)
        self.assertNotIn("stub model", body)

    def test_no_client_says_so(self):
        body = self._notice(model_client=None, log_dir=False)
        self.assertIn("No model is connected", body)
        self.assertNotIn("Live model connected", body)

    def test_unknown_client_is_not_claimed_live_or_stub(self):
        class MysteryClient:
            def chat(self, messages):
                return "mystery"

        body = self._notice(model_client=MysteryClient(),
                            log_dir=False)
        self.assertIn("MysteryClient", body)
        self.assertNotIn("Live model connected", body)
        self.assertNotIn("stub model", body)


class TestTickerAndResearchFallback(_HttpHelpers, unittest.TestCase):
    """The ticker and research pages use the live fallback too."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        root = Path(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)
        cls.store_dir = str(root / "watchlist")
        _seed_watchlist(cls.store_dir, ["NVDA", "MSFT", "D05"])
        cls.app = _make_app(cls.db_path, cls.store_dir, root)
        cls.base, cls._stop = _serve(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, (
            "production DB changed during chat rebuild tests")

    def test_ticker_nvda_live_card(self):
        _, _, body = self.get(self.base, "/ticker/NVDA")
        self.assertIn("Live-derived value", body)
        self.assertIn("$57.55", body)
        self.assertIn("$82.19", body)
        self.assertIn("provisional", body)
        self.assertIn("$186.64", body)

    def test_ticker_msft_keeps_stored_answer(self):
        _, _, body = self.get(self.base, "/ticker/MSFT")
        self.assertIn("$163.30", body)
        self.assertNotIn("Live-derived value", body)

    def test_ticker_bank_refused_no_dcf(self):
        _, _, body = self.get(self.base, "/ticker/D05")
        self.assertIn("Live-derived value", body)
        self.assertIn("FCF DCF refused for a bank", body)
        self.assertIn("insufficient", body.lower())
        # MSFT's price must not leak into the DBS page.
        self.assertNotIn("$507.12", body)

    def test_research_nvda_uses_live_fallback(self):
        status, body = self.post_form(
            self.base, "/research", "Research NVDA")
        self.assertEqual(status, 200)
        self.assertIn("Research brief", body)
        self.assertIn("$57.55", body)
        self.assertIn("live_fallback", body)

    def test_no_trade_controls_on_rebuilt_pages(self):
        for path in ("/chat", "/ticker/NVDA", "/ticker/D05",
                     "/research"):
            _, _, body = self.get(self.base, path)
            self.assertIsNone(
                _FORBIDDEN_CONTROL_RE.search(body),
                f"a trading control appears on {path}")


class TestChatPageWiring(_HttpHelpers, unittest.TestCase):
    """The page and script for the fetch-based chat."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        root = Path(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)
        cls.store_dir = str(root / "watchlist")
        _seed_watchlist(cls.store_dir, ["NVDA", "MSFT", "D05"])
        cls.app = _make_app(cls.db_path, cls.store_dir, root)
        cls.base, cls._stop = _serve(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, (
            "production DB changed during chat rebuild tests")

    def test_chat_page_has_history_and_fetch_form(self):
        _, _, body = self.get(self.base, "/chat")
        self.assertIn('id="chat-history"', body)
        self.assertIn('id="chat-form"', body)
        self.assertIn("No conversation yet", body)
        # The voice panel is still there, unchanged.
        self.assertIn('id="voice-panel"', body)
        self.assertIn('id="voice-mic"', body)
        self.assertIn('id="voice-toggle"', body)
        self.assertIn('id="voice-picker"', body)

    def test_chat_js_posts_to_api_and_speaks_replies(self):
        _, _, js = self.get(self.base, "/static/app.js")
        self.assertIn('fetch("/api/chat"', js)
        self.assertIn('"jarvis-reply"', js)
        self.assertIn("requestSubmit", js)
        # A voice transcript still goes through the fetch path:
        # requestSubmit (not submit) so the submit handler runs.
        self.assertIn("form.requestSubmit()", js)

    def test_fallback_off_by_default(self):
        # The Step 58 rule: a UI that does not opt in never
        # touches the live machinery — NVDA stays "not enough
        # data" even with a fetcher supplied.
        app = _make_app(self.db_path, self.store_dir,
                        Path(self.tmp.name), live_fallback=False,
                        log_dir=False)
        base, stop = _serve(app)
        try:
            _, _, payload = self.post_json(
                base, "/api/chat",
                {"q": "What is the intrinsic value of NVDA?"})
        finally:
            stop()
        self.assertIn("I do not have enough data", payload["reply"])
        self.assertNotIn("$57.55", payload["reply"])


if __name__ == "__main__":
    unittest.main()
