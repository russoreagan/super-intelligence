"""Felt vs expressed emotion: what the entity feels, and how its voice sounds to the listener.

The felt emotion (``affect["emotion"]``, named by the hypothalamus) is the entity's own
state. It drives chemistry, memory, rewards and interoception. Some of it is about the
entity itself: metacognition's appraisal of its own last reply ("proud" of a well-made
answer, "embarrassed" by vetoed drafts). That is real and worth feeling, but it is no reason
to talk to someone in that tone. A struggling person should not hear pride.

The expressed emotion (``affect["expressed_emotion"]``) is what the voice carries this turn.
It extends the deliberate-expression path that already exists (``set_mood`` for a whole
turn, ``[mood:X]…[/mood]`` for a span): the drafter may open its reply with a bare
``[mood:X]`` naming how the whole reply should sound. Resolution, most deliberate first:

  1. ``set_mood`` tool this turn (source "tool")
  2. the drafter's whole-reply ``[mood:X]`` (source "drafter"). If the expression basis
     voices in the same register (e.g. felt "somber" → [softly] under a declared "warm"),
     the feeling shows through and the basis is kept: feeling shapes expression to a
     degree, the reply decides the register.
  3. the expression basis (source "felt"): the felt emotion, or, when that felt emotion
     is a self-directed appraisal, the chemistry-derived label underneath it.

Partners see one layer: the expressed emotion is the mood the API reports and the voice it
tags. The console shows both.
"""

from __future__ import annotations

import re

from brain.emotion_presets import EMOTION_PRESETS, EMOTION_TAG_MAP, get_tag
from brain.settings import settings

# Registers a voice tag can sit in. A tag may sit in several. Used only to decide whether a
# felt tag is compatible with the register the drafter declared. Every tag the presets and
# the reactive map can produce appears at least once, so no tag is unreachable.
TAG_REGISTERS: dict[str, tuple[str | None, ...]] = {
    "comforting": ("[gently]", "[softly]", "[warmly]", "[sighs]"),
    "affirming": ("[warmly]", "[happy]", "[gently]", "[confidently]"),
    "inquiring": ("[curious]", "[thoughtfully]", "[confused]"),
    "reflective": (None, "[thoughtfully]", "[softly]", "[confidently]", "[drawn out]"),
    "celebrating": ("[happy]", "[excited]", "[enthusiastic]", "[proudly]", "[laughs softly]"),
    "playful": ("[playfully]", "[laughs softly]", "[sarcastically]", "[dryly]", "[deadpan]"),
    "firm": ("[firmly]", "[confidently]", "[urgently]", "[angrily]", "[rushed]"),
    "grieving": ("[sadly]", "[softly]", "[resigned tone]", "[sighs]"),
    "uneasy": ("[nervously]", "[hesitates]", "[bashfully]", "[shyly]", "[gasps]"),
}

# A whole-reply declaration: a leading [mood:X] that does not open a closed span.
_LEADING_MOOD_RE = re.compile(r"^\s*\[mood:\s*([a-z][a-z \-]{0,30})\s*\]\s*", re.IGNORECASE)


def enabled() -> bool:
    """Kill switch (default ON): 0 restores felt == expressed."""
    return bool(settings.get("expressed_emotion_split", 1))


def known_emotion(name: str | None) -> bool:
    key = (name or "").strip().lower()
    return bool(key) and (key in EMOTION_PRESETS or key in EMOTION_TAG_MAP)


def take_reply_expression(text: str) -> tuple[str, str | None]:
    """Split a reply into (text without a whole-reply declaration, declared emotion).

    Only a leading ``[mood:X]`` that is NOT the opening of a closed ``[mood:X]…[/mood]``
    span counts. Unknown names are still removed from the text (they would otherwise
    reach TTS as a literal tag) but are not returned."""
    if not text:
        return text, None
    m = _LEADING_MOOD_RE.match(text)
    if not m:
        return text, None
    rest_lower = text[m.end() :].lower()
    close = rest_lower.find("[/mood]")
    reopen = rest_lower.find("[mood:")
    if close != -1 and (reopen == -1 or close < reopen):
        return text, None  # it opens a closed span: a performed sentence, not the whole reply
    name = m.group(1).strip().lower()
    rest = text[m.end() :]
    return rest, (name if known_emotion(name) else None)


def tag_of(label: str | None) -> str | None:
    """Voice tag for a label via the same resolution the reactive path uses."""
    if not label:
        return None
    from brain.pns import PNS

    return PNS._v3_audio_tag_from_affect({"emotion": label})


def same_register(tag_a: str | None, tag_b: str | None) -> bool:
    if tag_a == tag_b:
        return True
    return any(tag_a in tags and tag_b in tags for tags in TAG_REGISTERS.values())


def resolve(
    basis: str | None,
    *,
    declared: str | None = None,
    deliberate: str | None = None,
) -> tuple[str, str]:
    """(expressed_emotion, source). See module docstring for the order."""
    basis = (basis or "neutral").lower()
    if deliberate and known_emotion(deliberate):
        return deliberate.lower(), "tool"
    if declared and known_emotion(declared):
        declared = declared.lower()
        if declared != basis and same_register(tag_of(basis), get_tag(declared)):
            return basis, "felt"
        return declared, "drafter"
    return basis, "felt"


def voice_emotion(affect: dict | None) -> str | None:
    """The emotion every voice path and the partner mood should read: the expressed
    emotion when this turn resolved one, else the expression basis (a turn that returned
    before articulation still never voices a self-directed appraisal), else felt."""
    affect = affect or {}
    if affect.get("expressed_emotion"):
        return affect["expressed_emotion"]
    if enabled() and affect.get("expression_basis"):
        return affect["expression_basis"]
    return affect.get("emotion")


def apply(affect: dict, *, declared: str | None, deliberate: str | None) -> dict:
    """Stamp expressed_emotion / expression_source onto a turn's affect dict (in place)."""
    if not enabled():
        affect["expressed_emotion"] = affect.get("emotion") or "neutral"
        affect["expression_source"] = "felt"
        return affect
    basis = affect.get("expression_basis") or affect.get("emotion")
    expressed, source = resolve(basis, declared=declared, deliberate=deliberate)
    affect["expressed_emotion"] = expressed
    affect["expression_source"] = source
    return affect


def _engine_tags() -> frozenset[str]:
    tags = {t for t in EMOTION_TAG_MAP.values() if t}
    tags |= {p["tag"] for p in EMOTION_PRESETS.values() if p["tag"]}
    tags |= {t for ts in TAG_REGISTERS.values() for t in ts if t}
    return frozenset(tags)


def partner_base_tag(affect: dict | None) -> str | None:
    """The tag a partner replays from a turn's curated affect block (``{base_tag, segments}``
    passed back into /v1/tts, as the API guide tells them to). Returns the tag, "" for an
    explicit natural voice (``base_tag: null``), or None when this is not such a block.
    Only tags the engine itself produces are honoured, so a partner can't inject arbitrary
    text into the speech."""
    if not affect or "base_tag" not in affect:
        return None
    tag = affect.get("base_tag")
    if tag is None:
        return ""
    return tag if isinstance(tag, str) and tag in _engine_tags() else None
