"""Tests for Step 45 — voice interaction (agent/voice.py).

Stubbed speech clients only; no network, no audio
libraries, no production writes. The production database is
opened only through read-only copies in TemporaryDirectory.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent import long_term_memory as ltm
from agent import voice as voice_mod
from agent.orchestrator import ModelUnavailable, StubModelClient
from agent.voice import (
    SpeechToText,
    StubSTT,
    StubTTS,
    TextToSpeech,
    VoiceError,
    VoiceSession,
    get_voice_preference,
    record_owner_text_preference,
    voice_turn,
)

PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_DB = PROJECT_ROOT / "database" / "assistant.db"
PRODUCTION_HASH = (
    "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187"
)


def _db_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _temp_db_copy(directory):
    """A throwaway copy of the production DB in ``directory``."""
    target = Path(directory) / "assistant.db"
    shutil.copy(PRODUCTION_DB, target)
    return str(target)


class VoiceStep45TestCase(unittest.TestCase):
    """Shared temp-dir + temp-DB setup for the Step 45 tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db_path = _temp_db_copy(self.dir)
        self.log_dir = self.dir / "orchestration"
        self.memory_dir = self.dir / "memory"
        self.tts_dir = self.dir / "voice"
        self.model = StubModelClient(reply="Stub reply.")

    def _turn(self, transcript, **kwargs):
        kwargs.setdefault("model_client", self.model)
        kwargs.setdefault("db_path", self.db_path)
        kwargs.setdefault("log_dir", self.log_dir)
        kwargs.setdefault("memory_dir", self.memory_dir)
        return voice_turn(transcript, **kwargs)

    @classmethod
    def tearDownClass(cls):
        assert _db_hash(PRODUCTION_DB) == PRODUCTION_HASH, (
            "Step 45 tests changed the production database"
        )

    # -- interfaces and stubs -------------------------------------

    def test_base_clients_raise_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            SpeechToText().transcribe("x.wav")
        with self.assertRaises(NotImplementedError):
            TextToSpeech().speak("hello")

    def test_voice_error_is_model_unavailable_with_reason(self):
        err = VoiceError("boom", reason="stt_unavailable")
        self.assertIsInstance(err, ModelUnavailable)
        self.assertEqual(err.reason, "stt_unavailable")

    def test_stub_stt_returns_supplied_transcript(self):
        stt = StubSTT(transcript="what is the intrinsic value of MSFT?")
        self.assertEqual(
            stt.transcribe("audio.wav"),
            "what is the intrinsic value of MSFT?",
        )
        self.assertEqual(stt.calls, ["audio.wav"])

    def test_stub_stt_per_path_transcripts(self):
        stt = StubSTT(transcripts={"a.wav": "hello", "b.wav": "hi"})
        self.assertEqual(stt.transcribe("a.wav"), "hello")
        self.assertEqual(stt.transcribe("b.wav"), "hi")

    def test_stub_stt_can_raise(self):
        stt = StubSTT(raise_error=True)
        with self.assertRaises(VoiceError) as ctx:
            stt.transcribe("a.wav")
        self.assertEqual(ctx.exception.reason, "stt_unavailable")

    def test_stub_tts_writes_reply_text_and_returns_path(self):
        tts = StubTTS(output_dir=self.tts_dir)
        path = tts.speak("Hello, this is a test reply.")
        self.assertTrue(Path(path).exists())
        self.assertEqual(
            Path(path).read_text(encoding="utf-8"),
            "Hello, this is a test reply.",
        )
        self.assertEqual(tts.calls, ["Hello, this is a test reply."])

    def test_stub_tts_can_raise(self):
        tts = StubTTS(raise_error=True)
        with self.assertRaises(VoiceError) as ctx:
            tts.speak("hello")
        self.assertEqual(ctx.exception.reason, "tts_unavailable")

    # -- the owner's voice preference ------------------------------

    def test_default_preference_is_text(self):
        # No memory store at all: voice is OFF by default.
        self.assertEqual(get_voice_preference(None), "text")
        self.assertEqual(
            get_voice_preference(self.memory_dir), "text")

    def test_record_owner_text_preference(self):
        record = record_owner_text_preference(
            memory_dir=self.memory_dir)
        self.assertEqual(record["type"], "preference")
        self.assertEqual(record["voice_mode"], "text")
        self.assertIn("voice", record["tags"])
        self.assertEqual(record["stated_by"], "owner")
        # The stored preference is read back by the gate.
        self.assertEqual(
            get_voice_preference(self.memory_dir), "text")

    def test_record_owner_text_preference_is_idempotent(self):
        first = record_owner_text_preference(
            memory_dir=self.memory_dir)
        second = record_owner_text_preference(
            memory_dir=self.memory_dir)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            len(ltm.list_records(memory_dir=self.memory_dir)), 1)

    def test_voice_preference_record_allows_voice(self):
        record = ltm.new_record(
            "preference",
            "The owner prefers voice replies for this session.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        self.assertEqual(
            get_voice_preference(self.memory_dir), "voice")

    def test_voice_preference_falls_back_to_text_in_record(self):
        # A voice-tagged preference without a voice_mode key is
        # decided by its wording.
        record = ltm.new_record(
            "preference",
            "The owner prefers text replies.",
            stated_by="owner", tags=["voice"],
        )
        ltm.save_record(record, memory_dir=self.memory_dir)
        self.assertEqual(
            get_voice_preference(self.memory_dir), "text")

    def test_superseded_preference_is_not_used(self):
        old = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        old["voice_mode"] = "voice"
        ltm.save_record(old, memory_dir=self.memory_dir)
        new = ltm.new_record(
            "preference",
            "The owner prefers text replies.",
            stated_by="owner", tags=["voice"],
        )
        new["voice_mode"] = "text"
        ltm.supersede_record(old["id"], new,
                             memory_dir=self.memory_dir)
        self.assertEqual(
            get_voice_preference(self.memory_dir), "text")

    # -- the voice turn --------------------------------------------

    def test_voice_turn_valuation_traces_to_step_21_23(self):
        result = self._turn("what is the intrinsic value of MSFT?")
        self.assertEqual(result["mode"], "voice")
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["transcript_source"], "supplied")
        self.assertIn("overvalued", result["reply"])
        self.assertIn("$98.52", result["reply"])
        self.assertIn("$163.30", result["reply"])
        self.assertIn("$234.64", result["reply"])
        self.assertIn("$507.12", result["reply"])
        self.assertIn("$78.82", result["reply"])
        self.assertIn(
            "agents/margin_of_safety.py", result["modules_called"])
        # No spoken reply: not requested, and the default
        # preference is text.
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("not requested", result["spoken_reply_reason"])

    def test_voice_turn_via_audio_path_and_stub_stt(self):
        stt = StubSTT(transcript="what is the intrinsic value of MSFT?")
        result = self._turn(None, audio_path="clip.wav",
                            stt_client=stt)
        self.assertEqual(result["transcript_source"], "audio")
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(result["status"], "ok")
        self.assertIn("$163.30", result["reply"])

    def test_voice_turn_supplied_transcript_wins_over_audio(self):
        stt = StubSTT(transcript="hello")
        result = self._turn("what is the intrinsic value of MSFT?",
                            audio_path="clip.wav", stt_client=stt)
        self.assertEqual(result["transcript_source"], "supplied")
        self.assertEqual(result["intent"], "valuation")
        self.assertEqual(stt.calls, [])

    def test_spoken_reply_only_on_explicit_request(self):
        # Preference allows voice, but no per-turn request:
        # nothing is spoken.
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        result = self._turn(
            "what is the intrinsic value of MSFT?",
            tts_client=StubTTS(output_dir=self.tts_dir),
        )
        self.assertEqual(result["voice_preference"], "voice")
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("not requested", result["spoken_reply_reason"])

    def test_spoken_reply_when_requested_and_allowed(self):
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        tts = StubTTS(output_dir=self.tts_dir)
        result = self._turn(
            "what is the intrinsic value of MSFT?",
            tts_client=tts, speak_reply=True,
        )
        self.assertEqual(result["voice_preference"], "voice")
        self.assertIsNotNone(result["spoken_reply"])
        self.assertIsNone(result["spoken_reply_reason"])
        path = Path(result["spoken_reply"])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"),
                         result["reply"])
        self.assertEqual(tts.calls, [result["reply"]])

    def test_spoken_reply_refused_when_preference_is_text(self):
        record_owner_text_preference(memory_dir=self.memory_dir)
        tts = StubTTS(output_dir=self.tts_dir)
        result = self._turn(
            "what is the intrinsic value of MSFT?",
            tts_client=tts, speak_reply=True,
        )
        self.assertEqual(result["voice_preference"], "text")
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("prefers text", result["spoken_reply_reason"])
        self.assertEqual(tts.calls, [])

    def test_spoken_reply_refused_with_no_stored_preference(self):
        # Default is text even when the caller asks: the
        # preference gate, not just the request, decides.
        tts = StubTTS(output_dir=self.tts_dir)
        result = self._turn(
            "what is the intrinsic value of MSFT?",
            tts_client=tts, speak_reply=True,
        )
        self.assertEqual(result["voice_preference"], "text")
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("prefers text", result["spoken_reply_reason"])
        self.assertEqual(tts.calls, [])

    def test_spoken_reply_refused_without_tts_client(self):
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        result = self._turn(
            "what is the intrinsic value of MSFT?",
            speak_reply=True,
        )
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("no text-to-speech client",
                      result["spoken_reply_reason"])

    def test_tts_failure_does_not_break_the_turn(self):
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        tts = StubTTS(raise_error=True)
        result = self._turn(
            "what is the intrinsic value of MSFT?",
            tts_client=tts, speak_reply=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("$163.30", result["reply"])
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("unavailable", result["spoken_reply_reason"])

    def test_model_unavailable_turn_is_not_spoken(self):
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        down = StubModelClient(raise_unavailable=True)
        tts = StubTTS(output_dir=self.tts_dir)
        result = self._turn(
            "hello, how are you?",
            model_client=down, tts_client=tts, speak_reply=True,
        )
        self.assertEqual(result["status"], "model_unavailable")
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("model_unavailable",
                      result["spoken_reply_reason"])
        self.assertEqual(tts.calls, [])

    def test_trade_request_refused_and_not_spoken(self):
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        tts = StubTTS(output_dir=self.tts_dir)
        result = self._turn(
            "buy 100 shares of MSFT at $500",
            tts_client=tts, speak_reply=True,
        )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["route"], "guardrail_trade")
        self.assertIsNone(result["spoken_reply"])
        self.assertEqual(tts.calls, [])

    def test_stt_failure_is_unavailable_not_a_crash(self):
        stt = StubSTT(raise_error=True)
        result = self._turn(None, audio_path="clip.wav",
                            stt_client=stt)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["mode"], "voice")
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("no transcript", result["spoken_reply_reason"])

    def test_no_input_is_unavailable(self):
        result = self._turn(None)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["spoken_reply"])
        self.assertIn("no input", result["spoken_reply_reason"])

    def test_empty_transcript_is_unavailable(self):
        result = self._turn("   ")
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("empty", result["spoken_reply_reason"])

    def test_audio_without_stt_client_is_unavailable(self):
        result = self._turn(None, audio_path="clip.wav")
        self.assertEqual(result["status"], "unavailable")

    def test_turn_record_fields_and_log(self):
        result = self._turn("what is the intrinsic value of MSFT?")
        self.assertEqual(result["mode"], "voice")
        self.assertEqual(result["transcript_source"], "supplied")
        self.assertEqual(result["voice_preference"], "text")
        self.assertIn("spoken_reply", result)
        self.assertIn("spoken_reply_reason", result)
        self.assertIn("checked_at", result)
        log_file = Path(result["log_file"])
        self.assertTrue(log_file.exists())
        lines = log_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        logged = json.loads(lines[0])
        self.assertEqual(logged["mode"], "voice")
        self.assertEqual(logged["intent"], "valuation")
        self.assertIsNone(logged["spoken_reply"])

    def test_log_dir_false_skips_logging(self):
        result = voice_turn(
            "what is the intrinsic value of MSFT?",
            model_client=self.model, db_path=self.db_path,
            log_dir=False, memory_dir=self.memory_dir,
        )
        self.assertNotIn("log_file", result)
        self.assertFalse((self.log_dir / "turns.jsonl").exists())

    def test_voice_session_class(self):
        record = ltm.new_record(
            "preference", "The owner prefers voice replies.",
            stated_by="owner", tags=["voice"],
        )
        record["voice_mode"] = "voice"
        ltm.save_record(record, memory_dir=self.memory_dir)
        session = VoiceSession(
            stt_client=StubSTT(
                transcript="what is the intrinsic value of MSFT?"),
            tts_client=StubTTS(output_dir=self.tts_dir),
            model_client=self.model, db_path=self.db_path,
            memory_dir=self.memory_dir, log_dir=self.log_dir,
        )
        self.assertEqual(session.preference(), "voice")
        result = session.turn(audio_path="clip.wav",
                              speak_reply=True)
        self.assertEqual(result["mode"], "voice")
        self.assertEqual(result["transcript_source"], "audio")
        self.assertIsNotNone(result["spoken_reply"])
        # A per-call override can still refuse the spoken reply.
        quiet = session.turn(
            "what is the intrinsic value of MSFT?",
            speak_reply=False,
        )
        self.assertIsNone(quiet["spoken_reply"])

    def test_d05_insufficient_data_through_voice(self):
        result = self._turn("what is the intrinsic value of D05?")
        self.assertEqual(result["status"], "ok")
        self.assertIn("do not have enough data", result["reply"])


if __name__ == "__main__":
    unittest.main()
