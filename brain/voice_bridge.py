"""
Voice bridge — pure functions for routing utterances from streaming_mic.
Extracted from run.py so the routing decisions can be unit-tested without
spinning up the whole brain. The async glue in run.py imports these and
wires them to pns + ui_message_queue.
"""
from __future__ import annotations

import re

# Default set of barge-in keywords. Override via BRAIN_BARGE_IN_WORDS env var
# (comma-separated, case-insensitive). When the brain is speaking, any
# utterance containing one of these triggers TTS interrupt + dispatch.
DEFAULT_BARGE_IN_WORDS = [
    "stop", "wait", "shut up", "hold on", "pause", "enough",
    "never mind", "hey brain", "brain stop",
    "cut it out", "knock it off", "quiet", "be quiet", "hush", "shush",
    "okay enough", "that's enough", "thats enough",
]

# Bleed-overlap threshold (Jaccard word-set overlap with current TTS text).
# Above this, the utterance is treated as TTS bleed-through and dropped.
# Below, it's treated as a genuine user utterance and queued.
BLEED_OVERLAP_THRESHOLD = 0.4


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
    speaking_text: str,
    barge_words: list[str],
    bleed_threshold: float = BLEED_OVERLAP_THRESHOLD,
) -> tuple[str, dict]:
    """Decide what to do with an utterance.

    Returns (decision, info) where decision is one of:
      - "drop_empty"   — empty transcript
      - "dispatch"     — brain not speaking, normal turn
      - "barge_in"     — brain speaking + barge-in keyword → interrupt + dispatch
      - "drop_bleed"   — brain speaking + transcript looks like TTS bleed
      - "queue"        — brain speaking + genuine user utterance, queue for later
    info dict carries diagnostic fields (overlap, matched_word, etc.).
    """
    text = (text or "").strip()
    if not text:
        return "drop_empty", {}

    if not brain_is_speaking:
        return "dispatch", {}

    if is_barge_in(text, barge_words):
        return "barge_in", {}

    overlap = bleed_overlap(text, speaking_text)
    if overlap > bleed_threshold:
        return "drop_bleed", {"overlap": round(overlap, 3)}

    return "queue", {"overlap": round(overlap, 3)}


def pick_dispatch_from_queue(queued: list[str]) -> tuple[str | None, int]:
    """When TTS ends, decide what to dispatch from the queue.

    Strategy: JOIN all queued utterances with spaces. Deepgram's endpointing
    splits a single sentence with natural pauses into multiple utterances;
    if the user said one long thing during TTS we want all of it as a single
    turn, not just the last fragment. Returns (joined_text, count_joined).

    For most cases this is what the user means. If they truly said multiple
    separate thoughts during TTS, joining them is still a more recoverable
    failure mode than silently dropping all but the last.
    """
    if not queued:
        return None, 0
    if len(queued) == 1:
        return queued[0], 1
    joined = " ".join(q.strip() for q in queued if q.strip())
    return joined, len(queued)
