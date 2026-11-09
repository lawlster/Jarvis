"""Tests for Step 37 — quarterly portfolio reporting.

Fixture portfolios and price histories only; no network,
no production database writes.  Real-data checks run
against a temp copy of the production database.
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import quarterly_report as qr

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "database" / "assistant.db"


def fixture_portfolio():
    """Three positions with monthly prices through Q3 2026.

    Start (2026-06-30) values are all $1,000, so the quarter
    opens at $3,000; it closes at $3,600.  A 2026-10-31 price
    is included to prove quarter slicing ignores it.
    """
    holdings = [
        {"ticker": "ALPHA", "shares": 100, "cost_basis": 10,
         "quarter_dividends": 20},
        {"ticker": "BETA", "shares": 50, "cost_basis": 20,
         "quarter_dividends": 10},
        {"ticker": "GAMMA", "shares": 200, "cost_basis": 5,
         "quarter_dividends": 0},
    ]
    price_history = {
        "ALPHA": [("2026-06-30", 10), ("2026-07-31", 12),
                  ("2026-08-31", 11), ("2026-09-30", 13),
                  ("2026-10-31", 14)],
        "BETA": [("2026-06-30", 20), ("2026-07-31", 19),
                ("2026-08-31", 21), ("2026-09-30", 18),
                ("2026-10-31", 17)],
        "GAMMA": [("2026-06-30", 5), ("2026-07-31", 6),
                  ("2026-08-31", 6.5), ("2026-09-30", 7),
                  ("2026-10-31", 7.5)],
    }
    benchmark = [("2026-06-30", 1000), ("2026-07-31", 1050),
                 ("2026-08-31", 1020), ("2026-09-30", 1100),
                 ("2026-10-31", 1150)]
    return holdings, price_history, benchmark


class TestParseQuarter(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(
            qr.parse_quarter("Q3 2026"),
            qr.parse_quarter("2026-Q3"))
        self.assertEqual(
            qr.parse_quarter("2026Q3"),
            qr.parse_quarter("Q3 2026"))
        start, end = qr.parse_quarter("Q3 2026")
        self.assertEqual(start.isoformat(), "2026-07-01")
        self.assertEqual(end.isoformat(), "2026-09-30")
        start, end = qr.parse_quarter("Q4 2026")
        self.assertEqual(end.isoformat(), "2026-12-31")
        start, end = qr.parse_quarter("Q1 2026")
        self.assertEqual((start.isoformat(), end.isoformat()),
                         ("2026-01-01", "2026-03-31"))

    def test_invalid_label(self):
        with self.assertRaises(ValueError):
            qr.parse_quarter("H2 2026")
        with self.assertRaises(ValueError):
            qr.parse_quarter("Q5 2026")


class TestQuarterReport(unittest.TestCase):
    def test_fixture_quarter_values(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        hold = report["holdings_summary"]
        self.assertEqual(hold["start_value"], 3000)
        self.assertEqual(hold["end_value"], 3600)
        self.assertEqual(hold["quarter_gain"], 600)
        # Quarter slicing: the October prices must not move
        # the quarter-end value (ALPHA 13, not 14).
        alpha = [p for p in hold["positions"]
                 if p["ticker"] == "ALPHA"][0]
        self.assertEqual(alpha["end_price"], 13)
        self.assertEqual(alpha["quarter_gain"], 300)

    def test_fixture_quarter_returns(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        perf = report["performance"]
        # Price return +20%, total return +21% ($30 dividends).
        self.assertAlmostEqual(
            perf["quarter_return"]["value"], 0.20, places=4)
        self.assertAlmostEqual(
            perf["quarter_total_return"]["value"], 0.21, places=4)
        # Benchmark +10%, so excess return +11%.
        self.assertAlmostEqual(
            perf["benchmark_return"]["value"], 0.10, places=4)
        self.assertAlmostEqual(
            perf["excess_return_vs_benchmark"]["value"], 0.11,
            places=4)
        # TWR covers the whole quarter: 3000 -> 3350 -> 3450
        # -> 3600, so 3600/3000 - 1 = +20%.
        self.assertAlmostEqual(
            perf["time_weighted_return"]["value"], 0.20, places=4)

    def test_contributors_and_detractors(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        perf = report["performance"]
        self.assertEqual(
            [c["ticker"] for c in perf["contributors"]],
            ["GAMMA", "ALPHA"])
        self.assertEqual(
            [d["ticker"] for d in perf["detractors"]], ["BETA"])
        self.assertEqual(perf["detractors"][0]["quarter_gain"],
                         -100)

    def test_risk_snapshot(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD",
            periods_per_year=4)
        risk = report["risk"]
        self.assertEqual(risk["volatility"]["status"], "ok")
        # The quarter value series only rises, so drawdown 0.
        self.assertEqual(risk["max_drawdown"]["value"], 0.0)
        # GAMMA is 1400/3600 = 38.9% of value — above the
        # 25% single-name threshold, so a flag is raised.
        self.assertTrue(any("GAMMA" in f
                            for f in risk["risk_flags"]))

    def test_income(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        income = report["income"]
        self.assertEqual(
            income["dividends_received"]["value"], 30)
        self.assertAlmostEqual(
            income["yield_on_cost"]["value"], 0.01, places=4)

    def test_no_benchmark(self):
        holdings, history, _ = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026", currency="USD")
        self.assertEqual(
            report["performance"]["benchmark_return"]["status"],
            "missing")
        self.assertEqual(
            report["performance"]["excess_return_vs_benchmark"]
            ["status"], "missing")
        self.assertTrue(any("benchmark" in lim.lower()
                            for lim in report["limitations"]))

    def test_money_weighted_return(self):
        holdings, history, benchmark = fixture_portfolio()
        cash_flows = [("2026-07-01", -3000),
                      ("2026-09-30", 3600)]
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD",
            cash_flows=cash_flows)
        mwr = report["performance"]["money_weighted_return"]
        self.assertEqual(mwr["status"], "ok")
        # 3600/3000 over 91 days annualises to well over 100%.
        self.assertGreater(mwr["value"], 1.0)

    def test_valuation_snapshots_supplied(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD",
            valuation_snapshots={
                "ALPHA": {"tier": "reasonable_price",
                          "quality_rating": "good",
                          "health_verdict": "strong",
                          "alert_price": 8.0}})
        alpha = report["valuation"]["ALPHA"]
        self.assertEqual(alpha["tier"], "reasonable_price")
        self.assertEqual(alpha["quality_rating"], "good")
        # The other two holdings have no snapshot.
        self.assertEqual(
            report["valuation"]["BETA"]["status"],
            "insufficient_data")
        self.assertIn("BETA", " ".join(report["limitations"]))

    def test_events_and_news(self):
        holdings, history, benchmark = fixture_portfolio()
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            (events_dir / "e1.json").write_text(json.dumps({
                "event_id": "2026-08-15_ALPHA_01",
                "ticker": "ALPHA", "event_type": "earnings",
                "title": "ALPHA earnings",
                "date": "2026-08-15", "source": "fixture"}))
            (events_dir / "e2.json").write_text(json.dumps({
                "event_id": "2026-10-01_ALPHA_01",
                "ticker": "ALPHA", "event_type": "earnings",
                "title": "ALPHA Q4 earnings",
                "date": "2026-10-01", "source": "fixture"}))
            (events_dir / "e3.json").write_text(json.dumps({
                "event_id": "2026-08-01_MARKET_01",
                "ticker": None, "event_type": "economic",
                "title": "August CPI",
                "date": "2026-08-01", "source": "fixture"}))
            (news_dir / "run.json").write_text(json.dumps({
                "run_id": "r1", "articles": [{
                    "title": "ALPHA beats estimates",
                    "source": "Fixture News",
                    "published_date_iso": "2026-09-10",
                    "tickers": ["ALPHA"]}]}))
            report = qr.generate_quarterly_report(
                holdings, history, quarter="Q3 2026",
                benchmark_history=benchmark, currency="USD",
                events_dir=str(events_dir),
                news_dir=str(news_dir))
        titles = [e["title"] for e in report["events"]]
        self.assertIn("ALPHA earnings", titles)
        self.assertIn("August CPI", titles)
        self.assertIn("ALPHA beats estimates", titles)
        # The October event is outside the quarter.
        self.assertNotIn("ALPHA Q4 earnings", titles)

    def test_quarter_over_quarter(self):
        holdings, history, benchmark = fixture_portfolio()
        # Q2 uses the same holdings without the Q3 dividends.
        q2_holdings = [{**h, "quarter_dividends": 0}
                       for h in holdings]
        q2_history = {
            t: [("2026-03-31", p[0][1]), ("2026-06-30", p[0][1])]
            for t, p in history.items()}
        # Q2: flat prices, so the quarter return is 0 and the
        # value is unchanged at $3,000.
        q2 = qr.generate_quarterly_report(
            q2_holdings, q2_history, quarter="Q2 2026",
            benchmark_history=[("2026-03-31", 900),
                               ("2026-06-30", 1000)],
            currency="USD",
            valuation_snapshots={
                "ALPHA": {"tier": "fairly_valued",
                          "quality_rating": "good",
                          "health_verdict": "strong",
                          "alert_price": 8.0}})
        q3 = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD",
            valuation_snapshots={
                "ALPHA": {"tier": "reasonable_price",
                          "quality_rating": "good",
                          "health_verdict": "strong",
                          "alert_price": 7.5}},
            previous_report=q2)
        qoq = q3["quarter_over_quarter"]
        self.assertEqual(qoq["status"], "ok")
        changes = qoq["value"]
        self.assertEqual(changes["value_change"]["previous"],
                         3000)
        self.assertEqual(changes["value_change"]["current"], 3600)
        self.assertAlmostEqual(
            changes["return_change"]["previous"], 0.0, places=4)
        self.assertAlmostEqual(
            changes["return_change"]["current"], 0.21, places=4)
        self.assertEqual(
            changes["tier_changes"],
            [{"ticker": "ALPHA", "previous": "fairly_valued",
              "current": "reasonable_price"}])
        self.assertEqual(
            changes["alert_price_changes"],
            [{"ticker": "ALPHA", "previous": 8.0,
              "current": 7.5}])

    def test_no_previous_report(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        self.assertEqual(
            report["quarter_over_quarter"]["status"], "missing")
        self.assertTrue(any("previous quarter" in lim.lower()
                            for lim in report["limitations"]))

    def test_empty_portfolio(self):
        report = qr.generate_quarterly_report(
            [], {}, quarter="Q3 2026", currency="USD")
        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(
            report["holdings_summary"]["positions_total"], 0)
        self.assertTrue(report["limitations"])

    def test_single_price_point(self):
        # One distinct price point: no quarter return, no TWR.
        holdings = [{"ticker": "SOLO", "shares": 10,
                     "cost_basis": 100}]
        history = {"SOLO": [("2026-08-15", 120)]}
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026", currency="USD")
        self.assertEqual(
            report["performance"]["quarter_return"]["status"],
            "missing")
        self.assertEqual(
            report["performance"]["time_weighted_return"]
            ["status"], "missing")
        self.assertTrue(report["limitations"])

    def test_explicit_dates(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter_start="2026-07-01",
            quarter_end="2026-09-30",
            benchmark_history=benchmark, currency="USD")
        self.assertEqual(report["quarter"],
                         "2026-07-01 to 2026-09-30")
        self.assertEqual(
            report["holdings_summary"]["end_value"], 3600)

    def test_missing_quarter(self):
        holdings, history, _ = fixture_portfolio()
        with self.assertRaises(ValueError):
            qr.generate_quarterly_report(holdings, history)

    def test_markdown_has_no_recommendation_words(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        markdown = qr.render_markdown(report)
        self.assertIn("Quarterly portfolio report", markdown)
        self.assertIn("ALPHA", markdown)
        self.assertIn("Limitations", markdown)
        lowered = markdown.lower()
        self.assertNotIn("buy", lowered)
        self.assertNotIn("sell", lowered)

    def test_save_and_load_round_trip(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        with tempfile.TemporaryDirectory() as tmp:
            paths = qr.save_report(report, directory=tmp)
            self.assertTrue(Path(paths["json_path"]).exists())
            self.assertTrue(Path(paths["markdown_path"]).exists())
            loaded = qr.load_report(paths["json_path"])
            self.assertEqual(loaded["quarter"], "Q3 2026")
            self.assertEqual(
                loaded["holdings_summary"]["end_value"], 3600)

    def test_beta_is_not_a_percentage(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        markdown = qr.render_markdown(report)
        beta_line = [ln for ln in markdown.splitlines()
                     if ln.startswith("- Portfolio beta:")][0]
        self.assertNotIn("%", beta_line)
        self.assertTrue(any("beta 0.29" in line
                            for line in report["summary_lines"]))

    def test_summary_lines(self):
        holdings, history, benchmark = fixture_portfolio()
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            benchmark_history=benchmark, currency="USD")
        lines = report["summary_lines"]
        self.assertTrue(any("Q3 2026" in line for line in lines))
        self.assertTrue(any("21.00%" in line for line in lines))


class TestRealDataMSFT(unittest.TestCase):
    """MSFT on a temp copy of the production database."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_copy = Path(cls._tmp.name) / "assistant.db"
        shutil.copy(DB_PATH, cls.db_copy)
        conn = sqlite3.connect(cls.db_copy)
        cls.snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM financial_snapshots").fetchone()[0]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM financial_snapshots").fetchone()[0]
        conn.close()
        assert count == cls.snapshot_count, (
            "production financial_snapshots changed")
        cls._tmp.cleanup()

    def test_msft_valuation_snapshot_from_db(self):
        # MSFT has only the single 2026-11-03 snapshot price,
        # so the quarter is Q4 2026 and the history is the
        # snapshot price itself.
        holdings = [{"ticker": "MSFT", "shares": 10,
                     "cost_basis": 400}]
        history = {"MSFT": [("2026-10-01", 500.00),
                            ("2026-11-03", 507.12)]}
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q4 2026",
            currency="USD", db_path=str(self.db_copy))
        snap = report["valuation"]["MSFT"]
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["tier"], "overvalued")
        self.assertEqual(snap["quality_rating"], "excellent")
        self.assertEqual(snap["health_verdict"], "strong")
        self.assertAlmostEqual(snap["alert_price"], 78.82,
                               places=2)

    def test_msft_single_price_point_quarter(self):
        # Q3 2026 has no MSFT price at all: the quarter return
        # is missing, never estimated from the November price.
        holdings = [{"ticker": "MSFT", "shares": 10,
                     "cost_basis": 400}]
        history = {"MSFT": [("2026-11-03", 507.12)]}
        report = qr.generate_quarterly_report(
            holdings, history, quarter="Q3 2026",
            currency="USD", db_path=str(self.db_copy))
        self.assertEqual(
            report["performance"]["quarter_return"]["status"],
            "missing")
        self.assertEqual(
            report["holdings_summary"]["positions_valued"], 0)


if __name__ == "__main__":
    unittest.main()
