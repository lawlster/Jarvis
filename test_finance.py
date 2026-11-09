"""Tests for tools/finance.py — Step 54 retry behaviour.

All provider access is stubbed (no network): the tests
monkeypatch ``tools.finance.provider_manager`` with a stub
whose ``get_company_financials`` fails or succeeds on cue.
"""

import unittest

from tools import finance as finance_mod


def _success_result(ticker="MSFT"):
    return {
        "provider": "Yahoo Finance",
        "retrieved_at_utc": "2026-11-09T00:00:00+00:00",
        "price_data_as_of_utc": "2026-11-06T21:00:00+00:00",
        "financial_statements_latest_period": {
            "income_statement": "2026-06-30",
            "cash_flow": "2026-06-30",
            "balance_sheet": "2026-06-30",
        },
        "financials": {
            "market_cap": 1000,
            "current_price": 100.0,
            "free_cash_flow": {"2026-06-30": 50.0},
            "total_debt": {"2026-06-30": 10.0},
            "cash": {"2026-06-30": 20.0},
            "shares_outstanding": 10,
        },
        "data_available": True,
    }


class _StubProviderManager:
    """Provider manager stub with a scripted financials outcome."""

    def __init__(self, outcomes):
        # outcomes: list of dicts returned in order by
        # get_company_financials (last one repeats).
        self.outcomes = list(outcomes)
        self.financials_calls = 0
        self.profile_calls = 0

    def get_company_profile(self, ticker, provider_name="yahoo"):
        self.profile_calls += 1
        return {"company_name": "Stub Co"}

    def get_company_financials(self, ticker, provider_name="yahoo"):
        self.financials_calls += 1
        index = min(self.financials_calls - 1,
                    len(self.outcomes) - 1)
        return self.outcomes[index]


def _transient_error(message="Cookie/crumb fetch failed (Timeout)"):
    return {
        "error": "All registered financial providers failed.",
        "ticker": "MSFT",
        "providers_attempted": [
            {"provider": "yahoo", "error": message},
        ],
    }


class FinanceRetryTestCase(unittest.TestCase):
    def setUp(self):
        self._old_manager = finance_mod.provider_manager
        self.sleeps = []

    def tearDown(self):
        finance_mod.provider_manager = self._old_manager

    def _run(self, outcomes, **kwargs):
        stub = _StubProviderManager(outcomes)
        finance_mod.provider_manager = stub
        kwargs.setdefault("sleep", self.sleeps.append)
        kwargs.setdefault("retry_delay_seconds", 1.0)
        result = finance_mod.get_company_financials("MSFT", **kwargs)
        return result, stub

    def test_success_first_try_no_retry(self):
        result, stub = self._run([_success_result()])
        self.assertTrue(result["data_available"])
        self.assertEqual(result["current_price"], 100.0)
        self.assertEqual(stub.financials_calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_transient_failure_then_success(self):
        result, stub = self._run(
            [_transient_error(), _success_result()])
        self.assertTrue(result["data_available"])
        self.assertEqual(stub.financials_calls, 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_transient_failure_twice_then_success(self):
        result, stub = self._run(
            [_transient_error(), _transient_error(),
             _success_result()])
        self.assertTrue(result["data_available"])
        self.assertEqual(stub.financials_calls, 3)
        # Exponential backoff: 1s, then 2s.
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_persistent_transient_failure_is_provider_unavailable(self):
        result, stub = self._run(
            [_transient_error()], max_attempts=3)
        self.assertFalse(result["data_available"])
        self.assertTrue(result["provider_unavailable"])
        self.assertIn("crumb", result["provider_error"].lower())
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(stub.financials_calls, 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_genuine_no_data_not_retried(self):
        # No such ticker: data_available False, no "error" key.
        no_data = dict(_success_result())
        no_data["data_available"] = False
        no_data["financials"] = {}
        result, stub = self._run([no_data])
        self.assertFalse(result["data_available"])
        self.assertNotIn("provider_unavailable", result)
        self.assertEqual(stub.financials_calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_non_transient_error_not_retried(self):
        result, stub = self._run([{
            "error": "Provider 'bogus' is not registered.",
            "ticker": "MSFT",
            "providers_attempted": [],
        }])
        self.assertEqual(
            result["error"], "Provider 'bogus' is not registered.")
        self.assertNotIn("provider_unavailable", result)
        self.assertEqual(stub.financials_calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_transient_marker_in_top_level_error(self):
        # The aggregate message itself carries the marker.
        result, stub = self._run([{
            "error": "Connection timed out after 30s",
            "ticker": "MSFT",
            "providers_attempted": [],
        }, _success_result()])
        self.assertTrue(result["data_available"])
        self.assertEqual(stub.financials_calls, 2)

    def test_max_attempts_one_never_sleeps(self):
        result, stub = self._run(
            [_transient_error()], max_attempts=1)
        self.assertTrue(result["provider_unavailable"])
        self.assertEqual(stub.financials_calls, 1)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
