"""
Relationship bond model — pure functions for relational decay and recovery.

Two quantities per speaker:
  - **affection** (−50..+100): live warmth, updated per-turn by tone, injected
    into drafter prompts. Decays toward 0 on absence.
  - **bond** (0..100): latent closeness high-water mark. Decays much more slowly
    than affection and is the substrate that enables fast reunion recovery.

The design mirrors real relationships: closeness creates a bond that decays
slowly and recovers fast; a thin acquaintance fades to nothing over the same
gap. Half-lives grow *exponentially* with bond, so a casual acquaintance fades
in weeks, a close friend barely declines over months, and a profound bond is
effectively permanent.

All functions are pure (no I/O, no settings mutation) so they're unit-testable
in isolation. Callers pass the tunable constants in (sourced from settings).
"""

from __future__ import annotations

import math

# Familiarity tier ordering (history depth, distinct from affection warmth)
TIER_ORDER = {"new": 0, "acquainted": 1, "close": 2}
TIER_NAMES = ["new", "acquainted", "close"]


def affection_half_life_days(bond: float, base: float, scale: float) -> float:
    """Half-life of the live affection score, growing exponentially with bond.
    base ≈ 25 d, scale ≈ 23 → bond 10 ≈ 39 d, 30 ≈ 92 d, 60 ≈ 340 d, 100 ≈ ~5 y."""
    return base * math.exp(max(0.0, bond) / scale)


def bond_half_life_days(bond: float, base: float, scale: float) -> float:
    """Half-life of the latent bond — same shape, larger base (slower substrate).
    base ≈ 90 d → bond 10 ≈ 139 d, 60 ≈ 1220 d."""
    return base * math.exp(max(0.0, bond) / scale)


def decay_affection(
    affection: float, bond: float, elapsed_days: float, base: float, scale: float
) -> float:
    """Decay affection toward 0 over an absence of `elapsed_days`, bond-protected.
    Works symmetrically for negative affection (relaxes toward 0)."""
    if elapsed_days <= 0:
        return affection
    hl = affection_half_life_days(bond, base, scale)
    return affection * (0.5 ** (elapsed_days / hl))


def decay_bond(bond: float, elapsed_days: float, base: float, scale: float) -> float:
    """Decay the latent bond toward 0 over an absence — much slower than affection."""
    if elapsed_days <= 0 or bond <= 0:
        return bond
    hl = bond_half_life_days(bond, base, scale)
    return bond * (0.5 ** (elapsed_days / hl))


def reunion_boost(affection: float, bond: float, gain: float) -> float:
    """Multiplier applied to a POSITIVE affection delta during reengagement.
    Larger when affection sits well below the prior bond (a former-close friend
    reconnecting), tapering to 1.0 as affection approaches bond. Never < 1.0."""
    gap = max(0.0, bond - affection)
    return 1.0 + gain * (gap / 100.0)


def familiarity_from_bond(bond: float, close_bond: float, acquainted_bond: float) -> str:
    """Familiarity tier as a pure function of bond (history depth), NOT affection.
    A fight (low affection) doesn't erase familiarity; only a long absence that
    decays the bond does."""
    if bond >= close_bond:
        return "close"
    if bond >= acquainted_bond:
        return "acquainted"
    return "new"


def apply_absence(
    affection: float,
    bond: float,
    elapsed_days: float,
    *,
    aff_base: float,
    bond_base: float,
    scale: float,
) -> tuple[float, float]:
    """Apply an absence gap to both quantities. Returns (affection, bond).
    Bond is also kept as a high-water mark vs the (now-decayed) affection."""
    new_aff = decay_affection(affection, bond, elapsed_days, aff_base, scale)
    new_bond = decay_bond(bond, elapsed_days, bond_base, scale)
    # Bond never drops below current (decayed) affection — affection can't exceed
    # the closeness it represents.
    new_bond = max(new_bond, new_aff)
    return new_aff, new_bond
