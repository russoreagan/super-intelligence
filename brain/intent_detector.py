"""Embedding-based intent detection with a learned, growing exemplar bank.

Replaces brittle hardcoded phrase lists for the fast-path intent gates
(self_reference, epistemic_action, …). Each intent holds a small bank of exemplar
phrasings; the input is matched by cosine similarity against them, so paraphrases
the seed list never anticipated still fire. The bank starts from seed phrases and
**grows over time**: whenever the understanding integrator (the LLM) confirms an
intent that the embedding match missed, that phrasing is added as a new exemplar —
so the expensive LLM judgment is needed less with use, and detection improves on
this user's actual phrasings. Degrades gracefully to the literal seed list when no
embedder is available or the feature is disabled.

This keeps an LLM off the per-turn critical path (the match is a single local
embedding plus cosine), while still getting LLM-quality coverage via the cache that
the integrator teaches on the turns it already runs.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from brain.settings import settings

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class _PersonaIntentState:
    """One persona's exemplar bank + per-turn detection cache."""

    __slots__ = ("bank", "dirty", "seeded", "last_vec", "last_text", "last_fired")

    def __init__(self, seeds: dict[str, list[str]]) -> None:
        self.bank: dict[str, list[dict]] = {k: [] for k in seeds}
        self.dirty = False
        self.seeded = False  # True once the bank has been populated (loaded or embedded)
        self.last_vec: list[float] | None = None
        self.last_text: str = ""
        self.last_fired: dict[str, bool] = {}


class IntentDetector:
    """Per-intent exemplar banks matched by embedding cosine, with a literal-seed
    fast path / fallback and an LLM-taught growth loop. Banks are PER PERSONA
    (each grows from its own traffic), resolved from the turn binding at each
    access — the constructor path serves the home persona; bound personas route
    to their own intent_bank.json (persona_state_root)."""

    def __init__(self, bank_path: str | Path, seeds: dict[str, list[str]]) -> None:
        self._path = Path(bank_path)
        self._seeds = {k: [s.lower() for s in v] for k, v in seeds.items()}
        # Persona → state; each bundle hydrates from its persisted bank on first
        # access (_state), so no eager load here.
        self._by_persona: dict[str, _PersonaIntentState] = {}

    def _active(self) -> str:
        try:
            from brain.second_brain.store import active_persona

            return active_persona() or ""
        except Exception:
            return ""

    def _state(self) -> _PersonaIntentState:
        try:
            from brain.persona_key import persona_slug

            key = persona_slug(self._active())
        except Exception:
            key = ""
        st = self._by_persona.get(key)
        if st is None:
            st = self._by_persona[key] = _PersonaIntentState(self._seeds)
            self._load()  # lazily hydrate a first-seen persona's persisted bank
        return st

    def _bank_path(self) -> Path:
        persona = self._active()
        if persona:
            try:
                from brain.persona_key import persona_state_root

                return persona_state_root(persona) / "intent_bank.json"
            except Exception:
                pass
        return self._path

    # Legacy attribute names — method bodies address the ACTIVE persona's state.
    @property
    def _bank(self) -> dict[str, list[dict]]:
        return self._state().bank

    @property
    def _dirty(self) -> bool:
        return self._state().dirty

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._state().dirty = value

    @property
    def _seeded(self) -> bool:
        return self._state().seeded

    @_seeded.setter
    def _seeded(self, value: bool) -> None:
        self._state().seeded = value

    @property
    def _last_vec(self) -> list[float] | None:
        return self._state().last_vec

    @_last_vec.setter
    def _last_vec(self, value: list[float] | None) -> None:
        self._state().last_vec = value

    @property
    def _last_text(self) -> str:
        return self._state().last_text

    @_last_text.setter
    def _last_text(self, value: str) -> None:
        self._state().last_text = value

    @property
    def _last_fired(self) -> dict[str, bool]:
        return self._state().last_fired

    @_last_fired.setter
    def _last_fired(self, value: dict[str, bool]) -> None:
        self._state().last_fired = value

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(self._bank_path().read_text(encoding="utf-8"))
            for k, items in data.items():
                self._bank[k] = [{"t": i["t"], "v": i["v"]} for i in items if i.get("v")]
            if any(self._bank.values()):
                self._seeded = True
        except Exception:
            pass  # no bank yet — seeded lazily on first detect

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            path = self._bank_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._bank), encoding="utf-8")
            self._dirty = False
        except Exception as e:
            logger.debug("[IntentDetector] save failed: %s", e)

    # ── matching ─────────────────────────────────────────────────────────────
    def _literal(self, text: str, intent: str) -> bool:
        t = text.lower()
        return any(p in t for p in self._seeds.get(intent, []))

    async def _ensure_seeded(self, embed_fn) -> None:
        """Embed the seed phrases into any empty bank so semantic matching works from
        the first turn, not only literal matching. One-time per persona; persisted."""
        if self._seeded:
            return
        for intent, phrases in self._seeds.items():
            if self._bank.get(intent):
                continue
            for p in phrases:
                try:
                    v = await embed_fn(p)
                except Exception:
                    v = None
                if v:
                    self._bank[intent].append({"t": p, "v": list(v)})
                    self._dirty = True
        self._seeded = True
        self.save()

    async def detect_all(self, text: str, embed_fn) -> dict[str, bool]:
        """Return {intent: fired}. Embeds the input once and shares it across intents;
        a literal seed hit short-circuits. Falls back to literal-only when disabled or
        no embedder is available."""
        if not settings.get("intent_detector_enabled", 1):
            return {k: self._literal(text, k) for k in self._seeds}

        fired = {k: self._literal(text, k) for k in self._seeds}
        self._last_vec = None
        self._last_text = text

        if not all(fired.values()) and embed_fn is not None:
            await self._ensure_seeded(embed_fn)
            try:
                vec = await embed_fn(text)
            except Exception:
                vec = None
            if vec:
                self._last_vec = list(vec)
                thr = float(settings.get("intent_fire_threshold", 0.62))
                for intent in self._seeds:
                    if fired[intent]:
                        continue
                    best = max(
                        (_cosine(vec, e["v"]) for e in self._bank.get(intent, [])),
                        default=0.0,
                    )
                    if best >= thr:
                        fired[intent] = True

        self._last_fired = dict(fired)
        return fired

    # ── learning ─────────────────────────────────────────────────────────────
    def learn_from_llm(self, llm_flags: dict[str, bool]) -> None:
        """Add exemplars for intents the LLM confirmed this turn but the embedding
        match missed. Reuses the input vector from the preceding detect_all (no extra
        embedding), so learning is free on the turns the integrator already ran."""
        if self._last_vec is None:
            return
        dedup = float(settings.get("intent_dedup_threshold", 0.95))
        cap = int(settings.get("intent_bank_max", 200))
        added = False
        for intent, is_true in llm_flags.items():
            if not is_true or self._last_fired.get(intent):
                continue  # only genuine misses — LLM said yes, we said no
            bank = self._bank.setdefault(intent, [])
            if any(_cosine(self._last_vec, e["v"]) >= dedup for e in bank):
                continue  # already covered by a near-duplicate exemplar
            bank.append({"t": self._last_text, "v": self._last_vec})
            if len(bank) > cap:
                bank.pop(0)  # FIFO eviction bounds bank size / match cost
            added = True
        if added:
            self._dirty = True
            self.save()
