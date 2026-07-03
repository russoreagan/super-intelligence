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
        self._syn_mtime: float = 0.0
        # Keys other modules keep in sequence_weights.json (e.g. the sleep pass's
        # last_synonym_pass_ts) — carried through save() so we never drop them.
        self._extra: dict = {}
        self._dirty = False
        # The persona whose thought stream this instance belongs to. The DMN keeps
        # one predictor per persona (_PerPersona bundle, constructed under that
        # persona's binding), so capture the binding at construction — persistence
        # then routes to that persona's own files instead of everyone clobbering
        # the home persona's (last-saver-wins, and every persona booted with
        # home's history). Empty (unbound/tests/single-persona) → home paths.
        try:
            from brain.second_brain.store import active_persona

            self._persona: str = active_persona() or ""
        except Exception:
            self._persona = ""

    # ── persona-routed paths (resolved at call time, see persona_state_root) ──

    def _weights_path(self) -> str:
        from brain.persona_key import persona_state_root

        return str(persona_state_root(self._persona) / "sequence_weights.json")

    def _synonyms_path(self) -> str:
        from brain.persona_key import persona_state_root

        return str(persona_state_root(self._persona) / "angle_synonyms.json")

    # ── canonical form ───────────────────────────────────────────────────────

    def _canonical(self, angle: str) -> str:
        # mtime-checked so a sleep-pass rewrite of angle_synonyms.json takes
        # effect in the SAME session (previously synonyms loaded only at startup,
        # giving the clustering a one-full-session latency).
        self._load_synonyms()
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

    def informativeness(self) -> float:
        """How non-trivial a correct next-angle prediction is right now, ∈ [0, 1].
        = 1 − dominant_next_frequency over the outcomes following the current context (trigram
        first, then bigram). A context whose next angle is near-constant → ~0 (predicting it is
        trivial); a context with varied continuations → higher. Used by neuron.prediction_reward
        so a constant-angle loop can't farm reward."""
        hist = list(self._history)
        if not hist:
            return 0.0
        if len(hist) >= 2:
            a, b = hist[-2], hist[-1]
            matching = {c: n for (x, y, c), n in self._trigrams.items() if x == a and y == b}
            total = sum(matching.values())
            if total >= _MIN_OBSERVATIONS:
                return 1.0 - max(matching.values()) / total
        last = hist[-1]
        matching = {c: n for (x, c), n in self._bigrams.items() if x == last}
        total = sum(matching.values())
        if total >= _MIN_OBSERVATIONS:
            return 1.0 - max(matching.values()) / total
        return 0.0

    def top_transitions(self, n: int = 10) -> list[dict]:
        """Return the N most frequent bigrams — useful for the LLM similarity pass
        and for observability (understanding what patterns have actually emerged)."""
        return [
            {"from": a, "to": b, "count": count} for (a, b), count in self._bigrams.most_common(n)
        ]

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> None:
        self._load_synonyms()
        try:
            path = os.path.abspath(self._weights_path())
            if not os.path.exists(path):
                return
            with open(path) as f:
                data = json.load(f)
            self._history.clear()  # idempotent: a re-load must not duplicate history
            for a in data.get("history", []):
                if a:
                    self._history.append(str(a))
            self._bigrams = Counter(
                {tuple(k.split(_SEP, 1)): v for k, v in data.get("bigrams", {}).items() if k}
            )
            self._trigrams = Counter(
                {tuple(k.split(_SEP, 2)): v for k, v in data.get("trigrams", {}).items() if k}
            )
            self._extra = {
                k: v for k, v in data.items() if k not in ("history", "bigrams", "trigrams")
            }
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
            path = os.path.abspath(self._weights_path())
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Pick up bookkeeping another writer (the sleep synonym pass) may have
            # stamped since our load, then overlay our learned state on top.
            try:
                if os.path.exists(path):
                    with open(path) as f:
                        on_disk = json.load(f)
                    for k, v in on_disk.items():
                        if k not in ("history", "bigrams", "trigrams"):
                            self._extra[k] = v
            except Exception:
                pass
            data = {
                **self._extra,
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
        """(Re)load the synonym map when the file is new or changed — cheap stat
        per call, so hot callers (_canonical) can invoke it freely."""
        try:
            path = os.path.abspath(self._synonyms_path())
            if not os.path.exists(path):
                return
            mtime = os.stat(path).st_mtime
            if mtime == self._syn_mtime:
                return
            with open(path) as f:
                self._synonyms = json.load(f)
            self._syn_mtime = mtime
            logger.debug("[SeqPredictor] Loaded %d synonyms", len(self._synonyms))
        except Exception as e:
            logger.warning("[SeqPredictor] Synonyms load failed: %s", e)
