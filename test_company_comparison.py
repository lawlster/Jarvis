"""Tests for Step 28b — agents/company_comparison.py.

Fixture rows + temporary database copies only; no network
requests, no production database writes.

Fixture expectations (verified by probe against the real
Step 17-24/27 pipeline before these tests were written):
- MID_SPEC (test_business_quality) at $5.00: revenue 1220,
  net margin 12.7%, ROE 16.8%, ROIC 15.2%, D/E 0.32,
  current ratio 1.35, P/E 3.23, DCF base $23.04, quality
  "good" (mean score 1.43), health "strong", deterioration
  "improving".
- BIG_SPEC (below) at $20.00: revenue 2440, net margin 18.3%,
  ROE 16.8%, ROIC 19.0%, D/E 0.07, current ratio 1.35 (tied
  with MID), P/E 4.47, DCF base $90.49, quality "good" (1.57),
  health "strong", deterioration "stable".
- CHEAP_WEAK_SPEC (test_market_scanner) at $0.30: revenue 990,
  net margin 0.4%, ROE 0.5%, ROIC 0.8%, D/E 0.38, P/E 7.50,
  DCF base $2.11, quality "weak" (0.0), health "weak",
  deterioration "deteriorating" (material).
- 10y CAGRs are missing for every fixture company: 6 annual
  periods cannot support an 11-period 10y CAGR (the reason is
  recorded on the metric).
- MSFT on a temp copy of the real DB vs NVDA/AVGO/GOOGL: the
  target is comparable, but no peer has statements, so no
  ranking is meaningful; MSFT profile values match Step 17-24.
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import test_business_quality as tbq
import test_market_scanner as tms
from agents import company_comparison as cc

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_fingerprint import content_fingerprint  # noqa: E402
DB_PATH = BASE_DIR / "database" / "assistant.db"


# Larger, more profitable peer: bigger revenue, wider margins,
# higher ROIC, less debt — but a higher P/E and P/B than MID.
BIG_SPEC = {
    "revenue": [1900, 2000, 2100, 2200, 2320, 2440],
    "cost_of_revenue": [1140, 1200, 1250, 1300, 1360, 1420],
    "gross_profit": [760, 800, 850, 900, 960, 1020],
    "research_and_development": [76, 80, 84, 88, 92, 96],
    "selling_general_administrative": [266, 280, 294, 308, 324, 340],
    "operating_income": [418, 440, 472, 504, 544, 584],
    "income_before_tax": [399, 420, 451, 482, 521, 559],
    "income_tax": [80, 84, 90, 96, 104, 112],
    "net_income": [319, 336, 361, 386, 417, 447],
    "eps_basic": [3.19, 3.36, 3.61, 3.86, 4.17, 4.47],
    "eps_diluted": [3.19, 3.36, 3.61, 3.86, 4.17, 4.47],
    "shares_outstanding_basic": [100, 100, 100, 100, 100, 100],
    "shares_outstanding_diluted": [100, 100, 100, 100, 100, 100],
    "total_assets": [3900, 4000, 4100, 4200, 4360, 4500],
    "total_liabilities": [1560, 1600, 1640, 1680, 1744, 1800],
    "total_equity": [2340, 2400, 2460, 2520, 2616, 2700],
    "cash_and_equivalents": [270, 300, 330, 360, 390, 420],
    "short_term_debt": [50, 50, 50, 50, 50, 50],
    "long_term_debt": [150, 150, 150, 150, 150, 150],
    "current_assets": [1260, 1300, 1340, 1380, 1420, 1460],
    "current_liabilities": [980, 1000, 1020, 1040, 1060, 1080],
    "inventory": [230, 240, 250, 260, 270, 280],
    "accounts_receivable": [250, 260, 270, 280, 290, 300],
    "accounts_payable": [210, 220, 230, 240, 250, 260],
    "short_term_investments": [90, 100, 110, 120, 130, 140],
    "operating_cash_flow": [426, 450, 480, 510, 540, 570],
    "capital_expenditure": [116, 120, 124, 130, 136, 140],
    "dividends_paid": [108, 120, 132, 144, 156, 168],
    "share_repurchases": [30, 30, 30, 30, 30, 30],
    "share_based_compensation": [10, 10, 10, 10, 10, 10],
    "investing_cash_flow": [-116, -120, -124, -130, -136, -140],
    "financing_cash_flow": [-138, -150, -162, -174, -186, -198],
}

PRICES = {"MID": 5.00, "BIG": 20.00, "WEAK": 0.30}


def _fixture_conn():
    rows = (tbq._company_rows("MID", tbq.MID_SPEC)
            + tbq._company_rows("BIG", BIG_SPEC)
            + tbq._company_rows("WEAK", tms.CHEAP_WEAK_SPEC))
    return tbq._fixture_conn(rows)


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


class TestFixtureComparison(unittest.TestCase):
    def setUp(self):
        self.conn = _fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_three_company_ranks(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES)
        self.assertEqual(comp["status"], "comparable")
        self.assertTrue(comp["ranking_meaningful"])
        self.assertEqual(comp["comparable_peers"], ["BIG", "WEAK"])
        self.assertEqual(comp["not_comparable_peers"], {})
        metrics = comp["metrics"]
        # Size: BIG largest, MID middle, WEAK smallest.
        self.assertEqual(
            [metrics["revenue"]["values"][t]["rank"]
             for t in ("BIG", "MID", "WEAK")], [1, 2, 3])
        self.assertEqual(
            [metrics["revenue"]["values"][t]["value"]
             for t in ("BIG", "MID", "WEAK")], [2440, 1220, 990])
        # Profitability: BIG widest margins and ROIC, WEAK thinnest.
        self.assertEqual(
            metrics["net_margin"]["values"]["BIG"]["rank"], 1)
        self.assertEqual(
            metrics["net_margin"]["values"]["WEAK"]["rank"], 3)
        self.assertAlmostEqual(
            metrics["net_margin"]["values"]["MID"]["value"],
            0.127, places=3)
        self.assertAlmostEqual(
            metrics["roic"]["values"]["BIG"]["value"],
            0.1904, places=3)
        # Health: lower D/E ranks first; BIG least levered.
        self.assertEqual(
            metrics["debt_to_equity"]["values"]["BIG"]["rank"], 1)
        self.assertEqual(
            metrics["debt_to_equity"]["values"]["WEAK"]["rank"], 3)
        # Valuation: lower P/E ranks first — MID is cheapest here.
        self.assertEqual(
            metrics["pe_ratio"]["values"]["MID"]["rank"], 1)
        self.assertEqual(
            metrics["pe_ratio"]["values"]["WEAK"]["rank"], 3)
        self.assertAlmostEqual(
            metrics["pe_ratio"]["values"]["MID"]["value"],
            3.2258, places=3)
        # DCF base value: BIG highest, WEAK lowest.
        self.assertEqual(
            [metrics["dcf_base"]["values"][t]["rank"]
             for t in ("BIG", "MID", "WEAK")], [1, 2, 3])
        self.assertAlmostEqual(
            metrics["dcf_base"]["values"]["MID"]["value"],
            23.04, places=1)
        # Quality: BIG best, WEAK worst.
        self.assertEqual(
            [metrics["quality_mean_score"]["values"][t]["rank"]
             for t in ("BIG", "MID", "WEAK")], [1, 2, 3])

    def test_tie_shares_rank(self):
        # MID and BIG have the same current ratio (1.3519): both
        # rank 1, WEAK ranks 3 — dense ranking, no rank 2.
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES)
        values = comp["metrics"]["current_ratio"]["values"]
        self.assertEqual(values["MID"]["rank"], 1)
        self.assertEqual(values["BIG"]["rank"], 1)
        self.assertEqual(values["WEAK"]["rank"], 3)

    def test_missing_metric_never_ranked(self):
        # 10y CAGRs need 11 annual periods; the fixtures have 6,
        # so nobody is ranked and the reason is recorded.
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES)
        table = comp["metrics"]["revenue_cagr_10y"]
        self.assertEqual(table["ranked_count"], 0)
        for ticker in ("MID", "BIG", "WEAK"):
            entry = table["values"][ticker]
            self.assertFalse(entry["present"])
            self.assertIsNone(entry["rank"])
            self.assertIn("insufficient history", entry["reason"])
        # 5y CAGRs exist for all three.
        self.assertEqual(
            comp["metrics"]["revenue_cagr_5y"]["ranked_count"], 3)

    def test_stands_out_and_lags(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES)
        standout = {item["metric"] for item in comp["stands_out"]}
        lags = {item["metric"] for item in comp["lags"]}
        # MID is cheapest on P/E and tied best on current ratio.
        self.assertIn("pe_ratio", standout)
        self.assertIn("current_ratio", standout)
        # MID is last on latest net-income YoY.
        self.assertIn("net_income_yoy_latest", lags)
        # BIG's advantages are not listed as MID standouts.
        self.assertNotIn("net_margin", standout)
        self.assertNotIn("revenue", standout)

    def test_profile_assessments(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES)
        mid = comp["target_profile"]
        self.assertEqual(mid["health_verdict"], "strong")
        self.assertEqual(mid["quality_rating"], "good")
        self.assertAlmostEqual(mid["quality_mean_score"], 1.4286,
                               places=3)
        self.assertEqual(mid["deterioration_verdict"], "improving")
        self.assertFalse(mid["material_deterioration"])
        weak = comp["peer_profiles"]["WEAK"]
        self.assertEqual(weak["health_verdict"], "weak")
        self.assertEqual(weak["quality_rating"], "weak")
        self.assertEqual(weak["deterioration_verdict"],
                         "deteriorating")
        self.assertTrue(weak["material_deterioration"])

    def test_summary_text(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES)
        summary = cc.comparison_summary(comp)
        self.assertIn("MID vs 2 comparable peer(s)", summary)
        self.assertIn("Stands out:", summary)
        self.assertIn("Lags:", summary)
        self.assertNotIn("buy", summary.lower())
        self.assertNotIn("sell", summary.lower())

    def test_one_peer_means_no_meaningful_ranking(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG"], prices=PRICES)
        self.assertEqual(comp["status"], "comparable")
        self.assertFalse(comp["ranking_meaningful"])
        self.assertIsNotNone(comp["ranking_note"])
        # Values are still shown, ranks are not assigned.
        self.assertIsNone(
            comp["metrics"]["revenue"]["values"]["MID"]["rank"])
        self.assertEqual(
            comp["metrics"]["revenue"]["values"]["MID"]["value"],
            1220)
        self.assertEqual(comp["stands_out"], [])
        self.assertEqual(comp["lags"], [])

    def test_target_without_statements(self):
        comp = cc.compare_companies(
            self.conn, "NOPE", ["MID", "BIG"], prices=PRICES)
        self.assertEqual(comp["status"], "insufficient_data")
        self.assertIn("no financial statement rows",
                      comp["target_profile"]["missing_inputs"][0])
        self.assertEqual(comp["metrics"], {})
        self.assertIn("no comparison possible",
                      cc.comparison_summary(comp))

    def test_peer_without_statements_listed_not_dropped(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "NOPE"], prices=PRICES)
        self.assertEqual(comp["comparable_peers"], ["BIG"])
        self.assertIn("NOPE", comp["not_comparable_peers"])
        self.assertIn("no financial statement rows",
                      comp["not_comparable_peers"]["NOPE"][0])
        # One comparable peer: ranking not meaningful.
        self.assertFalse(comp["ranking_meaningful"])

    def test_duplicate_peers_and_target_in_peers(self):
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "BIG", "MID", "WEAK"],
            prices=PRICES)
        self.assertEqual(comp["peers_requested"], ["BIG", "WEAK"])
        self.assertEqual(comp["comparable_peers"], ["BIG", "WEAK"])

    def test_sector_filter_excludes_unmatched(self):
        # The fixture DB has no market_universe table, so every
        # peer's stored sector is None and none matches the filter.
        comp = cc.compare_companies(
            self.conn, "MID", ["BIG", "WEAK"], prices=PRICES,
            sector="technology")
        self.assertEqual(comp["comparable_peers"], [])
        self.assertEqual(set(comp["excluded_peers"]),
                         {"BIG", "WEAK"})
        self.assertIn("stored sector is None",
                      comp["excluded_peers"]["BIG"])
        self.assertFalse(comp["ranking_meaningful"])


class TestRealDataComparison(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hash_before = content_fingerprint(DB_PATH)
        cls.db_path = _temp_db_copy()

    @classmethod
    def tearDownClass(cls):
        Path(cls.db_path).unlink(missing_ok=True)
        assert content_fingerprint(DB_PATH) == cls.hash_before, (
            "production DB changed during tests")

    def test_msft_vs_real_peers(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro",
                               uri=True)
        try:
            comp = cc.compare_companies(
                conn, "MSFT", ["NVDA", "AVGO", "GOOGL"])
        finally:
            conn.close()
        # MSFT has statements, so the comparison exists — but no
        # peer has any, so no ranking is meaningful.
        self.assertEqual(comp["status"], "comparable")
        self.assertEqual(comp["comparable_peers"], [])
        self.assertEqual(set(comp["not_comparable_peers"]),
                         {"NVDA", "AVGO", "GOOGL"})
        self.assertFalse(comp["ranking_meaningful"])
        self.assertEqual(comp["stands_out"], [])
        self.assertEqual(comp["lags"], [])
        # MSFT profile matches Steps 17-24 on the real DB.
        msft = comp["target_profile"]
        self.assertEqual(msft["reporting_period"], "2026-06-30")
        self.assertAlmostEqual(
            msft["metrics"]["revenue"]["value"], 331839000000,
            delta=1000000)
        self.assertAlmostEqual(
            msft["metrics"]["net_margin"]["value"], 0.4031,
            places=3)
        self.assertAlmostEqual(
            msft["metrics"]["roe"]["value"], 0.3404, places=3)
        self.assertAlmostEqual(
            msft["metrics"]["roic"]["value"], 0.3059, places=3)
        self.assertAlmostEqual(
            msft["metrics"]["debt_to_equity"]["value"], 0.09,
            places=2)
        self.assertAlmostEqual(
            msft["metrics"]["pe_ratio"]["value"], 28.25, places=1)
        self.assertAlmostEqual(
            msft["metrics"]["dcf_base"]["value"], 163.30, places=1)
        self.assertEqual(msft["health_verdict"], "strong")
        self.assertEqual(msft["quality_rating"], "excellent")
        self.assertEqual(msft["deterioration_verdict"], "warning")
        self.assertFalse(msft["material_deterioration"])
        self.assertEqual(msft["price"], 507.12)
        self.assertEqual(msft["price_as_of"], "2026-11-03")

    def test_msft_as_target_without_statements_peer(self):
        # DBS has rows but no standardised concepts: it is
        # "comparable" by row count only if the pipeline yields
        # metrics — here it must not be treated as a peer with data.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro",
                               uri=True)
        try:
            prof = cc.build_company_profile(conn, "D05")
        finally:
            conn.close()
        self.assertTrue(prof["has_statements"])
        self.assertIsNone(prof["metrics"]["revenue"]["value"])
        self.assertIsNone(prof["metrics"]["net_margin"]["value"])
        self.assertIsNone(prof["quality_mean_score"])


if __name__ == "__main__":
    unittest.main()
