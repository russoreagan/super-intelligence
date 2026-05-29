"""
Emotion hierarchy — feeling-wheel-inspired 3-tier taxonomy (SKETCH, not wired in).

Three tiers:
  CORE  — universal feeling families (the inner ring of the wheel)
  MID   — basic differentiation within a family (the middle ring)
  LEAF  — context-driven refinements; metacognition appraisal produces these

Every emotion label our system can emit is tagged with its (core, mid) ancestors.
That unlocks three things:
  1. Inheritance fallback: tag/guidance tables can walk leaf → mid → core, so a
     new label automatically inherits its family's delivery until something more
     specific is added for it.
  2. Structured appraisal: metacognition rules become "refine sad → embarrassed
     vs. apologetic vs. disappointed" instead of inventing flat labels.
  3. Coarse aggregation: turn logs / dashboards can group by core to surface
     family-level distribution ("anger 14% of turns") without listing every leaf.

Several of our labels (thoughtful, restless, agitated) are cognitive/arousal
states without a clean feeling-wheel home. They go under COGNITIVE — a
pragmatic 7th core that keeps the taxonomy total over the labels we actually
emit, rather than forcing a bad fit under happy/sad/anger.
"""

from __future__ import annotations

# ── Core tier (8 buckets including pragmatic extras) ─────────────────────
CORES: tuple[str, ...] = (
    "happy",
    "sad",
    "anger",
    "fear",
    "disgust",
    "surprise",
    "cognitive",  # thought/arousal states with no clean feeling home
    "neutral",  # baseline — explicit so logs can count it
)


# ── Label → (core, mid) parents ──────────────────────────────────────────
# Covers every label produced by:
#   - emotion_vocabulary.EMOTION_TABLE         (neuromod-derived)
#   - metacognition._appraise                  (context-driven leaves)
#   - frontal._EXPRESSIVE_BY_EMOTION extras    (delivery-only labels)
EMOTION_PARENTS: dict[str, tuple[str, str | None]] = {
    # ── happy family ──────────────────────────────────────────────────────
    "joy": ("happy", "joyful"),
    "excitement": ("happy", "excited"),
    "enthusiasm": ("happy", "energetic"),
    "content": ("happy", "peaceful"),
    "warm": ("happy", "loving"),
    "confident": ("happy", "powerful"),
    "proud": ("happy", "proud"),
    "amused": ("happy", "playful"),
    "playful": ("happy", "playful"),
    "joking": ("happy", "playful"),
    "flirty": ("happy", "playful"),
    "tender": ("happy", "loving"),
    "affectionate": ("happy", "loving"),
    "grateful": ("happy", "accepted"),
    "relieved": ("happy", "peaceful"),
    "sympathetic": ("happy", "loving"),  # other-oriented warmth
    # ── sad family ────────────────────────────────────────────────────────
    "sad": ("sad", "lonely"),
    "somber": ("sad", "depressed"),
    "melancholy": ("sad", "lonely"),
    "wistful": ("sad", "lonely"),
    "disappointed": ("sad", "disappointed"),
    "embarrassed": ("sad", "humiliated"),
    "shy": ("sad", "humiliated"),
    "apologetic": ("sad", "remorseful"),
    # ── anger family ──────────────────────────────────────────────────────
    "angry": ("anger", "mad"),
    "frustrated": ("anger", "frustrated"),
    "irritated": ("anger", "frustrated"),
    "agitated": ("anger", "mad"),
    "defensive": ("anger", "threatened"),
    "sarcastic": ("anger", "critical"),
    # ── fear family ───────────────────────────────────────────────────────
    "anxious": ("fear", "anxious"),
    "inhibited": ("fear", "submissive"),
    # ── surprise family ───────────────────────────────────────────────────
    "surprised": ("surprise", "startled"),
    "confused": ("surprise", "confused"),
    "curious": ("surprise", "amazed"),
    # ── cognitive / arousal (no feeling-wheel home) ───────────────────────
    "thoughtful": ("cognitive", "deliberative"),
    "curious-uncertain": ("cognitive", "deliberative"),
    "engaged": ("cognitive", "deliberative"),
    "cautious-agitated": ("cognitive", "tense"),
    "cautious-warm": ("cognitive", "tense"),  # CORT-modulated warmth
    "stirred": ("cognitive", "tense"),  # Glu-driven alertness
    "restless": ("cognitive", "tense"),
    "flat": ("cognitive", "minimal"),
    # ── fear family (additions) ───────────────────────────────────────────
    "stressed": ("fear", "tense"),  # GABA+Glu stress state
    "overwhelmed": ("fear", "overwhelmed"),  # system taxed, submissive
    "uneasy": ("fear", "tense"),  # GABA+Glu rising
    "guarded": ("fear", "anxious"),  # high CORT + anxious base
    "vigilant": ("fear", "anxious"),  # NE-driven sharp attention
    # ── happy family (additions) ──────────────────────────────────────────
    "serene": ("happy", "peaceful"),  # DA high, everything settled
    "lively": ("happy", "energetic"),  # DA+ACh elevated, warm
    "eased": ("happy", "peaceful"),  # stress buffered by AEA
    "connected": ("happy", "loving"),  # high OXT + positive base
    # ── sad family (additions) ────────────────────────────────────────────
    "withdrawn": ("sad", "lonely"),  # high CORT, low OXT
    "dysphoric": ("sad", "depressed"),  # low 5HT baseline
    # ── neutral ───────────────────────────────────────────────────────────
    "neutral": ("neutral", None),
    "settled": ("neutral", None),  # quiet baseline: low GABA, moderate DA
    "composed": ("neutral", None),  # balanced with mild inhibitory tone present
}


# ── Helpers ──────────────────────────────────────────────────────────────


def parents(emotion: str) -> tuple[str, str | None] | None:
    """Return (core, mid) for an emotion, or None if not tagged."""
    return EMOTION_PARENTS.get((emotion or "").lower())


def core_of(emotion: str) -> str:
    """Return the core for an emotion. 'neutral' for unknowns — never raises.
    Useful for log/eval grouping."""
    p = parents(emotion)
    return p[0] if p else "neutral"


# ── Valence per core emotion family ──────────────────────────────────────
# Maps each core in CORES to a scalar valence in roughly [-1, +1]. Used by
# Hebbian plasticity (sleep.py) AND the DMN speak gate to read whether the
# current emotion biases toward staying on topic (positive) or deflecting
# (negative). Single source of truth — DO NOT duplicate this table.
CORE_VALENCE: dict[str, float] = {
    "happy": +1.0,
    "sad": -1.0,
    "anger": -1.0,
    "fear": -0.5,
    "disgust": -0.5,
    "surprise": 0.0,
    "cognitive": +0.3,
    "neutral": 0.0,
}


def valence_of(emotion: str | None) -> float:
    """Return the valence (≈[-1, +1]) for any emotion label by resolving its
    core family. Unknown emotions and None map to 0.0 (neutral)."""
    return CORE_VALENCE.get(core_of(emotion or "neutral"), 0.0)


def lookup_with_inheritance(emotion: str, table: dict[str, str]) -> str | None:
    """Walk leaf → mid → core, returning the first non-empty hit in `table`.

    Drop-in for _V3_TAG_BY_EMOTION and _EXPRESSIVE_BY_EMOTION lookups:
    a leaf without an explicit entry inherits its mid's entry, which in turn
    inherits its core's entry. Empty strings (intentional "no tag") are
    treated as 'no result' so inheritance continues up — callers can still
    short-circuit on `key in table` if they want the literal empty.
    """
    key = (emotion or "").lower()
    if table.get(key):
        return table[key]
    p = parents(key)
    if not p:
        return None
    core, mid = p
    if mid and table.get(mid):
        return table[mid]
    if table.get(core):
        return table[core]
    return None


def all_leaves_for(core: str) -> list[str]:
    """All known labels whose core matches `core`. For log aggregation."""
    return [label for label, (c, _m) in EMOTION_PARENTS.items() if c == core]
