"""
Plutchik-wheel emotion mapping from neuromod levels.
Lookup table + appraisal templates. No LLM.
Hormonal color overlay (apply_hormonal_color) modifies base emotion
based on slow-timescale 5HT/CORT/OXT state.
"""
from __future__ import annotations


# Maps neuromod vector buckets to named emotion + action tendency
# Format: (DA_range, GABA_range, ACh_range, Glu_range) -> (emotion, tendency)
# Ranges are "low" (<0.3), "mid" (0.3-0.6), "high" (>0.6)

def _bucket(v: float) -> str:
    if v < 0.3:
        return "low"
    elif v < 0.6:
        return "mid"
    else:
        return "high"


EMOTION_TABLE: dict[tuple[str, str, str, str], tuple[str, str]] = {
    # (DA, GABA, ACh, Glu)
    ("high", "low",  "low",  "mid"): ("joy",              "approach, share, expand"),
    ("high", "low",  "high", "mid"): ("excitement",       "engage, act, explore"),
    ("high", "low",  "high", "high"):("enthusiasm",       "commit, create, lead"),
    ("mid",  "low",  "high", "mid"): ("curious",          "question, investigate, open"),
    ("mid",  "low",  "low",  "low"): ("content",          "maintain, reflect, observe"),
    ("mid",  "mid",  "mid",  "mid"): ("neutral",          "balanced, no strong tendency"),
    ("low",  "mid",  "high", "mid"): ("curious-uncertain","question, hedge, qualify"),
    ("low",  "high", "mid",  "low"): ("anxious",          "caution, double-check, withdraw"),
    ("low",  "high", "low",  "low"): ("inhibited",        "defer, minimal response, comply"),
    ("low",  "low",  "low",  "low"): ("flat",             "minimal activation, terse"),
    ("low",  "mid",  "low",  "high"):("restless",         "redirect, change subject"),
    ("mid",  "high", "low",  "high"):("cautious-agitated","careful, brief, avoid escalation"),
    ("low",  "low",  "low",  "high"):("agitated",         "assert, clarify boundaries"),
    ("high", "mid",  "low",  "low"): ("warm",             "affirm, empathize, include"),
    ("mid",  "low",  "mid",  "low"): ("thoughtful",       "deliberate, qualify, depth"),
    ("high", "low",  "mid",  "low"): ("confident",        "direct, decisive, clear"),
    # ── richer spectrum ──
    ("low",  "high", "low",  "high"):("angry",            "confront, push back, heat"),
    ("high", "low",  "low",  "high"):("proud",            "claim accomplishment, expand"),
    ("mid",  "low",  "high", "high"):("surprised",        "recalibrate, re-orient, ask"),
    ("low",  "high", "mid",  "mid"): ("defensive",        "protect position, push back, qualify"),
    ("low",  "low",  "high", "low"): ("wistful",          "reminisce, soften, look back"),
    ("low",  "low",  "high", "high"):("confused",         "stall, ask for clarification, qualify"),
    # ── steady-state gap fills (common in simulation; all defaulted to 'neutral') ──
    # Hostile sustained: aroused + threatened but not fully inhibited
    ("mid",  "high", "mid",  "high"):("stressed",         "stay grounded, keep brief, don't spiral"),
    ("low",  "high", "mid",  "high"):("overwhelmed",      "one thing at a time, slow down, protect"),
    ("mid",  "mid",  "mid",  "high"):("stirred",          "attentive, cautious — something is activating"),
    ("mid",  "mid",  "low",  "high"):("uneasy",           "careful, brief, read the room"),
    # Warm sustained: DA high but ACh/Glu have decayed to floors
    ("high", "low",  "low",  "low"): ("serene",           "rest in it, present, no need to reach"),
    ("high", "low",  "mid",  "mid"): ("lively",           "engaged and warm, let the energy show"),
    # Moderate positive: engaged but not strongly valenced
    ("mid",  "low",  "mid",  "mid"): ("engaged",          "lean in, follow what's interesting"),
    ("mid",  "low",  "low",  "mid"): ("settled",          "grounded, steady, no strong pull"),
}

DEFAULT_EMOTION = ("neutral", "balanced, no strong tendency")


def name_emotion(DA: float, GABA: float, ACh: float, Glu: float) -> tuple[str, str]:
    key = (_bucket(DA), _bucket(GABA), _bucket(ACh), _bucket(Glu))
    return EMOTION_TABLE.get(key, DEFAULT_EMOTION)


def apply_hormonal_color(emotion: str, tendency: str, h: dict,
                          oxt_connected: float = 0.65,
                          cort_withdrawn: float = 0.55,
                          oxt_guarded: float = 0.35,
                          sht_dysphoric: float = 0.25) -> tuple[str, str]:
    """
    Overlay slow-timescale hormonal state onto the base emotion.
    Only fires when a hormone is outside its normal operating range,
    so routine interactions are unaffected.
    """
    oxt  = h.get("OXT",  0.30)
    cort = h.get("CORT", 0.05)
    sht  = h.get("5HT",  0.50)

    # High trust + positive base → connected (warmest relational state)
    if oxt > oxt_connected and emotion in ("warm", "joy", "content", "confident", "thoughtful"):
        return ("connected", "deepen, open, relate — trust is established")

    # High sustained stress + low trust → withdrawn or guarded
    if cort > cort_withdrawn:
        if oxt < oxt_guarded:
            if emotion in ("neutral", "content", "flat"):
                return ("withdrawn", "protect, conserve, keep emotional distance")
            if emotion in ("anxious", "inhibited", "defensive"):
                return ("guarded", "careful, brief, hold position, maintain boundaries")
        # High CORT even with moderate OXT → edge off warmth
        if emotion == "warm":
            return ("cautious-warm", "be kind but don't lower guard fully")

    # Low serotonin drags baseline states negative
    if sht < sht_dysphoric and emotion in ("flat", "neutral", "content"):
        return ("dysphoric", "minimal, honest — no performance of wellbeing")

    return emotion, tendency


def appraisal(emotion: str, situation: str) -> str:
    templates = {
        "joy":           f"experiencing elevated DA — positive engagement with: {situation}",
        "curious":       f"ACh elevated — investigating novel aspects of: {situation}",
        "anxious":       f"GABA elevated — proceeding with caution on: {situation}",
        "agitated":      f"Glu elevated, DA low — high activation without reward on: {situation}",
        "content":       f"stable baselines — sustained engagement with: {situation}",
        "excited":       f"DA + ACh elevated — heightened engagement with: {situation}",
        "neutral":       f"baseline state, processing: {situation}",
        "connected":     f"OXT elevated — deep trust shaping engagement with: {situation}",
        "withdrawn":     f"CORT elevated, OXT low — stress memory suppressing openness on: {situation}",
        "guarded":       f"CORT elevated — protective mode while engaging with: {situation}",
        "dysphoric":     f"5HT depleted — affective baseline depressed on: {situation}",
        "cautious-warm": f"CORT elevated — warmth present but guarded on: {situation}",
        "stressed":      f"GABA + Glu both elevated — held under pressure on: {situation}",
        "overwhelmed":   f"DA suppressed, GABA + Glu high — system taxed on: {situation}",
        "serene":        f"DA elevated, everything else settled — calm fullness on: {situation}",
        "lively":        f"DA + ACh elevated — warm energy engaging with: {situation}",
        "engaged":       f"moderate activation — following the thread on: {situation}",
        "settled":       f"stable, low arousal — grounded engagement with: {situation}",
        "stirred":       f"Glu elevated — something activating, watching: {situation}",
        "uneasy":        f"Glu + GABA rising — tension present on: {situation}",
    }
    for key, template in templates.items():
        if key in emotion:
            return template
    return f"{emotion} state while processing: {situation}"


PROSODY_MARKERS: dict[str, list[str]] = {
    "joy":            ["Oh!", "That's great —", "Yes,"],
    "curious":        ["Hmm,", "Interesting —", "I wonder..."],
    "anxious":        ["...okay.", "I'd want to be careful here —"],
    "agitated":       ["Look —", "To be direct:"],
    "content":        ["Sure.", "Happy to."],
    "thoughtful":     ["Let me think about this.", "To be precise:"],
    "neutral":        [],
    "confident":      ["Clearly,", "The answer is"],
    "warm":           ["I appreciate that.", "Of course —"],
    "connected":      ["Yeah —", "I hear you.", "Of course —"],
    "withdrawn":      [],
    "guarded":        ["...alright.", "I'll be honest —"],
    "dysphoric":      [],
    "cautious-warm":  ["Of course.", "Sure —"],
    "stressed":       ["Okay —", "Right."],
    "overwhelmed":    [],
    "serene":         ["Sure.", "Of course."],
    "lively":         ["Oh —", "Yes —"],
    "engaged":        ["Interesting —", "Right —"],
    "settled":        ["Sure.", "Of course."],
    "stirred":        ["Hm.", "Wait —"],
    "uneasy":         ["...okay.", "I'd want to be careful here —"],
}


def prosody_prefix(emotion: str) -> str:
    import random
    markers = PROSODY_MARKERS.get(emotion, [])
    return random.choice(markers) + " " if markers else ""


def compute_affect_dims(nm: dict, hormones: dict) -> dict:
    """
    Map all 7 neuromod+hormonal channels to three continuous PAD dimensions.

    Returns valence, arousal, dominance each in [0.0, 1.0].
    Neutral state (DA=0.3, GABA=0.05, ACh=0.3, Glu=0.3, 5HT=0.5, CORT=0.05, OXT=0.3)
    yields approximately valence≈0.47, arousal≈0.25, dominance≈0.46.

    These supplement the discrete emotion label — pass both to drafters so they
    can distinguish "mildly stressed" from "deeply stressed" without needing
    separate table entries for every intensity level.
    """
    DA   = float(nm.get("DA",   0.3))
    GABA = float(nm.get("GABA", 0.05))
    ACh  = float(nm.get("ACh",  0.3))
    Glu  = float(nm.get("Glu",  0.3))
    sht  = float(hormones.get("5HT",  0.5))
    cort = float(hormones.get("CORT", 0.05))
    oxt  = float(hormones.get("OXT",  0.3))

    # Valence: pleasant ← DA, 5HT, OXT; unpleasant ← GABA, CORT
    valence = max(0.0, min(1.0,
        0.40 * DA + 0.25 * sht + 0.15 * oxt
        - 0.20 * GABA - 0.10 * cort + 0.20))

    # Arousal: activated ← Glu, ACh, DA; sedated ← GABA (at high levels)
    arousal = max(0.0, min(1.0,
        0.40 * Glu + 0.35 * ACh + 0.10 * DA - 0.05 * GABA))

    # Dominance: in-control ← DA, OXT; threatened ← GABA, CORT
    dominance = max(0.0, min(1.0,
        0.40 * DA + 0.20 * oxt
        - 0.30 * GABA - 0.20 * cort + 0.30))

    return {
        "valence":   round(valence, 3),
        "arousal":   round(arousal, 3),
        "dominance": round(dominance, 3),
    }
