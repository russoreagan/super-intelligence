"""
Voice-mode barge-in: the full-duplex interruption path.

History: the original barge-in fired pns.interrupt() on Deepgram's raw
SpeechStarted VAD event and was removed because the mic hears its own TTS
playback on open speakers (and VAD fires on coughs/keyboard). The keyword
fallback that replaced it couldn't fire either, because the default
half-duplex mode muted + paused the mic for the whole TTS window.

Voice mode restores interruption on ASR-confirmed transcripts instead of
raw VAD: streaming_mic surfaces interim/final transcripts heard while the
entity is speaking, and should_voice_interrupt() gates them (keywords
instantly; other speech needs ≥min_words real words and low word-overlap
with the TTS text so open-speaker echo can't self-cancel the playback).
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.voice_bridge import (
    DEFAULT_BARGE_IN_WORDS,
    barge_in_mode,
    should_voice_interrupt,
)

# ── barge_in_mode resolution ─────────────────────────────────────────────────


def test_mode_defaults_to_voice(monkeypatch):
    monkeypatch.delenv("BRAIN_BARGE_IN_MODE", raising=False)
    monkeypatch.delenv("BRAIN_MIC_MUTE_DURING_TTS", raising=False)
    assert barge_in_mode() == "voice"


def test_mode_env_wins(monkeypatch):
    monkeypatch.setenv("BRAIN_BARGE_IN_MODE", "keyword")
    monkeypatch.setenv("BRAIN_MIC_MUTE_DURING_TTS", "true")
    assert barge_in_mode() == "keyword"


def test_legacy_mute_true_maps_to_off(monkeypatch):
    monkeypatch.delenv("BRAIN_BARGE_IN_MODE", raising=False)
    monkeypatch.setenv("BRAIN_MIC_MUTE_DURING_TTS", "true")
    assert barge_in_mode() == "off"


def test_legacy_mute_false_maps_to_voice(monkeypatch):
    monkeypatch.delenv("BRAIN_BARGE_IN_MODE", raising=False)
    monkeypatch.setenv("BRAIN_MIC_MUTE_DURING_TTS", "false")
    assert barge_in_mode() == "voice"


def test_unknown_mode_value_falls_through(monkeypatch):
    monkeypatch.setenv("BRAIN_BARGE_IN_MODE", "bogus")
    monkeypatch.delenv("BRAIN_MIC_MUTE_DURING_TTS", raising=False)
    assert barge_in_mode() == "voice"


# ── should_voice_interrupt policy ────────────────────────────────────────────

SPEAKING = "the hebbian weight economy credits every edge that fired"


def test_keyword_interrupts_even_one_word():
    assert should_voice_interrupt("stop", SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS)


def test_real_speech_interrupts():
    assert should_voice_interrupt(
        "actually can you check the webhook logs", SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS
    )


def test_single_nonkeyword_word_is_ignored():
    assert not should_voice_interrupt("hmm", SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS)


def test_tts_echo_is_ignored():
    echo = "hebbian weight economy credits every edge"
    assert not should_voice_interrupt(echo, SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS)


def test_empty_text_is_ignored():
    assert not should_voice_interrupt("  ", SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS)


def test_min_words_env_override(monkeypatch):
    monkeypatch.setenv("BRAIN_BARGE_IN_MIN_WORDS", "4")
    assert not should_voice_interrupt(
        "check the logs", SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS
    )
    assert should_voice_interrupt(
        "check the webhook logs please", SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS
    )


# ── streaming_mic surfaces live speech during TTS ────────────────────────────


class _FakeSocket:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        self._it = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeBus:
    async def publish_dict(self, topic, payload, source=""):
        pass


def _results(transcript: str, is_final: bool = False) -> SimpleNamespace:
    alt = SimpleNamespace(transcript=transcript, words=[])
    return SimpleNamespace(
        type="Results", is_final=is_final, channel=SimpleNamespace(alternatives=[alt])
    )


def _mic(messages, *, speaking: bool):
    from brain.streaming_mic import StreamingMicSession

    heard: list[str] = []
    session = StreamingMicSession(
        bus=_FakeBus(),
        is_speaking_fn=lambda: speaking,
        on_live_speech=heard.append,
    )
    session._socket = _FakeSocket(messages)
    session._muted = False
    return session, heard


async def test_interim_transcript_surfaces_while_speaking():
    session, heard = _mic([_results("wait actually"), _results("wait actually stop")], speaking=True)
    await session._read_loop()
    assert heard == ["wait actually", "wait actually stop"]


async def test_no_live_speech_when_entity_silent():
    session, heard = _mic([_results("hello there")], speaking=False)
    await session._read_loop()
    assert heard == []


async def test_no_live_speech_while_muted():
    session, heard = _mic([_results("hello there")], speaking=True)
    session._muted = True
    await session._read_loop()
    assert heard == []


async def test_empty_interim_not_surfaced():
    session, heard = _mic([_results("   ")], speaking=True)
    await session._read_loop()
    assert heard == []
