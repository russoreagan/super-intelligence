"""
Budget primitives — plus the inventory of every spend/effort bound in the brain.

The 2026-07 review found budget logic scattered with inconsistent chemistry
multipliers. Most "budgets" are genuinely different mechanisms and stay owned by
their modules; this module owns the ONE shared primitive (the chemistry→effort
curve) and serves as the map to everything else:

  EFFORT (how hard to try this turn/job)
  - chem_budget() here — THE canonical DA-up/CORT-down curve.
    Consumers: motor_cortex._effective_budget (tools/turn, base 3, [1,5]),
    motor_cortex._effective_job_budget (steps/job, base 12, [6,20]).
  - brainstem.check_budget — HARD per-turn LLM-call ceiling
    (BRAIN_MAX_LLM_CALLS_PER_TURN). A tripwire, not chemistry-modulated.

  ATTENTION (what to surface / recall)
  - hippocampus._allocate_recall_budget — splits a fixed recall fan-out between
    schema and episodes by strategy weight (allocation, not effort).
  - dmn._routing_budget_from — idle-thought surfacing budget from ACh + user
    load signals (its own curve; conversational, not task effort).

  MONEY (hard dollar ceilings — never chemistry-modulated)
  - model_router._enforce_cloud_budget / cloud_budget_exhausted — daily USD cap;
    the exhausted() form is the gate for out-of-band spenders (CMA).
  - brain.autonomy.AutonomousBudget — the autonomous-only pool ($30 soft pause /
    $50 hard stop) enforced by SpendRiskGate before a job plans and by the CMA
    mid-flight backstop while it runs.
  - model_router bg token bucket (bg_cloud_token_rate) — background rate limit.

Chemistry may modulate EFFORT and ATTENTION. It must never widen MONEY.
"""

from __future__ import annotations


def chem_budget(chem: dict[str, float] | None, *, base: int, gain: float, lo: int, hi: int) -> int:
    """The canonical chemistry→effort curve: DA (motivated pursuit) raises the
    budget, CORT (stress) lowers it, symmetrically by `gain`, clamped to
    [lo, hi]. Missing/empty chemistry → `base` unchanged. Both neuromod values
    are centred on 0.5, so resting chemistry is exactly `base`."""
    if not chem:
        return int(base)
    da = float(chem.get("DA", 0.5))
    cort = float(chem.get("CORT", 0.5))
    shift = (da - 0.5) * gain - (cort - 0.5) * gain
    return max(lo, min(hi, int(base) + int(round(shift))))


def chem_effort(chem: dict[str, float] | None, *, gain: float = 1.0) -> float:
    """Float sibling of chem_budget: the same DA-up/CORT-down curve as a normalized
    effort level in [0, 1] instead of a clamped integer. 0.5 at resting chemistry.

    Consumed by the stance draw's cognitive-economy term: how DEEP a reasoning method
    the current state can afford (a stressed brain reaches for the fast heuristic, a
    motivated calm one can carry a contemplative protocol). Same file, same invariant:
    chemistry modulates EFFORT and ATTENTION — never money."""
    if not chem:
        return 0.5
    da = float(chem.get("DA", 0.5))
    cort = float(chem.get("CORT", 0.5))
    return max(0.0, min(1.0, 0.5 + (da - 0.5) * gain - (cort - 0.5) * gain))
