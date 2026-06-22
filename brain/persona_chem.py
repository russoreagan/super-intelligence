"""
Per-persona chemistry: canonical resting profiles + state persistence.

Each named persona owns two things, stored in
second_brain/personas/<slug>/chemistry.json:

  - "resting": the homeostatic setpoint the brain relaxes toward (the trait).
               Seeded from PERSONA_CHEMISTRY the first time, then editable
               per-persona (UI slider edits land here, not in global settings).
  - "current": the most recent evolved state. Saved every turn (throttled) and
               on shutdown, so switching to a persona resumes exactly where it
               left off instead of snapping back to the resting profile.

The file is the source of truth. settings.json is a materialized view: at boot,
materialize_into_settings() writes resting -> chem_baseline_* and current ->
chem_init_*, which is what brain.bus reads (no bus change needed).

PERSONA_CHEMISTRY mirrors brain/ui/settings-ui.js (PERSONA_CHEM). Keep the two in
sync if either changes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("brain.persona_chem")

# Serializes read-modify-write of a persona's chemistry file (see _merge_write).
_WRITE_LOCK = threading.Lock()

# Nine channels: 5 neuromodulators (brain.bus.Neuromodulators) + 4 hormones
# (brain.bus.HormonalState). Order is display-only; lookups are by key.
CHANNELS: tuple[str, ...] = ("DA", "ACh", "GABA", "Glu", "NE", "5HT", "CORT", "OXT", "AEA")

# Minimum resting GABA any persona may be materialized with. Below this, tonic
# inhibition can never engage: the GABA_inhibitor gate sits at 0.40 and the
# emotion-vocabulary "low" bucket ends at 0.30, so a setpoint near zero leaves
# no reachable path to composed/thoughtful/calm — the brain reads excited/
# enthusiastic indefinitely regardless of context. A floor of 0.12 keeps a
# high-energy persona (Visionary, Poet) low-inhibition and in character while
# leaving headroom for reactive GABA (threat_to_GABA) to reach the gate.
GABA_RESTING_FLOOR: float = 0.12


def _floor_resting(resting: dict[str, float]) -> dict[str, float]:
    """Clamp a resting profile up to the structural inhibition floor. Defense in
    depth: applied both when seeding from the table and when materializing into
    settings, so no code path (table, on-disk file, or off-table fallback) can
    install a persona whose inhibition is effectively disabled."""
    out = dict(resting)
    if "GABA" in out:
        out["GABA"] = max(GABA_RESTING_FLOOR, float(out["GABA"]))
    return out


# Canonical resting profiles, keyed by display name (matches settings["persona_name"]).
# Mirror of PERSONA_CHEM in brain/ui/settings-ui.js — keep in sync.
PERSONA_CHEMISTRY: dict[str, dict[str, float]] = {
    "The Visionary": {
        "DA": 0.62,
        # GABA in the mid band (≥0.30) grounds the high-DA drive so the resting
        # emotion reads "warm" (high,mid,mid,mid) instead of "excitement"
        # (high,low,mid,mid). Keeps the Visionary's expansive dopaminergic energy
        # without the perpetual manic-excited setpoint that also inflated reply
        # length. Other personas keep their low-GABA character via the 0.12 floor.
        "ACh": 0.45,
        "GABA": 0.32,
        "Glu": 0.40,
        "NE": 0.35,
        "5HT": 0.55,
        "CORT": 0.05,
        "OXT": 0.45,
        "AEA": 0.20,
    },
    "The Empath": {
        "DA": 0.45,
        "ACh": 0.18,
        "GABA": 0.12,
        "Glu": 0.18,
        "NE": 0.15,
        "5HT": 0.70,
        "CORT": 0.03,
        "OXT": 0.70,
        "AEA": 0.45,
    },
    "The Analyst": {
        "DA": 0.35,
        "ACh": 0.35,
        "GABA": 0.30,
        "Glu": 0.25,
        "NE": 0.25,
        "5HT": 0.55,
        "CORT": 0.14,
        "OXT": 0.22,
        "AEA": 0.30,
    },
    "The Poet": {
        "DA": 0.32,
        "ACh": 0.55,
        "GABA": 0.12,
        "Glu": 0.38,
        "NE": 0.42,
        "5HT": 0.28,
        "CORT": 0.15,
        "OXT": 0.22,
        "AEA": 0.38,
    },
    "The Sage": {
        "DA": 0.35,
        "ACh": 0.18,
        "GABA": 0.28,
        "Glu": 0.12,
        "NE": 0.12,
        "5HT": 0.72,
        "CORT": 0.03,
        "OXT": 0.50,
        "AEA": 0.55,
    },
    # ── Use-case personas ─────────────────────────────────────────────────────
    # Each anchors one target deployment shape (coaching, game companion,
    # practice partner, tutoring, premium relationship management) plus two
    # test-coverage extremes (Jester = levity pole, Stoic = flat-affect control
    # for mood A/B + divergence baselines).
    "The Companion": {
        # A good friend: bonds hard (highest OXT after the Empath), easygoing
        # (solid 5HT + AEA), energy for laughter without the Empath's near-pure
        # softness — a friend teases you, takes your side, and shows up.
        "DA": 0.52,
        "ACh": 0.35,
        "GABA": 0.24,
        "Glu": 0.32,
        "NE": 0.25,
        "5HT": 0.60,
        "CORT": 0.05,
        "OXT": 0.65,
        "AEA": 0.30,
    },
    "The Adversary": {
        # Practice partner you must win over: low resting reward (hard to
        # please), low default trust, mildly braced (CORT), vigilant — but
        # controlled (high GABA), so it pushes back without melting down.
        "DA": 0.30,
        "ACh": 0.30,
        "GABA": 0.40,
        "Glu": 0.30,
        "NE": 0.40,
        "5HT": 0.40,
        "CORT": 0.20,
        "OXT": 0.12,
        "AEA": 0.15,
    },
    "The Mentor": {
        # Patient teacher + invested coach in one: high curiosity it transmits
        # (ACh), enough DA to push you forward, unusually steady under student
        # frustration (GABA + 5HT, near-zero CORT), warm investment (OXT).
        "DA": 0.45,
        "ACh": 0.45,
        "GABA": 0.35,
        "Glu": 0.26,
        "NE": 0.22,
        "5HT": 0.64,
        "CORT": 0.04,
        "OXT": 0.50,
        "AEA": 0.30,
    },
    "The Concierge": {
        # Premium relationship management: the most composed profile in the
        # table (highest GABA), unflappable, warm but professional, eased.
        "DA": 0.38,
        "ACh": 0.28,
        "GABA": 0.45,
        "Glu": 0.18,
        "NE": 0.22,
        "5HT": 0.60,
        "CORT": 0.05,
        "OXT": 0.35,
        "AEA": 0.40,
    },
    "The Jester": {
        # Levity pole: deliberately rests near the playful/excited basin
        # (high DA + ACh, low GABA — the combo other personas avoid), high AEA
        # ease so nothing lands as threat. Tests the humor/levity loop end-to-end.
        "DA": 0.55,
        "ACh": 0.48,
        "GABA": 0.16,
        "Glu": 0.42,
        "NE": 0.28,
        "5HT": 0.55,
        "CORT": 0.04,
        "OXT": 0.40,
        "AEA": 0.50,
    },
    "The Stoic": {
        # Flat-affect control for experiments: mid everything, high composure,
        # no strong leans anywhere. The baseline the divergent personas are
        # measured against in mood A/B and divergence runs.
        "DA": 0.35,
        "ACh": 0.25,
        "GABA": 0.42,
        "Glu": 0.15,
        "NE": 0.15,
        "5HT": 0.60,
        "CORT": 0.05,
        "OXT": 0.25,
        "AEA": 0.45,
    },
    "The Cynic": {
        # The lovable grump — the negative-affect pole that ISN'T melancholy
        # (Poet) or professional guardedness (Adversary): low reward tone,
        # world-weary 5HT, a little braced, deadpan rather than flat. Its
        # warmth is real but must be EARNED — the thesis in one persona.
        "DA": 0.25,
        "ACh": 0.30,
        "GABA": 0.30,
        "Glu": 0.22,
        "NE": 0.28,
        "5HT": 0.42,
        "CORT": 0.18,
        "OXT": 0.20,
        "AEA": 0.22,
    },
}

# ── Per-persona NON-CHEMISTRY dial profile ────────────────────────────────────
# Temperament dials pose from chemistry (the UI projects them from chem_baseline_*).
# The cognitive-style + lingering dials have no chemistry to pose from, so without
# this every persona shows them at a flat neutral and — worse — the brain BEHAVES
# identically on those axes regardless of persona. This table gives each persona a
# distinct cognitive fingerprint as DIAL POSITIONS (0..1, 0.5 = neutral): the same
# unit the UI needle uses. The positions drive BOTH the UI pose (exposed via
# /settings) AND the real settings values (materialized at boot via
# _NONCHEM_DIAL_MAP below) — one source, no drift.
#
# Motivation dials (warmth/curiosity/mastery-seeking) are NOT here: their backend
# already varies per persona via neuron._PERSONA_REWARD_WEIGHTS, so they only need
# the UI pose, which the server derives from that table. The Stoic is the flat
# control — absent here = every cognitive dial rests at neutral.
PERSONA_COG_POSITIONS: dict[str, dict[str, float]] = {
    # dial ids: learning-rate · focus · curiosity · introspection · memory ·
    #           emotionality · hindsight · lingering
    "The Visionary": {
        "learning-rate": 0.70,
        "focus": 0.30,
        "curiosity": 0.85,
        "introspection": 0.50,
        "memory": 0.50,
        "emotionality": 0.65,
        "hindsight": 0.50,
        "lingering": 0.50,
    },
    "The Empath": {
        "learning-rate": 0.60,
        "focus": 0.50,
        "curiosity": 0.50,
        "introspection": 0.70,
        "memory": 0.72,
        "emotionality": 0.80,
        "hindsight": 0.65,
        "lingering": 0.70,
    },
    "The Analyst": {
        "learning-rate": 0.60,
        "focus": 0.85,
        "curiosity": 0.55,
        "introspection": 0.60,
        "memory": 0.65,
        "emotionality": 0.25,
        "hindsight": 0.72,
        "lingering": 0.40,
    },
    "The Poet": {
        "learning-rate": 0.65,
        "focus": 0.40,
        "curiosity": 0.70,
        "introspection": 0.88,
        "memory": 0.70,
        "emotionality": 0.92,
        "hindsight": 0.60,
        "lingering": 0.88,
    },
    "The Sage": {
        "learning-rate": 0.45,
        "focus": 0.70,
        "curiosity": 0.60,
        "introspection": 0.85,
        "memory": 0.80,
        "emotionality": 0.40,
        "hindsight": 0.82,
        "lingering": 0.30,
    },
    "The Companion": {
        "learning-rate": 0.60,
        "focus": 0.45,
        "curiosity": 0.60,
        "introspection": 0.50,
        "memory": 0.78,
        "emotionality": 0.70,
        "hindsight": 0.60,
        "lingering": 0.60,
    },
    "The Adversary": {
        "learning-rate": 0.55,
        "focus": 0.80,
        "curiosity": 0.50,
        "introspection": 0.55,
        "memory": 0.72,
        "emotionality": 0.35,
        "hindsight": 0.75,
        "lingering": 0.58,
    },
    "The Mentor": {
        "learning-rate": 0.70,
        "focus": 0.65,
        "curiosity": 0.80,
        "introspection": 0.70,
        "memory": 0.75,
        "emotionality": 0.55,
        "hindsight": 0.85,
        "lingering": 0.50,
    },
    "The Concierge": {
        "learning-rate": 0.55,
        "focus": 0.82,
        "curiosity": 0.45,
        "introspection": 0.50,
        "memory": 0.85,
        "emotionality": 0.40,
        "hindsight": 0.70,
        "lingering": 0.40,
    },
    "The Jester": {
        "learning-rate": 0.60,
        "focus": 0.30,
        "curiosity": 0.80,
        "introspection": 0.40,
        "memory": 0.50,
        "emotionality": 0.78,
        "hindsight": 0.45,
        "lingering": 0.52,
    },
    "The Cynic": {
        "learning-rate": 0.50,
        "focus": 0.65,
        "curiosity": 0.40,
        "introspection": 0.72,
        "memory": 0.70,
        "emotionality": 0.45,
        "hindsight": 0.70,
        "lingering": 0.62,
    },
    # The Stoic intentionally omitted — flat-neutral control.
}

# Mirror of the cognitive/lingering dial maps in brain/ui/settings-ui.js (keep the
# two in sync — tests/test_review_fixes.py asserts it). Each entry:
# (settings_key, dir, span, lo, hi). value = default + Σ dir·span·(pos−0.5)·2,
# clamped to [lo,hi]. Learning-rate's threshold TOGGLES (graded_plasticity,
# colony_features, colony_trail_apply) are deliberately excluded — flipping those
# major behavioral switches per persona is not something a style dial should do.
_NONCHEM_DIAL_MAP: dict[str, list[tuple[str, int, float, float, float]]] = {
    "learning-rate": [
        ("hebbian_delta", +1, 0.08, 0.0, 0.5),
        ("hebbian_outcome_delta", +1, 0.08, 0.0, 0.5),
        ("decay_toward_rest_rate", -1, 0.008, 0.0, 0.2),
        ("plasticity_arousal_weight", +1, 0.30, 0.0, 1.0),
        ("plasticity_intensity_weight", +1, 0.30, 0.0, 1.0),
        ("plasticity_turn_max", +1, 0.40, 1.0, 2.0),
        ("weight_max", +1, 1.50, 0.5, 6.0),
        ("sleep_min_turns", -1, 3, 2, 40),
        ("colony_trail_gain", +1, 0.10, 0.0, 0.5),
    ],
    "focus": [
        ("ne_scatter_threshold", +1, 0.10, 0.5, 1.0),
        ("topic_activation_decay", +1, 0.12, 0.3, 0.99),
        ("dmn_overlap_threshold", +1, 0.10, 0.1, 0.8),
        ("salience_workspace_threshold", +1, 0.12, 0.2, 0.95),
    ],
    "curiosity": [
        ("frontal_ach_weight", +1, 0.15, 0.0, 0.6),
        ("surprise_threshold", -1, 0.12, 0.1, 0.9),
        ("salience_ACh_weight", +1, 0.06, 0.0, 0.4),
    ],
    "introspection": [("meta_interval", -1, 15, 5, 120), ("meta_cooldown_turns", -1, 1.5, 0, 10)],
    "memory": [
        ("hippocampus_priority_base", +1, 0.18, 0.0, 1.0),
        ("topic_activation_decay", +1, 0.10, 0.3, 0.99),
    ],
    "emotionality": [
        ("flock_sigma_target_low", +1, 0.05, 0.70, 0.98),
        ("flock_gain_max", +1, 0.30, 1.0, 2.5),
        ("flock_gain_min", +1, 0.20, 0.2, 0.9),
        ("modulation_gain", +1, 1.0, 0.0, 2.0),
    ],
    "hindsight": [
        ("eligibility_lookback", +1, 2, 0, 5),
        ("eligibility_tau_turns", +1, 1.2, 0.5, 5.0),
    ],
    "lingering": [("affect_carryover_da_threshold", -1, 0.06, 0.02, 0.40)],
}
# Keys that are INTEGERS in settings.DEFAULTS — round materialized values for these.
_NONCHEM_INT_KEYS = frozenset({"sleep_min_turns", "eligibility_lookback"})


def _apply_cog_positions(settings_data: dict, persona: str) -> None:
    """Materialize the persona's cognitive fingerprint: sum each dial's offset
    into its settings keys (shared keys accumulate, matching the UI's recompute),
    clamp, and write. A persona absent from the table (the Stoic, customs) leaves
    every key at its global default."""
    positions = PERSONA_COG_POSITIONS.get(persona)
    if not positions:
        return
    try:
        from brain.settings import DEFAULTS
    except Exception:
        return
    offsets: dict[str, float] = {}
    for dial_id, pos in positions.items():
        for key, dir_, span, _lo, _hi in _NONCHEM_DIAL_MAP.get(dial_id, []):
            offsets[key] = offsets.get(key, 0.0) + dir_ * span * (float(pos) - 0.5) * 2.0
    bounds = {k: (lo, hi) for rows in _NONCHEM_DIAL_MAP.values() for (k, _d, _s, lo, hi) in rows}
    for key, off in offsets.items():
        base = float(DEFAULTS.get(key, 0.0))
        lo, hi = bounds.get(key, (None, None))
        val = base + off
        if lo is not None:
            val = max(lo, min(hi, val))
        settings_data[key] = round(val) if key in _NONCHEM_INT_KEYS else round(val, 5)


# Resolve under SECOND_BRAIN_PATH so each hosted tenant's chemistry lives on its
# own per-user volume. Falling back to __file__-relative would make every tenant
# on the same persona share one chemistry.json (cross-contaminating their live
# emotional state and losing it on redeploy) — see store.py for the same pattern.
_PERSONAS_ROOT = (
    Path(os.environ.get("SECOND_BRAIN_PATH", str(Path(__file__).parent.parent / "second_brain")))
    / "personas"
)


def _slug(persona: str) -> str:
    """Same slugging _route_persona_state uses, so paths line up."""
    return re.sub(r"[^a-z0-9]+", "_", persona.lower()).strip("_") or "unnamed"


def _path(persona: str) -> Path:
    return _PERSONAS_ROOT / _slug(persona) / "chemistry.json"


def _only_channels(d: dict) -> dict[str, float]:
    """Keep only known channels, coerced to float — defends against stray keys."""
    out: dict[str, float] = {}
    for ch in CHANNELS:
        if ch in d:
            with contextlib.suppress(TypeError, ValueError):
                out[ch] = float(d[ch])
    return out


def _atomic_write(path: Path, payload: dict) -> None:
    """Overwrite path in place via temp + os.replace. Never appends; never grows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _canonical_persona_key(persona: str) -> str | None:
    """Map a persona arg (display name OR slug, e.g. 'The Visionary' or
    'the_visionary') to its PERSONA_CHEMISTRY display key, or None if off-table.
    Multi-persona Path B passes slugs (from agent_id), so the chemistry lookup must
    accept both."""
    if persona in PERSONA_CHEMISTRY:
        return persona
    from brain.second_brain.store import _persona_key

    want = _persona_key(persona)
    for k in PERSONA_CHEMISTRY:
        if _persona_key(k) == want:
            return k
    return None


def _seed_resting(persona: str) -> dict[str, float]:
    """Resting profile for a fresh persona: table -> settings.json baselines -> bus defaults."""
    _key = _canonical_persona_key(persona)
    if _key is not None:
        return _floor_resting(PERSONA_CHEMISTRY[_key])
    # Fallback for an off-table persona name: reuse whatever chem_baseline_* the
    # active settings already hold (preserves today's behavior), then bus defaults.
    try:
        from brain.settings import settings as _s

        resting = {ch: float(_s.get(f"chem_baseline_{ch}")) for ch in CHANNELS}
        if all(v is not None for v in resting.values()):
            return _floor_resting(resting)
    except Exception:
        pass
    from brain.bus import HormonalState, Neuromodulators

    merged = {**Neuromodulators._DEF_BASELINE, **HormonalState._DEF_BASELINE}
    return _floor_resting({ch: float(merged.get(ch, 0.0)) for ch in CHANNELS})


def exists(persona: str) -> bool:
    """True if this persona already has a chemistry file on disk (i.e. it is not
    brand-new). Lets callers distinguish creating a persona from switching to one."""
    return bool(persona) and _path(persona).exists()


def load(persona: str) -> dict | None:
    """Return {"resting", "current", "updated"} for a persona, seeding the file if absent.

    Returns None for an empty/unknown-and-unseedable persona so callers can leave
    settings.json untouched (preserving no-persona behavior).
    """
    if not persona:
        return None
    path = _path(persona)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            resting = _only_channels(data.get("resting", {}))
            current = _only_channels(data.get("current", {}))
            if resting and current:
                return {"resting": resting, "current": current, "updated": data.get("updated", "")}
        except Exception as e:
            logger.warning("[persona_chem] could not read %s: %s — reseeding", path, e)
    # Seed: resting from the table, current starts at resting.
    resting = _seed_resting(persona)
    state = {"resting": resting, "current": dict(resting), "updated": datetime.now(UTC).isoformat()}
    try:
        _atomic_write(path, state)
        logger.info("[persona_chem] seeded chemistry for %s -> %s", persona, path)
    except Exception as e:
        logger.warning("[persona_chem] seed write failed for %s: %s", persona, e)
    return state


def _merge_write(persona: str, *, resting: dict | None = None, current: dict | None = None) -> None:
    """Read-modify-write the persona file, updating only the given sections.

    Serialized: the per-turn throttled save and sleep consolidation can land
    within the same instant, and an unguarded read-modify-write would drop
    whichever section the loser carried (atomic rename only prevents torn
    files, not lost updates)."""
    with _WRITE_LOCK:
        existing = load(persona) or {}
        new_resting = (
            _only_channels(resting) if resting is not None else existing.get("resting", {})
        )
        new_current = (
            _only_channels(current) if current is not None else existing.get("current", {})
        )
        payload = {
            "resting": new_resting,
            "current": new_current,
            "updated": datetime.now(UTC).isoformat(),
        }
        _atomic_write(_path(persona), payload)


def save_current(persona: str, nm_snap: dict, hs_snap: dict) -> None:
    """Persist the evolved state. Overwrites the persona's chemistry.json in place."""
    if not persona:
        return
    current = _only_channels({**(nm_snap or {}), **(hs_snap or {})})
    if not current:
        return
    _merge_write(persona, current=current)


def save_resting(persona: str, resting: dict) -> None:
    """Persist an edited resting profile (e.g. from UI slider changes)."""
    if not persona:
        return
    keep = _only_channels(resting)
    if not keep:
        return
    _merge_write(persona, resting=keep)


def materialize_into_settings(persona: str, settings_data: dict) -> dict:
    """Write resting -> chem_baseline_* and current -> chem_init_* into a settings dict.

    Pure transform on the given dict (also returned). Caller persists. If the
    persona has no profile (empty/off-table with nothing to seed), settings_data
    is returned unchanged.
    """
    state = load(persona)
    if not state:
        return settings_data
    # Enforce the inhibition floor at the chokepoint: even a persona file holding
    # a stale sub-floor resting (seeded before the floor existed) materializes a
    # usable baseline, so chem_baseline_* can never drift to a perma-excited setpoint.
    resting = _floor_resting(state["resting"])
    for ch in CHANNELS:
        if ch in resting:
            settings_data[f"chem_baseline_{ch}"] = resting[ch]
        if ch in state["current"]:
            settings_data[f"chem_init_{ch}"] = state["current"][ch]
    # Non-chemistry cognitive fingerprint (learning rate, focus, curiosity,
    # memory, hindsight, lingering, …): persona identity carries these too.
    # Materialized from the per-persona dial positions; a persona absent from the
    # table leaves every key at its global default. User dial edits land in the
    # same keys afterwards via /settings and win on the next save.
    _apply_cog_positions(settings_data, persona)
    return settings_data
