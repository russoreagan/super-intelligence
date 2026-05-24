"""
PredictorSwitch + CompositePredictor — predict-and-surprise gating (Active Inference).
Each cluster gets one predictor. On low surprise, integrators stay asleep.
On high surprise, the integrator wakes with the failed prediction as context.

_should_bypass_gating() is the single emotion-aware veto: when the entity or
user is in a non-routine emotional state, force the integrator to wake — the
prediction is statistically valid but morally wrong (the moment deserves
fresh attention, not a cached response).
"""
from __future__ import annotations

from collections import deque, Counter
from dataclasses import dataclass, field
from typing import Hashable
from brain.settings import settings as _settings

# Emotional states where the entity should NOT lean on prediction. These are
# either reactive (responding to something fresh) or relational (where stale
# behavior reads as cold).
_FRESH_ATTENTION_EMOTIONS = frozenset({
    "angry", "defensive", "frustrated", "irritated", "sympathetic",
    "embarrassed", "apologetic", "flirty", "surprised", "sad", "melancholy",
    "tender", "shy",
})

# User emotional states that deserve the full Multiple Drafts engine — no
# shortcuts.
_USER_EMOTIONS_NEEDING_CARE = frozenset({
    "distressed", "sad", "frustrated", "hostile", "overwhelmed",
    "anxious", "angry", "upset",
})


@dataclass
class PredictorSwitch:
    """
    Maintains a short history of (input_features → output_tag) pairs.
    Predicts the likely output tag for the next input using last-N frequency.
    """
    name: str
    cluster: str
    window: int = field(default=8)
    surprise_threshold: float = field(default=0.4)

    def __post_init__(self):
        self.window = int(_settings.get("predictor_window"))
        self.surprise_threshold = float(_settings.get("surprise_threshold"))
        self._history = deque(maxlen=self.window)

    _history: deque = field(default_factory=lambda: deque(maxlen=8), init=False)

    def record(self, input_signature: str, output_tag: str) -> None:
        self._history.append((input_signature, output_tag))

    def predict(self, input_signature: str) -> tuple[str | None, float]:
        """Returns (predicted_tag, confidence). Confidence 0 = no data."""
        if not self._history:
            return None, 0.0
        matching = [tag for sig, tag in self._history if sig == input_signature]
        if not matching:
            counts = Counter(tag for _, tag in self._history)
            best, n = counts.most_common(1)[0]
            return best, n / len(self._history) * 0.5  # penalty for no signature match
        counts = Counter(matching)
        best, n = counts.most_common(1)[0]
        return best, n / len(matching)

    def surprise(self, predicted_tag: str | None, actual_tag: str,
                 predicted_confidence: float) -> float:
        """Surprise ∈ [0, 1]. High surprise = prediction wrong or low confidence."""
        if predicted_tag is None:
            return 1.0
        if predicted_tag == actual_tag:
            return 1.0 - predicted_confidence
        return min(1.0, 0.6 + (1.0 - predicted_confidence) * 0.4)

    def should_wake_integrator(self, surprise_score: float) -> bool:
        return surprise_score >= self.surprise_threshold


@dataclass
class CompositePredictor:
    """
    Predicts a structured label (tuple) from a structured input signature (tuple).
    Used where prediction is over a richer feature vector than a single string.

    Example: signature = (intent, register, has_memory, DA_bucket, GABA_bucket)
             label     = (response_type, target_length, tone)
    """
    name: str
    cluster: str
    window: int = field(default=12)
    surprise_threshold: float = field(default=0.4)
    confidence_skip_threshold: float = field(default=0.7)

    def __post_init__(self):
        self.surprise_threshold = float(_settings.get("surprise_threshold"))
        self.confidence_skip_threshold = float(_settings.get("confidence_skip_threshold"))
        self._history = deque(maxlen=self.window)
        self._outcome_scores = {}

    _history: deque = field(default_factory=lambda: deque(maxlen=12), init=False)
    # Track per-signature outcome history for "consistently high score" checks
    _outcome_scores: dict[tuple, deque] = field(default_factory=dict, init=False)

    def record(self, sig: tuple[Hashable, ...], label: tuple[Hashable, ...]) -> None:
        self._history.append((tuple(sig), tuple(label)))

    def record_outcome(self, sig: tuple[Hashable, ...], score: float) -> None:
        """Record a quality score for a signature → used by critic-skip predictor."""
        key = tuple(sig)
        if key not in self._outcome_scores:
            self._outcome_scores[key] = deque(maxlen=6)
        self._outcome_scores[key].append(float(score))

    def predict(self, sig: tuple[Hashable, ...]) -> tuple[tuple | None, float]:
        if not self._history:
            return None, 0.0
        key = tuple(sig)
        matching = [label for s, label in self._history if s == key]
        if not matching:
            counts = Counter(label for _, label in self._history)
            best, n = counts.most_common(1)[0]
            return best, n / len(self._history) * 0.4
        counts = Counter(matching)
        best, n = counts.most_common(1)[0]
        return best, n / len(matching)

    def surprise(self, predicted: tuple | None, actual: tuple,
                 confidence: float) -> float:
        if predicted is None:
            return 1.0
        if predicted == actual:
            return 1.0 - confidence
        # Partial credit: count fraction of matching positions
        match_frac = sum(1 for a, b in zip(predicted, actual) if a == b) / max(len(predicted), 1)
        miss = 1.0 - match_frac
        return min(1.0, 0.4 + miss * 0.5 + (1.0 - confidence) * 0.1)

    def should_skip_integrator(self, predicted: tuple | None, confidence: float) -> bool:
        """Stronger gate than should_wake_integrator: requires both low expected
        surprise AND high confidence before we actually skip the LLM."""
        if predicted is None:
            return False
        return confidence >= self.confidence_skip_threshold

    def avg_recent_outcome(self, sig: tuple[Hashable, ...]) -> float | None:
        """Mean recent quality score for a signature, or None if no data."""
        key = tuple(sig)
        scores = self._outcome_scores.get(key)
        if not scores:
            return None
        return sum(scores) / len(scores)


def input_signature(text: str) -> str:
    """Cheap structural fingerprint of input text (no LLM)."""
    words = text.strip().split()
    length_bucket = "tiny" if len(words) <= 3 else "short" if len(words) <= 15 else "long"
    has_question = "?" in text
    has_memory_ref = any(w in text.lower() for w in ("remember", "told you", "last", "before", "that", "what was"))
    return f"{length_bucket}|q={has_question}|mem={has_memory_ref}"


def _neuromod_bucket(level: float) -> str:
    """Bucket a neuromod level into low/mid/high for predictor signatures."""
    if level < 0.25:
        return "low"
    if level < 0.55:
        return "mid"
    return "high"


def composite_signature(features: dict, affect: dict | None = None) -> tuple:
    """Build a CompositePredictor signature from temporal features + affect."""
    affect = affect or {}
    nm = affect.get("neuromod") or {}
    return (
        features.get("intent", "other"),
        features.get("register", "casual"),
        bool(features.get("requires_memory")),
        _neuromod_bucket(nm.get("DA", 0.5)),
        _neuromod_bucket(nm.get("GABA", 0.0)),
    )


def should_bypass_gating(affect: dict | None, features: dict | None) -> tuple[bool, str]:
    """
    The emotion-aware veto: when the moment deserves fresh attention regardless
    of how predictable it looks, return (True, reason). Every cluster's
    predictor consults this before checking surprise.

    Returns (False, "") when gating is safe.
    """
    affect = affect or {}
    features = features or {}

    if affect.get("high_GABA"):
        return True, "high_GABA"

    emotion = (affect.get("emotion") or "").lower()
    if emotion in _FRESH_ATTENTION_EMOTIONS:
        return True, f"emotion={emotion}"

    user_emotion = (features.get("user_emotion") or "").lower()
    if user_emotion in _USER_EMOTIONS_NEEDING_CARE:
        return True, f"user_emotion={user_emotion}"

    vocal_tone = (affect.get("vocal_tone") or "").lower()
    if vocal_tone in ("stressed", "whisper"):
        return True, f"vocal_tone={vocal_tone}"

    if features.get("_enrollment_result") or features.get("_enrollment_results"):
        return True, "enrollment_active"

    return False, ""
