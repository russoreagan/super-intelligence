"""
PredictorSwitch + ComparatorSwitch — predict-and-surprise gating (Active Inference).
Each cluster gets one predictor. On low surprise, integrators stay asleep.
On high surprise, the integrator wakes with the failed prediction as context.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class PredictorSwitch:
    """
    Maintains a short history of (input_features → output_tag) pairs.
    Predicts the likely output tag for the next input using last-N frequency.
    """
    name: str
    cluster: str
    window: int = 8
    surprise_threshold: float = 0.4   # prediction error above this wakes integrator

    _history: deque = field(default_factory=lambda: deque(maxlen=8), init=False)

    def record(self, input_signature: str, output_tag: str) -> None:
        self._history.append((input_signature, output_tag))

    def predict(self, input_signature: str) -> tuple[str | None, float]:
        """Returns (predicted_tag, confidence). Confidence 0 = no data."""
        if not self._history:
            return None, 0.0
        matching = [tag for sig, tag in self._history if sig == input_signature]
        if not matching:
            # fall back to overall most-common
            from collections import Counter
            counts = Counter(tag for _, tag in self._history)
            best, n = counts.most_common(1)[0]
            return best, n / len(self._history) * 0.5  # penalty for no signature match
        from collections import Counter
        counts = Counter(matching)
        best, n = counts.most_common(1)[0]
        return best, n / len(matching)

    def surprise(self, predicted_tag: str | None, actual_tag: str,
                 predicted_confidence: float) -> float:
        """
        Surprise ∈ [0, 1]. High surprise = prediction wrong or low confidence.
        """
        if predicted_tag is None:
            return 1.0
        if predicted_tag == actual_tag:
            # Correct prediction: surprise ∝ (1 - confidence)
            return 1.0 - predicted_confidence
        else:
            # Wrong prediction
            return min(1.0, 0.6 + (1.0 - predicted_confidence) * 0.4)

    def should_wake_integrator(self, surprise_score: float) -> bool:
        return surprise_score >= self.surprise_threshold


def input_signature(text: str) -> str:
    """Cheap structural fingerprint of input text (no LLM)."""
    words = text.strip().split()
    length_bucket = "tiny" if len(words) <= 3 else "short" if len(words) <= 15 else "long"
    has_question = "?" in text
    has_memory_ref = any(w in text.lower() for w in ("remember", "told you", "last", "before", "that", "what was"))
    return f"{length_bucket}|q={has_question}|mem={has_memory_ref}"
