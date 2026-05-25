"""
Tests for the voice bridge routing logic — the rules that decide what
happens to each utterance from streaming_mic depending on whether the
brain is currently speaking and whether the transcript is empty.
"""
from __future__ import annotations

import pytest

from brain.voice_bridge import (
    DEFAULT_BARGE_IN_WORDS,
    bleed_overlap,
    classify_utterance,
    is_barge_in,
    parse_barge_words,
    pick_dispatch_from_queue,
)

# ── parse_barge_words ────────────────────────────────────────────────────────

def test_parse_barge_words_default():
    out = parse_barge_words(None)
    assert "stop" in out
    assert "shut up" in out
    assert all(w == w.lower() for w in out)


def test_parse_barge_words_empty_string_uses_default():
    out = parse_barge_words("")
    assert out == DEFAULT_BARGE_IN_WORDS


def test_parse_barge_words_custom():
    out = parse_barge_words("STOP, Pause , shush ,, hey BRAIN")
    assert out == ["stop", "pause", "shush", "hey brain"]


# ── is_barge_in ──────────────────────────────────────────────────────────────

def test_is_barge_in_matches_keyword_in_substring():
    assert is_barge_in("Stop please", ["stop"]) is True
    assert is_barge_in("Please STOP now", ["stop"]) is True


def test_is_barge_in_matches_multiword_keyword():
    assert is_barge_in("hey brain are you there", ["hey brain"]) is True


def test_is_barge_in_no_match():
    assert is_barge_in("can you help me with this", ["stop", "wait"]) is False


def test_is_barge_in_empty_inputs():
    assert is_barge_in("", ["stop"]) is False
    assert is_barge_in("   ", ["stop"]) is False
    assert is_barge_in("stop", []) is False


# ── bleed_overlap ────────────────────────────────────────────────────────────

def test_bleed_overlap_zero_for_empty():
    assert bleed_overlap("", "anything") == 0.0
    assert bleed_overlap("anything", "") == 0.0
    assert bleed_overlap("", "") == 0.0


def test_bleed_overlap_high_for_echo():
    # The mic captures TTS bleed nearly verbatim
    tts = "Yeah that audio bleed will kill a conversation every time"
    bleed = "yeah that audio bleed will kill a conversation"
    o = bleed_overlap(bleed, tts)
    assert o > 0.5


def test_bleed_overlap_low_for_genuine_followup():
    # User asks a new question while TTS is still playing
    tts = "Yeah that audio bleed will kill a conversation every time"
    user = "this list of voices does not look correct"
    o = bleed_overlap(user, tts)
    assert o < 0.2


def test_bleed_overlap_ignores_one_letter_words():
    # 'a' and 'i' would otherwise inflate overlap on any English text
    tts = "i a i a i a"
    user = "i a i a"
    assert bleed_overlap(user, tts) == 0.0


def test_bleed_overlap_is_jaccard():
    # Half of the unique words match → 0.5 Jaccard
    a = "alpha beta gamma delta"
    b = "alpha beta epsilon zeta"
    # union: {alpha, beta, gamma, delta, epsilon, zeta} = 6
    # intersection: {alpha, beta} = 2
    assert bleed_overlap(a, b) == pytest.approx(2 / 6)


# ── classify_utterance ──────────────────────────────────────────────────────

WORDS = DEFAULT_BARGE_IN_WORDS


def test_classify_empty_dropped():
    d, _ = classify_utterance("", brain_is_speaking=False, barge_words=WORDS)
    assert d == "drop_empty"


def test_classify_whitespace_dropped():
    d, _ = classify_utterance("   ", brain_is_speaking=True, barge_words=WORDS)
    assert d == "drop_empty"


def test_classify_dispatch_when_brain_silent():
    d, _ = classify_utterance("hello there", brain_is_speaking=False, barge_words=WORDS)
    assert d == "dispatch"


def test_classify_dispatch_when_brain_silent_even_with_barge_word():
    d, _ = classify_utterance("stop the music", brain_is_speaking=False, barge_words=WORDS)
    assert d == "dispatch"


# ── bleed-protection window ──────────────────────────────────────────────────

_TTS = "Yeah that audio bleed will kill a conversation every time"


def test_classify_drop_bleed_in_protection_window():
    """High-overlap transcript arriving within 2.5s of TTS ending → drop_bleed."""
    bleed = "yeah that audio bleed will kill a conversation"
    d, info = classify_utterance(
        bleed, brain_is_speaking=False, barge_words=WORDS,
        last_spoken_text=_TTS, secs_since_speaking_ended=1.0,
    )
    assert d == "drop_bleed"
    assert info["overlap"] > 0.35


def test_classify_dispatch_genuine_speech_in_protection_window():
    """Low-overlap transcript in the bleed window → dispatch (real user speech)."""
    user = "this list of voices does not look correct"
    d, _ = classify_utterance(
        user, brain_is_speaking=False, barge_words=WORDS,
        last_spoken_text=_TTS, secs_since_speaking_ended=1.0,
    )
    assert d == "dispatch"


def test_classify_dispatch_after_protection_window_expires():
    """Even high-overlap transcript is dispatched once the window (2.5s) closes."""
    bleed = "yeah that audio bleed will kill a conversation"
    d, _ = classify_utterance(
        bleed, brain_is_speaking=False, barge_words=WORDS,
        last_spoken_text=_TTS, secs_since_speaking_ended=3.0,
    )
    assert d == "dispatch"


def test_classify_dispatch_no_last_spoken_text():
    """No last_spoken_text → no bleed filtering even within the window."""
    d, _ = classify_utterance(
        "yeah that audio bleed will kill a conversation",
        brain_is_speaking=False, barge_words=WORDS,
        last_spoken_text="", secs_since_speaking_ended=0.5,
    )
    assert d == "dispatch"


def test_classify_queue_bleed_during_tts_not_dropped_at_bridge():
    """Bleed arriving WHILE TTS plays is still 'queue' — filtering happens
    at drain time in run.py, not here at the bridge classify step."""
    bleed = "yeah that audio bleed will kill a conversation"
    d, _ = classify_utterance(
        bleed, brain_is_speaking=True, barge_words=WORDS,
        last_spoken_text=_TTS, secs_since_speaking_ended=0.0,
    )
    assert d == "queue"


def test_classify_barge_in_during_tts():
    d, _ = classify_utterance("stop please", brain_is_speaking=True, barge_words=WORDS)
    assert d == "barge_in"


def test_classify_genuine_speech_during_tts_is_queued():
    d, _ = classify_utterance(
        "this list of voices does not look correct",
        brain_is_speaking=True, barge_words=WORDS,
    )
    assert d == "queue"


def test_classify_any_non_empty_speech_during_tts_queued_not_dropped():
    d, _ = classify_utterance(
        "audio bleed will kill a conversation every single",
        brain_is_speaking=True, barge_words=WORDS,
    )
    assert d == "queue"


# ── pick_dispatch_from_queue ─────────────────────────────────────────────────

def test_pick_dispatch_empty_queue():
    text, n = pick_dispatch_from_queue([])
    assert text is None
    assert n == 0


def test_pick_dispatch_single():
    text, n = pick_dispatch_from_queue(["only one"])
    assert text == "only one"
    assert n == 1


def test_pick_dispatch_joins_all_as_one_block():
    text, n = pick_dispatch_from_queue([
        "i was thinking",
        "we should probably",
        "go ahead and try the calendar",
    ])
    assert text == "i was thinking we should probably go ahead and try the calendar"
    assert n == 3


def test_pick_dispatch_skips_empty_strings():
    text, n = pick_dispatch_from_queue(["hello", "  ", "world", ""])
    assert text == "hello world"
    assert n == 4
