"""
stance_affinity — the pure math under the chemistry-modulated stance draw.

Two distinct effects, on the two stance axes (docs plan: approach competition, Phase B):

  AFFINITY (info axis) — which information POSTURE the current chemical state reaches
  for. Each stance-*.md file declares a small per-channel affinity map in frontmatter
  (e.g. ``affinity: {CORT: 0.7}`` on propose-before-acting): the data rides with the
  stance, not a hardcoded table in code. High CORT pulls toward caution and small
  reversible moves; high DA toward do-and-report; high NE toward verify/freshness;
  high OXT toward asking rather than assuming.

  COGNITIVE ECONOMY (method axis) — how DEEP a method the state can afford. A stressed
  brain does not run a twelve-step contemplative protocol; it reaches for the fast
  heuristic. Scored as congruence between a method's derived complexity and the current
  effort level (brain.budget.chem_effort — the same DA-up/CORT-down curve as every other
  effort bound in the brain).

  Both are BIASES, never gates: floored_softmax_pick keeps a floor probability under
  every candidate, so no chemical state can make a stance unreachable. If high CORT
  could *exclude* freshness-check, a stressed brain would become structurally unable to
  notice it needs current data — and the info axis carries authority over tool use, so
  that failure would silently suppress real action. Temperament colors cognition; it
  must never disable it.

Pure functions, no I/O, no state — every substrate keeps its own storage (mirrors
brain/bounded_ledger.py), so all of it is unit-testable without a brain.
"""

from __future__ import annotations

import hashlib
import math

# Channels an affinity map may reference: the neuromod bus (DA/GABA/ACh/NE/Glu/5HT) and
# the hormonal bus (CORT/OXT + 5HT again). Unknown keys in a stance's frontmatter are
# ignored rather than erroring — a typo in one stance file must not break the draw.
KNOWN_CHANNELS: frozenset[str] = frozenset({"DA", "GABA", "ACh", "NE", "Glu", "5HT", "CORT", "OXT"})


def affinity_score(chem: dict[str, float] | None, affinity_map: dict | None) -> float:
    """How strongly the current chemistry pulls toward a stance, in [-1, 1].

    Each entry ``{channel: coef}`` contributes coef × (level − 0.5) × 2 — positive coef
    means the stance is favored when the channel runs HIGH (and disfavored when low, the
    symmetric reading: ``{DA: -0.6}`` on answer-from-known = favored under LOW drive).
    Missing channels read as resting (0.5) and contribute nothing.
    """
    if not chem or not affinity_map:
        return 0.0
    total = 0.0
    for channel, coef in affinity_map.items():
        if channel not in KNOWN_CHANNELS:
            continue
        try:
            level = float(chem.get(channel, 0.5))
            total += float(coef) * (level - 0.5) * 2.0
        except (TypeError, ValueError):
            continue
    return max(-1.0, min(1.0, total))


def complexity_congruence(complexity: float, effort: float) -> float:
    """Cognitive-economy term in [-1, 0]: 0 when a method's depth matches what the
    state can afford, falling linearly with mismatch. Two-sided by design — stress
    pulls toward simple methods AND calm-motivated states pull toward depth (the
    converse of "a stressed person picks the quick heuristic" is also true)."""
    try:
        return -abs(max(0.0, min(1.0, float(complexity))) - max(0.0, min(1.0, float(effort))))
    except (TypeError, ValueError):
        return 0.0


def turn_seed(turn_id: str, salt: int) -> int:
    """Deterministic per-(turn, slot) seed — same construction as the drafter explore
    roll, so a draw is reproducible in tests and across retries of the same turn."""
    return int.from_bytes(hashlib.sha1(f"{turn_id}:stance:{salt}".encode()).digest()[:8], "big")


def floored_softmax_pick(
    ids: list[str],
    logits: list[float],
    *,
    floor: float,
    seed: int,
    temperature: float = 1.0,
) -> str | None:
    """Sample one id ∝ softmax(logits/temperature), with every candidate held at a
    probability floor (renormalized) — chemistry and learning BIAS the draw but can
    never zero a stance out. Deterministic given the seed."""
    if not ids or len(ids) != len(logits):
        return None
    temp = max(1e-3, float(temperature))
    mx = max(float(z) for z in logits)
    exps = [math.exp((float(z) - mx) / temp) for z in logits]
    total = sum(exps) or 1.0
    probs = [e / total for e in exps]
    f = max(0.0, min(1.0 / len(ids), float(floor)))
    probs = [max(p, f) for p in probs]
    norm = sum(probs)
    probs = [p / norm for p in probs]
    # Golden-ratio multiplicative mix → [0,1) well-spread even for SEQUENTIAL seeds,
    # so the draw doesn't silently depend on callers pre-hashing their seed.
    mixed = (seed * 0x9E3779B97F4A7C15) % (1 << 64)
    roll = mixed / float(1 << 64)
    acc = 0.0
    for sid, p in zip(ids, probs, strict=True):
        acc += p
        if roll < acc:
            return sid
    return ids[-1]
