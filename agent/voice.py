"""Step 45 — Voice interaction.

A voice layer over the Step 39 orchestrator: pluggable
speech-to-text and text-to-speech clients, and a voice turn
that feeds the orchestrator as a normal text request.

Voice is a capability, NOT a default. The owner prefers text
replies and declined ongoing voice replies on 2026-11-03, so:

- Voice is OFF by default. The default voice preference is
  "text".
- A spoken reply is produced ONLY when the caller explicitly
  asks for one for that turn AND the stored preference allows
  it.
- The owner's text preference is recorded as a Step 43
  long-term memory record (type ``preference``, tag ``voice``)
  by :func:`record_owner_text_preference`, and the voice turn
  reads it back with :func:`get_voice_preference` before
  speaking. With no stored preference, the default is text.

No microphone, speaker, or speech engine exists on this
machine, and no cloud speech API key exists. This module
builds the interfaces and the turn; the only clients shipped
are stubs (``StubSTT`` returns a caller-supplied transcript;
``StubTTS`` writes the reply text to a file and returns its
path). No audio is synthesised, no network is touched, no
live call is made to any speech provider. A real voice turn
happens only on the owner's PC with a real engine and his
explicit per-turn request.

The module never places a trade, sends a message, or
schedules anything (Step 46). It never writes to the
production database. It is NOT wired into ``main.py``
(Step 50).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from agent.orchestrator import ModelUnavailable, _log_turn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "data" / "orchestration"
DEFAULT_LOG_NAME = "turns.jsonl"
DEFAULT_TTS_DIR = PROJECT_ROOT / "data" / "voice"

# The owner's standing voice preference, stated 2026-11-03:
# text replies; ongoing voice replies declined.
OWNER_TEXT_PREFERENCE = (
    "The owner prefers text replies and declined ongoing voice "
    "replies on 2026-11-03. Voice replies only when he explicitly "
    "asks for one for that turn."
)
VOICE_MODE_TEXT = "text"
VOICE_MODE_VOICE = "voice"


class VoiceError(ModelUnavailable):
    """A voice step failed in a structured way.

    Carries a machine-readable ``reason`` (``"stt_unavailable"``,
    ``"tts_unavailable"``, ``"empty_transcript"``,
    ``"no_input"``). Like the Step 40 cloud errors, it is a
    :class:`ModelUnavailable` subclass so callers can treat any
    model/voice failure the same way: report it, never invent
    an answer.
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Client interfaces — pluggable, same call-and-raise shape as the
# Step 39/40 model clients.
# ---------------------------------------------------------------------------

class SpeechToText:
    """Speech-to-text client interface.

    ``transcribe(audio_path)`` returns the transcript text for
    the audio file at ``audio_path``. Any failure raises
    :class:`VoiceError` with a machine-readable reason — never
    a crash, never invented text.
    """

    def transcribe(self, audio_path):
        raise NotImplementedError(
            "SpeechToText.transcribe is not implemented by this client"
        )


class TextToSpeech:
    """Text-to-speech client interface.

    ``speak(text, output_path=None)`` produces spoken audio for
    ``text`` and returns the path of the audio file it wrote.
    Any failure raises :class:`VoiceError` with a
    machine-readable reason — never a crash, never silence
    reported as speech.
    """

    def speak(self, text, output_path=None):
        raise NotImplementedError(
            "TextToSpeech.speak is not implemented by this client"
        )


class StubSTT(SpeechToText):
    """A speech-to-text client for tests and offline use.

    Returns a caller-supplied transcript (or the transcript for
    a matching audio path), and can be told to raise
    :class:`VoiceError` so the failure path can be exercised
    without a microphone or a network.
    """

    def __init__(self, transcript="", transcripts=None,
                 raise_error=False):
        self.transcript = transcript
        self.transcripts = transcripts or {}
        self.raise_error = raise_error
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(str(audio_path))
        if self.raise_error:
            raise VoiceError(
                "stub speech-to-text unavailable",
                reason="stt_unavailable",
            )
        return self.transcripts.get(str(audio_path), self.transcript)


class StubTTS(TextToSpeech):
    """A text-to-speech client for tests and offline use.

    Writes the reply text to a file (no audio synthesis — no
    engine exists here) and returns the path, so a voice turn
    can be verified end to end without a speaker. Can be told
    to raise :class:`VoiceError` so the failure path can be
    exercised.
    """

    def __init__(self, output_dir=None, raise_error=False):
        self.output_dir = (
            Path(output_dir) if output_dir else DEFAULT_TTS_DIR
        )
        self.raise_error = raise_error
        self.calls = []

    def speak(self, text, output_path=None):
        self.calls.append(str(text))
        if self.raise_error:
            raise VoiceError(
                "stub text-to-speech unavailable",
                reason="tts_unavailable",
            )
        if output_path is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S%f")
            output_path = self.output_dir / f"reply-{stamp}.txt"
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(str(text), encoding="utf-8")
        return str(output_path)


# ---------------------------------------------------------------------------
# The owner's voice preference — Step 43 long-term memory.
# ---------------------------------------------------------------------------

def record_owner_text_preference(memory_dir=None):
    """Record the owner's text preference in the Step 43 store.

    Writes one preference record (type ``preference``, tags
    ``voice`` + ``preference``, ``voice_mode`` ``"text"``,
    stated by the owner on 2026-11-03). If an active record
    with the same text already exists, it is returned unchanged
    rather than duplicated. Returns the record dict.
    """
    from agent import long_term_memory as ltm

    existing = ltm.recall(
        memory_dir=memory_dir, record_type="preference",
        tag="voice", keyword="prefers text replies",
        include_superseded=False,
    )
    for record in existing:
        if record.get("voice_mode") == VOICE_MODE_TEXT:
            return record
    record = ltm.new_record(
        "preference",
        OWNER_TEXT_PREFERENCE,
        stated_by="owner",
        stated_at="2026-11-03",
        source="owner preference stated 2026-11-03",
        source_kind="owner",
        tags=["voice", "preference"],
    )
    record["voice_mode"] = VOICE_MODE_TEXT
    ltm.save_record(record, memory_dir=memory_dir)
    return record


def get_voice_preference(memory_dir=None):
    """The owner's voice preference: ``"text"`` or ``"voice"``.

    Reads the newest active Step 43 preference record tagged
    ``voice`` when a memory directory is supplied. A record
    with a ``voice_mode`` of ``"text"`` or ``"voice"`` decides;
    otherwise the record text is read ("prefer voice" →
    voice, "prefer text" → text). With no stored preference
    at all, the default is ``"text"`` — voice is OFF unless
    the owner has said otherwise.
    """
    if memory_dir is None:
        return VOICE_MODE_TEXT
    from agent import long_term_memory as ltm

    records = ltm.recall(
        memory_dir=memory_dir, record_type="preference",
        tag="voice", include_superseded=False,
    )
    for record in records:
        mode = record.get("voice_mode")
        if mode in (VOICE_MODE_TEXT, VOICE_MODE_VOICE):
            return mode
        text = (record.get("text") or "").lower()
        if "prefer voice" in text or "voice replies allowed" in text:
            return VOICE_MODE_VOICE
        if "prefer text" in text or "declined" in text:
            return VOICE_MODE_TEXT
    return VOICE_MODE_TEXT


# ---------------------------------------------------------------------------
# The voice turn
# ---------------------------------------------------------------------------

def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def voice_turn(transcript=None, audio_path=None, stt_client=None,
               tts_client=None, model_client=None, db_path=None,
               store_dir=None, news_dir=None, context=None,
               log_dir=None, speak_reply=False, memory_dir=None,
               as_of=None):
    """Handle one voice turn through the Step 39 orchestrator.

    The transcript (supplied directly, or produced by
    ``stt_client`` from ``audio_path``) is run through the
    orchestrator as a normal text request. A spoken reply is
    produced ONLY when ``speak_reply`` is True for this turn
    AND the stored voice preference allows it AND the turn
    status is ``"ok"``. Otherwise ``spoken_reply`` is None and
    ``spoken_reply_reason`` says why.

    Returns a dict in the Step 39 turn-log shape plus
    ``mode`` (``"voice"``), ``transcript_source``,
    ``spoken_reply`` (a file path, or None),
    ``spoken_reply_reason`` and ``voice_preference``. The turn
    is appended to the JSON-lines log unless ``log_dir`` is
    False. Nothing here places a trade, sends, or schedules;
    the orchestrator's guardrails still apply.
    """
    from agent import orchestrator as orch

    checked_at = as_of or _utcnow_iso()
    preference = get_voice_preference(memory_dir)
    spoken_reply = None
    spoken_reason = None
    transcript_source = None

    if transcript:
        text = str(transcript).strip()
        transcript_source = "supplied"
    elif audio_path is not None and stt_client is not None:
        try:
            text = str(stt_client.transcribe(audio_path)).strip()
        except VoiceError as exc:
            result = {
                "request": "",
                "intent": "unknown",
                "intent_method": "keyword",
                "route": "none",
                "status": "unavailable",
                "reply": (
                    "I could not transcribe that audio "
                    f"({exc}), so there is nothing to answer. "
                    "Nothing was invented instead."
                ),
                "modules_called": [],
                "sources": ["speech-to-text (unavailable)"],
                "checked_at": checked_at,
                "mode": "voice",
                "transcript_source": "audio",
                "spoken_reply": None,
                "spoken_reply_reason": (
                    "no spoken reply: no transcript was produced"
                ),
                "voice_preference": preference,
            }
            if log_dir is not False:
                result["log_file"] = _log_turn(result, log_dir=log_dir)
            return result
        transcript_source = "audio"
    else:
        result = {
            "request": "",
            "intent": "unknown",
            "intent_method": "keyword",
            "route": "none",
            "status": "unavailable",
            "reply": (
                "I did not receive a transcript or an audio file, "
                "so there is nothing to answer."
            ),
            "modules_called": [],
            "sources": ["speech-to-text (no input)"],
            "checked_at": checked_at,
            "mode": "voice",
            "transcript_source": None,
            "spoken_reply": None,
            "spoken_reply_reason": "no spoken reply: no input",
            "voice_preference": preference,
        }
        if log_dir is not False:
            result["log_file"] = _log_turn(result, log_dir=log_dir)
        return result

    if not text:
        result = {
            "request": "",
            "intent": "unknown",
            "intent_method": "keyword",
            "route": "none",
            "status": "unavailable",
            "reply": (
                "The transcript was empty, so there is nothing "
                "to answer."
            ),
            "modules_called": [],
            "sources": ["speech-to-text (empty transcript)"],
            "checked_at": checked_at,
            "mode": "voice",
            "transcript_source": transcript_source,
            "spoken_reply": None,
            "spoken_reply_reason": (
                "no spoken reply: the transcript was empty"
            ),
            "voice_preference": preference,
        }
        if log_dir is not False:
            result["log_file"] = _log_turn(result, log_dir=log_dir)
        return result

    # The orchestrator handles the request as text; its own
    # logging is suppressed (log_dir=False) so this turn is
    # logged once, below, with the voice fields included.
    turn = orch.handle(
        text, model_client=model_client, db_path=db_path,
        store_dir=store_dir, news_dir=news_dir, context=context,
        log_dir=False, as_of=checked_at,
    )

    if not speak_reply:
        spoken_reason = (
            "no spoken reply: a spoken reply was not requested "
            "for this turn (voice is off by default)"
        )
    elif preference != VOICE_MODE_VOICE:
        spoken_reason = (
            "no spoken reply: the owner prefers text replies "
            "(stored voice preference is 'text')"
        )
    elif turn.get("status") != "ok":
        spoken_reason = (
            "no spoken reply: the turn status was "
            f"'{turn.get('status')}', not 'ok'"
        )
    elif tts_client is None:
        spoken_reason = (
            "no spoken reply: no text-to-speech client is "
            "configured"
        )
    else:
        try:
            spoken_reply = tts_client.speak(turn.get("reply") or "")
        except VoiceError as exc:
            spoken_reason = f"no spoken reply: {exc}"
        except Exception as exc:  # noqa: BLE001 - record, never crash
            spoken_reason = (
                "no spoken reply: text-to-speech failed "
                f"({exc})"
            )

    result = dict(turn)
    result["mode"] = "voice"
    result["transcript_source"] = transcript_source
    result["spoken_reply"] = spoken_reply
    result["spoken_reply_reason"] = spoken_reason
    result["voice_preference"] = preference
    if log_dir is not False:
        result["log_file"] = _log_turn(result, log_dir=log_dir)
    return result


class VoiceSession:
    """A reusable voice session holding the default clients/paths.

    ``turn`` delegates to the module-level :func:`voice_turn`
    with these defaults; any argument can be overridden per
    call. Voice is off by default — a spoken reply still
    needs an explicit per-turn request.
    """

    def __init__(self, stt_client=None, tts_client=None,
                 model_client=None, db_path=None, store_dir=None,
                 news_dir=None, memory_dir=None, log_dir=None):
        self.stt_client = stt_client
        self.tts_client = tts_client
        self.model_client = model_client
        self.db_path = db_path
        self.store_dir = store_dir
        self.news_dir = news_dir
        self.memory_dir = memory_dir
        self.log_dir = log_dir

    def turn(self, transcript=None, audio_path=None, **overrides):
        kwargs = {
            "stt_client": self.stt_client,
            "tts_client": self.tts_client,
            "model_client": self.model_client,
            "db_path": self.db_path,
            "store_dir": self.store_dir,
            "news_dir": self.news_dir,
            "memory_dir": self.memory_dir,
            "log_dir": self.log_dir,
        }
        kwargs.update(overrides)
        return voice_turn(
            transcript=transcript, audio_path=audio_path, **kwargs
        )

    def preference(self):
        return get_voice_preference(self.memory_dir)
