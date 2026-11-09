"""Tests for Step 55 — the Iron Man-style UI redesign.

The redesigned UI (dark mission-control theme, card layout,
sortable watchlist table, canvas DCF charts, command chips) is
exercised over real localhost HTTP against a server in a thread.
The database is a temp copy; the model client is a stub; the
live-scan data is a small fixture JSON; the readiness verdict is
a fixture dict. No production writes, no live scan is ever run.
"""

import gc
import hashlib
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from agent import orchestrator as orch
from agent import ui
from agents import watchlist_foundation as wf

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from db_fingerprint import (  # noqa: E402
    cleanup_temp_dir,
    content_fingerprint,
    temp_dir_with_db_cleanup,
)
PROD_DB = BASE / "database" / "assistant.db"

# A control whose name, action, or label suggests trading must
# never appear in the served HTML.
_FORBIDDEN_CONTROL_RE = re.compile(
    r"<(?:button|input|select|form)[^>]*(?:buy|sell|trade|order|"
    r"place_order|checkout)",
    re.IGNORECASE,
)


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fixture_scan():
    """A small Step 54 live-scan file: 2 strongest-alert bands,
    1 watch band, 1 fairly-valued row, 2 not-scannable rows."""
    def row(ticker, price, band, tier, conservative, base,
            optimistic, status="ok", reason=None):
        record = {
            "ticker": ticker, "yahoo_symbol": ticker,
            "status": status, "detection_band": band,
            "provisional": bool(band),
            "deterioration_unknown": True, "quality_unknown": True,
            "dcf_basis": "10-year FCF DCF, WACC 11%, terminal 2.5%",
            "company_name": f"{ticker} Corp", "sector": "Test",
            "currency": "USD", "price_as_of": "2026-11-06T21:00:00Z",
            "retrieved_at": "2026-11-09T04:35:00Z",
            "price": price, "reason": reason,
        }
        if status == "ok":
            record["fair_value_range"] = {
                "conservative": conservative, "base": base,
                "optimistic": optimistic}
            record["margin_of_safety_vs_conservative"] = (
                (conservative - price) / conservative
                if conservative else 0.0)
            record["tier"] = tier
        return record

    return {
        "results": [
            row("BKE", 41.96, "strongest_alert", "reasonable_price",
                54.83, 79.42, 114.81),
            row("VC", 75.68, "strongest_alert", "reasonable_price",
                174.96, 243.96, 343.29),
            # KSPI's band is the known currency artifact from the
            # Step 54 report (FCF in tenge, price in USD).
            row("KSPI", 91.68, "strongest_alert", "reasonable_price",
                39436.00, 55341.00, 78240.00),
            row("DHI", 152.28, "watch_discount", "reasonable_price",
                162.00, 210.00, 280.00),
            # DBS's band is the known model-invalid bank case.
            row("DBS", 69.72, "watch_discount", "reasonable_price",
                80.00, 110.00, 150.00),
            row("GOOGL", 284.78, None, "fairly_valued",
                174.06, 250.64, 360.90),
            row("ALC", None, None, None, None, None, None,
                status="provider_unavailable",
                reason="curl: (28) Operation timed out."),
            row("BN", 41.38, None, None, None, None, None,
                status="insufficient_data",
                reason="latest annual free cash flow is not "
                       "positive."),
        ],
        "summary": {"selected": 8, "scannable": 6, "candidates": 5,
                    "by_band": {"strongest_alert": 3,
                                "watch_discount": 2,
                                "watch_below_base": 0},
                    "strongest_alerts": 3, "watch": 2,
                    "provider_unavailable": 1, "insufficient_data": 1},
        "scanned_at": "2026-11-09T04:18:15+00:00",
        "method": {"dcf_basis": "10-year FCF DCF, WACC 11%"},
        "limitations": ["Bands are provisional — quality and "
                        "deterioration are unknown from a live pull."],
        "run_seconds": 12.5,
    }


_FIXTURE_READINESS = {
    "verdict": "not_ready_for_always_on",
    "generated_at": "2026-11-09T12:00:00+00:00",
    "checks": [{"name": "ollama", "status": "fail",
                "detail": "no Ollama reachable"}],
    "live_checks_done": {"live_ollama_turn": False,
                         "live_backup_verified": False,
                         "live_ui_session": False,
                         "live_restore_drill": False},
    "blockers": ["ollama (fail): no Ollama reachable",
                 "live check not done on the deployment machine: "
                 "live_ollama_turn"],
}


class _ServerCase(unittest.TestCase):
    """One localhost server per test class, fixture scan + readiness."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = temp_dir_with_db_cleanup()
        root = Path(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)
        cls._prod_fp_before = content_fingerprint(PROD_DB)

        cls.store_dir = str(root / "watchlist")
        wf.add_ticker("MSFT", "owner_core",
                      reason="Owner core watchlist.",
                      company_name="Microsoft Corporation",
                      store_dir=cls.store_dir)
        conn = sqlite3.connect(cls.db_path)
        try:
            wf.refresh_watchlist(conn, store_dir=cls.store_dir)
        finally:
            conn.close()

        cls.scan_path = root / "step54-live-scan.json"
        cls.scan_path.write_text(
            json.dumps(_fixture_scan()), encoding="utf-8")
        cls.scan_hash = _hash(cls.scan_path)

        cls.app = ui.JarvisUI(
            db_path=cls.db_path,
            store_dir=cls.store_dir,
            news_dir=str(root / "news"),
            events_dir=str(root / "events"),
            alert_state_path=str(root / "alerts" / "state.json"),
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(root / "data"),
            live_scan_path=str(cls.scan_path),
            readiness=_FIXTURE_READINESS,
        )
        cls.server = ui.make_server(cls.app, port=0)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = (
            f"http://127.0.0.1:{cls.server.server_address[1]}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cleanup_temp_dir(cls.tmp)
        assert content_fingerprint(PROD_DB) == cls._prod_fp_before, (
            "production DB changed during UI redesign tests")

    # -- HTTP helpers -------------------------------------------------

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), \
                resp.read().decode("utf-8")

    def post(self, path, question):
        data = urllib.parse.urlencode({"q": question}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data)
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")

    def get_error(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


class TestRedesignPages(_ServerCase):
    def test_all_pages_return_200(self):
        for path in ("/", "/ticker/MSFT", "/ticker/D05",
                     "/research", "/chat", "/status", "/dashboard"):
            status, _, _ = self.get(path)
            self.assertEqual(status, 200, path)

    def test_header_shell_on_every_page(self):
        for path in ("/", "/dashboard", "/chat", "/status"):
            _, _, body = self.get(path)
            self.assertIn("J.A.R.V.I.S.", body, path)
            self.assertIn("LOCAL ONLY", body, path)
            self.assertIn("/static/app.js", body, path)
            self.assertIn("Stored data only", body, path)

    def test_css_is_dark_theme(self):
        status, content_type, body = self.get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertIn("#050a12", body)
        self.assertIn("--cyan", body)
        self.assertIn(".card", body)
        self.assertIn(".badge-strongest_alert", body)

    def test_js_served_and_draws_charts(self):
        status, content_type, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        self.assertIn("drawDcfChart", body)
        self.assertIn("sortable", body)
        self.assertIn("cmd-chip", body)
        # No external resources: no http(s) URL in the script.
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)

    def test_home_is_cards(self):
        _, _, body = self.get("/")
        self.assertIn("Daily market summary", body)
        self.assertIn('class="card', body)
        self.assertIn("MSFT", body)
        self.assertIn("No active alerts.", body)
        self.assertIn("Limitations", body)

    def test_ticker_page_has_dcf_chart(self):
        _, _, body = self.get("/ticker/MSFT")
        self.assertIn("overvalued", body)
        self.assertIn("$163.30", body)
        self.assertIn("$78.82", body)
        self.assertIn("ticker-dcf-data", body)
        self.assertIn("<canvas", body)
        # The chart data carries the stored DCF range.
        self.assertIn("98.52", body)
        self.assertIn("163.29", body)

    def test_ticker_without_range_has_no_chart(self):
        _, _, body = self.get("/ticker/D05")
        self.assertIn("insufficient", body.lower())
        self.assertNotIn("ticker-dcf-data", body)
        self.assertNotIn("$507.12", body)

    def test_chat_has_command_chips_and_missing_notice(self):
        _, _, body = self.get("/chat")
        for cmd in ("/research", "/watchlist", "/alerts", "/summary",
                    "/memory", "/dashboard", "/ui", "/voice", "/help",
                    "/quit", "/livescan"):
            self.assertIn(cmd, body, cmd)
        self.assertIn("What is missing here", body)
        self.assertIn("no live Ollama turn", body)

    def test_chat_still_answers_and_refuses_trades(self):
        status, body = self.post(
            "/chat", "What is the intrinsic value of MSFT?")
        self.assertEqual(status, 200)
        self.assertIn("$163.30", body)
        self.assertIn("Sources", body)
        _, body = self.post("/chat", "Buy 100 shares of MSFT")
        self.assertIn("will not place a trade", body)

    def test_no_trade_controls_anywhere(self):
        pages = [self.get("/")[2], self.get("/ticker/MSFT")[2],
                 self.get("/research")[2], self.get("/chat")[2],
                 self.get("/status")[2], self.get("/dashboard")[2]]
        for body in pages:
            self.assertIsNone(_FORBIDDEN_CONTROL_RE.search(body),
                              "a trading control appears in served HTML")

    def test_localhost_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            ui.make_server(self.app, host="0.0.0.0")


class TestDashboardLiveScan(_ServerCase):
    def test_dashboard_renders_stored_live_scan(self):
        _, _, body = self.get("/dashboard")
        self.assertIn("Live watchlist scan", body)
        self.assertIn("BKE", body)
        self.assertIn("VC", body)
        self.assertIn("strongest_alert", body)
        self.assertIn("$41.96", body)
        self.assertIn("$54.83", body)
        self.assertIn("live-scan-dcf-data", body)
        self.assertIn("provisional", body.lower())
        self.assertIn("Not scannable", body)
        self.assertIn("ALC", body)
        self.assertIn("provider-unavailable", body)
        self.assertIn("BN", body)

    def test_dashboard_flags_known_scan_exclusions(self):
        _, _, body = self.get("/dashboard")
        self.assertIn("Known exclusions in this scan", body)
        self.assertIn("currency artifact", body)
        self.assertIn("Kazakhstani tenge", body)
        self.assertIn("does not apply to banks", body)

    def test_dashboard_watchlist_table_is_sortable(self):
        _, _, body = self.get("/dashboard")
        self.assertIn('class="sortable"', body)
        # Strongest-alert rows come first in the table.
        self.assertLess(body.index("BKE"), body.index("GOOGL"))

    def test_dashboard_shows_readiness_verdict(self):
        _, _, body = self.get("/dashboard")
        self.assertIn("Deployment readiness", body)
        self.assertIn("not_ready_for_always_on", body)
        self.assertIn("2 blocker(s)", body)
        self.assertIn("no Ollama reachable", body)
        self.assertIn("live_ollama_turn", body)

    def test_dashboard_still_shows_module_snapshot(self):
        _, _, body = self.get("/dashboard")
        self.assertIn("undervalued_detection", body)
        self.assertIn("orchestrator", body)
        self.assertIn("watchlist_alerts", body)
        self.assertIn("Recent activity", body)
        self.assertIn("Gaps", body)
        self.assertIn("No cloud model configured", body)

    def test_dashboard_does_not_modify_the_scan_file(self):
        self.get("/dashboard")
        self.assertEqual(_hash(self.scan_path), self.scan_hash)

    def test_dashboard_degrades_without_a_scan(self):
        app = ui.JarvisUI(
            db_path=self.db_path,
            store_dir=self.store_dir,
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(Path(self.tmp.name) / "data"),
            live_scan_path=str(Path(self.tmp.name) / "no-scan.json"),
            readiness=_FIXTURE_READINESS,
        )
        server = ui.make_server(app, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/dashboard") as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
            self.assertIn("No live scan has been stored yet", body)
            self.assertIn("/livescan", body)
            self.assertIn("not_ready_for_always_on", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_corrupt_scan_file_is_reported_not_a_crash(self):
        bad = Path(self.tmp.name) / "bad-scan.json"
        bad.write_text("{broken", encoding="utf-8")
        app = ui.JarvisUI(
            db_path=self.db_path,
            store_dir=self.store_dir,
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(Path(self.tmp.name) / "data"),
            live_scan_path=str(bad),
            readiness=_FIXTURE_READINESS,
        )
        server = ui.make_server(app, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/dashboard") as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
            self.assertIn("Could not read", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_readiness_computes_in_background(self):
        # Without a precomputed report, the first call starts a
        # background computation and returns "computing"; the
        # report appears once the thread finishes. The real
        # readiness check is stubbed here so the test stays fast.
        import agent.ui as ui_mod
        app = ui.JarvisUI(
            db_path=self.db_path,
            store_dir=self.store_dir,
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(Path(self.tmp.name) / "data"),
            live_scan_path=str(self.scan_path),
        )
        original = ui_mod.dep.readiness_report
        ui_mod.dep.readiness_report = lambda: _FIXTURE_READINESS
        try:
            report, error = ui._readiness(app)
            self.assertIsNone(report)
            self.assertEqual(error, "computing")
            for _ in range(100):
                if app._readiness_cache is not None:
                    break
                time.sleep(0.1)
            report, error = ui._readiness(app)
            self.assertIsNotNone(report)
            self.assertEqual(
                report["verdict"], "not_ready_for_always_on")
        finally:
            ui_mod.dep.readiness_report = original

    def test_dashboard_shows_placeholder_while_computing(self):
        app = ui.JarvisUI(
            db_path=self.db_path,
            store_dir=self.store_dir,
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(Path(self.tmp.name) / "data"),
            live_scan_path=str(self.scan_path),
        )
        app._readiness_computing = True
        body = ui.dashboard_html(app)
        self.assertIn("being computed in the background", body)
        self.assertIn("refresh this page", body)


class TestMainStartsUI(unittest.TestCase):
    """`python main.py --ui` serves the redesigned UI, then quits."""

    def test_main_ui_serves_home_then_quits(self):
        before = content_fingerprint(PROD_DB)
        with temp_dir_with_db_cleanup() as tmp:
            db_path = str(Path(tmp) / "test.db")
            shutil.copy(PROD_DB, db_path)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            proc = subprocess.Popen(
                [sys.executable, "main.py", "--ui",
                 "--db-path", db_path, "--port", str(port)],
                cwd=str(BASE), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            try:
                base = f"http://127.0.0.1:{port}"
                body = None
                # Startup does the banner + self-check (including a
                # 1s Ollama reachability probe); under CPU load it
                # can take a while, so wait up to 60s and fail fast
                # with the process output if it exits early.
                for _ in range(120):
                    if proc.poll() is not None:
                        out, err = proc.communicate()
                        self.fail(
                            "main.py exited early with code "
                            f"{proc.returncode}:\n{out}\n{err}")
                    try:
                        with urllib.request.urlopen(
                                base + "/", timeout=2) as resp:
                            self.assertEqual(resp.status, 200)
                            body = resp.read().decode("utf-8")
                        break
                    except (urllib.error.URLError, ConnectionError,
                            TimeoutError, OSError):
                        time.sleep(0.5)
                self.assertIsNotNone(
                    body, "the UI did not answer within 60s")
                self.assertIn("J.A.R.V.I.S.", body)
                self.assertIn("Daily market summary", body)
            finally:
                try:
                    proc.stdin.write("/quit\n")
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0)
            assert content_fingerprint(PROD_DB) == before, (
                "production DB changed during main.py UI test")
            gc.collect()


if __name__ == "__main__":
    unittest.main()
