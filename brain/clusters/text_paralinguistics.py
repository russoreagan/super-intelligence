"""
Text Paralinguistics — lightweight LLM-free extractor for text-channel turns.

Produces TextParalinguisticFeatures: the text-channel equivalent of prosody data.
Voice turns carry rich acoustic signal (f0, energy, jitter, tone_label); text turns
have none of that. This module extracts the paralinguistic signals that *do* exist
in text: laughter markers, warmth signals, negativity markers, excitement, informal
register abbreviations, emoji, and punctuation density.

Analogous role to auditory_cortex → audio_dsp for voice.
Called from session_turn.py for text turns. Voice turns skip the full extractor
(prosody carries the acoustic signal) with one narrow exception: extract_laughter()
runs over voice transcripts too, because STT engines often transcribe a real laugh
as "ha ha" — a laughter marker in a transcript is evidence regardless of channel.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

# ── Signal word lists ─────────────────────────────────────────────────────────

_LAUGHTER = frozenset(
    [
        "lol",
        "lmao",
        "lmfao",
        "haha",
        "hahaha",
        "hehe",
        "heh",
        "rofl",
        "😂",
        "🤣",
        "😆",
        "😄",
        "😁",
    ]
)

_WARMTH = frozenset(
    [
        ":)",
        ":-)",
        ":)",
        "=)",
        "^_^",
        "😊",
        "😀",
        "😃",
        "🙂",
        "🥰",
        "😍",
        "❤️",
        "❤",
        "💙",
        "💚",
        "💛",
        "🧡",
        "💜",
        "🖤",
        "🤍",
        "♥",
        "💕",
        "💞",
        "💓",
        "💗",
        "💖",
        "💝",
        "👍",
        "🙌",
        "🤗",
        "😘",
        "🥲",
        "thank",
        "thanks",
        "thankyou",
        "ty",
        "tyvm",
        "thx",
    ]
)

_NEGATIVITY = frozenset(
    [
        ":(",
        ":-(",
        ":/",
        ":-/",
        ":/",
        "=(",
        ">:(",
        "😢",
        "😭",
        "😔",
        "😞",
        "😟",
        "😣",
        "😤",
        "😡",
        "🤬",
        "👎",
        "🙁",
        "☹️",
        "ugh",
        "argh",
        "smh",
        "wtf",
        "wth",
    ]
)

_EXCITEMENT = frozenset(
    [
        "omg",
        "omfg",
        "wow",
        "wow!",
        "whoa",
        "woah",
        "damn",
        "dang",
        "holy",
        "yesss",
        "yasss",
        "yay",
        "woohoo",
        "woo",
        "🔥",
        "🚀",
        "💥",
        "⚡",
        "✨",
        "🎉",
        "🎊",
        "🏆",
        "💯",
        "🤩",
        "😱",
    ]
)

# Informal/casual register markers — high density indicates relaxed communication
_INFORMALITY = frozenset(
    [
        "tbh",
        "ngl",
        "idk",
        "idc",
        "imo",
        "imho",
        "afaik",
        "afaict",
        "btw",
        "fwiw",
        "iirc",
        "tfw",
        "smh",
        "nvm",
        "rn",
        "irl",
        "bc",
        "cuz",
        "coz",
        "gonna",
        "wanna",
        "gotta",
        "kinda",
        "sorta",
        "prolly",
        "def",
        "totes",
        "legit",
        "lit",
        "vibe",
        "vibes",
        "fr",
        "fr fr",
        "no cap",
        "slay",
        "lowkey",
        "highkey",
        "lmk",
        "hmu",
        "ttyl",
        "brb",
        "gtg",
        "np",
        "yw",
        "ur",
        "u",
        "r",
    ]
)

# Pre-compiled regex for emoji detection (broad unicode ranges)
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U00002702-\U000027b0"  # dingbats
    "\U000024c2-\U0001f251"  # enclosed
    "]+",
    flags=re.UNICODE,
)


# ── Extractor ─────────────────────────────────────────────────────────────────


@dataclass
class TextParalinguisticFeatures:
    """
    Paralinguistic features extracted from a text message.
    Values are 0–1 floats (normalized by message length), except emoji_count.
    """

    laughter: float = 0.0  # lol / haha / 😂 markers
    warmth: float = 0.0  # :) / ❤️ / thanks markers
    negativity: float = 0.0  # :( / 😡 / ugh markers
    excitement: float = 0.0  # omg / 🔥 / wow markers
    informality: float = 0.0  # tbh / ngl / idk abbreviation density
    emoji_count: int = 0  # raw emoji count
    exclamation_density: float = 0.0  # exclamation marks per word

    def to_dict(self) -> dict:
        return asdict(self)


def _tokenise(text: str) -> list[str]:
    """Lowercase word tokens, preserving common emoji-adjacent punctuation."""
    # Strip combining characters, normalise unicode
    text = unicodedata.normalize("NFC", text.lower())
    # Split on whitespace and common punctuation (but not ! or ?)
    tokens = re.split(r"[\s,;:\"'\(\)\[\]{}|\\/<>]+", text)
    return [t for t in tokens if t]


# STT engines render real laughs as separated syllables ("ha ha", "ha ha ha",
# "heh heh") or as event annotations ("(laughs)", "[laughter]"). Neither form
# tokenises into the _LAUGHTER set, so they get their own patterns. Two or more
# consecutive syllables are required — a lone "ha" is sarcasm as often as mirth.
_TRANSCRIBED_LAUGH_RE = re.compile(
    r"\b(?:ha|heh?|hee)(?:[\s,.!]+(?:ha|heh?|hee)){1,}\b", re.IGNORECASE
)
_STT_LAUGH_ANNOTATIONS = ("(laugh", "[laugh", "(chuckl", "[chuckl", "(giggl", "[giggl")


def extract_laughter(text: str) -> float:
    """
    Laughter-only extraction, safe for VOICE transcripts.

    The full extractor is text-channel-only (voice has prosody instead), but
    laughter markers in a transcript are evidence of a real laugh regardless of
    channel. Returns the same 0–1 normalized score as the `laughter` field of
    extract_text_paralinguistics, plus STT-specific forms ("ha ha" syllable
    runs, "(laughs)" annotations) that the token list misses.
    """
    if not text or not text.strip():
        return 0.0

    tokens = _tokenise(text)
    n = max(len(tokens), 1)

    hits = sum(1 for tok in tokens if tok in _LAUGHTER)
    for match_str in _EMOJI_RE.findall(text):
        hits += sum(1 for glyph in match_str if glyph in _LAUGHTER)

    hits += len(_TRANSCRIBED_LAUGH_RE.findall(text))
    text_lower = text.lower()
    hits += sum(1 for marker in _STT_LAUGH_ANNOTATIONS if marker in text_lower)

    # Same non-linear normalisation as the full extractor: one strong signal
    # in a short message should register.
    return min(1.0, hits / n * 3.0)


def extract_text_paralinguistics(text: str) -> TextParalinguisticFeatures:
    """
    Extract paralinguistic features from a text message.
    Runs in O(n) with no LLM calls — safe to call inline every turn.
    """
    if not text or not text.strip():
        return TextParalinguisticFeatures()

    tokens = _tokenise(text)
    n = max(len(tokens), 1)

    # Count emoji separately (they don't tokenise cleanly)
    emojis = _EMOJI_RE.findall(text)
    emoji_count = sum(len(e) for e in emojis)  # individual glyphs

    # Score each signal type — count hits, normalise by token count
    laughter_hits = 0
    warmth_hits = 0
    negativity_hits = 0
    excitement_hits = 0
    informality_hits = 0

    # Also check raw text for multi-token patterns like "no cap", "fr fr"
    text_lower = text.lower()
    for phrase in ["no cap", "fr fr"]:
        if phrase in text_lower:
            informality_hits += 1

    # Check ascii emoticons in raw text
    for marker in (":)", ":-)", "=)", "^_^", ":(", ":-(", ":/", ":-/", ">:("):
        if marker in text:
            if marker in _WARMTH:
                warmth_hits += 1
            elif marker in _NEGATIVITY:
                negativity_hits += 1

    for tok in tokens:
        if tok in _LAUGHTER:
            laughter_hits += 1
        if tok in _WARMTH:
            warmth_hits += 1
        if tok in _NEGATIVITY:
            negativity_hits += 1
        if tok in _EXCITEMENT:
            excitement_hits += 1
        if tok in _INFORMALITY:
            informality_hits += 1

    # Emoji contribute to signal categories based on which list they appear in
    for match_str in emojis:
        for glyph in match_str:
            if glyph in _LAUGHTER:
                laughter_hits += 1
            if glyph in _WARMTH:
                warmth_hits += 1
            if glyph in _NEGATIVITY:
                negativity_hits += 1
            if glyph in _EXCITEMENT:
                excitement_hits += 1

    # Exclamation density
    exclamation_count = text.count("!")
    exclamation_density = min(1.0, exclamation_count / n)

    # Excitement boost from multiple exclamation marks
    if exclamation_count >= 2:
        excitement_hits += 1

    # Normalise to 0–1; cap at 1.0
    def norm(count: int) -> float:
        # Non-linear: even 1 strong signal in a short message should register
        return min(1.0, count / max(1, n) * 3.0)

    return TextParalinguisticFeatures(
        laughter=norm(laughter_hits),
        warmth=norm(warmth_hits),
        negativity=norm(negativity_hits),
        excitement=norm(excitement_hits),
        informality=min(1.0, informality_hits / max(1, n) * 4.0),
        emoji_count=emoji_count,
        exclamation_density=exclamation_density,
    )
