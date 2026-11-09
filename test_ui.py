"""Tests for Step 48 — the local web UI (agent/ui.py).

The server runs in a thread on 127.0.0.1 with an ephemeral port
and is exercised with a plain HTTP client — no live browser, no
network beyond localhost, no production writes. The database is a
temp copy; the model client is a stub.
"""

import re
import shutil
import sqlite3
import sys
import tempfile
import threading
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


class TestUI(unittest.TestCase):
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
        wf.add_ticker("GOOGL", "owner_core",
                      reason="Owner core watchlist.",
                      company_name="Alphabet Inc.",
                      store_dir=cls.store_dir)
        conn = sqlite3.connect(cls.db_path)
        try:
            wf.refresh_watchlist(conn, store_dir=cls.store_dir)
        finally:
            conn.close()

        cls.news_dir = str(root / "news")
        cls.events_dir = str(root / "events")
        cls.alert_state_path = str(root / "alerts" / "state.json")
        cls.app = ui.JarvisUI(
            db_path=cls.db_path,
            store_dir=cls.store_dir,
            news_dir=cls.news_dir,
            events_dir=cls.events_dir,
            alert_state_path=cls.alert_state_path,
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
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
            "production DB changed during UI tests")

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

    # -- Pages ---------------------------------------------------------

    def test_home_renders(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Daily market summary", body)
        self.assertIn("MSFT", body)
        self.assertIn("No active alerts.", body)
        self.assertIn("Limitations", body)

    def test_ticker_msft_traces_to_step_21_23(self):
        status, _, body = self.get("/ticker/MSFT")
        self.assertEqual(status, 200)
        self.assertIn("overvalued", body)
        self.assertIn("$163.30", body)
        self.assertIn("$78.82", body)
        self.assertIn("excellent", body)

    def test_ticker_d05_insufficient_data_not_invented(self):
        status, _, body = self.get("/ticker/D05")
        self.assertEqual(status, 200)
        self.assertIn("insufficient", body.lower())
        # MSFT's price must not leak into the DBS page.
        self.assertNotIn("$507.12", body)

    def test_ticker_invalid(self):
        status, body = self.get_error("/ticker/!!!")
        self.assertEqual(status, 400)

    def test_research_page_runs_session(self):
        status, body = self.post("/research", "Research MSFT")
        self.assertEqual(status, 200)
        self.assertIn("Research brief", body)
        self.assertIn("MSFT", body)
        self.assertIn("Source:", body)

    def test_research_trade_refused(self):
        status, body = self.post("/research", "Buy 100 shares of MSFT")
        self.assertEqual(status, 200)
        self.assertIn("refused", body.lower())

    def test_chat_valuation_traces_to_step_21_23(self):
        status, body = self.post(
            "/chat", "What is the intrinsic value of MSFT?")
        self.assertEqual(status, 200)
        self.assertIn("$163.30", body)
        self.assertIn("Sources", body)

    def test_chat_trade_refused(self):
        status, body = self.post("/chat", "Buy 100 shares of MSFT")
        self.assertEqual(status, 200)
        self.assertIn("will not place a trade", body)

    def test_chat_stub_reply(self):
        status, body = self.post("/chat", "Hello, how are you?")
        self.assertEqual(status, 200)
        self.assertIn("Stub reply.", body)

    def test_status_page_lists_limitations(self):
        status, _, body = self.get("/status")
        self.assertEqual(status, 200)
        self.assertIn("no live price feed", body)
        self.assertIn("No holdings store", body)
        self.assertIn("No alert has ever fired", body)
        self.assertIn("Voice is off by default", body)
        self.assertIn("no trading controls", body)

    def test_no_trade_controls_anywhere(self):
        pages = [self.get("/")[2], self.get("/ticker/MSFT")[2],
                 self.get("/research")[2], self.get("/chat")[2],
                 self.get("/status")[2]]
        for body in pages:
            self.assertIsNone(_FORBIDDEN_CONTROL_RE.search(body),
                              "a trading control appears in served HTML")

    def test_unknown_route_404(self):
        status, _ = self.get_error("/nope")
        self.assertEqual(status, 404)

    def test_static_css(self):
        status, content_type, body = self.get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertIn("font-family", body)

    def test_localhost_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            ui.make_server(self.app, host="0.0.0.0")

    def test_markdown_escaping(self):
        html_out = ui._md_to_html("# Title\n<script>alert(1)</script>")
        self.assertIn("<h2>Title</h2>", html_out)
        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)


if __name__ == "__main__":
    unittest.main()
