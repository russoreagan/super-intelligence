"""Coarse valence for the user_emotion labels the temporal lobe emits.

One table, shared by everything that needs to know whether the person seems to be
having a hard time: sleep consolidation (mood shifts turn to turn) and the
conversational skill gate. Sign and rough magnitude only.

The first block is the temporal parser's closed vocabulary (UNDERSTANDING_SYSTEM in
brain/clusters/temporal.py). The second holds labels the parser LLM emits anyway
even though they're off-list, so they don't read as neutral. Unknown labels are 0.0.
"""

from __future__ import annotations

USER_EMOTION_VALENCE: dict[str, float] = {
    "happy": 0.7,
    "playful": 0.6,
    "amused": 0.6,
    "warm": 0.6,
    "affectionate": 0.8,
    "excited": 0.7,
    "curious": 0.4,
    "engaged": 0.4,
    "surprised": 0.1,
    "neutral": 0.0,
    "tired": -0.3,
    "confused": -0.3,
    "disappointed": -0.5,
    "annoyed": -0.5,
    "frustrated": -0.6,
    "angry": -0.8,
    "sad": -0.6,
    "anxious": -0.6,
    "distressed": -0.7,
    "struggling": -0.5,
    # Off-vocabulary labels seen in practice.
    "worried": -0.5,
    "stuck": -0.4,
    "exhausted": -0.5,
    "overwhelmed": -0.7,
    "upset": -0.6,
    "hostile": -0.8,
    "scared": -0.6,
    "lost": -0.4,
    "hopeless": -0.8,
    "lonely": -0.6,
    "hurt": -0.6,
    "stressed": -0.5,
}

# At or below this the person reads as having a hard time: every negative label in
# the vocabulary clears it, neutral and surprised don't.
NEGATIVE_VALENCE_FLOOR = -0.3


def user_emotion_valence(label: str | None) -> float:
    return USER_EMOTION_VALENCE.get((label or "").strip().lower(), 0.0)


def is_negative_state(label: str | None, sentiment: float | None = None) -> bool:
    """True when the person seems to be having a hard time: a negative-valence
    emotion label, or a negative message sentiment (the parser's continuous -1..1
    read) when the label is neutral, unknown or missing."""
    if user_emotion_valence(label) <= NEGATIVE_VALENCE_FLOOR:
        return True
    try:
        return sentiment is not None and float(sentiment) <= NEGATIVE_VALENCE_FLOOR
    except (TypeError, ValueError):
        return False
