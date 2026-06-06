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
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("brain.persona_chem")

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
        "ACh": 0.45,
        "GABA": 0.12,
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
}

_PERSONAS_ROOT = Path(__file__).parent.parent / "second_brain" / "personas"


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


def _seed_resting(persona: str) -> dict[str, float]:
    """Resting profile for a fresh persona: table -> settings.json baselines -> bus defaults."""
    if persona in PERSONA_CHEMISTRY:
        return _floor_resting(PERSONA_CHEMISTRY[persona])
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
    """Read-modify-write the persona file, updating only the given sections."""
    existing = load(persona) or {}
    new_resting = _only_channels(resting) if resting is not None else existing.get("resting", {})
    new_current = _only_channels(current) if current is not None else existing.get("current", {})
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
    return settings_data
