"""Phase 8 (colony features): aggregate-state neuromodulation feedback."""

from __future__ import annotations

import pytest

from brain.bus import Bus
from brain.clusters.hypothalamus import HypothalamusCluster
from brain.settings import settings


@pytest.fixture
def colony_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_state_feedback_gain", 0.02)
    monkeypatch.setitem(settings._data, "colony_state_feedback_clamp", 0.05)


def test_no_feedback_when_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    h = HypothalamusCluster(Bus())
    h._prev_aggregate = {"arousal": 1.0, "inhibition": 1.0}
    before = h._bus.neuromod.get("Glu")
    h._apply_state_feedback()
    assert h._bus.neuromod.get("Glu") == before


def test_no_feedback_without_prior_aggregate(colony_on):
    """Apply uses the PRIOR turn's aggregate; with none captured yet it's a no-op."""
    h = HypothalamusCluster(Bus())
    assert h._prev_aggregate is None
    before = h._bus.neuromod.get("Glu")
    h._apply_state_feedback()
    assert h._bus.neuromod.get("Glu") == before


def test_feedback_nudges_glu_and_gaba(colony_on):
    h = HypothalamusCluster(Bus())
    h._prev_aggregate = {"arousal": 1.0, "inhibition": 1.0}
    glu0, gaba0 = h._bus.neuromod.get("Glu"), h._bus.neuromod.get("GABA")
    h._apply_state_feedback()
    assert h._bus.neuromod.get("Glu") > glu0  # effort → arousal
    assert h._bus.neuromod.get("GABA") > gaba0  # conflict → caution


def test_feedback_contribution_is_clamped(colony_on, monkeypatch):
    monkeypatch.setitem(settings._data, "colony_state_feedback_gain", 1.0)  # would overshoot
    h = HypothalamusCluster(Bus())
    h._prev_aggregate = {"arousal": 1.0, "inhibition": 0.0}
    glu0 = h._bus.neuromod.get("Glu")
    h._apply_state_feedback()
    # gain*signal = 1.0 but clamped to colony_state_feedback_clamp (0.05)
    assert h._bus.neuromod.get("Glu") - glu0 == pytest.approx(0.05)


def test_feedback_self_limits_under_sustained_aggregate(colony_on):
    """Sustained high aggregate → Glu reaches a bounded steady state, no divergence."""
    h = HypothalamusCluster(Bus())
    h._prev_aggregate = {"arousal": 1.0, "inhibition": 0.0}
    glu_levels = []
    for _ in range(50):
        h._apply_state_feedback()
        h._bus.neuromod.decay(1.0)  # the per-turn homeostatic relaxation
        glu_levels.append(h._bus.neuromod.get("Glu"))
    # The safety property is CONVERGENCE (no runaway), not a specific ceiling:
    assert abs(glu_levels[-1] - glu_levels[-2]) < 0.005  # converged to a steady state
    assert glu_levels[-1] < 0.8  # stabilizes well below the 1.0 channel ceiling
    assert max(glu_levels) == glu_levels[-1]  # monotone approach — never overshoots/diverges
