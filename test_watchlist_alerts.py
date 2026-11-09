"""Step 38 — Automated watchlist and valuation alerts: tests.

Fixture detection results / summaries / prices only; no network,
no production writes. Every test cleans up after itself via
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

from agents import watchlist_alerts as wa

PROJECT_ROOT = Path(__file__).resolve().parent
PROD_DB = PROJECT_ROOT / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"

AS_OF_1 = "2026-11-09T02:00:00Z"
AS_OF_2 = "2026-11-09T03:00:00Z"
AS_OF_3 = "2026-11-09T04:00:00Z"


def _db_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate(ticker, band=None, tier="reasonable_price", price=5.00,
               conservative=25.00, base=40.00, optimistic=60.00,
               quality="excellent", health="strong",
               deteriorated=False, price_as_of="2026-11-08"):
    """A Step 26 detection candidate dict (fixture)."""
    return {
        "ticker": ticker,
        "company_name": f"{ticker} Fixture Co",
        "detection_band": band,
        "tier": tier,
        "price": price,
        "price_as_of": price_as_of,
        "fair_value_range": {
            "conservative": conservative, "base": base,
            "optimistic": optimistic},
        "margin_of_safety_vs_conservative":
            (conservative - price) / conservative,
        "margin_of_safety_vs_base": (base - price) / base,
        "strongest_alert_threshold_price": conservative * 0.8,
        "quality_rating": quality,
        "health_verdict": health,
        "deteriorated": deteriorated,
        "qualifies_reasons": ["fixture reason"],
    }


def _summary(ticker, tier="reasonable_price", price=5.00,
             conservative=25.00, base=40.00, optimistic=60.00,
             quality="excellent", deteriorated=False):
    """A Step 23 margin_of_safety_summary dict (fixture)."""
    return {
        "ticker": ticker,
        "tier": tier,
        "price": price,
        "price_as_of": "2026-11-08",
        "fair_value_range": {
            "conservative": conservative, "base": base,
            "optimistic": optimistic},
        "margin_of_safety_vs_conservative":
            (conservative - price) / conservative,
        "strongest_alert_threshold_price": conservative * 0.8,
        "business_quality": {"rating": quality},
        "deterioration": {"deteriorated": deteriorated,
                          "unknown": False, "signals": []},
    }


def _detection(*candidates):
    return {"candidates": list(candidates), "value_traps": [],
            "summary": {}, "coverage": {}}


def _thesis_review(ticker, suggested, thesis_id="thesis-1"):
    return {
        "ticker": ticker,
        "thesis_id": thesis_id,
        "suggested_status": suggested,
        "suggested_reason": "fixture condition met",
        "confirm_met": 1, "confirm_total": 1,
        "invalidate_met": 0, "invalidate_total": 1,
        "changes": [],
    }


class _TempDbTestCase(unittest.TestCase):
    """A temp DB copy shared by the class; removed afterwards."""

    db_path = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="step38-")
        cls.db_path = Path(cls.tmp.name) / "assistant.db"
        shutil.copy(PROD_DB, cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        assert _db_hash(PROD_DB) == PROD_HASH, \
            "production DB changed during tests"

    def _connect(self):
        return sqlite3.connect(self.db_path)


class TestStrongestAlertRule(unittest.TestCase):
    def test_strongest_alert_candidate_fires(self):
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(len(result["new_alerts"]), 1)
        alert = result["new_alerts"][0]
        self.assertEqual(alert["alert_key"], "strongest_alert:FIXA")
        self.assertEqual(alert["severity"], "strongest")
        self.assertEqual(alert["status"], "active")
        self.assertEqual(alert["raised_at"], AS_OF_1)
        self.assertAlmostEqual(alert["trigger_value"], 0.8)
        self.assertAlmostEqual(alert["threshold"], 0.20)
        self.assertEqual(alert["evidence"]["quality_rating"],
                         "excellent")
        self.assertEqual(alert["evidence"]["health_verdict"], "strong")
        self.assertIn("FIXA", alert["briefing_line"])
        self.assertEqual(result["evaluated"], ["FIXA"])
        self.assertEqual(result["not_evaluated"], [])

    def test_strongest_alert_from_summary_tier(self):
        # No detection candidate at all — the Step 23 tier alone
        # is enough.
        result = wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": _summary(
                "FIXA", tier="strongest_alert")},
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(len(result["new_alerts"]), 1)
        self.assertEqual(result["new_alerts"][0]["alert_type"],
                         "strongest_alert")

    def test_overvalued_ticker_fires_nothing(self):
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", tier="overvalued", price=100.00)),
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(result["evaluated"], ["FIXA"])


class TestPriceCrossingRule(unittest.TestCase):
    def test_fresh_price_below_alert_price_fires(self):
        # Stored tier says overvalued (computed on the old
        # price); the fresh caller-supplied price crossed the
        # strongest-alert threshold price.
        result = wa.evaluate_alerts(
            ["MSFT"], summaries={"MSFT": _summary(
                "MSFT", tier="overvalued", price=507.12,
                conservative=98.52, base=163.30, optimistic=234.64)},
            prices={"MSFT": 75.00},
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(len(result["new_alerts"]), 1)
        alert = result["new_alerts"][0]
        self.assertEqual(alert["alert_type"],
                         "price_below_alert_price")
        self.assertEqual(alert["trigger_value"], 75.00)
        self.assertAlmostEqual(alert["threshold"], 78.816, places=2)
        self.assertIn("75.00", alert["briefing_line"])
        self.assertIn("78.82", alert["briefing_line"])

    def test_price_above_alert_price_fires_nothing(self):
        result = wa.evaluate_alerts(
            ["MSFT"], summaries={"MSFT": _summary(
                "MSFT", tier="overvalued", price=507.12,
                conservative=98.52, base=163.30, optimistic=234.64)},
            prices={"MSFT": 80.00},
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])

    def test_price_crossing_does_not_double_fire_with_strongest(self):
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            prices={"FIXA": 5.00},
            state=wa.new_state(), as_of=AS_OF_1)
        types = [a["alert_type"] for a in result["new_alerts"]]
        self.assertEqual(types, ["strongest_alert"])


class TestWatchBandRule(unittest.TestCase):
    def test_watch_discount_fires_as_watch(self):
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="watch_discount", price=21.00)),
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(len(result["new_alerts"]), 1)
        alert = result["new_alerts"][0]
        self.assertEqual(alert["alert_type"], "watch_discount")
        self.assertEqual(alert["severity"], "watch")

    def test_watch_below_base_fires_as_watch(self):
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="watch_below_base", price=30.00,
                conservative=25.00)),
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(len(result["new_alerts"]), 1)
        self.assertEqual(result["new_alerts"][0]["alert_type"],
                         "watch_below_base")


class TestDedupeClearRefire(unittest.TestCase):
    def _firing_state(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=state, as_of=AS_OF_1)
        return state

    def test_second_run_raises_nothing_new(self):
        state = self._firing_state()
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=state, as_of=AS_OF_2)
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(len(result["already_active"]), 1)
        self.assertEqual(len(wa.list_active_alerts(state)), 1)

    def test_cleared_when_condition_gone_then_refires(self):
        state = self._firing_state()
        # Price recovered: overvalued, no band — the alert clears.
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", tier="overvalued", price=100.00)),
            state=state, as_of=AS_OF_2)
        self.assertEqual(len(result["cleared_alerts"]), 1)
        self.assertEqual(result["cleared_alerts"][0]["status"],
                         "cleared")
        self.assertEqual(result["cleared_alerts"][0]["cleared_at"],
                         AS_OF_2)
        self.assertEqual(wa.list_active_alerts(state), [])
        self.assertEqual(len(wa.list_cleared_alerts(state)), 1)
        # And fires again when the condition returns.
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=state, as_of=AS_OF_3)
        self.assertEqual(len(result["new_alerts"]), 1)
        self.assertEqual(result["new_alerts"][0]["refire_count"], 1)

    def test_not_evaluated_does_not_clear_active_alert(self):
        state = self._firing_state()
        # This run the ticker has no price at all: absence of
        # evidence is not evidence of clearing.
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(),
            state=state, as_of=AS_OF_2)
        self.assertEqual(result["cleared_alerts"], [])
        self.assertEqual(len(wa.list_active_alerts(state)), 1)
        self.assertEqual(len(result["not_evaluated"]), 1)

    def test_watch_upgraded_to_strongest_clears_watch(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="watch_discount", price=21.00)),
            state=state, as_of=AS_OF_1)
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=state, as_of=AS_OF_2)
        types = [a["alert_type"] for a in result["new_alerts"]]
        self.assertEqual(types, ["strongest_alert"])
        cleared = [a["alert_type"] for a in result["cleared_alerts"]]
        self.assertEqual(cleared, ["watch_discount"])


class TestDeteriorationAndQualityRules(unittest.TestCase):
    def test_first_run_establishes_baseline_only(self):
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", deteriorated=True)),
            state=wa.new_state(), as_of=AS_OF_1)
        types = [a["alert_type"] for a in result["new_alerts"]]
        self.assertNotIn("deterioration_material", types)

    def test_deterioration_turning_material_fires(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", deteriorated=False)),
            state=state, as_of=AS_OF_1)
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", deteriorated=True)),
            state=state, as_of=AS_OF_2)
        types = [a["alert_type"] for a in result["new_alerts"]]
        self.assertIn("deterioration_material", types)
        alert = [a for a in result["new_alerts"]
                 if a["alert_type"] == "deterioration_material"][0]
        self.assertEqual(alert["severity"], "warning")
        self.assertIs(alert["trigger_value"], True)
        self.assertIs(alert["threshold"], False)

    def test_deterioration_staying_material_does_not_refire(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", deteriorated=False)),
            state=state, as_of=AS_OF_1)
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", deteriorated=True)),
            state=state, as_of=AS_OF_2)
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", deteriorated=True)),
            state=state, as_of=AS_OF_3)
        types = [a["alert_type"] for a in result["new_alerts"]]
        self.assertNotIn("deterioration_material", types)
        self.assertEqual(len(result["already_active"]), 1)

    def test_quality_change_fires_with_old_and_new(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", quality="excellent")),
            state=state, as_of=AS_OF_1)
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", quality="good")),
            state=state, as_of=AS_OF_2)
        alerts = [a for a in result["new_alerts"]
                  if a["alert_type"] == "quality_changed"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["trigger_value"], "good")
        self.assertEqual(alerts[0]["threshold"], "excellent")
        self.assertIn("excellent -> good",
                      alerts[0]["briefing_line"])

    def test_first_rating_is_baseline_not_change(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": _summary(
                "FIXA", quality=None, tier="overvalued",
                price=100.00)},
            state=state, as_of=AS_OF_1)
        result = wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", quality="excellent")),
            state=state, as_of=AS_OF_2)
        types = [a["alert_type"] for a in result["new_alerts"]]
        self.assertNotIn("quality_changed", types)


class TestThesisRules(unittest.TestCase):
    def test_thesis_invalidated_fires(self):
        result = wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": _summary(
                "FIXA", tier="overvalued", price=100.00)},
            thesis_reviews=[_thesis_review("FIXA", "invalidated")],
            state=wa.new_state(), as_of=AS_OF_1)
        alerts = [a for a in result["new_alerts"]
                  if a["alert_type"] == "thesis_invalidated"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "warning")

    def test_thesis_confirmed_fires(self):
        result = wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": _summary(
                "FIXA", tier="overvalued", price=100.00)},
            thesis_reviews=[_thesis_review("FIXA", "confirmed")],
            state=wa.new_state(), as_of=AS_OF_1)
        alerts = [a for a in result["new_alerts"]
                  if a["alert_type"] == "thesis_confirmed"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "watch")

    def test_thesis_still_active_fires_nothing(self):
        result = wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": _summary(
                "FIXA", tier="overvalued", price=100.00)},
            thesis_reviews=[_thesis_review("FIXA", "active")],
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])

    def test_thesis_review_off_watchlist_is_skipped_not_dropped(self):
        result = wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": _summary(
                "FIXA", tier="overvalued", price=100.00)},
            thesis_reviews=[_thesis_review("OTHER", "invalidated")],
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(len(result["skipped_thesis_reviews"]), 1)
        self.assertEqual(result["skipped_thesis_reviews"][0]["ticker"],
                         "OTHER")


class TestNotEvaluated(unittest.TestCase):
    def test_no_data_at_all(self):
        result = wa.evaluate_alerts(
            ["NOPE"], state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(len(result["not_evaluated"]), 1)
        self.assertIn("no price",
                      result["not_evaluated"][0]["reason"])

    def test_insufficient_data_tier(self):
        result = wa.evaluate_alerts(
            ["DBS"], summaries={"DBS": {
                "ticker": "DBS", "tier": "insufficient_data",
                "price": None, "fair_value_range": {}}},
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(len(result["not_evaluated"]), 1)
        self.assertIn("insufficient_data",
                      result["not_evaluated"][0]["reason"])

    def test_price_without_value(self):
        result = wa.evaluate_alerts(
            ["FIXA"], summaries={"FIXA": {
                "ticker": "FIXA", "tier": "overvalued", "price": 10.00,
                "fair_value_range": {}, "business_quality": None,
                "deterioration": {"deteriorated": False}}},
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(result["new_alerts"], [])
        self.assertIn("conservative intrinsic value",
                      result["not_evaluated"][0]["reason"])

    def test_watchlist_entries_accepted(self):
        result = wa.evaluate_alerts(
            [{"ticker": "FIXA", "reason": "core watch"}],
            detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=wa.new_state(), as_of=AS_OF_1)
        self.assertEqual(len(result["new_alerts"]), 1)


class TestListingAndAcknowledging(unittest.TestCase):
    def test_acknowledge_active_alert(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA"], detection=_detection(_candidate(
                "FIXA", band="strongest_alert",
                tier="strongest_alert")),
            state=state, as_of=AS_OF_1)
        record = wa.acknowledge_alert(state, "strongest_alert:FIXA",
                                      as_of=AS_OF_2)
        self.assertIsNotNone(record)
        self.assertTrue(record["acknowledged"])
        self.assertEqual(record["acknowledged_at"], AS_OF_2)
        # Acknowledging does not clear it.
        self.assertEqual(len(wa.list_active_alerts(state)), 1)

    def test_acknowledge_unknown_key_returns_none(self):
        self.assertIsNone(wa.acknowledge_alert(
            wa.new_state(), "strongest_alert:NOPE"))

    def test_list_filters(self):
        state = wa.new_state()
        wa.evaluate_alerts(
            ["FIXA", "FIXB"],
            detection=_detection(
                _candidate("FIXA", band="strongest_alert",
                           tier="strongest_alert"),
                _candidate("FIXB", band="watch_discount",
                           price=21.00)),
            state=state, as_of=AS_OF_1)
        self.assertEqual(len(wa.list_active_alerts(state)), 2)
        self.assertEqual(len(wa.list_active_alerts(
            state, severity="strongest")), 1)
        self.assertEqual(len(wa.list_active_alerts(
            state, ticker="FIXB")), 1)
        self.assertEqual(wa.list_cleared_alerts(state), [])


class TestStatePersistence(unittest.TestCase):
    def test_run_alerts_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="step38-state-") as tmp:
            state_path = str(Path(tmp) / "alert_state.json")
            result = wa.run_alerts(
                ["FIXA"], detection=_detection(_candidate(
                    "FIXA", band="strongest_alert",
                    tier="strongest_alert")),
                state_path=state_path, as_of=AS_OF_1)
            self.assertEqual(result["state_file"], state_path)
            loaded = wa.load_state(state_path)
            self.assertEqual(len(loaded["active_alerts"]), 1)
            self.assertIn("FIXA", loaded["last_evidence"])
            # Second run from the persisted state: no re-fire.
            result2 = wa.run_alerts(
                ["FIXA"], detection=_detection(_candidate(
                    "FIXA", band="strongest_alert",
                    tier="strongest_alert")),
                state_path=state_path, as_of=AS_OF_2)
            self.assertEqual(result2["new_alerts"], [])
            self.assertEqual(len(result2["already_active"]), 1)

    def test_corrupt_state_raises(self):
        with tempfile.TemporaryDirectory(prefix="step38-state-") as tmp:
            state_path = Path(tmp) / "alert_state.json"
            state_path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                wa.load_state(str(state_path))


class TestNoRecommendationWords(unittest.TestCase):
    def test_briefing_lines_carry_no_buy_sell_words(self):
        state = wa.new_state()
        result = wa.evaluate_alerts(
            ["FIXA", "FIXB", "FIXC"],
            detection=_detection(
                _candidate("FIXA", band="strongest_alert",
                           tier="strongest_alert"),
                _candidate("FIXB", band="watch_discount",
                           price=21.00)),
            summaries={"FIXC": _summary(
                "FIXC", tier="overvalued", price=507.12,
                conservative=98.52, base=163.30, optimistic=234.64)},
            prices={"FIXC": 75.00},
            thesis_reviews=[_thesis_review("FIXA", "invalidated")],
            state=state, as_of=AS_OF_1)
        lines = wa.briefing_lines(result["new_alerts"])
        self.assertTrue(lines)
        for line in lines:
            lowered = line.lower()
            self.assertNotIn("buy", lowered)
            self.assertNotIn("sell", lowered)


class TestMsftRealData(_TempDbTestCase):
    def test_msft_produces_no_alerts(self):
        conn = self._connect()
        try:
            result = wa.evaluate_from_db(
                conn, ["MSFT"], state=wa.new_state(), as_of=AS_OF_1)
        finally:
            conn.close()
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(result["evaluated"], ["MSFT"])
        self.assertEqual(result["not_evaluated"], [])
        evidence = result["state"]["last_evidence"]["MSFT"]
        self.assertEqual(evidence["tier"], "overvalued")
        self.assertEqual(evidence["quality_rating"], "excellent")
        self.assertEqual(evidence["health_verdict"], "strong")
        self.assertAlmostEqual(evidence["price"], 507.12)
        self.assertAlmostEqual(evidence["alert_price"], 78.82,
                               places=2)
        self.assertIs(evidence["deteriorated"], False)

    def test_msft_fresh_price_below_alert_price_fires(self):
        # The fresh-price path: the detection and summary were
        # computed on the stored $507.12 price (tier overvalued),
        # then a caller-supplied $75.00 price crosses the
        # $78.82 alert price.  (Passing $75.00 into
        # evaluate_from_db instead would re-compute the tier at
        # $75.00 — strongest_alert — so the price-crossing rule
        # is correctly suppressed there.)
        from agents import margin_of_safety as mos
        from agents import undervalued_detector as ud
        conn = self._connect()
        try:
            detection = ud.detect_with_snapshot_prices(
                conn, tickers=["MSFT"])
            summaries = {"MSFT": mos.margin_of_safety_summary(
                conn, "MSFT")}
            result = wa.evaluate_alerts(
                ["MSFT"], detection=detection, summaries=summaries,
                prices={"MSFT": 75.00}, state=wa.new_state(),
                as_of=AS_OF_1)
        finally:
            conn.close()
        self.assertEqual(len(result["new_alerts"]), 1)
        alert = result["new_alerts"][0]
        self.assertEqual(alert["alert_type"],
                         "price_below_alert_price")
        self.assertEqual(alert["trigger_value"], 75.00)
        self.assertAlmostEqual(alert["threshold"], 78.82, places=2)

    def test_msft_and_dbs_together(self):
        conn = self._connect()
        try:
            result = wa.evaluate_from_db(
                conn, ["MSFT", "D05"], state=wa.new_state(),
                as_of=AS_OF_1)
        finally:
            conn.close()
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(result["evaluated"], ["MSFT"])
        self.assertEqual(
            [n["ticker"] for n in result["not_evaluated"]], ["D05"])


if __name__ == "__main__":
    unittest.main()
