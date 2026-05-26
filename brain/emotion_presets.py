"""
Deliberate emotion presets for the set_mood tool and [mood:X] inline markup.

These are PURELY cosmetic/audio — they affect only:
  1. The ElevenLabs v3 audio tag injected into TTS text
  2. The UI emotion badge display

They do NOT touch any neuromod or hormonal channels or affect any cognitive system.

Two use cases:
  - set_mood("angry") tool: whole-turn voice character override
  - [mood:angry]text[/mood] inline markup: per-segment voice override within a response
"""
from __future__ import annotations

# Each preset maps to:
#   "tag"  — ElevenLabs v3 inline audio tag (empty string = natural voice)
#   "desc" — human-readable description for tool help / logging

EMOTION_PRESETS: dict[str, dict[str, str]] = {
    # ── Positive ──────────────────────────────────────────────────────────────
    "happy":       {"tag": "[happy]",         "desc": "warm, cheerful delivery"},
    "excited":     {"tag": "[excited]",        "desc": "energetic, high-valence delivery"},
    "laughing":    {"tag": "[laughs softly]",  "desc": "amused, laughter-tinged delivery"},
    "proud":       {"tag": "[proudly]",        "desc": "confident, accomplishment-colored delivery"},
    "warm":        {"tag": "[warmly]",         "desc": "gentle, affectionate delivery"},
    "playful":     {"tag": "[playfully]",      "desc": "light, teasing delivery"},

    # ── Neutral / contemplative ────────────────────────────────────────────────
    "calm":        {"tag": "",                 "desc": "natural, uncolored delivery"},
    "curious":     {"tag": "[curious]",        "desc": "engaged, questioning delivery"},
    "thoughtful":  {"tag": "[thoughtfully]",   "desc": "measured, deliberate delivery"},
    "confident":   {"tag": "[confidently]",    "desc": "direct, assured delivery"},

    # ── Negative / reactive ────────────────────────────────────────────────────
    "sad":         {"tag": "[sadly]",          "desc": "subdued, melancholy delivery"},
    "angry":       {"tag": "[angrily]",        "desc": "forceful, heated delivery"},
    "anxious":     {"tag": "[nervously]",      "desc": "hesitant, unsettled delivery"},
    "embarrassed": {"tag": "[bashfully]",      "desc": "sheepish, flustered delivery"},
    "frustrated":  {"tag": "[firmly]",         "desc": "clipped, strained delivery"},
    "surprised":   {"tag": "[gasps]",          "desc": "startled, wide-eyed delivery"},
    "disappointed":{"tag": "[softly]",         "desc": "quiet, deflated delivery"},
    "sarcastic":   {"tag": "[sarcastically]",  "desc": "dry, pointed delivery"},
}

# Canonical list of valid emotion names for tool validation
VALID_EMOTIONS: list[str] = sorted(EMOTION_PRESETS.keys()) + ["auto"]


def get_tag(emotion: str) -> str | None:
    """Return the ElevenLabs v3 audio tag for a deliberate emotion, or None if unknown."""
    preset = EMOTION_PRESETS.get(emotion.lower())
    if preset is None:
        return None
    return preset["tag"] or None   # empty string → natural voice → return None
