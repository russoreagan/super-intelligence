"""
Per-(persona, end_user) chemistry registry — the engine-mode contract.

Companion mode (the deployed single-persona product) never touches this: turns
pass no ``end_user_id``, so no client pair is ever resolved and the brain uses
its single resting chemistry exactly as before. This module only comes alive
when an engine/API caller threads an ``end_user_id`` into a turn, i.e. when one
persona process serves many of a partner's customers.

Responsibilities (see reports/per_client_chemistry_design.md):
  • get_or_create(end_user_id) — a ChemPair per customer, seeded from the persona
    temperament baseline on first contact, or restored from a persisted snapshot
    with **absence-decay** (time away relaxes mood toward the temperament
    baseline, mirroring relationship.apply_absence for bond/affection).
  • persist(end_user_id) — snapshot the customer's mood to durable storage.
  • weighted_average(...) — the step-4 aggregate of the cycle's client moods,
    interaction-mass weighted, returned as a snapshot to blend into the persona
    RESTING mood. This is one-way by construction: there is deliberately NO method
    that writes an aggregate back onto a client pair, so the day's overall mood can
    never seed an individual customer's session.

Storage is behind the ``ChemStore`` protocol; the default is in-memory. A durable
(Supabase/relationship-store) backend implements the same three methods and plugs
in without changing the contract.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from brain.bus import Bus, ChemPair

# Reference cadence: how many seconds of absence equals one "turn" of decay
# toward baseline. Mood relaxes over minutes, so a few minutes away ≈ a turn.
_DEFAULT_ABSENCE_TURN_S = 180.0


@runtime_checkable
class ChemStore(Protocol):
    """Durable persistence for a customer's chemistry snapshot. Keyed by a string
    (typically ``f"{persona}:{end_user_id}"``). Implementations must be safe to
    call with unknown keys (return (None, None))."""

    def load(self, key: str) -> tuple[dict | None, float | None]:
        """Return (snapshot, last_seen_ts) for ``key``, or (None, None) if unseen."""
        ...

    def save(self, key: str, snapshot: dict, last_seen_ts: float) -> None:
        """Persist a snapshot + last-seen timestamp for ``key``."""
        ...


class InMemoryChemStore:
    """Default ChemStore — process-local dict. Used in tests and as the companion
    fallback. Durable backends (Supabase, relationship store) mirror this API."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[dict, float]] = {}

    def load(self, key: str) -> tuple[dict | None, float | None]:
        rec = self._data.get(key)
        return (rec[0], rec[1]) if rec else (None, None)

    def save(self, key: str, snapshot: dict, last_seen_ts: float) -> None:
        self._data[key] = (snapshot, float(last_seen_ts))


class ClientChemRegistry:
    """Owns the live per-customer ChemPairs for one persona process."""

    def __init__(
        self,
        bus: Bus,
        store: ChemStore | None = None,
        *,
        persona: str = "",
        absence_turn_s: float = _DEFAULT_ABSENCE_TURN_S,
        now_fn=time.time,
    ) -> None:
        self._bus = bus
        self._store: ChemStore = store or InMemoryChemStore()
        self._persona = persona
        self._absence_turn_s = max(1.0, float(absence_turn_s))
        self._now = now_fn
        self._live: dict[str, ChemPair] = {}
        # Interaction mass since the last consolidation cycle (turns per customer),
        # the weight for weighted_average. Reset by reset_cycle_mass().
        self._mass: dict[str, float] = {}

    def _key(self, end_user_id: str) -> str:
        return f"{self._persona}:{end_user_id}" if self._persona else end_user_id

    def get_or_create(self, end_user_id: str) -> ChemPair:
        """The customer's live ChemPair. First call restores any persisted snapshot
        and applies absence-decay for time elapsed since they were last seen; a
        never-seen customer starts at the persona temperament baseline."""
        existing = self._live.get(end_user_id)
        if existing is not None:
            return existing

        pair = self._bus.new_chem()  # seeded from temperament baseline
        snap, last_seen = self._store.load(self._key(end_user_id))
        if snap is not None:
            pair.restore(snap)
            if last_seen is not None:
                elapsed = max(0.0, self._now() - float(last_seen))
                self._apply_absence(pair, elapsed)
        self._live[end_user_id] = pair
        return pair

    def _apply_absence(self, pair: ChemPair, elapsed_seconds: float) -> None:
        """Relax a restored pair toward the temperament baseline in proportion to
        time away — reusing the channels' own decay-toward-baseline dynamics."""
        turns = elapsed_seconds / self._absence_turn_s
        if turns <= 0:
            return
        pair.neuromod.decay(turns)
        pair.hormonal.decay(turns)

    def note_interaction(self, end_user_id: str, amount: float = 1.0) -> None:
        """Record interaction mass (e.g. one per turn) for the weighted average."""
        self._mass[end_user_id] = self._mass.get(end_user_id, 0.0) + float(amount)

    def persist(self, end_user_id: str) -> None:
        """Snapshot the customer's current mood to durable storage with now() as
        the last-seen stamp (so the next visit absence-decays correctly)."""
        pair = self._live.get(end_user_id)
        if pair is None:
            return
        self._store.save(self._key(end_user_id), pair.snapshot(), self._now())

    def weighted_average(self) -> dict | None:
        """Interaction-mass-weighted mean of the live customers' moods, as a
        snapshot ({"neuromod": {...}, "hormonal": {...}}). The persona's overall
        mood for this consolidation cycle. Returns None if there's nothing to
        average. ONE-WAY: callers blend this into the RESTING mood only — it must
        never be written back onto a client pair (no method here does)."""
        if not self._live:
            return None
        # Weight by recorded interaction mass; default 1.0 for a live-but-unmassed
        # customer so a present client always counts.
        weights = {uid: max(0.0, self._mass.get(uid, 1.0)) for uid in self._live}
        total_w = sum(weights.values())
        if total_w <= 0:
            return None

        out: dict[str, dict[str, float]] = {"neuromod": {}, "hormonal": {}}
        for layer, channels in (
            ("neuromod", self._bus.resting_chem.neuromod.CHANNELS),
            ("hormonal", self._bus.resting_chem.hormonal.CHANNELS),
        ):
            for ch in channels:
                acc = 0.0
                for uid, pair in self._live.items():
                    snap = pair.snapshot()
                    acc += weights[uid] * snap[layer][ch]
                out[layer][ch] = acc / total_w
        return out

    def reset_cycle_mass(self) -> None:
        """Clear interaction mass at the end of a consolidation cycle."""
        self._mass.clear()

    def active_client_count(self) -> int:
        """Distinct customers this persona process is holding live chemistry for."""
        return len(self._live)

    def is_fanned_out(self) -> bool:
        """True once this persona serves ≥2 distinct customers — the mode-emergent
        signal for engine behaviour (suppress proactive utterance, run mood
        averaging). Companion mode (0–1 customers) is always False, so every
        engine-only behaviour stays inert in the deployed single-persona product."""
        return len(self._live) >= 2

    def consolidate_into_resting(self, alpha: float = 0.3) -> dict | None:
        """At a sleep-consolidation cycle, blend the interaction-mass-weighted
        average of the cycle's client moods into the persona RESTING mood (lerp by
        ``alpha``), then clear cycle mass. Returns the new resting snapshot, or None
        when there's nothing to do (companion / not fanned out / no mass).

        THE ONE-WAY VALVE: this reads client pairs and writes ONLY the resting pair.
        There is deliberately no path that writes the aggregate back onto a client,
        so the day's overall mood can never seed an individual customer's session."""
        if not self.is_fanned_out():
            return None
        avg = self.weighted_average()
        if avg is None:
            return None
        resting = self._bus.resting_chem
        a = max(0.0, min(1.0, float(alpha)))
        for name, layer in (("neuromod", resting.neuromod), ("hormonal", resting.hormonal)):
            cur = layer.snapshot()
            layer.restore({ch: cur[ch] + a * (avg[name][ch] - cur[ch]) for ch in cur})
        self.reset_cycle_mass()
        return resting.snapshot()
