"""Phase 7 (colony features): graded mobilization cascade.

Recruitment (Phase 4) sweeps across a threshold-diverse population (Phase 5):
low-threshold responders fire first, higher-threshold reserves only as the need
escalates — and the positive feedback self-limits (bounded, decaying).
"""

from __future__ import annotations

import pytest

from brain.bus import Bus
from brain.neuron import SwitchNeuron
from brain.settings import settings


@pytest.fixture
def colony_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_conc_half_life_s", 10.0)
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)


def test_recruits_in_threshold_order(colony_on):
    """A diverse population mobilizes low→high as recruitment rises."""
    bus = Bus()
    # low / mid / high responders (threshold diversity, à la Phase 5)
    pop = [
        SwitchNeuron("low", "frontal", threshold=0.30, modulators={"RECRUIT": -0.4}),
        SwitchNeuron("mid", "frontal", threshold=0.50, modulators={"RECRUIT": -0.4}),
        SwitchNeuron("high", "frontal", threshold=0.70, modulators={"RECRUIT": -0.4}),
    ]
    input_level = 0.35  # a fixed, modest need signal

    def fired_at(recruit_amount, now):
        bus.recruit("frontal", recruit_amount, now=now)
        rc = bus.recruit_channel("frontal", now=now)
        snap = {"RECRUIT": rc}
        return [sw.name for sw in pop if sw.should_fire(input_level, snap)]

    none = [sw.name for sw in pop if sw.should_fire(input_level, {"RECRUIT": 0.5})]
    some = fired_at(0.5, now=0.0)
    more = fired_at(0.6, now=0.0)  # cumulative recruitment higher

    assert none == ["low"]  # only the low-threshold first responder
    # progressive: fired set only grows, and never includes high before mid
    assert set(none) <= set(some) <= set(more)
    for fired in (none, some, more):
        if "high" in fired:
            assert "mid" in fired and "low" in fired
        if "mid" in fired:
            assert "low" in fired


def test_cascade_self_limits_under_sustained_need(colony_on):
    """Sustained recruitment reaches a bounded steady state — no runaway."""
    bus = Bus()
    levels = []
    for t in range(0, 300, 5):
        bus.recruit("frontal", 0.30, now=float(t))
        levels.append(bus.recruitment_level("frontal", now=float(t)))
    assert max(levels) <= 1.0  # clamped — never diverges
    assert levels[-1] > 0.5  # but sustained need does keep it mobilized
    # monotone-ish convergence: late values are stable, not exploding
    assert abs(levels[-1] - levels[-2]) < 0.1


def test_no_mobilization_without_need(colony_on):
    """With no recruit() calls, recruitment stays at zero (channel neutral)."""
    bus = Bus()
    assert bus.recruitment_level("frontal", now=0.0) == 0.0
    assert bus.recruit_channel("frontal", now=0.0) == pytest.approx(0.5)  # neutral
