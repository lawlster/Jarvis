"""Tests for Step 56 — browser voice on the chat page.

Web Speech API voice (SpeechRecognition microphone input,
speechSynthesis spoken replies) is a client-side feature of the
redesigned chat page: a microphone button, a voice on/off switch
(OFF by default), a hands-free toggle, and a reply-voice picker.
The tests run over real localhost HTTP against a server in a
thread and assert the served markup and JavaScript — no
microphone or speaker exists on this machine, so the actual
talk-and-reply loop cannot be exercised here and is labelled
unverified until the owner tries it on his PC.

The database is a temp copy; the model client is a stub; no
production writes, no audio is ever sent anywhere (the served
JavaScript contains no network call).
"""

import hashlib
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from agent import orchestrator as orch
from agent import ui
from agents import watchlist_foundation as wf

BASE = Path(__file__).resolve().parent
PROD_DB = BASE / "database" / "assistant.db"
PROD_HASH = "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"

# A control whose name, action, or label suggests trading must
# never appear in the served HTML.
_FORBIDDEN_CONTROL_RE = re.compile(
    r"<(?:button|input|select|form)[^>]*(?:buy|sell|trade|order|"
    r"place_order|checkout)",
    re.IGNORECASE,
)


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _domcontentloaded_blocks(js):
    """The bodies of every DOMContentLoaded callback in ``js``.

    Found by brace counting from each listener registration, so
    the check below is structural, not a substring guess.
    """
    blocks = []
    needle = 'addEventListener("DOMContentLoaded"'
    start = 0
    while True:
        at = js.find(needle, start)
        if at == -1:
            return blocks
        brace = js.find("{", at)
        depth = 0
        i = brace
        while i < len(js):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(js[brace:i + 1])
                    break
            i += 1
        start = i + 1


def _function_body(js, name):
    """The body of ``function <name>(...) { ... }`` by brace count."""
    at = js.find("function " + name + "(")
    assert at != -1, f"no function {name} in served JavaScript"
    brace = js.find("{", at)
    depth = 0
    i = brace
    while i < len(js):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[brace:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}")


class _ServerCase(unittest.TestCase):
    """One localhost server per test class, stub model, temp DB."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.db_path = str(root / "test.db")
        shutil.copy(PROD_DB, cls.db_path)

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

        cls.app = ui.JarvisUI(
            db_path=cls.db_path,
            store_dir=cls.store_dir,
            news_dir=str(root / "news"),
            events_dir=str(root / "events"),
            alert_state_path=str(root / "alerts" / "state.json"),
            model_client=orch.StubModelClient(reply="Stub reply."),
            log_dir=False,
            data_dir=str(root / "data"),
            live_scan_path=str(root / "no-scan.json"),
            readiness={"verdict": "not_ready_for_always_on",
                       "generated_at": "2026-11-09T12:00:00+00:00",
                       "checks": [], "live_checks_done": {},
                       "blockers": []},
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
        cls.tmp.cleanup()
        assert _hash(PROD_DB) == PROD_HASH, (
            "production DB hash changed during browser voice tests")

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

    def chat_js(self):
        _, _, body = self.get("/static/app.js")
        return body


class TestVoiceControls(_ServerCase):
    def test_chat_page_has_voice_panel(self):
        _, _, body = self.get("/chat")
        self.assertIn('id="voice-panel"', body)
        self.assertIn('id="voice-mic"', body)
        self.assertIn('id="voice-toggle"', body)
        self.assertIn('id="voice-handsfree"', body)
        self.assertIn('id="voice-picker"', body)
        self.assertIn('id="voice-status"', body)
        self.assertIn("Voice off.", body)
        # The panel explains the honest limits in plain words.
        self.assertIn("no wake word", body.lower())
        self.assertIn("nothing is recorded or sent anywhere", body)
        self.assertIn("try Chrome or Edge", body)

    def test_voice_defaults_off(self):
        _, _, body = self.get("/chat")
        # The toggle is unchecked and the microphone is disabled
        # until the owner switches voice on.
        self.assertIn('<input type="checkbox" id="voice-toggle">',
                      body)
        self.assertIn(
            '<button type="button" id="voice-mic" class="voice-mic" '
            'disabled', body)
        self.assertIn('<select id="voice-picker" disabled>', body)
        # The served JavaScript also defaults to off: voice is on
        # only when localStorage explicitly says so.
        js = self.chat_js()
        self.assertIn('storeGet(LS_ON) === "on"', js)

    def test_chat_reply_is_speakable(self):
        _, body = self.post(
            "/chat", "What is the intrinsic value of MSFT?")
        self.assertIn('id="jarvis-reply-text"', body)
        self.assertIn("$163.30", body)

    def test_chat_still_answers_and_refuses_trades(self):
        _, body = self.post(
            "/chat", "What is the intrinsic value of MSFT?")
        self.assertIn("Sources", body)
        _, body = self.post("/chat", "Buy 100 shares of MSFT")
        self.assertIn("will not place a trade", body)

    def test_no_trade_controls_on_chat_page(self):
        _, _, body = self.get("/chat")
        self.assertIsNone(_FORBIDDEN_CONTROL_RE.search(body),
                          "a trading control appears in served HTML")

    def test_status_page_mentions_browser_voice(self):
        _, _, body = self.get("/status")
        self.assertIn("Browser voice", body)
        self.assertIn("No wake word", body)

    def test_localhost_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            ui.make_server(self.app, host="0.0.0.0")


class TestVoiceJavaScript(_ServerCase):
    def test_voice_apis_and_language_fallback(self):
        js = self.chat_js()
        self.assertIn("SpeechRecognition", js)
        self.assertIn("webkitSpeechRecognition", js)
        self.assertIn("speechSynthesis", js)
        # en-SG first, en-US only if the browser rejects en-SG.
        self.assertIn('rec.lang = "en-SG"', js)
        self.assertIn('rec.lang = "en-US"', js)
        self.assertLess(js.index('rec.lang = "en-SG"'),
                        js.index('rec.lang = "en-US"'))
        self.assertIn("language-not-supported", js)
        # Interim results are shown live in the input box.
        self.assertIn("interimResults", js)
        self.assertIn("isFinal", js)

    def test_reply_voice_preference_order(self):
        js = self.chat_js()
        body = _function_body(js, "pickVoice")
        # British male first, then the documented fallbacks.
        self.assertIn("Google UK English Male", body)
        self.assertIn("Daniel", body)
        self.assertIn("en-GB", body)
        self.assertLess(body.index("Google UK English Male"),
                        body.index("Daniel"))
        self.assertLess(body.index("Daniel"),
                        body.index('"en-GB"'))
        # Spoken replies use the movie-like settings.
        self.assertIn("utterance.rate = 0.95", js)
        self.assertIn("utterance.pitch = 1.0", js)
        # The picker is populated from the browser's voices and
        # the choice is remembered across page loads.
        self.assertIn("getVoices()", js)
        self.assertIn("onvoiceschanged", js)
        self.assertIn('storeGet(LS_VOICE_NAME)', js)

    def test_reply_text_is_cleaned_before_speaking(self):
        js = self.chat_js()
        body = _function_body(js, "stripForSpeech")
        self.assertIn("https?", body)
        self.assertIn("trim()", body)
        # The reply is read from the speakable paragraph only.
        self.assertIn('getElementById("jarvis-reply-text")', js)

    def test_voice_never_auto_starts_listening(self):
        js = self.chat_js()
        # recognition.start() appears exactly once, inside
        # startListening — never at the top level and never in a
        # page-load handler.
        self.assertEqual(js.count("recognition.start()"), 1)
        body = _function_body(js, "startListening")
        self.assertIn("recognition.start()", body)
        for block in _domcontentloaded_blocks(js):
            self.assertNotIn("recognition.start()", block)
            self.assertNotIn("startListening()", block)
        # startListening is only called from the microphone click
        # handler and from the spoken-reply / recognition end /
        # language-fallback handlers while hands-free is on — the
        # five occurrences are its definition plus those four
        # call sites, and none of them is a page-load handler.
        self.assertEqual(js.count("startListening()"), 5)

    def test_hands_free_resumes_after_speaking(self):
        js = self.chat_js()
        utterance_end = _function_body(js, "speakReply")
        self.assertIn("onend", utterance_end)
        self.assertIn("handsFree", utterance_end)
        self.assertIn("startListening()", utterance_end)

    def test_unsupported_browsers_degrade_in_words(self):
        js = self.chat_js()
        self.assertIn(
            "Voice input is not supported in this browser — try "
            "Chrome or Edge.", js)
        self.assertIn("Microphone access was denied", js)
        self.assertIn("Spoken replies are not supported", js)

    def test_voice_js_makes_no_network_calls(self):
        js = self.chat_js()
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)
        self.assertNotIn("fetch(", js)
        self.assertNotIn("XMLHttpRequest", js)

    def test_css_has_voice_styles(self):
        _, _, css = self.get("/static/style.css")
        self.assertIn(".voice-panel", css)
        self.assertIn(".voice-mic", css)
        self.assertIn("voice-pulse", css)
        self.assertIn(".voice-toggle", css)
        self.assertIn("#voice-picker", css)


if __name__ == "__main__":
    unittest.main()
