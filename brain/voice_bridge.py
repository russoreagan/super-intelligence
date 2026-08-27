"""
Voice bridge — pure functions for routing utterances from streaming_mic.
Extracted from run.py so the routing decisions can be unit-tested without
spinning up the whole brain. The async glue in run.py imports these and
wires them to pns + ui_message_queue.
"""

from __future__ import annotations

import os
import re

BARGE_IN_MODES = ("voice", "keyword", "off")

DEFAULT_BARGE_IN_WORDS = [
    "stop",
    "wait",
    "shut up",
    "hold on",
    "pause",
    "enough",
    "never mind",
    "hey brain",
    "brain stop",
    "cut it out",
    "knock it off",
    "quiet",
    "be quiet",
    "hush",
    "shush",
    "okay enough",
    "that's enough",
    "thats enough",
]


def barge_in_mode() -> str:
    """Resolve how voice interruption works while the entity is speaking:

      voice   — mic stays live during TTS; any transcribed speech interrupts
                (keywords instantly; other speech gated by the bleed guard)
      keyword — mic stays live during TTS; only explicit barge keywords
                interrupt, checked on completed utterances (slower)
      off     — half-duplex: mic muted + capture paused during TTS, no voice
                interruption at all (the pre-voice-mode default; also the
                escape hatch for shared full-duplex devices that hit the
                CoreAudio -10863 input/output collision)

    BRAIN_BARGE_IN_MODE wins when set. Otherwise the legacy
    BRAIN_MIC_MUTE_DURING_TTS maps: explicitly 'false' → voice (those users
    already ran a live mic during TTS), anything else explicit → off.
    Unset → voice.
    """
    raw = (os.environ.get("BRAIN_BARGE_IN_MODE") or "").strip().lower()
    if raw in BARGE_IN_MODES:
        return raw
    legacy = os.environ.get("BRAIN_MIC_MUTE_DURING_TTS")
    if legacy is not None:
        return "voice" if legacy.strip().lower() == "false" else "off"
    return "voice"


def echo_containment_max() -> float:
    """Containment threshold at/above which speech heard during TTS is treated
    as the mic hearing the entity's own playback (open speakers, no headphones).

    Reads BRAIN_BLEED_OVERLAP_MAX (name kept for compatibility). The default is
    0.7 rather than the 0.5 the old Jaccard measure used: containment is a much
    sharper signal, so a lower bar would start swallowing genuine short
    interruptions that happen to reuse the entity's vocabulary."""
    return float(os.environ.get("BRAIN_BLEED_OVERLAP_MAX", "0.7"))


def parse_barge_words(raw: str | None) -> list[str]:
    """Parse a comma-separated env var into a normalised list of keywords."""
    if not raw:
        return list(DEFAULT_BARGE_IN_WORDS)
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


def is_barge_in(text: str, words: list[str]) -> bool:
    """True if `text` contains any of the configured barge-in keywords
    (case-insensitive substring match)."""
    t = (text or "").lower().strip()
    if not t:
        return False
    return any(w in t for w in words)


def _tokenize(s: str) -> set[str]:
    """Word set for the overlap measures. Single-character "words" are filtered
    out so that articles ('a', 'i') don't inflate the score."""
    return {w for w in re.findall(r"[a-z']+", s.lower()) if len(w) > 1}


def bleed_overlap(transcript: str, speaking_text: str) -> float:
    """Word-set Jaccard overlap between two texts. SYMMETRIC.

    NOT the echo guard — use echo_containment() for that. Jaccard is dominated
    by the larger set, so a short echo of a long reply scores near zero (a
    6-word echo of an 80-word reply: 0.10). It is kept because two other
    callers want a genuine symmetric similarity between comparable texts:
    session_turn.py (prefetched-thought usefulness) and clusters/temporal.py
    (DMN prediction hit).

    Returns 0.0 for empty inputs.
    """
    if not transcript or not speaking_text:
        return 0.0

    a = _tokenize(transcript)
    b = _tokenize(speaking_text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def echo_containment(transcript: str, speaking_text: str) -> float:
    """Fraction of the HEARD words that appear in what the entity is saying.

    Asymmetric on purpose — |A∩B| / |A|, not Jaccard. This is the TTS
    bleed-through measure: what matters is "was everything I just heard already
    coming out of the speaker?", which must not depend on how long the reply
    is. A 6-word echo scores 1.0 against both an 8-word reply and an 800-word
    one, where Jaccard would score 0.83 and 0.01.

    Returns 0.0 for empty inputs.
    """
    if not transcript or not speaking_text:
        return 0.0

    a = _tokenize(transcript)
    b = _tokenize(speaking_text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def should_voice_interrupt(
    text: str,
    speaking_text: str,
    *,
    barge_words: list[str],
    min_words: int | None = None,
    bleed_max: float | None = None,
) -> bool:
    """Decide whether live-transcribed speech (heard while the entity is
    speaking) should cut TTS off. Used by voice-mode barge-in, fed from
    interim ASR results — transcribed words, unlike the raw SpeechStarted
    VAD event this replaces, don't fire on coughs or keyboard clatter.

      - explicit barge keywords always interrupt, even one word
      - otherwise require at least `min_words` real words (filters "uh",
        throat-clearing fragments)
      - and an echo containment against the current TTS text below `bleed_max`
        (open-speaker echo guard; moot with headphones)

    `bleed_max` is a CONTAINMENT threshold (fraction of the heard words that
    were already in the TTS text), not the old Jaccard overlap — Jaccard scored
    a perfect echo of a long reply at ~0.1, so the guard could never fire on
    the replies long enough to be worth interrupting.
    """
    t = (text or "").strip()
    if not t:
        return False
    if is_barge_in(t, barge_words):
        return True
    if min_words is None:
        min_words = int(os.environ.get("BRAIN_BARGE_IN_MIN_WORDS", "2"))
    words = [w for w in re.findall(r"[\w']+", t.lower()) if len(w) > 1]
    if len(words) < min_words:
        return False
    if bleed_max is None:
        bleed_max = echo_containment_max()
    if not speaking_text:
        return True
    return echo_containment(t, speaking_text) < bleed_max


def classify_utterance(
    text: str,
    *,
    brain_is_speaking: bool,
    barge_words: list[str],
) -> tuple[str, dict]:
    """Decide what to do with an utterance.

    Returns (decision, info) where decision is one of:
      - "drop_empty" — empty transcript (background noise)
      - "dispatch"   — brain not speaking, send immediately
      - "barge_in"   — brain speaking + explicit interrupt keyword
      - "queue"      — brain speaking, hold and flush when TTS ends
    Everything the user says is sent; only empty transcripts are dropped.
    """
    text = (text or "").strip()
    if not text:
        return "drop_empty", {}

    if not brain_is_speaking:
        return "dispatch", {}

    if is_barge_in(text, barge_words):
        return "barge_in", {}

    return "queue", {}


def pick_dispatch_from_queue(queued: list[str]) -> tuple[str | None, int]:
    """Join all queued utterances into one block for dispatch.

    Deepgram's endpointing can split a single thought into multiple
    utterances; joining ensures the brain hears the whole thing at once.
    """
    if not queued:
        return None, 0
    joined = " ".join(q.strip() for q in queued if q.strip())
    return joined or None, len(queued)
