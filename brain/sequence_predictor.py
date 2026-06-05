"""Angle-sequence predictor — learns topic-transition N-grams from DMN thought angles.

Records each new angle as it's appended to the DMN's _recent_angles deque, builds bigram
and trigram frequency counts, and predicts which territory is likely to appear next. The
prediction is injected into the prefetcher prompt as a low-weight hint so context can be
warmed before the user arrives there.

Vocabulary note: angles are LLM-generated free text, so semantically identical concepts
may carry different labels ("architecture-tradeoffs", "system-architecture"). The _synonyms
dict bridges these once populated. A periodic LLM similarity pass (sleep task, future work)
will ingest the collected history and write second_brain/angle_synonyms.json, which this
module loads on startup.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, deque

logger = logging.getLogger(__name__)

# Honor the active persona's namespaced second-brain root (set in run.py
# _route_persona_state before this module imports), so each persona learns its
# own DMN thought-transition patterns instead of sharing one global memory.
# Falls back to the shared second_brain/ for the neutral (no-persona) brain.
_SECOND_BRAIN_ROOT = os.environ.get(
    "SECOND_BRAIN_PATH", os.path.join(os.path.dirname(__file__), "..", "second_brain")
)
_WEIGHTS_PATH = os.path.join(_SECOND_BRAIN_ROOT, "sequence_weights.json")
_SYNONYMS_PATH = os.path.join(_SECOND_BRAIN_ROOT, "angle_synonyms.json")
_MAX_HISTORY = 200
_MIN_CONFIDENCE = 0.35
_MIN_OBSERVATIONS = 2  # require at least this many observations before predicting

# Separator that can't appear in angle strings (which are lowercase-alpha-hyphen).
_SEP = "\x1f"


def _normalize(angle: str) -> str:
    """Drop rumination tags, lowercase, keep first two hyphen-segments.

    "user-creative-process" → "user-creative"
    "rumination:curiosity"  → "" (excluded — not a topic signal)
    "architecture"          → "architecture"
    """
    a = (angle or "").strip().lower()
    if not a or a.startswith("rumination:"):
        return ""
    parts = a.split("-")
    return "-".join(parts[:2]) if len(parts) > 2 else a


class SequencePredictor:
    """N-gram frequency predictor over normalized DMN thought angles."""

    # Minimum confidence before a prediction is worth acting on. Exposed as an
    # instance attribute so callers (e.g. the prefetcher) can gate on it.
    min_confidence: float = _MIN_CONFIDENCE

    def __init__(self) -> None:
        self._history: deque[str] = deque(maxlen=_MAX_HISTORY)
        self._bigrams: Counter = Counter()
        self._trigrams: Counter = Counter()
        self._synonyms: dict[str, str] = {}
        self._dirty = False

    # ── canonical form ───────────────────────────────────────────────────────

    def _canonical(self, angle: str) -> str:
        norm = _normalize(angle)
        return self._synonyms.get(norm, norm)

    # ── public API ───────────────────────────────────────────────────────────

    def record(self, angle: str) -> None:
        """Call each time a new angle is committed to _recent_angles."""
        canon = self._canonical(angle)
        if not canon:
            return
        hist = list(self._history)
        if len(hist) >= 1:
            self._bigrams[(hist[-1], canon)] += 1
        if len(hist) >= 2:
            self._trigrams[(hist[-2], hist[-1], canon)] += 1
        self._history.append(canon)
        self._dirty = True

    def predict(self) -> tuple[str | None, float]:
        """Return (predicted_next_angle, confidence) from recent history.

        Tries trigram first (more specific), falls back to bigram. Returns
        (None, 0.0) when there's not enough data or confidence is below threshold.
        """
        hist = list(self._history)
        if not hist:
            return None, 0.0

        # Trigram: last two angles → likely next
        if len(hist) >= 2:
            a, b = hist[-2], hist[-1]
            matching = {c: n for (x, y, c), n in self._trigrams.items() if x == a and y == b}
            total = sum(matching.values())
            if total >= _MIN_OBSERVATIONS:
                best = max(matching, key=matching.__getitem__)
                conf = matching[best] / total
                if conf >= _MIN_CONFIDENCE:
                    return best, conf

        # Bigram: last angle → likely next
        last = hist[-1]
        matching = {c: n for (x, c), n in self._bigrams.items() if x == last}
        total = sum(matching.values())
        if total >= _MIN_OBSERVATIONS:
            best = max(matching, key=matching.__getitem__)
            conf = matching[best] / total
            if conf >= _MIN_CONFIDENCE:
                return best, conf

        return None, 0.0

    def top_transitions(self, n: int = 10) -> list[dict]:
        """Return the N most frequent bigrams — useful for the LLM similarity pass
        and for observability (understanding what patterns have actually emerged)."""
        return [
            {"from": a, "to": b, "count": count}
            for (a, b), count in self._bigrams.most_common(n)
        ]

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> None:
        self._load_synonyms()
        try:
            path = os.path.abspath(_WEIGHTS_PATH)
            if not os.path.exists(path):
                return
            with open(path) as f:
                data = json.load(f)
            for a in data.get("history", []):
                if a:
                    self._history.append(str(a))
            self._bigrams = Counter(
                {tuple(k.split(_SEP, 1)): v for k, v in data.get("bigrams", {}).items() if k}
            )
            self._trigrams = Counter(
                {tuple(k.split(_SEP, 2)): v for k, v in data.get("trigrams", {}).items() if k}
            )
            logger.debug(
                "[SeqPredictor] Loaded: %d history, %d bigrams, %d trigrams",
                len(self._history),
                len(self._bigrams),
                len(self._trigrams),
            )
        except Exception as e:
            logger.warning("[SeqPredictor] Load failed: %s", e)

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            path = os.path.abspath(_WEIGHTS_PATH)
            data = {
                "history": list(self._history),
                "bigrams": {_SEP.join(k): v for k, v in self._bigrams.items()},
                "trigrams": {_SEP.join(k): v for k, v in self._trigrams.items()},
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            self._dirty = False
        except Exception as e:
            logger.warning("[SeqPredictor] Save failed: %s", e)

    def _load_synonyms(self) -> None:
        try:
            path = os.path.abspath(_SYNONYMS_PATH)
            if not os.path.exists(path):
                return
            with open(path) as f:
                self._synonyms = json.load(f)
            logger.debug("[SeqPredictor] Loaded %d synonyms", len(self._synonyms))
        except Exception as e:
            logger.warning("[SeqPredictor] Synonyms load failed: %s", e)
