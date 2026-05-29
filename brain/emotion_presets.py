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
    "happy": {"tag": "[happy]", "desc": "warm, cheerful delivery"},
    "excited": {"tag": "[excited]", "desc": "energetic, high-valence delivery"},
    "laughing": {"tag": "[laughs softly]", "desc": "amused, laughter-tinged delivery"},
    "proud": {"tag": "[proudly]", "desc": "confident, accomplishment-colored delivery"},
    "warm": {"tag": "[warmly]", "desc": "gentle, affectionate delivery"},
    "playful": {"tag": "[playfully]", "desc": "light, teasing delivery"},
    # ── Neutral / contemplative ────────────────────────────────────────────────
    "calm": {"tag": "", "desc": "natural, uncolored delivery"},
    "curious": {"tag": "[curious]", "desc": "engaged, questioning delivery"},
    "thoughtful": {"tag": "[thoughtfully]", "desc": "measured, deliberate delivery"},
    "confident": {"tag": "[confidently]", "desc": "direct, assured delivery"},
    # ── Negative / reactive ────────────────────────────────────────────────────
    "sad": {"tag": "[sadly]", "desc": "subdued, melancholy delivery"},
    "angry": {"tag": "[angrily]", "desc": "forceful, heated delivery"},
    "anxious": {"tag": "[nervously]", "desc": "hesitant, unsettled delivery"},
    "embarrassed": {"tag": "[bashfully]", "desc": "sheepish, flustered delivery"},
    "frustrated": {"tag": "[firmly]", "desc": "clipped, strained delivery"},
    "surprised": {"tag": "[gasps]", "desc": "startled, wide-eyed delivery"},
    "disappointed": {"tag": "[softly]", "desc": "quiet, deflated delivery"},
    "sarcastic": {"tag": "[sarcastically]", "desc": "dry, pointed delivery"},
    # ── Performed-character tone cues (v3 tone family) ─────────────────────────
    "deadpan": {"tag": "[deadpan]", "desc": "flat, affectless delivery"},
    "dry": {"tag": "[dryly]", "desc": "dry, understated delivery"},
    "resigned": {"tag": "[resigned tone]", "desc": "weary, given-up delivery"},
    "enthusiastic": {"tag": "[enthusiastic]", "desc": "eager, animated delivery"},
}


# ──────────────────────────────────────────────────────────────────────────────
# Single source of truth for emotion → v3 audio tag.
#
# This comprehensive map (moved here from pns.py) covers every label the
# reactive affect system, set_mood(), and inline [mood:X] markup can resolve.
# Resolution walks: explicit entry → feeling-wheel ancestor (emotion_hierarchy)
# → neuromod fallback (handled in pns). Keeping it here means the reactive and
# deliberate paths can never drift apart.
# ──────────────────────────────────────────────────────────────────────────────
EMOTION_TAG_MAP: dict[str, str] = {
    # — neuromod-derivable (from emotion_vocabulary.EMOTION_TABLE) —
    "joy": "[happy]",
    "excitement": "[excited]",
    "enthusiasm": "[enthusiastic]",
    "curious": "[curious]",
    "curious-uncertain": "[curious]",
    "content": "",  # natural voice
    "warm": "[warmly]",
    "thoughtful": "[thoughtfully]",
    "confident": "[confidently]",
    "anxious": "[nervously]",
    "inhibited": "[softly]",
    "flat": "[softly]",
    "restless": "[urgently]",
    "cautious-agitated": "[urgently]",
    "agitated": "[firmly]",
    "angry": "[angrily]",
    "proud": "[proudly]",
    "surprised": "[gasps]",
    "defensive": "[firmly]",
    "wistful": "[softly]",
    "confused": "[confused]",
    "neutral": "",
    # — context-driven (no neuromod combo produces these on its own) —
    "amused": "[laughs softly]",
    "playful": "[playfully]",
    "joking": "[laughs softly]",
    "sad": "[sadly]",
    "somber": "[softly]",
    "melancholy": "[softly]",
    "frustrated": "[firmly]",
    "irritated": "[firmly]",
    "embarrassed": "[bashfully]",
    "shy": "[shyly]",
    "flirty": "[playfully]",
    "tender": "[gently]",
    "affectionate": "[warmly]",
    "apologetic": "[softly]",
    "grateful": "[warmly]",
    "relieved": "[sighs]",
    "disappointed": "[softly]",
    "sympathetic": "[gently]",
    "sarcastic": "[sarcastically]",
    "deadpan": "[deadpan]",
    "dry": "[dryly]",
    "resigned": "[resigned tone]",
    # — mid-tier defaults (feeling-wheel ancestors) for hierarchy fallback —
    "loving": "[warmly]",
    "peaceful": "",
    "joyful": "[happy]",
    "lonely": "[softly]",
    "humiliated": "[bashfully]",
    "mad": "[firmly]",
    "happy": "[happy]",
    "anger": "[firmly]",
    "fear": "[nervously]",
    "surprise": "[gasps]",
}


# Inline human-reaction / non-verbal audio tags the drafter may sprinkle into a
# response for realism (used SPARINGLY). These are bare tags (no [mood:...]
# wrapper). They pass through to v3 TTS but are stripped from chat display,
# memory, traces, and non-v3 TTS (which would read them literally).
REACTION_TAGS: list[str] = [
    "laughs",
    "laughs softly",
    "chuckles",
    "sighs",
    "exhales",
    "clears throat",
    "hesitates",
    "stammers",
    "scoffs",
    "gasps",
    "whispers",
    "pause",
    "rushed",
    "drawn out",
]

import re as _re  # noqa: E402

# Match longest tags first so "[laughs softly]" wins over "[laughs]".
REACTION_TAG_RE = _re.compile(
    r"\s*\[(?:"
    + "|".join(_re.escape(t) for t in sorted(REACTION_TAGS, key=len, reverse=True))
    + r")\]",
    _re.IGNORECASE,
)


def strip_reaction_tags(text: str) -> str:
    """Remove bare reaction/delivery audio tags from text (for chat display,
    memory, traces, and non-v3 TTS). Leaves [mood:X] markup untouched — that has
    its own stripper."""
    return REACTION_TAG_RE.sub("", text).strip()


# Canonical list of valid emotion names for tool validation
VALID_EMOTIONS: list[str] = sorted(EMOTION_PRESETS.keys()) + ["auto"]


def get_tag(emotion: str) -> str | None:
    """Return the ElevenLabs v3 audio tag for an emotion name, or None for an
    unknown/natural-voice emotion. Checks the deliberate presets first, then
    falls back to the comprehensive EMOTION_TAG_MAP so set_mood() and [mood:X]
    resolve identically to the reactive affect path."""
    key = emotion.lower()
    preset = EMOTION_PRESETS.get(key)
    if preset is not None:
        return preset["tag"] or None  # empty string → natural voice → None
    tag = EMOTION_TAG_MAP.get(key)
    return tag or None
