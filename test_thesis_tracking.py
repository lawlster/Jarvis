"""Tests for Step 30 — agents/thesis_tracking.py.

Fixture rows and temporary database copies only; no network
requests, no production database writes.  Thesis JSON files are
written to temporary directories only.

Hand-checked MSFT expectations use the figures verified against
the real database in Steps 17-24 and 27: FY2026 revenue
$331,839m, net income $133,749m, FCF $66,987m; health STRONG;
quality excellent (7/7); deterioration warning, no material
deterioration; DCF bear $98.52, base $163.30, optimistic
$234.64; tier overvalued at $507.12 (2026-11-03 snapshot);
strongest-alert price $78.82.

The SOLID fixture (copied from the Step 29 tests) is a steady,
profitable, modest-debt synthetic business: on an annual FCF
basis its fair-value range is conservative $39.46, base $65.21,
optimistic $93.56, so a price of $100 is overvalued and a price
of $25 is a strongest alert.  Those values were probe-verified
against the real Step 23 summary before these tests were
written.
"""

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agents import thesis_tracking as tt

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_fingerprint import content_fingerprint  # noqa: E402
DB_PATH = BASE_DIR / "database" / "assistant.db"

PRICE_HIGH = {"price": 100.0, "as_of_date": "2026-11-08",
              "source": "test (caller-supplied)"}
PRICE_LOW = {"price": 25.0, "as_of_date": "2026-11-08",
             "source": "test (caller-supplied)"}

BUY_SELL = re.compile(r"\b(buy|sell)\b", re.IGNORECASE)

PERIODS_6Y = ["2021-06-30", "2022-06-30", "2023-06-30",
              "2024-06-30", "2025-06-30", "2026-06-30"]

INCOME_CONCEPTS = (
    "revenue", "cost_of_revenue", "gross_profit",
    "research_and_development", "selling_general_administrative",
    "operating_income", "income_before_tax", "income_tax",
    "net_income", "eps_basic", "eps_diluted",
    "shares_outstanding_basic", "shares_outstanding_diluted",
)
BALANCE_CONCEPTS = (
    "total_assets", "total_liabilities", "total_equity",
    "cash_and_equivalents", "short_term_debt", "long_term_debt",
    "current_assets", "current_liabilities", "inventory",
    "accounts_receivable", "accounts_payable",
    "short_term_investments",
)
CASHFLOW_CONCEPTS = (
    "operating_cash_flow", "capital_expenditure", "dividends_paid",
    "share_repurchases", "share_based_compensation",
    "investing_cash_flow", "financing_cash_flow",
)

# A solid synthetic business: steady growth, profitable, modest
# debt, dividends every year, no dilution.  Values in $m.
SOLID_SPEC = {
    "revenue": [100, 110, 121, 133, 146, 161],
    "cost_of_revenue": [40, 44, 48, 53, 58, 64],
    "gross_profit": [60, 66, 73, 80, 88, 97],
    "research_and_development": [10, 11, 12, 13, 14, 16],
    "selling_general_administrative": [20, 22, 24, 26, 29, 31],
    "operating_income": [30, 33, 37, 41, 45, 50],
    "income_before_tax": [28, 31, 35, 39, 43, 48],
    "income_tax": [6, 7, 8, 9, 10, 11],
    "net_income": [22, 24, 27, 30, 33, 37],
    "eps_basic": [2.2, 2.4, 2.7, 3.0, 3.3, 3.7],
    "eps_diluted": [2.2, 2.4, 2.7, 3.0, 3.3, 3.7],
    "shares_outstanding_basic": [10, 10, 10, 10, 10, 10],
    "shares_outstanding_diluted": [10, 10, 10, 10, 10, 10],
    "total_assets": [200, 210, 220, 235, 250, 270],
    "total_liabilities": [80, 82, 84, 88, 90, 95],
    "total_equity": [120, 128, 136, 147, 160, 175],
    "cash_and_equivalents": [20, 22, 24, 26, 28, 30],
    "short_term_debt": [5, 5, 5, 5, 5, 5],
    "long_term_debt": [30, 30, 30, 30, 30, 30],
    "current_assets": [60, 65, 70, 75, 80, 85],
    "current_liabilities": [40, 42, 44, 46, 48, 50],
    "inventory": [5, 5, 5, 5, 5, 5],
    "accounts_receivable": [15, 16, 18, 20, 22, 24],
    "accounts_payable": [20, 22, 24, 26, 28, 30],
    "short_term_investments": [0, 0, 0, 0, 0, 0],
    "operating_cash_flow": [28, 30, 33, 36, 40, 44],
    "capital_expenditure": [8, 9, 9, 10, 11, 12],
    "dividends_paid": [5, 5, 6, 6, 7, 7],
    "share_repurchases": [2, 2, 2, 2, 2, 2],
    "share_based_compensation": [1, 1, 1, 1, 1, 1],
    "investing_cash_flow": [-10, -11, -11, -12, -13, -14],
    "financing_cash_flow": [-8, -8, -9, -9, -10, -10],
}

RAW_KEYS = {
    "eps_basic": "basic_eps",
    "eps_diluted": "diluted_eps",
    "shares_outstanding_basic": "basic_shares",
    "shares_outstanding_diluted": "diluted_shares",
    "total_equity": "shareholders_equity",
}


def _temp_db_copy():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy(DB_PATH, tmp.name)
    return tmp.name


def _entry(value):
    return {"value": value, "unit": "USD", "tag": "Test",
            "filed": "2026-07-29", "form": "10-K"}


def _fixture_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE financial_statements ("
        "id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
        "exchange TEXT, country TEXT, statement_type TEXT, "
        "reporting_period TEXT, period_type TEXT, fiscal_year INTEGER, "
        "fiscal_quarter INTEGER, currency TEXT, source TEXT, "
        "source_document TEXT, data_json TEXT, collected_at TEXT)")
    conn.execute(
        "CREATE TABLE financial_snapshots (id INTEGER PRIMARY KEY, "
        "ticker TEXT, snapshot_date TEXT, financial_data TEXT)")
    conn.execute(
        "CREATE TABLE market_universe ("
        "id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
        "exchange TEXT, country TEXT, currency TEXT, sector TEXT, "
        "industry TEXT, security_type TEXT, market_cap REAL, "
        "is_active INTEGER, sec_cik TEXT)")
    for row in rows:
        conn.execute(
            "INSERT INTO financial_statements "
            "(ticker, company_name, exchange, country, "
            "statement_type, reporting_period, period_type, "
            "fiscal_year, fiscal_quarter, currency, source, "
            "source_document, data_json, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["ticker"], row["company_name"], row["exchange"],
             row["country"], row["statement_type"],
             row["reporting_period"], row["period_type"],
             row["fiscal_year"], row["fiscal_quarter"],
             row["currency"], row["source"], row["source_document"],
             row["data_json"], row["collected_at"]))
    return conn


def _company_rows(ticker, spec, periods=PERIODS_6Y):
    """One income / balance / cash-flow row per period from a spec."""
    rows = []
    for i, period in enumerate(periods):
        for stmt, concepts in (
                ("income_statement", INCOME_CONCEPTS),
                ("balance_sheet", BALANCE_CONCEPTS),
                ("cash_flow_statement", CASHFLOW_CONCEPTS)):
            data = {RAW_KEYS.get(c, c): _entry(spec[c][i])
                    for c in concepts if c in spec}
            rows.append({
                "ticker": ticker, "company_name": ticker,
                "exchange": "NASDAQ", "country": "USA",
                "statement_type": stmt, "reporting_period": period,
                "period_type": "annual", "fiscal_year": None,
                "fiscal_quarter": None, "currency": "USD",
                "source": "SEC EDGAR Company Facts",
                "source_document": "10-K",
                "data_json": json.dumps(data),
                "collected_at": "2026-11-03",
            })
    return rows


def _bad_spec():
    """SOLID with a loss in the latest year (deterioration)."""
    spec = {k: list(v) for k, v in SOLID_SPEC.items()}
    spec["net_income"][-1] = -5
    spec["eps_basic"][-1] = -0.5
    spec["eps_diluted"][-1] = -0.5
    return spec


class TestEvaluateCondition(unittest.TestCase):
    """Each rule kind, met / unmet / unevaluable."""

    SNAPSHOT = {
        "price": 25.0,
        "fair_value_range": {"conservative": 39.46, "base": 65.21,
                             "optimistic": 93.56},
        "margins_graham": {"conservative": 0.366},
        "tier": "strongest_alert",
        "quality_rating": "excellent",
        "material_deterioration": False,
    }

    def _eval(self, description, rule, snapshot=None):
        return tt.evaluate_condition(
            {"description": description, "rule": rule},
            snapshot if snapshot is not None else self.SNAPSHOT)

    def test_price_rules(self):
        self.assertTrue(self._eval(
            "x", {"kind": "price_at_or_below", "value": 31.57})["met"])
        self.assertFalse(self._eval(
            "x", {"kind": "price_at_or_below", "value": 20.0})["met"])
        self.assertTrue(self._eval(
            "x", {"kind": "price_at_or_above", "value": 25.0})["met"])

    def test_tier_rules(self):
        self.assertTrue(self._eval(
            "x", {"kind": "tier_is",
                  "value": "strongest_alert"})["met"])
        self.assertFalse(self._eval(
            "x", {"kind": "tier_is", "value": "overvalued"})["met"])
        self.assertTrue(self._eval(
            "x", {"kind": "tier_is_not",
                  "value": "overvalued"})["met"])

    def test_deterioration_rule(self):
        self.assertTrue(self._eval(
            "x", {"kind": "material_deterioration",
                  "value": False})["met"])
        self.assertFalse(self._eval(
            "x", {"kind": "material_deterioration",
                  "value": True})["met"])

    def test_quality_rules(self):
        self.assertTrue(self._eval(
            "x", {"kind": "quality_at_or_above",
                  "value": "good"})["met"])
        self.assertFalse(self._eval(
            "x", {"kind": "quality_at_or_below",
                  "value": "weak"})["met"])

    def test_margin_and_value_rules(self):
        self.assertTrue(self._eval(
            "x", {"kind": "margin_vs_conservative_at_least",
                  "value": 0.20})["met"])
        self.assertFalse(self._eval(
            "x", {"kind": "margin_vs_conservative_at_least",
                  "value": 0.50})["met"])
        self.assertTrue(self._eval(
            "x", {"kind": "conservative_value_at_or_above",
                  "value": 30.0})["met"])

    def test_no_rule_is_not_machine_checkable(self):
        result = tt.evaluate_condition(
            {"description": "management stays honest"}, self.SNAPSHOT)
        self.assertIsNone(result["met"])
        self.assertIn("owner to judge", result["reason"])

    def test_unknown_rule_kind(self):
        result = self._eval(
            "x", {"kind": "insider_feeling", "value": 1})
        self.assertIsNone(result["met"])

    def test_missing_input_is_not_machine_checkable(self):
        snapshot = dict(self.SNAPSHOT, price=None)
        result = self._eval(
            "x", {"kind": "price_at_or_below", "value": 10.0},
            snapshot=snapshot)
        self.assertIsNone(result["met"])
        self.assertIn("missing", result["reason"])


class TestDefaultConditions(unittest.TestCase):
    """Defaults are built from the snapshot; already-met ones skip."""

    def test_msft_like_snapshot_skips_overvalued_invalidate(self):
        snapshot = {
            "tier": "overvalued",
            "strongest_alert_threshold_price": 78.82,
            "price": 507.12,
            "margins_graham": {"conservative": -4.147},
            "quality_rating": "excellent",
            "material_deterioration": False,
            "fair_value_range": {"conservative": 98.52},
        }
        confirm, invalidate, skipped = tt.default_conditions(snapshot)
        # The "tier becomes overvalued" invalidate candidate is
        # already true at creation, so it must be skipped, not
        # carried as a live condition.
        self.assertIn(
            "Tier becomes overvalued and stays there while the "
            "price rises — the entry case has gone away", skipped)
        self.assertEqual(len(confirm), 3)
        self.assertEqual(len(invalidate), 2)
        self.assertTrue(all("rule" in c for c in confirm + invalidate))

    def test_alert_snapshot_has_no_confirm_conditions_left(self):
        snapshot = {
            "tier": "strongest_alert",
            "strongest_alert_threshold_price": 31.57,
            "price": 25.0,
            "margins_graham": {"conservative": 0.366},
            "quality_rating": "excellent",
            "material_deterioration": False,
            "fair_value_range": {"conservative": 39.46},
        }
        confirm, _invalidate, skipped = tt.default_conditions(
            snapshot)
        # All three confirm candidates are already met at a $25
        # price, so none is a live condition.
        self.assertEqual(confirm, [])
        self.assertEqual(len(skipped), 3)


class TestCreateThesis(unittest.TestCase):
    """Create, save, load and list theses on fixture data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.theses_dir = Path(self._tmp.name) / "theses"
        self.conn = _fixture_conn(
            _company_rows("SOLID", SOLID_SPEC))
        self.addCleanup(self.conn.close)

    def test_create_fixture_thesis(self):
        thesis = tt.create_thesis(
            self.conn, "SOLID",
            "Solid business, fairly priced today; worth a closer "
            "look only if the price falls towards value.",
            price_input=PRICE_HIGH, created_date="2026-11-08",
            theses_dir=self.theses_dir)
        self.assertEqual(thesis["ticker"], "SOLID")
        self.assertEqual(thesis["status"], "active")
        self.assertEqual(thesis["thesis_id"], "SOLID_2026-11-08_01")
        snapshot = thesis["evidence_snapshot"]
        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["tier"], "overvalued")
        self.assertEqual(snapshot["quality_rating"], "excellent")
        self.assertFalse(snapshot["material_deterioration"])
        self.assertAlmostEqual(
            snapshot["fair_value_range"]["conservative"], 39.46,
            places=1)
        self.assertAlmostEqual(
            snapshot["strongest_alert_threshold_price"], 31.57,
            places=1)
        self.assertEqual(snapshot["price"], 100.0)
        # The tier-overvalued invalidate candidate was skipped.
        self.assertEqual(len(thesis["confirm_conditions"]), 3)
        self.assertEqual(len(thesis["invalidate_conditions"]), 2)
        self.assertEqual(len(
            thesis["conditions_skipped_already_met"]), 1)
        # Saved to disk and reloadable.
        self.assertTrue(Path(thesis["file_path"]).exists())
        loaded = tt.load_thesis(thesis["file_path"])
        self.assertEqual(loaded["thesis_text"],
                         thesis["thesis_text"])
        self.assertEqual(loaded["evidence_snapshot"]["tier"],
                         "overvalued")

    def test_blank_thesis_text_is_refused(self):
        with self.assertRaises(ValueError):
            tt.create_thesis(self.conn, "SOLID", "   ",
                             price_input=PRICE_HIGH,
                             theses_dir=self.theses_dir)
        with self.assertRaises(ValueError):
            tt.create_thesis(self.conn, "SOLID", None,
                             price_input=PRICE_HIGH,
                             theses_dir=self.theses_dir)

    def test_create_with_caller_conditions(self):
        thesis = tt.create_thesis(
            self.conn, "SOLID", "Owner's own words here.",
            confirm_conditions=[{
                "description": "Price falls below $40",
                "rule": {"kind": "price_at_or_below", "value": 40.0}}],
            invalidate_conditions=[{
                "description": "Management loses its way"}],
            price_input=PRICE_HIGH, created_date="2026-11-08",
            theses_dir=self.theses_dir)
        self.assertEqual(thesis["conditions_from"],
                         "caller-supplied")
        self.assertEqual(len(thesis["confirm_conditions"]), 1)
        # A condition with no rule is kept and reported as
        # not machine-checkable on review, never guessed at.
        result = tt.evaluate_condition(
            thesis["invalidate_conditions"][0],
            thesis["evidence_snapshot"])
        self.assertIsNone(result["met"])
        self.assertIn("owner to judge", result["reason"])

    def test_list_and_history(self):
        first = tt.create_thesis(
            self.conn, "SOLID", "First thesis.",
            price_input=PRICE_HIGH, created_date="2026-11-08",
            theses_dir=self.theses_dir)
        second = tt.create_thesis(
            self.conn, "SOLID", "Second thesis.",
            price_input=PRICE_HIGH, created_date="2026-11-09",
            theses_dir=self.theses_dir)
        self.assertEqual(first["thesis_id"], "SOLID_2026-11-08_01")
        self.assertEqual(second["thesis_id"], "SOLID_2026-11-09_01")
        listed = tt.list_theses("SOLID", self.theses_dir)
        self.assertEqual([t["thesis_id"] for t in listed],
                         ["SOLID_2026-11-08_01", "SOLID_2026-11-09_01"])
        history = tt.thesis_history("SOLID", self.theses_dir)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["status"], "active")
        self.assertEqual(tt.list_theses("NOPE", self.theses_dir), [])
        self.assertEqual(tt.list_theses("SOLID", Path(self._tmp.name)
                                        / "missing"), [])

    def test_create_with_no_statements_is_stale(self):
        empty = _fixture_conn([])
        self.addCleanup(empty.close)
        thesis = tt.create_thesis(
            empty, "DBS", "No data yet.",
            price_input=None, created_date="2026-11-08",
            theses_dir=self.theses_dir)
        self.assertEqual(thesis["status"], "stale")
        self.assertEqual(
            thesis["evidence_snapshot"]["status"],
            "insufficient_data")


class TestReviewThesis(unittest.TestCase):
    """Review against unchanged, changed and missing data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.theses_dir = Path(self._tmp.name) / "theses"
        self.conn = _fixture_conn(
            _company_rows("SOLID", SOLID_SPEC))
        self.addCleanup(self.conn.close)

    def _thesis(self, price=PRICE_HIGH):
        return tt.create_thesis(
            self.conn, "SOLID",
            "Solid business; entry only near value.",
            price_input=price, created_date="2026-11-08",
            theses_dir=self.theses_dir)

    def test_review_unchanged_data_stays_active(self):
        thesis = self._thesis()
        review = tt.review_thesis(self.conn, thesis,
                                  price_input=PRICE_HIGH,
                                  review_date="2026-11-09")
        self.assertEqual(review["suggested_status"], "active")
        self.assertEqual(review["changes"], [])
        self.assertEqual(review["confirm_met"], 0)
        self.assertEqual(review["invalidate_met"], 0)

    def test_review_price_fall_confirms(self):
        thesis = self._thesis()
        review = tt.review_thesis(self.conn, thesis,
                                  price_input=PRICE_LOW,
                                  review_date="2026-11-09")
        self.assertEqual(review["suggested_status"], "confirmed")
        self.assertEqual(review["confirm_met"], 3)
        tiers = [c for c in review["changes"]
                 if c["field"] == "tier"]
        self.assertEqual(len(tiers), 1)
        self.assertEqual(tiers[0]["before"], "overvalued")
        self.assertEqual(tiers[0]["after"], "strongest_alert")
        prices = [c for c in review["changes"]
                  if c["field"] == "price"]
        self.assertEqual(prices[0]["before"], 100.0)
        self.assertEqual(prices[0]["after"], 25.0)

    def test_apply_review_updates_status_and_history(self):
        thesis = self._thesis()
        review = tt.review_thesis(self.conn, thesis,
                                  price_input=PRICE_LOW,
                                  review_date="2026-11-09")
        tt.apply_review(thesis, review)
        self.assertEqual(thesis["status"], "confirmed")
        self.assertEqual(len(thesis["reviews"]), 1)
        self.assertEqual(len(thesis["status_history"]), 2)
        self.assertEqual(thesis["status_history"][-1]["status"],
                         "confirmed")
        # Saving again persists the review.
        path = tt.save_thesis(thesis, self.theses_dir)
        loaded = tt.load_thesis(path)
        self.assertEqual(loaded["status"], "confirmed")
        self.assertEqual(len(loaded["reviews"]), 1)

    def test_review_loss_invalidates(self):
        thesis = self._thesis()
        bad_conn = _fixture_conn(_company_rows("SOLID", _bad_spec()))
        self.addCleanup(bad_conn.close)
        review = tt.review_thesis(bad_conn, thesis,
                                  price_input=PRICE_HIGH,
                                  review_date="2026-11-09")
        self.assertEqual(review["suggested_status"], "invalidated")
        self.assertGreaterEqual(review["invalidate_met"], 1)
        det = [c for c in review["changes"]
               if c["field"] == "material_deterioration"]
        self.assertEqual(len(det), 1)
        self.assertFalse(det[0]["before"])
        self.assertTrue(det[0]["after"])

    def test_review_no_data_is_stale(self):
        thesis = self._thesis()
        empty = _fixture_conn([])
        self.addCleanup(empty.close)
        review = tt.review_thesis(empty, thesis,
                                  price_input=None,
                                  review_date="2026-11-09")
        self.assertEqual(review["suggested_status"], "stale")
        self.assertEqual(
            review["snapshot"]["status"], "insufficient_data")

    def test_free_text_conditions_are_never_guessed(self):
        thesis = tt.create_thesis(
            self.conn, "SOLID", "Owner's own words here.",
            confirm_conditions=[{
                "description": "Management stays honest"}],
            invalidate_conditions=[{
                "description": "The story changes"}],
            price_input=PRICE_HIGH, created_date="2026-11-08",
            theses_dir=self.theses_dir)
        review = tt.review_thesis(self.conn, thesis,
                                  price_input=PRICE_LOW,
                                  review_date="2026-11-09")
        # Both conditions are unevaluable, so nothing can be
        # confirmed or invalidated from them.
        self.assertIsNone(review["confirm_conditions"][0]["met"])
        self.assertIsNone(review["invalidate_conditions"][0]["met"])
        self.assertEqual(review["suggested_status"], "active")

    def test_status_transitions_and_close(self):
        thesis = self._thesis()
        tt.update_thesis_status(thesis, "closed",
                                "Owner closed it.", "2026-11-10")
        self.assertEqual(thesis["status"], "closed")
        self.assertEqual(len(thesis["status_history"]), 2)
        with self.assertRaises(ValueError):
            tt.update_thesis_status(thesis, "archived", "Nope")

    def test_no_buy_sell_words_in_output(self):
        thesis = self._thesis()
        review = tt.review_thesis(self.conn, thesis,
                                  price_input=PRICE_LOW,
                                  review_date="2026-11-09")
        for text in (tt.thesis_summary(thesis),
                     review["suggested_reason"]):
            self.assertIsNone(BUY_SELL.search(text), text)
        for condition in (thesis["confirm_conditions"]
                          + thesis["invalidate_conditions"]):
            self.assertIsNone(
                BUY_SELL.search(condition["description"]))


class TestThesisRealData(unittest.TestCase):
    """MSFT on a temp copy of the real DB; production hash guarded."""

    @classmethod
    def setUpClass(cls):
        cls._hash_before = content_fingerprint(DB_PATH)

    @classmethod
    def tearDownClass(cls):
        assert content_fingerprint(DB_PATH) == cls._hash_before, \
            "production DB changed during tests"

    def _conn(self):
        self._db = _temp_db_copy()
        self.addCleanup(lambda: Path(self._db).unlink(missing_ok=True))
        conn = sqlite3.connect(self._db)
        self.addCleanup(conn.close)
        return conn

    def test_msft_thesis_snapshot(self):
        conn = self._conn()
        with tempfile.TemporaryDirectory() as tmp:
            thesis = tt.create_thesis(
                conn, "MSFT",
                "EXAMPLE THESIS (not the owner's words): quality "
                "is excellent and the business is strong, but the "
                "price is far above the valuation range; only a "
                "fall towards the strongest-alert price would "
                "change the picture.",
                price_input=None, created_date="2026-11-08",
                theses_dir=Path(tmp) / "theses")
            self.assertEqual(thesis["status"], "active")
            snapshot = thesis["evidence_snapshot"]
            self.assertEqual(snapshot["status"], "complete")
            self.assertEqual(snapshot["tier"], "overvalued")
            self.assertEqual(snapshot["quality_rating"], "excellent")
            self.assertEqual(snapshot["health_verdict"], "strong")
            self.assertEqual(snapshot["deterioration_verdict"],
                             "warning")
            self.assertFalse(snapshot["material_deterioration"])
            self.assertAlmostEqual(
                snapshot["fair_value_range"]["conservative"], 98.52,
                places=1)
            self.assertAlmostEqual(
                snapshot["fair_value_range"]["base"], 163.30,
                places=1)
            self.assertAlmostEqual(
                snapshot["fair_value_range"]["optimistic"], 234.64,
                places=1)
            self.assertAlmostEqual(
                snapshot["strongest_alert_threshold_price"], 78.82,
                places=1)
            self.assertEqual(snapshot["price"], 507.12)
            self.assertEqual(snapshot["price_as_of"], "2026-11-03")
            self.assertAlmostEqual(
                snapshot["key_figures"]["revenue"], 331_839_000_000,
                delta=1_000_000)
            self.assertAlmostEqual(
                snapshot["key_figures"]["net_income"], 133_749_000_000,
                delta=1_000_000)
            self.assertAlmostEqual(
                snapshot["key_figures"]["free_cash_flow"],
                66_987_000_000, delta=1_000_000)
            summary = tt.thesis_summary(thesis)
            self.assertIn("MSFT", summary)
            self.assertIn("overvalued", summary)
            self.assertIsNone(BUY_SELL.search(summary), summary)

    def test_msft_review_unchanged(self):
        conn = self._conn()
        with tempfile.TemporaryDirectory() as tmp:
            thesis = tt.create_thesis(
                conn, "MSFT", "EXAMPLE THESIS: see Step 30 report.",
                price_input=None, created_date="2026-11-08",
                theses_dir=Path(tmp) / "theses")
            review = tt.review_thesis(conn, thesis,
                                      price_input=None,
                                      review_date="2026-11-09")
            self.assertEqual(review["suggested_status"], "active")
            self.assertEqual(review["changes"], [])
            self.assertEqual(review["confirm_met"], 0)
            self.assertEqual(review["invalidate_met"], 0)

    def test_dbs_thesis_is_stale(self):
        conn = self._conn()
        with tempfile.TemporaryDirectory() as tmp:
            thesis = tt.create_thesis(
                conn, "D05", "EXAMPLE THESIS: no data yet.",
                price_input=None, created_date="2026-11-08",
                theses_dir=Path(tmp) / "theses")
            self.assertEqual(thesis["status"], "stale")
            self.assertEqual(
                thesis["evidence_snapshot"]["status"],
                "insufficient_data")


if __name__ == "__main__":
    unittest.main()
