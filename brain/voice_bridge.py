"""
Voice bridge — pure functions for routing utterances from streaming_mic.
Extracted from run.py so the routing decisions can be unit-tested without
spinning up the whole brain. The async glue in run.py imports these and
wires them to pns + ui_message_queue.
"""
from __future__ import annotations

import re

DEFAULT_BARGE_IN_WORDS = [
    "stop", "wait", "shut up", "hold on", "pause", "enough",
    "never mind", "hey brain", "brain stop",
    "cut it out", "knock it off", "quiet", "be quiet", "hush", "shush",
    "okay enough", "that's enough", "thats enough",
]

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


def bleed_overlap(transcript: str, speaking_text: str) -> float:
    """Word-set Jaccard overlap between the transcript and what the brain
    is currently saying. Used to detect TTS bleed-through.

    Returns 0.0 for empty inputs. Single-character "words" are filtered out
    so that articles ('a', 'i') don't inflate the score.
    """
    if not transcript or not speaking_text:
        return 0.0
    tokenize = lambda s: set(w for w in re.findall(r"[a-z']+", s.lower()) if len(w) > 1)
    a = tokenize(transcript)
    b = tokenize(speaking_text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def classify_utterance(
    text: str,
    *,
    brain_is_speaking: bool,
    barge_words: list[str],
) -> tuple[str, dict]:
    """Decide what to do with an utterance.

    Returns (decision, info) where decision is one of:
      - "drop_empty" — empty transcript (background noise)
      - "dispatch"   — brain not speaking, normal turn
      - "barge_in"   — brain speaking → interrupt + dispatch
    Everything the user says is sent; only empty transcripts are dropped.
    """
    text = (text or "").strip()
    if not text:
        return "drop_empty", {}

    if not brain_is_speaking:
        return "dispatch", {}

    return "barge_in", {}


