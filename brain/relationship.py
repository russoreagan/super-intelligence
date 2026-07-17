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


# ── Legacy → bond migration ───────────────────────────────────────────────────
# Schemas written before the bond model shipped carry `- Familiarity:`, `- Score:`
# and `- Interactions:` but no `- Bond:`. Reading that absent field as 0.0 is a
# category error: it conflates "this relationship has no depth" with "this file
# predates the field", and reports a long-standing close relationship as "new".
# The functions below reconstruct a defensible bond from the signals such a file
# DOES carry, so the tier it already earned survives the model change.

# Interaction counts the legacy count-based tiers used (hippocampus._FAMILIARITY_TIERS).
LEGACY_CLOSE_INTERACTIONS = 30
LEGACY_ACQUAINTED_INTERACTIONS = 8

# A seeded tier is placed this far into the band above its own floor. Seeding at
# exactly `close_bond` would put a migrated relationship on its own knife-edge —
# the first day of absence would decay it a hair below the threshold and demote
# it, permanently (bond only re-grows as a high-water mark of live affection).
SEED_MARGIN_FRAC = 0.25


def bond_from_history(interactions: float, close_bond: float, acquainted_bond: float) -> float:
    """Bond implied by a legacy interaction count, for files predating `- Bond:`.

    Piecewise-linear so the legacy model's tier boundaries land exactly on the
    bond model's: 8 interactions (legacy "acquainted") → `acquainted_bond`, 30
    (legacy "close") → `close_bond`. A migrated user therefore sits at the same
    relative position on the new scale as on the old, and this can never promote
    anyone the legacy model would not have. Deeper histories keep growing at the
    same slope but saturate one band above the close floor — a long history earns
    a stable bond, not an unbreakable one."""
    n = max(0.0, float(interactions))
    if n <= 0 or close_bond <= acquainted_bond:
        return 0.0
    if n <= LEGACY_ACQUAINTED_INTERACTIONS:
        return acquainted_bond * (n / LEGACY_ACQUAINTED_INTERACTIONS)
    span = LEGACY_CLOSE_INTERACTIONS - LEGACY_ACQUAINTED_INTERACTIONS
    slope = (close_bond - acquainted_bond) / span
    if n <= LEGACY_CLOSE_INTERACTIONS:
        return acquainted_bond + slope * (n - LEGACY_ACQUAINTED_INTERACTIONS)
    cap = close_bond + (close_bond - acquainted_bond)
    return min(cap, close_bond + slope * (n - LEGACY_CLOSE_INTERACTIONS))


def seed_bond_from_legacy(
    affection: float,
    interactions: float,
    tier: str,
    *,
    close_bond: float,
    acquainted_bond: float,
) -> float:
    """Reconstruct a bond for a schema that has relationship history but no
    `- Bond:` line. Takes the strongest of three independent legacy signals:

      - the tier the file already claims (plus a margin, so it is not seeded on
        its own knife-edge) — the only explicit record of history depth;
      - the depth implied by the interaction count — recovers files whose
        `- Familiarity:` line is missing or mangled;
      - live affection — bond is a high-water mark, so it can never sit below it.

    A genuinely fresh file carries none of these (no tier, no interactions, no
    score) and seeds 0.0 → "new", so this is safe to apply to every file: it is a
    migration, not a special case."""
    margin = max(0.0, (close_bond - acquainted_bond) * SEED_MARGIN_FRAC)
    t = (tier or "").strip().lower()
    if t == "close":
        floor = close_bond + margin
    elif t == "acquainted":
        floor = acquainted_bond + margin
    else:
        floor = 0.0
    hist = bond_from_history(interactions, close_bond, acquainted_bond)
    seed = max(floor, hist, max(0.0, float(affection)))
    return max(0.0, min(100.0, seed))


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


# ── Register / communication-style profile ─────────────────────────────────
# A rolling per-speaker memory of *how* the user tends to write — formal, casual,
# technical — distinct from affection (how warmly they treat the AI) and bond
# (history depth). The discrete per-turn register tag is classified cheaply
# upstream (parietal.classify_register, zero LLM cost); these pure functions just
# accumulate it into a stable distribution so a known user resumes with their
# typical register remembered, the same way affection/bond persist across
# absences. Pure (no I/O) so they're unit-testable in isolation.

REGISTER_CATEGORIES = ("casual", "neutral", "formal", "technical")


def update_register_profile(
    profile: dict[str, float], observed: str, alpha: float = 0.3
) -> dict[str, float]:
    """EMA-update a rolling register distribution with one observation.

    `profile` maps register category → weight (roughly summing to 1). Each turn
    nudges the observed category toward 1 and the others toward 0, so the profile
    tracks the user's *typical* register while still adapting to genuine drift.
    Higher `alpha` adapts faster. Unknown observations fold into 'neutral'.
    Returns a new dict (does not mutate the input)."""
    if observed not in REGISTER_CATEGORIES:
        observed = "neutral"
    prof = {c: float(profile.get(c, 0.0)) for c in REGISTER_CATEGORIES}
    for c in REGISTER_CATEGORIES:
        target = 1.0 if c == observed else 0.0
        prof[c] = alpha * target + (1.0 - alpha) * prof[c]
    return prof


def dominant_register(profile: dict[str, float], min_weight: float = 0.34) -> str:
    """Return the highest-weight register category, or '' when the profile is
    empty or too flat to call (no category clears `min_weight`). A flat profile
    reads as *no signal*, not as 'neutral' — only a genuinely dominant 'neutral'
    is reported as such."""
    if not profile:
        return ""
    cat, weight = max(profile.items(), key=lambda kv: kv[1])
    return cat if weight >= min_weight else ""
