"""
Plutchik-wheel emotion mapping from neuromod levels.
Lookup table + appraisal templates. No LLM.
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
}

DEFAULT_EMOTION = ("neutral", "balanced, no strong tendency")


def name_emotion(DA: float, GABA: float, ACh: float, Glu: float) -> tuple[str, str]:
    key = (_bucket(DA), _bucket(GABA), _bucket(ACh), _bucket(Glu))
    return EMOTION_TABLE.get(key, DEFAULT_EMOTION)


def appraisal(emotion: str, situation: str) -> str:
    templates = {
        "joy":       f"experiencing elevated DA — positive engagement with: {situation}",
        "curious":   f"ACh elevated — investigating novel aspects of: {situation}",
        "anxious":   f"GABA elevated — proceeding with caution on: {situation}",
        "agitated":  f"Glu elevated, DA low — high activation without reward on: {situation}",
        "content":   f"stable baselines — sustained engagement with: {situation}",
        "excited":   f"DA + ACh elevated — heightened engagement with: {situation}",
        "neutral":   f"baseline state, processing: {situation}",
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
}


def prosody_prefix(emotion: str) -> str:
    import random
    markers = PROSODY_MARKERS.get(emotion, [])
    return random.choice(markers) + " " if markers else ""
