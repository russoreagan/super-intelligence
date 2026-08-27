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


# ── the echo guard must not weaken as the reply gets longer ──────────────────
#
# Regression for the containment fix. The guard used to be Jaccard
# (|A∩B|/|A∪B|), which is dominated by the LONGER set: a six-word echo of an
# eighty-word reply scored 0.10 against a 0.5 threshold, so pure speaker bleed
# sailed through and the entity cut itself off partway through every long
# reply — exactly the replies worth interrupting. Containment (|A∩B|/|A|) asks
# the question that actually matters: was everything I just heard already
# coming out of the speaker?

LONG_SPEAKING = (
    "Sure, I can walk you through that. The scheduler polls every five minutes, "
    "which is why the pod keeps coming back up after you press sleep. There is no "
    "latch today, so the cron job respawns the brain by design. If you want a real "
    "sleep, we would need to add a latch that the scheduler checks before it spawns "
    "anything, and that is a decision I did not want to make for you."
)


def test_echo_of_a_long_reply_is_ignored():
    echo = "the scheduler polls every five minutes"
    assert not should_voice_interrupt(echo, LONG_SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS)


def test_genuine_interruption_during_a_long_reply_still_cuts():
    assert should_voice_interrupt(
        "no forget deployment", LONG_SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS
    )


def test_keyword_interrupts_during_a_long_reply():
    """Keywords bypass the guard entirely — 'stop' appears in no reply text but
    must cut regardless of what the containment score says."""
    assert should_voice_interrupt("stop", LONG_SPEAKING, barge_words=DEFAULT_BARGE_IN_WORDS)


def test_containment_does_not_decay_with_reply_length():
    from brain.voice_bridge import echo_containment

    echo = "the scheduler polls every five minutes"
    short = "The scheduler polls every five minutes."
    assert echo_containment(echo, short) == 1.0
    assert echo_containment(echo, LONG_SPEAKING) == 1.0


def test_bleed_overlap_stays_symmetric_jaccard():
    """bleed_overlap is NOT the echo guard any more, but session_turn.py and
    clusters/temporal.py use it as a genuine symmetric similarity between texts
    of comparable length. Changing it would silently reweight the DMN
    prediction-hit and prefetched-thought paths."""
    from brain.voice_bridge import bleed_overlap

    assert bleed_overlap("alpha beta", "beta alpha") == 1.0
    assert bleed_overlap("alpha beta", "gamma delta") == 0.0
    # Asymmetry is the whole point of the split: Jaccard collapses here where
    # containment stays at 1.0.
    assert bleed_overlap("the scheduler polls every five minutes", LONG_SPEAKING) < 0.2


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
    session, heard = _mic(
        [_results("wait actually"), _results("wait actually stop")], speaking=True
    )
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
