"""Tests for Step 28 — agents/portfolio_construction.py.

Fixture rows + temporary database copies only; no network
requests, no production database writes.  Temporary database
copies live in a TemporaryDirectory and are removed in tearDown
(the suite leaves temp copies behind otherwise).

Fixture expectations (from the Step 26 tests, verified by probe
against the real Step 23 summary before those tests were written):
- MID_SPEC (test_business_quality) range: conservative $13.39,
  base $23.04, optimistic $33.67; quality "good", no
  deterioration.  At $5.00 -> strongest_alert (margin 62.6%).
- CHEAP_WEAK_SPEC (test_market_scanner) at $0.30: conservative
  value $0.50, quality "weak", health "weak" -> value trap.
- MSFT on a temp copy of the real DB: tier overvalued, quality
  excellent -> not a candidate, no allocation proposed.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import test_business_quality as tbq
import test_market_scanner as tms
import test_undervalued_detector as tud
from agents import portfolio_construction as pc
from agents import undervalued_detector as ud

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def _hand_candidate(ticker, price, rank=1, band="strongest_alert",
                    sector="technology", country="USA",
                    quality="good", margin=0.626):
    """Hand-built detection record (no DB)."""
    return {
        "ticker": ticker, "company_name": ticker,
        "country": country, "sector": sector,
        "exchange": "NASDAQ", "security_type": "common_stock",
        "detection_band": band, "tier": "strongest_alert",
        "price": price, "price_as_of": "2026-11-03",
        "price_source": "test",
        "fair_value_range": {"conservative": 13.39, "base": 23.04,
                             "optimistic": 33.67},
        "margin_of_safety_vs_conservative": margin,
        "quality_rating": quality, "quality_mean_score": 1.5,
        "health_verdict": "strong",
        "rank": rank, "scanner_rank": rank,
    }


class TestKelly(unittest.TestCase):
    def test_kelly_hand_values(self):
        # f* = p - (1-p)/b = 0.6 - 0.4/2 = 0.40; half-Kelly 0.20.
        result = pc.kelly_fraction(0.6, 2.0)
        self.assertAlmostEqual(result["raw_kelly_fraction"], 0.40)
        self.assertAlmostEqual(result["fraction"], 0.20)
        self.assertTrue(result["bet"])

    def test_kelly_no_edge_means_no_bet(self):
        result = pc.kelly_fraction(0.4, 1.0)
        self.assertAlmostEqual(result["raw_kelly_fraction"], -0.20)
        self.assertEqual(result["fraction"], 0.0)
        self.assertFalse(result["bet"])

    def test_kelly_even_money_even_odds_is_zero(self):
        self.assertEqual(pc.kelly_fraction(0.5, 1.0)["fraction"], 0.0)

    def test_kelly_cap_binds(self):
        # Raw 0.88, half-Kelly 0.44, capped at the 0.25 default.
        result = pc.kelly_fraction(0.9, 5.0)
        self.assertAlmostEqual(result["raw_kelly_fraction"], 0.88)
        self.assertEqual(result["fraction"], 0.25)

    def test_kelly_full_multiplier_then_cap(self):
        result = pc.kelly_fraction(0.9, 5.0, kelly_multiplier=1.0)
        self.assertEqual(result["fraction"], 0.25)

    def test_kelly_invalid_inputs_raise(self):
        for bad in ((1.2, 2.0), (0.6, 0.0), (0.6, -1.0),
                    (0.6, 2.0, 1.5), (0.6, 2.0, 0.25, 0.0)):
            with self.assertRaises(ValueError):
                pc.kelly_fraction(*bad)

    def test_size_kelly_arithmetic(self):
        sized = pc.size_kelly(10000, 50, 0.6, 2.0)
        self.assertEqual(sized["fraction"], 0.20)
        self.assertEqual(sized["amount"], 2000.0)
        self.assertEqual(sized["shares"], 40)
        self.assertEqual(sized["cost"], 2000.0)
        self.assertEqual(sized["cash_leftover"], 0.0)

    def test_size_kelly_rounding_leftover_stays_cash(self):
        sized = pc.size_kelly(10000, 30, 0.6, 2.0)
        self.assertEqual(sized["shares"], 66)  # 2000 // 30
        self.assertEqual(sized["cost"], 1980.0)
        self.assertEqual(sized["cash_leftover"], 20.0)

    def test_fixed_fraction_and_equal_weight(self):
        fixed = pc.size_fixed_fraction(10000, 0.1)
        self.assertEqual(fixed["amount"], 1000.0)
        equal = pc.size_equal_weight(10000, 4)
        self.assertEqual(equal["amount_each"], 2500.0)
        with self.assertRaises(ValueError):
            pc.size_fixed_fraction(10000, 1.5)
        with self.assertRaises(ValueError):
            pc.size_equal_weight(10000, 0)
        with self.assertRaises(ValueError):
            pc.size_equal_weight(-100, 4)


class TestWaterfill(unittest.TestCase):
    def test_no_cap_hit(self):
        self.assertEqual(pc._waterfill_weights(5, 0.30),
                         [0.2, 0.2, 0.2, 0.2, 0.2])

    def test_cap_binds_everywhere(self):
        weights = pc._waterfill_weights(4, 0.20)
        self.assertEqual(weights, [0.2, 0.2, 0.2, 0.2])
        self.assertAlmostEqual(sum(weights), 0.80)

    def test_cap_binds_partly(self):
        weights = pc._waterfill_weights(3, 0.25)
        self.assertEqual(weights, [0.25, 0.25, 0.25])
        self.assertAlmostEqual(sum(weights), 0.75)


class TestBuildPortfolio(unittest.TestCase):
    def test_equal_weight_four_candidates(self):
        candidates = [
            _hand_candidate("A", 10, rank=1),
            _hand_candidate("B", 20, rank=2),
            _hand_candidate("C", 25, rank=3),
            _hand_candidate("D", 50, rank=4),
        ]
        proposal = pc.build_portfolio(candidates, 10000, currency="SGD")
        self.assertEqual(proposal["n_positions"], 4)
        self.assertEqual(proposal["total_invested"], 8000.0)
        self.assertEqual(proposal["cash_leftover"], 2000.0)
        by_ticker = {p["ticker"]: p for p in proposal["positions"]}
        self.assertEqual(by_ticker["A"]["shares"], 200)
        self.assertEqual(by_ticker["B"]["shares"], 100)
        self.assertEqual(by_ticker["C"]["shares"], 80)
        self.assertEqual(by_ticker["D"]["shares"], 40)
        for position in proposal["positions"]:
            self.assertEqual(position["target_weight"], 0.20)
            self.assertEqual(position["actual_weight"], 0.20)
        self.assertTrue(proposal["assumptions"]["proposal_only"])
        self.assertTrue(proposal["assumptions"]["no_trade_placed"])

    def test_cap_leaves_most_capital_as_cash_and_warns(self):
        candidates = [_hand_candidate("A", 10, rank=1),
                      _hand_candidate("B", 10, rank=2)]
        proposal = pc.build_portfolio(candidates, 10000, currency="SGD")
        self.assertEqual(proposal["total_invested"], 4000.0)
        self.assertEqual(proposal["cash_leftover"], 6000.0)
        self.assertTrue(any("only 40%" in w
                            for w in proposal["warnings"]))

    def test_max_positions_takes_top_by_rank(self):
        candidates = [_hand_candidate(f"T{i}", 10, rank=i)
                      for i in range(1, 6)]
        proposal = pc.build_portfolio(candidates, 10000, currency="SGD",
                                      max_positions=2)
        self.assertEqual([p["ticker"] for p in proposal["positions"]],
                         ["T1", "T2"])

    def test_missing_price_skipped_never_guessed(self):
        candidates = [_hand_candidate("A", 10, rank=1),
                      dict(_hand_candidate("B", 10, rank=2),
                           price=None)]
        proposal = pc.build_portfolio(candidates, 10000, currency="SGD")
        self.assertEqual([p["ticker"] for p in proposal["positions"]],
                         ["A"])
        self.assertEqual(len(proposal["skipped"]), 1)
        self.assertIn("no usable price", proposal["skipped"][0]["reason"])

    def test_position_too_small_for_one_share_skipped(self):
        candidates = [_hand_candidate("A", 10000, rank=1)]
        proposal = pc.build_portfolio(candidates, 100, currency="SGD")
        self.assertEqual(proposal["n_positions"], 0)
        self.assertEqual(proposal["cash_leftover"], 100.0)
        self.assertIn("0 shares", proposal["skipped"][0]["reason"])

    def test_currency_warning_when_unspecified(self):
        proposal = pc.build_portfolio(
            [_hand_candidate("A", 10, rank=1)], 10000)
        self.assertIsNone(proposal["currency"])
        self.assertTrue(any("currency not specified" in w
                            for w in proposal["warnings"]))

    def test_empty_candidates_proposes_nothing(self):
        proposal = pc.build_portfolio([], 10000, currency="USD")
        self.assertEqual(proposal["n_positions"], 0)
        self.assertEqual(proposal["cash_leftover"], 10000.0)
        self.assertTrue(any("no candidates" in w
                            for w in proposal["warnings"]))

    def test_invalid_inputs_raise(self):
        candidates = [_hand_candidate("A", 10, rank=1)]
        with self.assertRaises(ValueError):
            pc.build_portfolio(candidates, 0, currency="SGD")
        with self.assertRaises(ValueError):
            pc.build_portfolio(candidates, 10000, currency="SGD",
                               method="momentum")
        with self.assertRaises(ValueError):
            pc.build_portfolio(candidates, 10000, currency="SGD",
                               per_position_cap=1.5)
        with self.assertRaises(ValueError):
            pc.build_portfolio(candidates, 10000, currency="SGD",
                               method="kelly")
        with self.assertRaises(ValueError):
            pc.build_portfolio(candidates, 10000, currency="SGD",
                               method="fixed_fraction")

    def test_kelly_method_uses_edges_and_cap(self):
        candidates = [_hand_candidate("A", 10, rank=1),
                      _hand_candidate("B", 10, rank=2)]
        edges = {"A": (0.6, 2.0), "B": (0.9, 5.0)}
        proposal = pc.build_portfolio(
            candidates, 10000, currency="SGD", method="kelly",
            edges=edges)
        by_ticker = {p["ticker"]: p for p in proposal["positions"]}
        # A: half-Kelly 0.20; B: half-Kelly 0.44 capped at 0.20.
        self.assertEqual(by_ticker["A"]["shares"], 200)
        self.assertEqual(by_ticker["B"]["shares"], 200)

    def test_kelly_without_edge_skips_ticker(self):
        candidates = [_hand_candidate("A", 10, rank=1),
                      _hand_candidate("B", 10, rank=2)]
        proposal = pc.build_portfolio(
            candidates, 10000, currency="SGD", method="kelly",
            edges={"A": (0.6, 2.0)})
        self.assertEqual([p["ticker"] for p in proposal["positions"]],
                         ["A"])
        self.assertIn("no edge supplied",
                      proposal["skipped"][0]["reason"])

    def test_fixed_fraction_method(self):
        candidates = [_hand_candidate("A", 10, rank=1),
                      _hand_candidate("B", 10, rank=2)]
        proposal = pc.build_portfolio(
            candidates, 10000, currency="SGD",
            method="fixed_fraction", fixed_fraction=0.10)
        self.assertEqual(proposal["total_invested"], 2000.0)
        self.assertEqual(proposal["cash_leftover"], 8000.0)


class TestFromDetection(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.conn = tud._market_conn()

    def tearDown(self):
        self.conn.close()

    def test_fixture_detection_sizes_two_candidates(self):
        detection = ud.detect_undervalued(
            self.conn, tickers=["FIXA", "FIXB", "FIXW"],
            prices={"FIXA": 5.0, "FIXB": 5.0, "FIXW": 0.30})
        self.assertEqual(len(detection["candidates"]), 2)
        self.assertEqual(len(detection["value_traps"]), 1)
        proposal = pc.build_from_detection(
            detection, 10000, currency="SGD")
        self.assertEqual(proposal["n_positions"], 2)
        self.assertEqual(proposal["value_traps_excluded"], 1)
        by_ticker = {p["ticker"]: p for p in proposal["positions"]}
        self.assertEqual(by_ticker["FIXA"]["shares"], 400)
        self.assertEqual(by_ticker["FIXB"]["shares"], 400)
        self.assertEqual(proposal["total_invested"], 4000.0)
        self.assertEqual(proposal["cash_leftover"], 6000.0)

    def test_msft_real_data_proposes_nothing(self):
        db_path = os.path.join(self._tmpdir.name, "copy.db")
        shutil.copy(DB_PATH, db_path)
        conn = sqlite3.connect(db_path)
        try:
            detection = ud.detect_with_snapshot_prices(conn)
            self.assertEqual(len(detection["candidates"]), 0)
            proposal = pc.build_from_detection(
                detection, 10000, currency="SGD")
            self.assertEqual(proposal["n_positions"], 0)
            self.assertEqual(proposal["cash_leftover"], 10000.0)
            self.assertEqual(proposal["total_invested"], 0.0)
        finally:
            conn.close()


class TestSmallCapBasket(unittest.TestCase):
    def test_equal_split_basket(self):
        candidates = [_hand_candidate("S1", 10, rank=1),
                      _hand_candidate("S2", 4, rank=2),
                      _hand_candidate("S3", 25, rank=3)]
        basket = pc.build_small_cap_basket(candidates, 1000)
        self.assertEqual(basket["n_positions"], 3)
        by_ticker = {p["ticker"]: p for p in basket["positions"]}
        self.assertEqual(by_ticker["S1"]["shares"], 33)
        self.assertEqual(by_ticker["S2"]["shares"], 83)
        self.assertEqual(by_ticker["S3"]["shares"], 13)
        self.assertEqual(by_ticker["S1"]["cost"], 330.0)
        self.assertEqual(by_ticker["S2"]["cost"], 332.0)
        self.assertEqual(by_ticker["S3"]["cost"], 325.0)
        for position in basket["positions"]:
            self.assertEqual(position["amount_at_risk"],
                             position["cost"])
        self.assertEqual(basket["total_amount_at_risk"],
                         basket["total_invested"])
        self.assertTrue(any("currency not specified" in w
                            for w in basket["warnings"]))
        self.assertIn("Accept-the-loss",
                      basket["assumptions"]["discipline"])

    def test_max_loss_per_position_sizes_by_loss(self):
        candidates = [_hand_candidate(f"S{i}", 10, rank=i)
                      for i in range(1, 4)]
        basket = pc.build_small_cap_basket(
            candidates, 1000, currency="SGD",
            max_loss_per_position=200)
        self.assertEqual(basket["n_positions"], 3)
        for position in basket["positions"]:
            self.assertEqual(position["cost"], 200.0)
            self.assertEqual(position["amount_at_risk"], 200.0)
        self.assertEqual(basket["cash_leftover"], 400.0)

    def test_max_loss_caps_number_of_positions(self):
        candidates = [_hand_candidate(f"S{i}", 10, rank=i)
                      for i in range(1, 7)]
        basket = pc.build_small_cap_basket(
            candidates, 1000, currency="USD",
            max_loss_per_position=200)
        self.assertEqual(basket["n_positions"], 5)
        self.assertEqual(basket["total_invested"], 1000.0)
        self.assertEqual(basket["cash_leftover"], 0.0)

    def test_currency_supplied_removes_warning(self):
        basket = pc.build_small_cap_basket(
            [_hand_candidate("S1", 10, rank=1)], 1000, currency="SGD")
        self.assertFalse(any("currency not specified" in w
                             for w in basket["warnings"]))

    def test_empty_basket(self):
        basket = pc.build_small_cap_basket([], 1000, currency="SGD")
        self.assertEqual(basket["n_positions"], 0)
        self.assertEqual(basket["cash_leftover"], 1000.0)

    def test_invalid_basket_inputs_raise(self):
        candidates = [_hand_candidate("S1", 10, rank=1)]
        with self.assertRaises(ValueError):
            pc.build_small_cap_basket(candidates, 0)
        with self.assertRaises(ValueError):
            pc.build_small_cap_basket(candidates, 1000,
                                      max_loss_per_position=2000)
        with self.assertRaises(ValueError):
            pc.build_small_cap_basket(candidates, 1000,
                                      max_loss_per_position=0)


class TestConcentration(unittest.TestCase):
    def test_diversified_has_no_warnings(self):
        positions = [
            {"ticker": "A", "cost": 2000, "sector": "tech",
             "country": "USA"},
            {"ticker": "B", "cost": 2000, "sector": "health",
             "country": "SG"},
            {"ticker": "C", "cost": 2000, "sector": "energy",
             "country": "HK"},
            {"ticker": "D", "cost": 2000, "sector": "finance",
             "country": "USA"},
        ]
        check = pc.concentration_check(positions)
        self.assertEqual(check["warnings"], [])
        self.assertAlmostEqual(check["max_single_name_weight"], 0.25)

    def test_single_name_warning(self):
        positions = [{"ticker": "A", "cost": 5000, "sector": "tech",
                      "country": "USA"},
                     {"ticker": "B", "cost": 1000, "sector": "health",
                      "country": "USA"}]
        check = pc.concentration_check(positions)
        self.assertTrue(any("concentrated in one name" in w
                            for w in check["warnings"]))

    def test_sector_and_country_warnings(self):
        positions = [{"ticker": "A", "cost": 3000, "sector": "tech",
                      "country": "USA"},
                     {"ticker": "B", "cost": 3000, "sector": "tech",
                      "country": "USA"}]
        check = pc.concentration_check(positions)
        self.assertTrue(any("concentrated in one sector" in w
                            for w in check["warnings"]))
        self.assertTrue(any("concentrated in one country" in w
                            for w in check["warnings"]))

    def test_unknown_sector_flagged(self):
        positions = [{"ticker": "A", "cost": 2000, "sector": None,
                      "country": None}]
        check = pc.concentration_check(positions)
        self.assertTrue(any("sector unknown" in w
                            for w in check["warnings"]))

    def test_empty_positions(self):
        check = pc.concentration_check([])
        self.assertIsNone(check["max_single_name_weight"])
        self.assertTrue(check["warnings"])


class TestRebalance(unittest.TestCase):
    def test_sell_to_target_with_cash_remainder(self):
        holdings = [{"ticker": "A", "shares": 100},
                    {"ticker": "B", "shares": 50}]
        prices = {"A": 10, "B": 20}
        result = pc.rebalance_proposal(
            holdings, {"A": 0.25, "B": 0.25}, prices)
        self.assertEqual(result["portfolio_value"], 2000.0)
        by_ticker = {t["ticker"]: t for t in result["trades"]}
        self.assertEqual(by_ticker["A"]["action"], "sell")
        self.assertEqual(by_ticker["A"]["shares"], 50)
        self.assertEqual(by_ticker["B"]["action"], "sell")
        self.assertEqual(by_ticker["B"]["shares"], 25)
        self.assertEqual(result["sell_proceeds"], 1000.0)
        self.assertEqual(result["cash_after"], 1000.0)
        self.assertTrue(any("stays as cash" in w
                            for w in result["warnings"]))

    def test_buy_funded_by_sell(self):
        holdings = [{"ticker": "A", "shares": 100}]
        prices = {"A": 10, "B": 20}
        result = pc.rebalance_proposal(
            holdings, {"A": 0.5, "B": 0.5}, prices)
        by_ticker = {t["ticker"]: t for t in result["trades"]}
        self.assertEqual(by_ticker["A"]["action"], "sell")
        self.assertEqual(by_ticker["A"]["shares"], 50)
        self.assertEqual(by_ticker["B"]["action"], "buy")
        self.assertEqual(by_ticker["B"]["shares"], 25)
        self.assertEqual(result["cash_needed"], 0.0)

    def test_insufficient_cash_warning(self):
        holdings = [{"ticker": "A", "shares": 10}]
        prices = {"A": 10, "B": 30, "C": 30}
        result = pc.rebalance_proposal(
            holdings, {"B": 0.5, "C": 0.5}, prices)
        # Buys 2 x $30 + 2 x $30 = $120 against $100 of sales.
        self.assertEqual(result["buy_cost"], 120.0)
        self.assertEqual(result["sell_proceeds"], 100.0)
        self.assertEqual(result["cash_needed"], 20.0)
        self.assertTrue(any("exceed cash available" in w
                            for w in result["warnings"]))

    def test_no_trades_when_already_at_target(self):
        holdings = [{"ticker": "A", "shares": 100}]
        result = pc.rebalance_proposal(
            holdings, {"A": 1.0}, {"A": 10})
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["cash_needed"], 0.0)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            pc.rebalance_proposal(
                [{"ticker": "A", "shares": 10}],
                {"A": 0.6, "B": 0.6}, {"A": 10, "B": 10})
        with self.assertRaises(ValueError):
            pc.rebalance_proposal(
                [{"ticker": "A", "shares": 10}],
                {"A": 1.0}, {"A": 10}, cash_available=-5)
        with self.assertRaises(ValueError):
            pc.rebalance_proposal(
                [{"ticker": "A", "shares": 10}],
                {"A": 1.0}, {})
        with self.assertRaises(ValueError):
            pc.rebalance_proposal(
                [{"ticker": "A", "shares": 10}],
                {"A": 0.5, "B": 0.5}, {"A": 10})


class TestPersistenceAndPresentation(unittest.TestCase):
    def test_save_load_round_trip(self):
        proposal = pc.build_portfolio(
            [_hand_candidate("A", 10, rank=1)], 10000, currency="SGD")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "proposal.json")
            pc.save_proposal(proposal, path)
            loaded = pc.load_proposal(path)
        self.assertEqual(loaded["total_invested"],
                         proposal["total_invested"])
        self.assertEqual(loaded["positions"][0]["ticker"], "A")

    def test_portfolio_lines(self):
        proposal = pc.build_portfolio(
            [_hand_candidate("A", 10, rank=1)], 10000, currency="SGD")
        lines = pc.portfolio_lines(proposal)
        self.assertIn("A: 200 shares @ $10.00 = $2,000.00",
                      lines[0])
        summary = pc.portfolio_summary(proposal)
        self.assertEqual(summary["n_positions"], 1)
        self.assertEqual(summary["total_invested"], 2000.0)

    def test_basket_lines_state_amount_at_risk(self):
        basket = pc.build_small_cap_basket(
            [_hand_candidate("S1", 10, rank=1)], 1000, currency="SGD")
        lines = pc.portfolio_lines(basket)
        self.assertIn("at risk $1,000.00", lines[0])


if __name__ == "__main__":
    unittest.main()
