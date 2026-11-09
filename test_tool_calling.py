"""Step 44 — Natural-language tool calling: tests.

Stubbed model clients and stubbed fetchers only; no live
network, no production writes. The production DB is copied
once per test class into the temp directory and the copy is
removed in tearDownClass; the production hash is asserted
unchanged. Every test cleans up after itself via
TemporaryDirectory.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import tool_calling as tc
from agent.orchestrator import StubModelClient
from agent.tool_calling import (
    ArgumentValidationError,
    DuplicateToolError,
    ForbiddenToolError,
    Tool,
    ToolCaller,
    ToolRegistry,
    UnknownToolError,
    default_registry,
    run_tool_loop,
    select_tool,
    validate_arguments,
)

PROJECT_ROOT = Path(__file__).resolve().parent
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"
NEWS_DIR = PROJECT_ROOT / "data" / "news"
EVENTS_DIR = PROJECT_ROOT / "data" / "events"


def _db_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stub_search(query, max_results=5):
    return [{
        "title": f"Stub result for {query}",
        "url": "https://example.com/stub",
        "date": "2026-11-09",
        "description": "A stubbed search result.",
    }]


def _stub_financials(ticker, provider_name="yahoo"):
    return {
        "ticker": ticker,
        "company_name": "Stub Corp",
        "free_cash_flow": 59957000000,
        "total_debt": 50000000000,
        "cash": 30000000000,
        "shares_outstanding": 7446000000,
    }


class ToolCallingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "assistant.db"
        shutil.copy(PROD_DB, cls.db_path)
        cls.store_dir = Path(cls.tmp.name) / "watchlist"
        cls.sessions_dir = Path(cls.tmp.name) / "sessions"
        cls.registry = default_registry(
            db_path=str(cls.db_path),
            store_dir=str(cls.store_dir),
            news_dir=str(NEWS_DIR),
            events_dir=str(EVENTS_DIR),
            sessions_dir=str(cls.sessions_dir),
            search_function=_stub_search,
            financials_function=_stub_financials,
        )
        cls.stub = StubModelClient(reply="Stub chat reply.")

    @classmethod
    def tearDownClass(cls):
        assert _db_hash(PROD_DB) == PROD_HASH, \
            "production DB hash changed"
        cls.tmp.cleanup()

    def _select(self, text, **kwargs):
        kwargs.setdefault("registry", self.registry)
        kwargs.setdefault("db_path", str(self.db_path))
        return select_tool(text, **kwargs)

    def _run(self, text, **kwargs):
        kwargs.setdefault("registry", self.registry)
        kwargs.setdefault("db_path", str(self.db_path))
        return run_tool_loop(text, **kwargs)

    # -- registry ---------------------------------------------------

    def test_default_registry_has_the_expected_tools(self):
        names = self.registry.names()
        for expected in (
            "calculate", "search_web", "get_company_financials",
            "calculate_dcf", "valuation_summary", "scan_ticker",
            "watchlist_view", "list_news", "list_events",
            "research_session",
        ):
            self.assertIn(expected, names)
        for forbidden in ("trade", "buy", "sell", "send", "schedule"):
            self.assertNotIn(forbidden, names)

    def test_ollama_tools_shape_matches_main_py(self):
        specs = self.registry.ollama_tools()
        by_name = {s["function"]["name"]: s for s in specs}
        self.assertEqual(by_name["calculate"]["type"], "function")
        self.assertEqual(
            by_name["calculate"]["function"]["parameters"]["required"],
            ["expression"],
        )
        self.assertEqual(
            by_name["calculate_dcf"]["function"]["parameters"]["required"],
            ["free_cash_flow", "growth_rate", "discount_rate",
             "terminal_growth_rate"],
        )

    def test_register_duplicate_name_rejected(self):
        registry = ToolRegistry()
        registry.register_function(
            "echo", "Echoes its text.",
            {"type": "object",
             "properties": {"text": {"type": "string"}},
             "required": ["text"]},
            lambda text: text,
        )
        with self.assertRaises(DuplicateToolError):
            registry.register_function(
                "echo", "Echoes again.",
                {"type": "object", "properties": {}},
                lambda: "x",
            )

    def test_forbidden_names_refused_at_registration(self):
        for bad_name in (
            "trade", "buy_shares", "sell_stock", "send_message",
            "send_email", "schedule_reminder", "place_order",
            "remind_me", "purchase", "short_stock",
        ):
            with self.assertRaises(ForbiddenToolError, msg=bad_name):
                Tool(bad_name, "Forbidden.", {"type": "object",
                                              "properties": {}},
                     lambda: None)
        registry = ToolRegistry()
        with self.assertRaises(ForbiddenToolError):
            registry.register_function(
                "buy", "Buys shares.",
                {"type": "object", "properties": {}},
                lambda: None,
            )

    def test_innocent_names_are_not_forbidden(self):
        # "buyback" contains "buy" but is not a trade verb.
        tool = Tool("buyback_info", "Buyback history.",
                    {"type": "object", "properties": {}}, lambda: "ok")
        self.assertEqual(tool.name, "buyback_info")

    def test_unknown_tool_lookup(self):
        with self.assertRaises(UnknownToolError):
            self.registry.get("does_not_exist")

    # -- argument validation ----------------------------------------

    def test_validate_missing_required(self):
        tool = self.registry.get("calculate")
        ok, reason = validate_arguments(tool, {})
        self.assertFalse(ok)
        self.assertIn("expression", reason)

    def test_validate_unknown_argument(self):
        tool = self.registry.get("calculate")
        ok, reason = validate_arguments(tool, {"expression": "1+1",
                                               "surprise": True})
        self.assertFalse(ok)
        self.assertIn("surprise", reason)

    def test_validate_wrong_type_not_coerced(self):
        tool = self.registry.get("calculate_dcf")
        ok, reason = validate_arguments(tool, {
            "free_cash_flow": "50000",  # a string, not a number
            "growth_rate": 0.10,
            "discount_rate": 0.11,
            "terminal_growth_rate": 0.025,
        })
        self.assertFalse(ok)
        self.assertIn("free_cash_flow", reason)
        self.assertIn("not converted", reason)

    def test_validate_bool_is_not_a_number(self):
        tool = self.registry.get("calculate_dcf")
        ok, _reason = validate_arguments(tool, {
            "free_cash_flow": True,
            "growth_rate": 0.10,
            "discount_rate": 0.11,
            "terminal_growth_rate": 0.025,
        })
        self.assertFalse(ok)

    def test_validate_ok(self):
        tool = self.registry.get("calculate")
        ok, reason = validate_arguments(tool, {"expression": "2 + 3"})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    # -- keyword selection, one per tool ----------------------------

    def test_select_calculate(self):
        selected = self._select("calculate 2 + 3 * 4")
        self.assertEqual(selected["tool"], "calculate")
        self.assertEqual(selected["arguments"]["expression"], "2 + 3 * 4")
        self.assertEqual(selected["method"], "keyword")

    def test_select_search_web(self):
        selected = self._select("search the web for Microsoft earnings")
        self.assertEqual(selected["tool"], "search_web")
        self.assertEqual(selected["arguments"]["query"],
                         "Microsoft earnings")

    def test_select_get_company_financials(self):
        selected = self._select("show me MSFT financials")
        self.assertEqual(selected["tool"], "get_company_financials")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_calculate_dcf_with_labelled_assumptions(self):
        selected = self._select(
            "calculate DCF with free cash flow 50000, growth 10%, "
            "discount 11%, terminal 2.5%")
        self.assertEqual(selected["tool"], "calculate_dcf")
        self.assertEqual(selected["arguments"]["free_cash_flow"], 50000.0)
        self.assertAlmostEqual(selected["arguments"]["growth_rate"], 0.10)
        self.assertAlmostEqual(selected["arguments"]["discount_rate"], 0.11)
        self.assertAlmostEqual(
            selected["arguments"]["terminal_growth_rate"], 0.025)

    def test_select_calculate_dcf_without_assumptions_falls_through(self):
        # No labelled assumptions: no partial DCF is ever run on
        # guessed numbers — the valuation engine is chosen instead.
        selected = self._select("calculate the DCF value of MSFT")
        self.assertEqual(selected["tool"], "valuation_summary")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_valuation_summary(self):
        selected = self._select("what is the intrinsic value of MSFT?")
        self.assertEqual(selected["tool"], "valuation_summary")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")
        self.assertEqual(selected["method"], "keyword")

    def test_select_scan_ticker(self):
        selected = self._select("scan MSFT")
        self.assertEqual(selected["tool"], "scan_ticker")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_watchlist_view(self):
        selected = self._select("show me my watchlist")
        self.assertEqual(selected["tool"], "watchlist_view")
        self.assertEqual(selected["arguments"], {})

    def test_select_list_news(self):
        selected = self._select("any news on MSFT?")
        self.assertEqual(selected["tool"], "list_news")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_list_events(self):
        selected = self._select("what events are coming up for MSFT?")
        self.assertEqual(selected["tool"], "list_events")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_research_session(self):
        selected = self._select("research MSFT")
        self.assertEqual(selected["tool"], "research_session")
        self.assertEqual(selected["arguments"]["question"], "research MSFT")

    def test_select_no_tool(self):
        selected = self._select("tell me a joke")
        self.assertIsNone(selected["tool"])
        self.assertEqual(selected["method"], "none")

    def test_select_empty_request(self):
        selected = self._select("   ")
        self.assertIsNone(selected["tool"])

    def test_select_lowercase_word_is_not_a_ticker(self):
        # "are" spells the ticker ARE, but in "what events are
        # coming up for MSFT?" the ticker is MSFT.
        selected = self._select("what events are coming up for MSFT?")
        self.assertEqual(selected["tool"], "list_events")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_lowercase_ticker_still_works(self):
        selected = self._select("what is the intrinsic value of msft?")
        self.assertEqual(selected["tool"], "valuation_summary")
        self.assertEqual(selected["arguments"]["ticker"], "MSFT")

    def test_select_unknown_ticker_not_accepted(self):
        # ZZZZ is not in market_universe, so no ticker argument
        # can be built — the valuation rule is skipped.
        selected = self._select("what is the intrinsic value of ZZZZ?")
        self.assertIsNone(selected["tool"])

    # -- model fallback selection -----------------------------------

    def test_model_fallback_selects_tool_and_is_labelled(self):
        client = StubModelClient(reply=json.dumps({
            "tool": "calculate",
            "arguments": {"expression": "1 + 1"},
        }))
        selected = self._select("do some maths for me", model_client=client)
        self.assertEqual(selected["tool"], "calculate")
        self.assertEqual(selected["method"], "model_fallback")

    def test_model_fallback_garbage_is_no_tool(self):
        client = StubModelClient(reply="I think you should calculate")
        selected = self._select("do some maths for me", model_client=client)
        self.assertIsNone(selected["tool"])
        self.assertEqual(selected["method"], "model_fallback")

    def test_model_fallback_unknown_tool_is_no_tool(self):
        client = StubModelClient(reply=json.dumps({
            "tool": "buy_shares", "arguments": {"ticker": "MSFT"},
        }))
        selected = self._select("do some maths for me", model_client=client)
        self.assertIsNone(selected["tool"])

    def test_model_fallback_invalid_arguments_is_no_tool(self):
        client = StubModelClient(reply=json.dumps({
            "tool": "calculate", "arguments": {},
        }))
        selected = self._select("do some maths for me", model_client=client)
        self.assertIsNone(selected["tool"])
        self.assertIn("validation", selected["reason"])

    def test_model_fallback_null_tool_is_no_tool(self):
        client = StubModelClient(reply=json.dumps({
            "tool": None, "arguments": {},
        }))
        selected = self._select("do some maths for me", model_client=client)
        self.assertIsNone(selected["tool"])

    # -- execution and recording ------------------------------------

    def test_run_calculate_records_the_call(self):
        result = self._run("calculate 2 + 3 * 4")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tools_called"], ["calculate"])
        call = result["calls"][0]
        self.assertTrue(call["success"])
        self.assertEqual(call["result"], "14")
        self.assertEqual(call["method"], "keyword")
        self.assertIsNotNone(call["duration_seconds"])
        self.assertGreaterEqual(call["duration_seconds"], 0)
        self.assertIn("14", call["result_summary"])

    def test_run_no_tool(self):
        result = self._run("tell me a joke")
        self.assertEqual(result["status"], "no_tool")
        self.assertEqual(result["calls"], [])
        self.assertIsNone(result["final_result"])

    def test_run_tool_error_recorded_not_raised(self):
        registry = ToolRegistry()
        registry.register_function(
            "boom", "Always fails.",
            {"type": "object", "properties": {}},
            lambda: 1 / 0,
        )
        client = StubModelClient(reply=json.dumps({
            "tool": "boom", "arguments": {},
        }))
        result = run_tool_loop("do something", registry,
                               model_client=client)
        self.assertEqual(result["status"], "tool_error")
        self.assertIn("ZeroDivisionError", result["calls"][0]["error"])

    def test_execute_call_argument_rejection(self):
        record = tc._execute_call(
            self.registry, "calculate", {"expression": 5})
        self.assertFalse(record["success"])
        self.assertIn("argument validation failed", record["error"])

    # -- multi-step chains ------------------------------------------

    def test_financials_then_dcf_chain(self):
        result = self._run(
            "get MSFT financials and value it with a DCF")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tools_called"],
                         ["get_company_financials", "calculate_dcf"])
        first, second = result["calls"]
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(second["method"], "follow_up")
        self.assertIn("Step 21 defaults", second["selection_reason"])
        # The DCF ran on the stubbed FCF with the Step 21 defaults.
        self.assertEqual(second["arguments"]["free_cash_flow"], 59957000000.0)
        self.assertAlmostEqual(second["arguments"]["growth_rate"], 0.10)
        self.assertAlmostEqual(second["arguments"]["discount_rate"], 0.11)
        self.assertAlmostEqual(
            second["arguments"]["terminal_growth_rate"], 0.025)
        self.assertEqual(second["arguments"]["years"], 10)
        self.assertAlmostEqual(second["arguments"]["net_debt"], 20000000000.0)
        final = result["final_result"]
        self.assertIsNotNone(final.get("intrinsic_value_per_share"))
        self.assertGreater(final["intrinsic_value_per_share"], 0)

    def test_no_follow_up_when_request_is_not_valuation(self):
        result = self._run("show me MSFT financials")
        self.assertEqual(result["tools_called"],
                         ["get_company_financials"])

    def test_no_follow_up_when_financials_have_no_fcf(self):
        registry = default_registry(
            db_path=str(self.db_path),
            financials_function=lambda ticker, provider_name="yahoo": {
                "ticker": ticker, "free_cash_flow": {},
            },
        )
        result = run_tool_loop(
            "get MSFT financials and value it with a DCF",
            registry, db_path=str(self.db_path))
        self.assertEqual(result["tools_called"],
                         ["get_company_financials"])

    def test_chain_cap(self):
        # max_calls=1: the financials call runs, the DCF follow-up
        # exists but the cap stops the chain.
        result = self._run(
            "get MSFT financials and value it with a DCF", max_calls=1)
        self.assertEqual(result["status"], "max_calls_reached")
        self.assertEqual(result["tools_called"], ["get_company_financials"])

    def test_loop_guard(self):
        # Force the follow-up rule to return the same call twice;
        # the guard must stop the loop after one execution.
        first = {
            "tool": "calculate",
            "arguments": {"expression": "1 + 1"},
            "method": "follow_up",
            "reason": "forced repeat",
        }
        with mock.patch.object(tc, "_follow_up", return_value=first):
            result = self._run("calculate 1 + 1")
        self.assertEqual(result["status"], "loop_guard")
        self.assertEqual(len(result["calls"]), 1)

    # -- MSFT valuation through the loop (temp DB copy) -------------

    def test_msft_valuation_through_the_loop(self):
        result = self._run("what is the intrinsic value of MSFT?")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tools_called"], ["valuation_summary"])
        final = result["final_result"]
        # Traces to the Step 21/23 outputs. The raw Step 23
        # summary carries the tier as a dict {"tier", "reasons",
        # ...} (the same shape Step 38 unwraps); the loop returns
        # the tool result as-is and does not reshape it.
        tier = final.get("tier")
        if isinstance(tier, dict):
            tier = tier.get("tier")
        self.assertEqual(tier, "overvalued")
        rng = final.get("fair_value_range") or {}
        self.assertAlmostEqual(rng.get("conservative"), 98.52, places=1)
        self.assertAlmostEqual(rng.get("base"), 163.30, places=1)
        self.assertAlmostEqual(rng.get("optimistic"), 234.64, places=1)
        self.assertAlmostEqual(final.get("price"), 507.12, places=2)
        self.assertAlmostEqual(
            final.get("strongest_alert_threshold_price"), 78.82, places=1)

    def test_msft_scan_through_the_loop(self):
        result = self._run("scan MSFT")
        self.assertEqual(result["status"], "ok")
        final = result["final_result"]
        self.assertEqual(final.get("tier"), "overvalued")
        self.assertEqual(final.get("quality_rating"), "excellent")

    def test_msft_news_through_the_loop(self):
        result = self._run("any news on MSFT?")
        self.assertEqual(result["status"], "ok")
        articles = result["final_result"].get("articles")
        self.assertEqual(len(articles), 10)

    def test_msft_events_through_the_loop(self):
        result = self._run("what events are coming up for MSFT?")
        self.assertEqual(result["status"], "ok")
        self.assertIn("events", result["final_result"])

    def test_watchlist_view_through_the_loop(self):
        result = self._run("show me my watchlist")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final_result"], {"lines": []})

    def test_search_web_through_the_loop_uses_stub(self):
        result = self._run("search the web for Microsoft earnings")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final_result"][0]["title"],
                         "Stub result for Microsoft earnings")

    def test_research_session_through_the_loop(self):
        result = self._run("research MSFT")
        self.assertEqual(result["status"], "ok")
        final = result["final_result"]
        self.assertEqual(final.get("kind"), "single")
        self.assertEqual(final.get("tickers"), ["MSFT"])

    # -- ToolCaller class -------------------------------------------

    def test_tool_caller_class(self):
        caller = ToolCaller(registry=self.registry,
                            db_path=str(self.db_path))
        result = caller.run("calculate 3 * 3")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final_result"], "9")
        selected = caller.select("scan MSFT")
        self.assertEqual(selected["tool"], "scan_ticker")


if __name__ == "__main__":
    unittest.main()
