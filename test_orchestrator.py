"""Step 39 — Unified AI orchestration: tests.

Stubbed model client only; no network, no production writes.
The production DB is copied once per test class into the temp
directory and the copy is removed in tearDownClass; the
production hash is asserted unchanged. Every test cleans up
after itself via TemporaryDirectory (the /tmp housekeeping gap
noted in Steps 27/33b: older _temp_db_copy helpers never delete
their copies).
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import orchestrator as orch
from agent.orchestrator import (
    ModelUnavailable,
    OllamaModelClient,
    Orchestrator,
    StubModelClient,
    classify_intent,
    extract_tickers,
    handle,
)
from agents import watchlist_foundation as wf

PROJECT_ROOT = Path(__file__).resolve().parent
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"
NEWS_DIR = PROJECT_ROOT / "data" / "news"


def _db_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OrchestratorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "assistant.db"
        shutil.copy(PROD_DB, cls.db_path)
        cls.log_dir = Path(cls.tmp.name) / "orchestration"
        cls.store_dir = Path(cls.tmp.name) / "watchlist"
        cls.stub = StubModelClient(reply="Stub chat reply.")

    @classmethod
    def tearDownClass(cls):
        assert _db_hash(PROD_DB) == PROD_HASH, \
            "production DB hash changed"
        cls.tmp.cleanup()

    def _handle(self, text, **kwargs):
        kwargs.setdefault("model_client", self.stub)
        kwargs.setdefault("db_path", str(self.db_path))
        kwargs.setdefault("log_dir", self.log_dir)
        return handle(text, **kwargs)

    # -- intent classification -------------------------------------

    def test_classify_each_intent_by_keyword(self):
        cases = {
            "What is the intrinsic value of MSFT?": "valuation",
            "What is MSFT's revenue and free cash flow?": "financial_research",
            "Show me my watchlist": "watchlist",
            "Any news on MSFT?": "news",
            "How did my portfolio perform?": "portfolio",
            "Buy 100 shares of MSFT": "trade",
            "Remind me to check NVDA tomorrow": "schedule_send",
            "Hello, how are you?": "chat",
        }
        for text, expected in cases.items():
            result = classify_intent(text)
            self.assertEqual(result["intent"], expected, text)
            self.assertEqual(result["method"], "keyword", text)

    def test_classify_unknown_without_model(self):
        result = classify_intent("blorp zibble wumpus")
        self.assertEqual(result["intent"], "unknown")
        self.assertEqual(result["method"], "keyword")

    def test_classify_model_fallback_labelled(self):
        stub = StubModelClient(reply="valuation")
        result = classify_intent("blorp zibble wumpus",
                                 model_client=stub)
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(result["method"], "model_fallback")
        self.assertEqual(result["model_classification"], "valuation")

    def test_classify_model_fallback_unavailable_is_unknown(self):
        stub = StubModelClient(raise_unavailable=True)
        result = classify_intent("blorp zibble wumpus",
                                 model_client=stub)
        self.assertEqual(result["intent"], "unknown")

    # -- ticker extraction -----------------------------------------

    def test_extract_tickers_validated_against_universe(self):
        import sqlite3
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            tickers = extract_tickers(
                "What is the valuation of MSFT?", conn=conn)
            self.assertEqual(tickers, ["MSFT"])
            # A plausible-looking token that is not a ticker is dropped.
            tickers = extract_tickers("What about ZZZQ?", conn=conn)
            self.assertEqual(tickers, [])
        finally:
            conn.close()

    def test_extract_tickers_normalises_brk_b(self):
        import sqlite3
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            tickers = extract_tickers("BRK.B valuation?", conn=conn)
            self.assertEqual(tickers, ["BRK-B"])
        finally:
            conn.close()

    # -- valuation route (MSFT, real data) -------------------------

    def test_valuation_msft_traces_to_step_21_23(self):
        result = self._handle("What is the intrinsic value of MSFT?")
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(result["status"], "ok")
        self.assertIn("overvalued", result["reply"])
        self.assertIn("$98.52", result["reply"])  # Step 21 bear/conservative
        self.assertIn("$163.30", result["reply"])  # Step 21 base
        self.assertIn("$234.64", result["reply"])  # Step 21 optimistic
        self.assertIn("$78.82", result["reply"])  # Step 23 alert price
        self.assertIn("excellent", result["reply"])  # Step 24 quality
        self.assertEqual(result["modules_called"],
                         ["agents/margin_of_safety.py"])
        self.assertTrue(any("Step 23" in s for s in result["sources"]))

    def test_valuation_without_ticker_is_unavailable(self):
        result = self._handle("What is the intrinsic value?")
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("Which company", result["reply"])

    def test_valuation_without_db_is_unavailable(self):
        result = self._handle(
            "What is the intrinsic value of MSFT?", db_path=None)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["modules_called"], [])

    def test_valuation_insufficient_data_ticker(self):
        # D05 is DBS in the universe; it has announcement metadata
        # only, so no valuation range can be computed.
        result = self._handle("What is the intrinsic value of D05?")
        self.assertEqual(result["status"], "ok")
        self.assertIn("do not have enough data", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/margin_of_safety.py"])

    # -- financial research route ----------------------------------

    def test_financial_research_msft(self):
        result = self._handle(
            "What is MSFT's revenue and free cash flow?")
        self.assertEqual(result["intent"], "financial_research")
        self.assertEqual(result["status"], "ok")
        self.assertIn("overvalued", result["reply"])
        self.assertIn("excellent", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/market_scanner.py"])

    # -- watchlist route -------------------------------------------

    def test_watchlist_route_lists_seeded_entries(self):
        wf.seed_watchlist(store_dir=self.store_dir)
        result = self._handle("Show me my watchlist",
                              store_dir=self.store_dir)
        self.assertEqual(result["intent"], "watchlist")
        self.assertEqual(result["status"], "ok")
        self.assertIn("MSFT", result["reply"])
        self.assertIn("GOOGL", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/watchlist_foundation.py"])

    def test_watchlist_route_empty_store(self):
        empty_dir = Path(self.tmp.name) / "empty-watchlist"
        result = self._handle("Show me my watchlist",
                              store_dir=empty_dir)
        self.assertEqual(result["status"], "ok")
        self.assertIn("empty", result["reply"])

    # -- news route ------------------------------------------------

    def test_news_route_lists_stored_articles(self):
        result = self._handle("Any news on MSFT?", news_dir=NEWS_DIR)
        self.assertEqual(result["intent"], "news")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Stored news for MSFT", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/news_collection.py"])

    def test_news_route_no_stored_articles(self):
        empty_dir = Path(self.tmp.name) / "empty-news"
        result = self._handle("Any news on MSFT?", news_dir=empty_dir)
        self.assertEqual(result["status"], "ok")
        self.assertIn("No stored news articles for MSFT", result["reply"])

    # -- portfolio route -------------------------------------------

    def test_portfolio_route_fixture(self):
        context = {
            "holdings": [
                {"ticker": "ALPHA", "shares": 100,
                 "cost_basis": 10.00},
                {"ticker": "BETA", "shares": 50,
                 "cost_basis": 20.00},
            ],
            "price_history": {
                "ALPHA": [("2026-07-01", 10.00),
                          ("2026-10-01", 15.00)],
                "BETA": [("2026-07-01", 20.00),
                         ("2026-10-01", 18.00)],
            },
            "currency": "USD",
        }
        result = self._handle("How did my portfolio perform?",
                              context=context)
        self.assertEqual(result["intent"], "portfolio")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Portfolio summary", result["reply"])
        self.assertEqual(result["modules_called"],
                         ["agents/portfolio_performance.py"])

    def test_portfolio_route_without_inputs_is_unavailable(self):
        result = self._handle("How did my portfolio perform?",
                              context=None)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["modules_called"], [])
        self.assertIn("no holdings are invented", result["reply"])

    # -- guardrails ------------------------------------------------

    def test_trade_request_is_refused_and_model_never_called(self):
        calls_before = len(self.stub.calls)
        result = self._handle("Buy 100 shares of MSFT at $500")
        self.assertEqual(result["intent"], "trade")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["route"], "guardrail_trade")
        self.assertIn("will not place a trade", result["reply"])
        self.assertIn("approval", result["reply"])
        self.assertEqual(result["modules_called"], [])
        self.assertEqual(len(self.stub.calls), calls_before)

    def test_schedule_send_request_is_refused(self):
        result = self._handle("Remind me to check NVDA tomorrow")
        self.assertEqual(result["intent"], "schedule_send")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["route"], "guardrail_schedule_send")
        self.assertIn("not wired up yet", result["reply"])

    # -- chat and model failure ------------------------------------

    def test_chat_path_via_stub_model(self):
        result = self._handle("Hello, how are you?")
        self.assertEqual(result["intent"], "chat")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reply"], "Stub chat reply.")
        self.assertTrue(any("model" in s for s in result["sources"]))

    def test_model_unavailable_is_structured_not_invented(self):
        stub = StubModelClient(raise_unavailable=True)
        result = self._handle("Hello, how are you?",
                              model_client=stub)
        self.assertEqual(result["status"], "model_unavailable")
        self.assertIn("unreachable", result["reply"])
        self.assertIn("have not invented", result["reply"])

    def test_ollama_client_raises_model_unavailable(self):
        client = OllamaModelClient(url="http://127.0.0.1:1/api/chat",
                                   timeout=2)
        with self.assertRaises(ModelUnavailable):
            client.chat([{"role": "user", "content": "hi"}])

    def test_unknown_intent_goes_to_model(self):
        stub = StubModelClient(reply="Model answer.")
        result = self._handle("blorp zibble wumpus", model_client=stub)
        self.assertEqual(result["intent"], "unknown")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reply"], "Model answer.")

    def test_empty_request_is_unavailable(self):
        result = self._handle("   ")
        self.assertEqual(result["status"], "unavailable")

    # -- turn log --------------------------------------------------

    def test_turn_log_round_trip(self):
        log_dir = Path(self.tmp.name) / "log-round-trip"
        self._handle("What is the intrinsic value of MSFT?",
                     log_dir=log_dir)
        self._handle("Buy 100 shares of MSFT at $500", log_dir=log_dir)
        log_file = log_dir / "turns.jsonl"
        self.assertTrue(log_file.exists())
        records = [json.loads(line) for line in
                   log_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["intent"], "valuation")
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["route"], "valuation")
        self.assertEqual(records[1]["intent"], "trade")
        self.assertEqual(records[1]["status"], "refused")
        for record in records:
            self.assertIn("request", record)
            self.assertIn("modules_called", record)
            self.assertIn("sources", record)
            self.assertIn("checked_at", record)

    def test_log_disabled_with_false(self):
        result = self._handle("Hello, how are you?", log_dir=False)
        self.assertNotIn("log_file", result)

    # -- Orchestrator class ----------------------------------------

    def test_orchestrator_class_defaults_and_override(self):
        orch_obj = Orchestrator(
            model_client=self.stub, db_path=str(self.db_path),
            log_dir=self.log_dir)
        result = orch_obj.handle("What is the intrinsic value of MSFT?")
        self.assertEqual(result["status"], "ok")
        self.assertIn("overvalued", result["reply"])
        refused = orch_obj.handle("Sell 10 shares of MSFT")
        self.assertEqual(refused["status"], "refused")
        classified = orch_obj.classify("Show me my watchlist")
        self.assertEqual(classified["intent"], "watchlist")


if __name__ == "__main__":
    unittest.main()
