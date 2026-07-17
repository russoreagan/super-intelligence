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

Storage is behind the ``ChemStore`` protocol. ``default_store()`` is the wiring
the engine layer uses: a ``FileChemStore`` on the tenant's volume, routed through
``persona_state_root`` so one org's customer moods can never land in another's
tree and a non-home persona can never write the home persona's files. It degrades
to ``InMemoryChemStore`` when the volume is unwritable — a customer's mood is
worth losing before a turn is. Any other durable backend (Supabase, the
relationship store) implements the same two methods and plugs in unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from brain.bus import Bus, ChemPair

logger = logging.getLogger(__name__)

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


class FileChemStore:
    """Durable ChemStore backing snapshots to one JSON file per key under ``root``.

    This is the persistence model the multi-tenant deployment already uses for
    per-tenant local state (the brain's chemistry/weights live on the tenant's
    volume, not Supabase — Supabase holds episodes/facts). Filenames are content
    hashes of the key (the plaintext key is also stored inside the file) so any
    persona/end_user string is filesystem-safe and collision-free."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Content-addressed filename, not a security digest.
        return self._root / (
            hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:20] + ".json"
        )

    def load(self, key: str) -> tuple[dict | None, float | None]:
        path = self._path(key)
        if not path.exists():
            return (None, None)
        try:
            rec = json.loads(path.read_text())
            return (rec.get("snapshot"), rec.get("last_seen"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("[FileChemStore] unreadable %s: %s", path.name, exc)
            return (None, None)

    def save(self, key: str, snapshot: dict, last_seen_ts: float) -> None:
        path = self._path(key)
        payload = {"key": key, "snapshot": snapshot, "last_seen": float(last_seen_ts)}
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)  # atomic
        except OSError as exc:
            logger.warning("[FileChemStore] write failed %s: %s", path.name, exc)


def default_store(persona: str = "") -> ChemStore:
    """THE durable store for a persona's per-customer moods — the engine wiring.

    Path resolution goes through ``persona_key.persona_state_root``, the canonical
    routing rule for anything file-backed that follows a persona. Two failure modes
    it exists to prevent, both of which this repo has shipped before:

      • Cross-tenant bleed — the hosted deployment gives each org its own volume via
        SECOND_BRAIN_PATH, read at CALL time. Resolving anything __file__-relative
        would pool every tenant's customer moods into one tree.
      • Home-persona capture — SECOND_BRAIN_PATH is frozen at boot to the HOME
        persona's root, so a non-home persona that doesn't route through here
        silently reads and writes the home persona's files.

    Keys stay persona-qualified independently (see ``_key``), so a persona is fenced
    by path AND key — two customers of two personas can never alias.

    Never raises: an unwritable volume degrades to in-memory, which is exactly the
    behaviour that shipped before this store existed.
    """
    try:
        from brain.persona_key import persona_state_root

        return FileChemStore(persona_state_root(persona) / "client_chem")
    except Exception as exc:
        logger.warning(
            "[client_chem] durable store unavailable for persona %r (%s) — using memory",
            persona,
            exc,
        )
        return InMemoryChemStore()


class ClientChemRegistry:
    """Owns the live per-customer ChemPairs for one persona process."""

    def __init__(
        self,
        bus: Bus,
        store: ChemStore | None = None,
        *,
        persona: str = "",
        absence_turn_s: float = _DEFAULT_ABSENCE_TURN_S,
        min_persist_interval_s: float = 0.0,
        now_fn=time.time,
    ) -> None:
        self._bus = bus
        # Attach so Bus.rebaseline_chem() can reach live client pairs when a
        # temperament edit moves the resting setpoints mid-process.
        bus._chem_registry = self
        self._store: ChemStore = store or InMemoryChemStore()
        self._persona = persona
        self._absence_turn_s = max(1.0, float(absence_turn_s))
        # Per-customer write throttle. 0 = persist on every call (the default, and
        # what the tests pin); the engine passes a few seconds so a fanned-out
        # persona doesn't write one file per customer per turn. See persist().
        self._min_persist_interval_s = max(0.0, float(min_persist_interval_s))
        self._last_persist: dict[str, float] = {}
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
        snap, last_seen = self._load(end_user_id)
        if snap is not None:
            pair.restore(snap)
            if last_seen is not None:
                elapsed = max(0.0, self._now() - float(last_seen))
                self._apply_absence(pair, elapsed)
        self._live[end_user_id] = pair
        return pair

    def _load(self, end_user_id: str) -> tuple[dict | None, float | None]:
        """Read a snapshot, containing any store failure. A dead or corrupt backend
        must never reach the turn: the worst case is this customer starting at the
        temperament baseline, which is what a first contact does anyway."""
        try:
            return self._store.load(self._key(end_user_id))
        except Exception as exc:
            logger.warning("[client_chem] load failed for %s: %s", self._key(end_user_id), exc)
            return (None, None)

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

    def persist(self, end_user_id: str, *, force: bool = False) -> None:
        """Snapshot the customer's current mood to durable storage with now() as
        the last-seen stamp (so the next visit absence-decays correctly).

        Throttled per customer by ``min_persist_interval_s`` (0 = every call). The
        turn path calls this every turn and chemistry moves every turn, so a
        fanned-out persona would otherwise write a file per customer per turn. A
        skipped write only leaves the stored last-seen stamp slightly stale, which
        costs at most one throttle interval of extra absence-decay on the next
        visit — seconds against a 180s decay turn, i.e. nothing. ``force``
        bypasses the throttle (``flush`` uses it at shutdown).

        Best-effort by construction: a store error degrades to in-memory
        behaviour — the live pair is untouched and the turn never sees it.
        """
        pair = self._live.get(end_user_id)
        if pair is None:
            return
        now = self._now()
        if not force and self._min_persist_interval_s > 0.0:
            last = self._last_persist.get(end_user_id)
            if last is not None and (now - last) < self._min_persist_interval_s:
                return
        # Stamp the ATTEMPT, not the success: a persistently broken store then
        # retries (and logs) once per interval rather than once per turn.
        self._last_persist[end_user_id] = now
        try:
            self._store.save(self._key(end_user_id), pair.snapshot(), now)
        except Exception as exc:
            logger.warning("[client_chem] persist failed for %s: %s", self._key(end_user_id), exc)

    def flush(self) -> None:
        """Force-persist every live customer — the shutdown counterpart to the
        throttled per-turn persist, mirroring persona_chem's save-on-shutdown.
        Best-effort per customer, so one bad key can't strand the rest. (A
        /restart is a raw os.execv that skips shutdown entirely, which is why the
        throttled per-turn save is what carries state across most restarts.)"""
        for end_user_id in list(self._live):
            self.persist(end_user_id, force=True)

    def forget(self, end_user_id: str) -> None:
        """Drop a customer's live mood + interaction mass (lifecycle purge). The
        durable snapshot is removed separately by the caller."""
        self._live.pop(end_user_id, None)
        self._mass.pop(end_user_id, None)
        self._last_persist.pop(end_user_id, None)

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
