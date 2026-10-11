"""Delivery-tone resolver — EXPLORATION PROTOTYPE (2026-10-11). Not wired to any caller.

Problem it explores: the v3 voice tag is chosen from the brain's FELT emotion, named
before drafting (hypothalamus) or carried over from the previous turn's metacognition
appraisal. Nothing looks at the reply that is actually spoken, so a calm, validating
reply can go out as [proudly] (last turn's intrinsic-pride appraisal) or [urgently]
(an unmapped label falling through to the neuromod threshold fallback).

The idea is a display-rule stage (felt vs expressed emotion):

  felt label ──► felt tag (complete map, every label resolves explicitly)
                     │
  final reply ──► tone families the reply supports (embedding read, LLM-teachable)
                     │
                     ▼
  keep the felt tag if the reply supports it; otherwise pick the supported tag nearest
  the felt state in valence/arousal space.

Chemistry still chooses WITHIN what the reply supports, so every tag stays reachable
(nothing is clamped to a "safe" subset); the reply only vetoes tags that contradict it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable

from brain.emotion_hierarchy import lookup_with_inheritance
from brain.emotion_presets import EMOTION_TAG_MAP

# ── 1. Complete label → tag map ───────────────────────────────────────────────
# Labels the chemistry/hormonal/NE colouring and the hypothalamus text fallback can
# emit that have NO entry (and no tag-bearing feeling-wheel ancestor) today. They
# fall through to PNS's neuromod threshold fallback, so the same label voices
# differently turn to turn ("engaged" → [curious] / [softly] / none / [urgently] in
# the 2026-10-11 Haven sample). "eased" and "serene" inherit [happy] via the
# 'happy' core, which reads wrong for a buffered-stress or quiet-calm state.
LABEL_TAG_FILL: dict[str, str] = {
    "engaged": "[curious]",
    "alert-curious": "[curious]",
    "composed": "",  # natural voice
    "settled": "",
    "serene": "",
    "eased": "",
    "stirred": "[thoughtfully]",
    "cautious-warm": "[gently]",
    "concerned": "[gently]",
    "down": "[softly]",
    "wary": "[hesitates]",
    "scattered": "[rushed]",
}


def tag_for_label(label: str | None) -> str | None:
    """Explicit-first label → tag. Returns None for natural voice or unknown labels."""
    key = (label or "").lower()
    if key in LABEL_TAG_FILL:
        return LABEL_TAG_FILL[key] or None
    if key in EMOTION_TAG_MAP:
        return EMOTION_TAG_MAP[key] or None
    return lookup_with_inheritance(key, EMOTION_TAG_MAP)


# ── 2. Tag geometry (valence, arousal in 0..1) ───────────────────────────────
# Rough placement used only to choose the nearest supported tag. None = natural voice.
TAG_VA: dict[str | None, tuple[float, float]] = {
    "[excited]": (0.85, 0.85),
    "[enthusiastic]": (0.85, 0.80),
    "[happy]": (0.85, 0.60),
    "[playfully]": (0.80, 0.60),
    "[proudly]": (0.80, 0.55),
    "[laughs softly]": (0.80, 0.45),
    "[warmly]": (0.80, 0.35),
    "[confidently]": (0.65, 0.50),
    "[gently]": (0.65, 0.20),
    "[curious]": (0.60, 0.50),
    "[thoughtfully]": (0.55, 0.30),
    None: (0.55, 0.35),
    "[softly]": (0.50, 0.15),
    "[bashfully]": (0.50, 0.45),
    "[shyly]": (0.50, 0.35),
    "[gasps]": (0.50, 0.85),
    "[rushed]": (0.50, 0.85),
    "[dryly]": (0.45, 0.30),
    "[deadpan]": (0.45, 0.15),
    "[confused]": (0.40, 0.45),
    "[hesitates]": (0.40, 0.40),
    "[sighs]": (0.40, 0.20),
    "[firmly]": (0.40, 0.60),
    "[sarcastically]": (0.35, 0.50),
    "[urgently]": (0.35, 0.80),
    "[nervously]": (0.30, 0.65),
    "[resigned tone]": (0.25, 0.15),
    "[sadly]": (0.20, 0.20),
    "[angrily]": (0.10, 0.85),
}

# ── 3. Tone families: what a reply's TEXT can support ─────────────────────────
# Seeds are generic delivery exemplars (bootstrap only — in the house pattern the bank
# grows from an LLM teacher, see brain/intent_detector.py). Deliberately NOT drawn from
# the evaluation sample.
TONE_FAMILIES: dict[str, dict] = {
    "comforting": {
        "desc": "validating someone's pain or tiredness; soothing, reassuring",
        "tags": ["[gently]", "[softly]", "[warmly]", "[sighs]"],
        "seeds": [
            "That sounds really hard, and it makes sense you feel worn down.",
            "I'm sorry you're carrying all of this.",
            "It's okay to feel this way. You're not doing anything wrong.",
            "That's a lot for anyone to hold.",
            "Of course you're tired. You've been running on empty for a while.",
            "You don't have to have it all figured out right now.",
        ],
    },
    "affirming": {
        "desc": "friendly agreement, appreciation, encouragement",
        "tags": ["[warmly]", "[happy]", "[gently]", "[confidently]"],
        "seeds": [
            "Yes, exactly. That's a really good way to put it.",
            "I appreciate you telling me that.",
            "You're right, and I'm glad you noticed it.",
            "That's a great instinct. Trust it.",
            "Thanks, that means a lot.",
            "Good, I'm glad that helped.",
        ],
    },
    "inquiring": {
        "desc": "exploring with open questions, wondering, checking understanding",
        "tags": ["[curious]", "[thoughtfully]", "[confused]"],
        "seeds": [
            "What do you think is underneath that?",
            "I'm wondering what that feels like for you.",
            "Can I ask what happens right before it starts?",
            "Is it more one thing or the other?",
            "How did that land for you?",
            "What would change if you tried it differently?",
        ],
    },
    "reflective": {
        "desc": "measured explanation or summary, neutral and steady",
        "tags": [None, "[thoughtfully]", "[softly]", "[confidently]"],
        "seeds": [
            "So the pattern seems to be that the pressure builds before anything visible happens.",
            "Here's how I'd think about it: there are two separate problems.",
            "The short version is that the deadline moved and the plan didn't.",
            "That depends on what you mean by better.",
            "Let me lay out the options as I see them.",
            "In other words, it's less about the task and more about the timing.",
        ],
    },
    "celebrating": {
        "desc": "upbeat, excited, proud, celebrating a win or good news",
        "tags": ["[happy]", "[excited]", "[enthusiastic]", "[proudly]", "[laughs softly]"],
        "seeds": [
            "That's amazing! Congratulations, you earned it!",
            "Oh, I love that. What great news!",
            "You did it! That's huge!",
            "I'm so proud of how far you've come.",
            "This is so exciting, tell me everything!",
            "Yes! That's the breakthrough we were waiting for.",
        ],
    },
    "playful": {
        "desc": "teasing, joking, wry or dry humour",
        "tags": ["[playfully]", "[laughs softly]", "[sarcastically]", "[dryly]", "[deadpan]"],
        "seeds": [
            "Oh sure, because that always goes well.",
            "Ha, okay, you got me there.",
            "Well, that's one way to make a Monday interesting.",
            "Bold move. I respect it.",
            "I'm choosing to believe the cat did it.",
            "Totally normal behaviour, nothing to see here.",
        ],
    },
    "firm": {
        "desc": "direct, setting a boundary, insisting, urgent warning",
        "tags": ["[firmly]", "[confidently]", "[urgently]", "[angrily]", "[rushed]"],
        "seeds": [
            "No. That's not okay, and you need to stop.",
            "Please call emergency services right now.",
            "I'm going to be direct: this has to change today.",
            "Stop and listen to me for a second.",
            "That line was crossed, and it matters.",
            "Do not send that email yet.",
        ],
    },
    "grieving": {
        "desc": "sad, heavy, mourning, resigned",
        "tags": ["[sadly]", "[softly]", "[resigned tone]", "[sighs]"],
        "seeds": [
            "I'm so sorry. Losing them must be unbearable.",
            "There's no fixing this, and that's the hardest part.",
            "Some things just stay broken, and we carry them anyway.",
            "I miss them too.",
            "It hurts, and it's going to hurt for a while.",
            "I wish it had ended differently.",
        ],
    },
    "uneasy": {
        "desc": "nervous, hesitant, unsure, embarrassed",
        "tags": ["[nervously]", "[hesitates]", "[bashfully]", "[shyly]", "[gasps]"],
        "seeds": [
            "Um, I'm not totally sure that's right, actually.",
            "Oh no. I think I got that wrong, sorry.",
            "I don't know how to say this, but something feels off.",
            "Wait, that can't be right, can it?",
            "This is a little awkward, but I have to ask.",
            "I might be overstepping here.",
        ],
    },
}

EmbedFn = Callable[[str], Awaitable[list[float] | None]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if len(s.strip()) > 2]


class ToneReader:
    """Embedding read of which tone families a reply's text supports.

    Per-sentence nearest-exemplar scores, softmaxed per sentence, then averaged with
    the opening sentence weighted double (the leading tag colours the start most)."""

    def __init__(self, embed: EmbedFn, temperature: float = 0.03) -> None:
        self._embed = embed
        self._tau = temperature
        self._bank: dict[str, list[list[float]]] = {}

    async def _ensure_bank(self) -> None:
        if self._bank:
            return
        for fam, spec in TONE_FAMILIES.items():
            vecs = [await self._embed(s) for s in spec["seeds"]]
            self._bank[fam] = [v for v in vecs if v]

    def add_exemplar(self, family: str, vec: list[float]) -> None:
        """Teaching hook: an LLM (critic / reflector) confirms a family the embedding missed."""
        self._bank.setdefault(family, []).append(vec)

    async def read(self, text: str) -> dict[str, float]:
        await self._ensure_bank()
        sents = split_sentences(text) or [text]
        totals = dict.fromkeys(self._bank, 0.0)
        wsum = 0.0
        for i, s in enumerate(sents):
            v = await self._embed(s)
            if not v:
                continue
            raw = {f: max((_cosine(v, e) for e in ex), default=0.0) for f, ex in self._bank.items()}
            m = max(raw.values())
            exps = {f: math.exp((r - m) / self._tau) for f, r in raw.items()}
            z = sum(exps.values())
            w = 2.0 if i == 0 else 1.0
            for f in totals:
                totals[f] += w * exps[f] / z
            wsum += w
        return {f: t / wsum for f, t in totals.items()} if wsum else {}


def supported_families(probs: dict[str, float], ratio: float = 0.5) -> list[str]:
    """Families within `ratio` of the top family's probability (mixtures allowed)."""
    if not probs:
        return []
    top = max(probs.values())
    return [f for f, p in sorted(probs.items(), key=lambda kv: -kv[1]) if p >= ratio * top]


def resolve_delivery(
    felt_label: str | None,
    families: list[str],
    felt_va: tuple[float, float] | None = None,
) -> tuple[str | None, str]:
    """Pick the voice tag: the felt tag when the reply supports it, else the supported
    tag nearest the felt state. Returns (tag, reason)."""
    felt_tag = tag_for_label(felt_label)
    if not families:
        return felt_tag, "no tone read — felt tag"
    allowed: list[str | None] = []
    for f in families:
        for t in TONE_FAMILIES[f]["tags"]:
            if t not in allowed:
                allowed.append(t)
    if felt_tag in allowed:
        return felt_tag, f"felt {felt_label} supported by {families}"
    # Anchor on the continuous felt state (PAD valence/arousal from chemistry) when
    # available — the label's own position is a lossy proxy and drags a vetoed
    # "proud"/"excited" toward [confidently] instead of the warm state underneath.
    anchor = felt_va or TAG_VA.get(felt_tag) or TAG_VA[None]
    best = min(
        allowed,
        key=lambda t: (TAG_VA[t][0] - anchor[0]) ** 2 + (TAG_VA[t][1] - anchor[1]) ** 2,
    )
    return best, f"felt {felt_label}→{felt_tag} not supported by {families}; nearest"
