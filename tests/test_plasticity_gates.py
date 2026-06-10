"""
Property tests pinning the GABA plasticity-gate semantics across both modes.

The graded_plasticity flag swaps two philosophies: legacy = skip learning
entirely on high-GABA single-draft turns; graded = dampen learning smoothly on
the inverted-U's descending limb. These tests pin the contract so the flag
transition stays predictable:

  1. graded never APPLIES MORE learning than legacy would allow where legacy
     skips outright is the wrong frame (legacy applies zero there) — instead:
     graded's effective weight change at high stress must be <= its own
     mid-band peak (descending limb actually descends).
  2. _turn_plasticity is continuous in GABA (no cliff at the knee).
  3. The clamp floor/ceiling hold across the whole grid.
  4. Legacy mode skips exactly where documented (high GABA AND single draft).
"""

from __future__ import annotations

from brain.hebbian import HebbianUpdater
from brain.observability.timeline import TurnTrace
from brain.settings import settings


def _trace(gaba: float, drafts: int = 1, emotion: str = "engaged") -> TurnTrace:
    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    t.neuromod = {"DA": 0.5, "GABA": gaba, "ACh": 0.4, "Glu": 0.3, "NE": 0.3}
    t.prior_neuromod = dict(t.neuromod)
    t.emotion = emotion
    t.draft_scores = [
        {"draft_id": f"d{i}", "overall": 0.7, "selected": i == 0, "critic_ran": True}
        for i in range(drafts)
    ]
    return t


def _updater() -> HebbianUpdater:
    return HebbianUpdater(None)


GABA_GRID = [i / 20 for i in range(21)]  # 0.00 .. 1.00


def test_turn_plasticity_clamped_over_full_grid():
    u = _updater()
    lo = float(settings.get("plasticity_turn_min", 0.4))
    hi = float(settings.get("plasticity_turn_max", 1.3))
    for g in GABA_GRID:
        p = u._turn_plasticity(_trace(g))
        assert lo <= p <= hi, f"plasticity {p} out of [{lo},{hi}] at GABA={g}"


def test_turn_plasticity_continuous_in_gaba():
    """No cliff: adjacent grid points (Δ=0.05 GABA) move plasticity smoothly."""
    u = _updater()
    vals = [u._turn_plasticity(_trace(g)) for g in GABA_GRID]
    for a, b in zip(vals, vals[1:]):
        assert abs(b - a) < 0.15, f"plasticity cliff: {a} → {b}"


def test_turn_plasticity_descending_limb_descends():
    """Above the stress knee, more GABA must never mean MORE plasticity."""
    u = _updater()
    knee = float(settings.get("plasticity_stress_knee", 0.7))
    above = [g for g in GABA_GRID if g >= knee]
    vals = [u._turn_plasticity(_trace(g)) for g in above]
    for a, b in zip(vals, vals[1:]):
        assert b <= a + 1e-9, "plasticity rose on the descending limb"


def test_legacy_skip_fires_exactly_where_documented(monkeypatch):
    u = _updater()
    monkeypatch.setattr(
        settings, "_data", {**settings._data, "graded_plasticity": 0}, raising=False
    )
    thr = float(settings.get("gaba_skip_threshold_high"))

    skip, reason = u._should_skip_hebbian(_trace(thr + 0.1, drafts=1), outcome=0.5)
    assert skip and reason == "defuse_path"

    # Multi-draft turns are exempt even at high GABA.
    skip, _ = u._should_skip_hebbian(_trace(thr + 0.1, drafts=3), outcome=0.5)
    assert not skip

    # Below threshold never defuses.
    skip, _ = u._should_skip_hebbian(_trace(thr - 0.1, drafts=1), outcome=0.5)
    assert not skip


def test_graded_mode_never_skips_on_gaba(monkeypatch):
    """Graded mode replaces the all-or-nothing skip with damping: the same
    high-GABA single-draft turn must NOT be skipped (it learns less, not zero)."""
    u = _updater()
    monkeypatch.setattr(
        settings, "_data", {**settings._data, "graded_plasticity": 1}, raising=False
    )
    thr = float(settings.get("gaba_skip_threshold_high"))
    skip, _ = u._should_skip_hebbian(_trace(thr + 0.1, drafts=1), outcome=0.5)
    assert not skip
    # And the damping is real: high-stress plasticity below mid-band plasticity.
    p_high = u._turn_plasticity(_trace(0.95))
    p_mid = u._turn_plasticity(_trace(0.35))
    assert p_high < p_mid


def test_near_zero_outcome_skips_in_both_modes(monkeypatch):
    u = _updater()
    for flag in (0, 1):
        monkeypatch.setattr(
            settings, "_data", {**settings._data, "graded_plasticity": flag}, raising=False
        )
        skip, reason = u._should_skip_hebbian(_trace(0.2), outcome=0.005)
        assert skip and reason == "outcome_near_zero"
