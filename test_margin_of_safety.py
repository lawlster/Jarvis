"""Step 23 tests — margin of safety, entry-price ladder, tiers,
deterioration flag, sensitivity.  Fixture rows + temp DB copies only;
no network requests, no production writes."""

import math
import os
import shutil
import sqlite3
import tempfile
import unittest

from agents import margin_of_safety as mos
from agents import intrinsic_valuation as iv

PROD_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "database", "assistant.db")

RANGE = {"conservative": 100.0, "base": 160.0, "optimistic": 230.0}


def _deterioration(deteriorated=False, unknown=False, signals=()):
    return {"deteriorated": deteriorated, "unknown": unknown,
            "signals": list(signals), "cautions": [],
            "rule": "test", "source": "test"}


class TestMarginArithmetic(unittest.TestCase):
    def test_margin_at_value(self):
        self.assertEqual(mos.margin_of_safety(100.0, 100.0), 0.0)

    def test_margin_20_below(self):
        self.assertAlmostEqual(mos.margin_of_safety(100.0, 80.0), 0.20)

    def test_margin_above_value_is_negative(self):
        # MSFT-like: value 163.30, price 507.12.
        self.assertAlmostEqual(
            mos.margin_of_safety(163.30, 507.12),
            (163.30 - 507.12) / 163.30, places=6)

    def test_margin_missing_inputs(self):
        self.assertIsNone(mos.margin_of_safety(None, 80.0))
        self.assertIsNone(mos.margin_of_safety(100.0, None))
        self.assertIsNone(mos.margin_of_safety(0.0, 80.0))
        self.assertIsNone(mos.margin_of_safety(100.0, 0.0))

    def test_margin_vs_price_is_step21_convention(self):
        # Step 21 reported -67.8% for MSFT vs base; Step 23 gives
        # -210.5%.  Both are shown, labelled, and must differ.
        self.assertAlmostEqual(mos.margin_vs_price(163.30, 507.12),
                               (163.30 - 507.12) / 507.12, places=6)
        self.assertAlmostEqual(mos.margin_vs_price(100.0, 80.0), 0.25)
        self.assertNotAlmostEqual(
            mos.margin_of_safety(100.0, 80.0),
            mos.margin_vs_price(100.0, 80.0))

    def test_price_at_margin(self):
        self.assertAlmostEqual(mos.price_at_margin(100.0, 0.20), 80.0)
        self.assertAlmostEqual(mos.price_at_margin(100.0, 0.0), 100.0)
        self.assertAlmostEqual(mos.price_at_margin(100.0, 0.40), 60.0)
        self.assertIsNone(mos.price_at_margin(None, 0.20))
        self.assertIsNone(mos.price_at_margin(0.0, 0.20))

    def test_margins_for_range(self):
        margins = mos.margins_for_range(RANGE, 80.0)
        self.assertAlmostEqual(margins["conservative"]["value"], 0.20)
        self.assertAlmostEqual(margins["base"]["value"], 0.50)
        self.assertAlmostEqual(margins["optimistic"]["value"],
                               (230.0 - 80.0) / 230.0)
        # Each margin carries the Step 21/22 convention too.
        self.assertAlmostEqual(
            margins["conservative"]["margin_vs_price"], 0.25)

    def test_margins_for_range_missing_point(self):
        margins = mos.margins_for_range(
            {"conservative": None, "base": 160.0, "optimistic": 230.0},
            80.0)
        self.assertFalse(margins["conservative"]["present"])
        self.assertIsNotNone(margins["conservative"]["reason"])
        self.assertTrue(margins["base"]["present"])

    def test_margins_for_range_missing_price(self):
        margins = mos.margins_for_range(RANGE, None)
        for point in margins.values():
            self.assertFalse(point["present"])


class TestEntryPriceLadder(unittest.TestCase):
    def test_ladder_prices(self):
        ladder = mos.entry_price_ladder(100.0)
        self.assertTrue(ladder["present"])
        prices = [r["entry_price"] for r in ladder["rungs"]]
        self.assertEqual(prices, [100.0, 80.0, 70.0, 60.0])
        discounts = [r["discount"] for r in ladder["rungs"]]
        self.assertEqual(discounts, [0.0, 0.20, 0.30, 0.40])
        self.assertTrue(ladder["rungs"][1]["is_strongest_alert_threshold"])
        self.assertFalse(ladder["rungs"][0]["is_strongest_alert_threshold"])

    def test_ladder_missing_conservative(self):
        ladder = mos.entry_price_ladder(None)
        self.assertFalse(ladder["present"])
        self.assertIsNotNone(ladder["reason"])

    def test_ladder_implied_fcf_growth(self):
        # At the conservative value itself the implied growth should
        # be the bear scenario growth (+3%) for the MSFT inputs.
        dcf_inputs = {
            "starting_fcf": 59_957_000_000,
            "net_debt": 15_200_000_000,
            "shares_outstanding": 7_446_000_000,
            "discount_rate": 0.11,
            "terminal_growth_rate": 0.025,
            "forecast_years": 10,
        }
        ladder = mos.entry_price_ladder(98.52, dcf_inputs=dcf_inputs)
        rung0 = ladder["rungs"][0]
        self.assertIsNotNone(rung0["implied_fcf_growth"])
        self.assertAlmostEqual(rung0["implied_fcf_growth"], 0.03,
                               places=2)
        # Lower ladder prices imply lower growth.
        growths = [r["implied_fcf_growth"] for r in ladder["rungs"]]
        self.assertTrue(all(g is not None for g in growths))
        self.assertTrue(all(growths[i] > growths[i + 1]
                            for i in range(len(growths) - 1)))

    def test_ladder_ddm_implied_growth(self):
        # Gordon inversion: DPS 2, k 10%, price 40 -> g = (4-2)/42.
        ladder = mos.entry_price_ladder(
            40.0, ddm_inputs={"dps": 2.0, "cost_of_equity": 0.10})
        rung0 = ladder["rungs"][0]
        self.assertAlmostEqual(rung0["implied_dividend_growth"],
                               (0.10 * 40.0 - 2.0) / 42.0, places=6)
        self.assertIsNone(rung0["implied_fcf_growth"])

    def test_implied_ddm_growth_edges(self):
        self.assertIsNone(mos.implied_ddm_growth(None, 2.0, 0.10))
        self.assertIsNone(mos.implied_ddm_growth(40.0, 0.0, 0.10))
        self.assertIsNone(mos.implied_ddm_growth(40.0, 2.0, 0.0))


class TestTierLogic(unittest.TestCase):
    def _margins(self, price):
        return (mos.margin_of_safety(RANGE["conservative"], price),
                mos.margin_of_safety(RANGE["base"], price),
                mos.margin_of_safety(RANGE["optimistic"], price))

    def test_exactly_20_below_is_alert(self):
        m = self._margins(80.0)  # exactly 20% below conservative 100
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "strongest_alert")
        self.assertFalse(tier["strongest_alert_blocked"])

    def test_just_above_20_below_is_not_alert(self):
        m = self._margins(80.10)  # 19.9% below conservative
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "reasonable_price")

    def test_deep_discount_is_alert(self):
        m = self._margins(50.0)
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "strongest_alert")

    def test_deterioration_blocks_alert(self):
        m = self._margins(50.0)
        tier = mos.classify_tier(
            *m, _deterioration(True, signals=["health verdict weak"]))
        self.assertEqual(tier["tier"], "reasonable_price")
        self.assertTrue(tier["strongest_alert_blocked"])
        self.assertIn("BLOCKED", tier["reasons"][0])

    def test_unknown_deterioration_blocks_alert(self):
        m = self._margins(50.0)
        tier = mos.classify_tier(*m, _deterioration(None, unknown=True))
        self.assertNotEqual(tier["tier"], "strongest_alert")
        self.assertTrue(tier["strongest_alert_blocked"])

    def test_reasonable_price_band(self):
        # Price 120: inside conservative-base range.
        m = self._margins(120.0)
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "reasonable_price")
        # Price 90: below conservative but <20% discount.
        m = self._margins(90.0)
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "reasonable_price")

    def test_fairly_valued_band(self):
        # Price 200: above base 160, below optimistic 230.
        m = self._margins(200.0)
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "fairly_valued")

    def test_overvalued(self):
        m = self._margins(300.0)
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertEqual(tier["tier"], "overvalued")
        self.assertTrue(any("optimistic" in r
                            for r in tier["reasons"]))

    def test_insufficient_data(self):
        tier = mos.classify_tier(None, None, None,
                                 _deterioration(False))
        self.assertEqual(tier["tier"], "insufficient_data")

    def test_conditions_to_change_present(self):
        m = self._margins(300.0)
        tier = mos.classify_tier(*m, _deterioration(False))
        self.assertTrue(tier["conditions_to_change"])
        self.assertTrue(any("80%" in c
                            for c in tier["conditions_to_change"]))

    def test_never_buy_or_sell(self):
        for price in (50.0, 80.0, 120.0, 200.0, 300.0):
            m = self._margins(price)
            tier = mos.classify_tier(*m, _deterioration(False))
            text = " ".join(tier["reasons"]
                            + tier["conditions_to_change"]).lower()
            self.assertNotIn("buy", text)
            self.assertNotIn("sell", text)


class TestSensitivity(unittest.TestCase):
    def test_dcf_sensitivity_shape_and_monotonicity(self):
        sens = mos.dcf_margin_sensitivity(
            59_957_000_000, 15_200_000_000, 7_446_000_000, 507.12)
        self.assertEqual(len(sens["rows"]), 9)  # 3 WACC x 3 growth
        # Higher WACC -> lower value -> lower (more negative) margin.
        by_growth = {}
        for row in sens["rows"]:
            by_growth.setdefault(row["growth_rate"], []).append(row)
        for rows in by_growth.values():
            rows.sort(key=lambda r: r["discount_rate_wacc"])
            values = [r["value"] for r in rows]
            self.assertTrue(all(values[i] > values[i + 1]
                                for i in range(len(values) - 1)))
        # Base cell (11%, +10%) reproduces Step 21: $163.30.
        base = [r for r in sens["rows"]
                if r["discount_rate_wacc"] == 0.11
                and r["growth_rate"] == 0.10][0]
        self.assertAlmostEqual(base["value"], 163.30, places=1)
        self.assertAlmostEqual(
            base["margin_of_safety"], (163.30 - 507.12) / 163.30,
            places=2)

    def test_ddm_sensitivity(self):
        sens = mos.ddm_margin_sensitivity(2.0, 40.0)
        self.assertEqual(len(sens["rows"]), 3)
        values = [r["value"] for r in sens["rows"]]
        self.assertTrue(all(values[i] > values[i + 1]
                            for i in range(len(values) - 1)))
        # k=10%, g=2.5%: 2 x 1.025 / 0.075 = 27.33.
        self.assertAlmostEqual(sens["rows"][1]["value"], 27.333, places=2)


class TestRelativePeCheck(unittest.TestCase):
    def test_relative_pe(self):
        check = mos.relative_pe_check(507.12, 17.95, RANGE)
        self.assertTrue(check["present"])
        self.assertAlmostEqual(check["current_pe"], 507.12 / 17.95,
                               places=2)
        self.assertAlmostEqual(
            check["implied_pe_by_range_point"]["base"],
            160.0 / 17.95, places=2)

    def test_relative_pe_missing(self):
        self.assertFalse(
            mos.relative_pe_check(507.12, None, RANGE)["present"])
        self.assertFalse(
            mos.relative_pe_check(507.12, 17.95, None)["present"])


class TestDeteriorationRule(unittest.TestCase):
    def test_override_true(self):
        out = mos.assess_deterioration(
            None, "MSFT", deterioration_override=True)
        self.assertTrue(out["deteriorated"])
        self.assertEqual(out["source"],
                         "caller-supplied deterioration_override")

    def test_override_false(self):
        out = mos.assess_deterioration(
            None, "MSFT", deterioration_override=False)
        self.assertFalse(out["deteriorated"])
        self.assertFalse(out["unknown"])


class TestMsftOnRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="step23-msft-")
        cls.db_path = os.path.join(cls.tmpdir, "assistant.db")
        shutil.copyfile(PROD_DB, cls.db_path)
        cls.conn = sqlite3.connect(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_msft_summary(self):
        summary = mos.margin_of_safety_summary(self.conn, "MSFT")
        # Step 21's report rounds these; the exact values are
        # 98.5207 / 163.30 / 234.64.
        self.assertAlmostEqual(
            summary["fair_value_range"]["conservative"], 98.52,
            places=2)
        self.assertAlmostEqual(
            summary["fair_value_range"]["base"], 163.30, places=1)
        self.assertAlmostEqual(
            summary["fair_value_range"]["optimistic"], 234.64,
            places=1)
        self.assertEqual(summary["price"], 507.12)
        self.assertEqual(summary["price_as_of"], "2026-11-03")
        # Step 23 margins (Graham convention).
        self.assertAlmostEqual(
            summary["margin_of_safety_vs_conservative"],
            (98.52 - 507.12) / 98.52, places=2)
        self.assertAlmostEqual(
            summary["margin_of_safety_vs_base"],
            (163.30 - 507.12) / 163.30, places=2)
        self.assertAlmostEqual(
            summary["margin_of_safety_vs_optimistic"],
            (234.64 - 507.12) / 234.64, places=2)
        # Tier: price above optimistic value.
        self.assertEqual(summary["tier"]["tier"], "overvalued")
        self.assertFalse(summary["tier"]["strongest_alert_blocked"])
        # No deterioration: health STRONG, revenue +17.8%, net
        # income +31.3% in FY2026.
        self.assertFalse(summary["deterioration"]["deteriorated"])
        self.assertEqual(
            summary["deterioration"]["inputs"]["health_verdict"],
            "strong")
        # FCF fell in FY2026 but that is a caution, not a signal.
        self.assertEqual(summary["deterioration"]["signals"], [])
        self.assertTrue(any("FCF" in c or "free cash flow" in c
                            for c in
                            summary["deterioration"]["cautions"]))

    def test_msft_ladder(self):
        summary = mos.margin_of_safety_summary(self.conn, "MSFT")
        ladder = summary["entry_price_ladder"]
        self.assertTrue(ladder["present"])
        prices = [r["entry_price"] for r in ladder["rungs"]]
        self.assertAlmostEqual(prices[0], 98.52, places=2)
        self.assertAlmostEqual(prices[1], 78.82, places=2)  # alert
        self.assertAlmostEqual(prices[2], 68.96, places=2)
        self.assertAlmostEqual(prices[3], 59.11, places=2)
        # Implied growth at the conservative value is the bear rate.
        self.assertAlmostEqual(
            ladder["rungs"][0]["implied_fcf_growth"], 0.03, places=2)
        # Alert threshold price is exactly 80% of conservative.
        self.assertAlmostEqual(
            summary["strongest_alert_threshold_price"], 78.82,
            places=2)

    def test_msft_sensitivity_reproduces_step21(self):
        summary = mos.margin_of_safety_summary(self.conn, "MSFT")
        sens = summary["sensitivity"]
        self.assertEqual(sens["method"], "dcf_margin_sensitivity")
        base = [r for r in sens["rows"]
                if r["discount_rate_wacc"] == 0.11
                and r["growth_rate"] == 0.10][0]
        self.assertAlmostEqual(base["value"], 163.30, places=1)

    def test_msft_relative_pe(self):
        summary = mos.margin_of_safety_summary(self.conn, "MSFT")
        rel = summary["relative_pe_check"]
        self.assertTrue(rel["present"])
        # Current P/E 28.25 (Step 17) vs base-implied P/E ~9.1.
        self.assertAlmostEqual(rel["current_pe"], 28.25, places=1)
        self.assertAlmostEqual(
            rel["implied_pe_by_range_point"]["base"],
            163.30 / 17.95, places=1)

    def test_dbs_insufficient_data(self):
        # DBS has no standardised statements: no range, no tier.
        summary = mos.margin_of_safety_summary(
            self.conn, "D05", sector_override="bank")
        self.assertEqual(summary["tier"]["tier"], "insufficient_data")
        self.assertIsNone(
            summary["fair_value_range"]["conservative"])
        self.assertIsNone(summary["price"])


if __name__ == "__main__":
    unittest.main()
