"""Phase 4 (colony features): recruitment amplification via the RECRUIT channel."""

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


# ── Bus recruitment primitive ─────────────────────────────────────────────────


def test_recruit_noop_when_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    bus = Bus()
    bus.recruit("frontal", 0.8, now=0.0)
    assert bus.recruitment_level("frontal", now=0.0) == 0.0
    assert bus.recruit_channel("frontal", now=0.0) is None


def test_recruit_raises_level(colony_on):
    bus = Bus()
    bus.recruit("frontal", 0.6, now=0.0)
    assert bus.recruitment_level("frontal", now=0.0) == pytest.approx(0.6)
    bus.recruit("frontal", 0.6, now=0.0)  # accumulates, clamped to 1.0
    assert bus.recruitment_level("frontal", now=0.0) == pytest.approx(1.0)


def test_recruit_decays(colony_on):
    bus = Bus()
    bus.recruit("frontal", 1.0, now=0.0)
    # half-life 10s
    assert bus.recruitment_level("frontal", now=10.0) == pytest.approx(0.5, abs=1e-6)


def test_recruit_channel_maps_zero_to_neutral(colony_on):
    bus = Bus()
    # no recruitment → channel at 0.5 (neutral under (level-0.5) centering)
    assert bus.recruit_channel("frontal", now=0.0) == pytest.approx(0.5)
    bus.recruit("frontal", 1.0, now=0.0)
    assert bus.recruit_channel("frontal", now=0.0) == pytest.approx(1.0)


# ── RECRUIT lowers effective_threshold (pure neuron mechanism) ────────────────


def test_recruit_channel_lowers_threshold():
    sw = SwitchNeuron("recruitable", "frontal", threshold=0.5, modulators={"RECRUIT": -0.4})
    neutral = sw.effective_threshold({"RECRUIT": 0.5})  # zero recruitment
    full = sw.effective_threshold({"RECRUIT": 1.0})  # max recruitment
    assert neutral == pytest.approx(0.5)  # neutral = base threshold
    assert full < neutral  # recruitment lowers the bar
    assert full == pytest.approx(0.3)  # 0.5 + (-0.4)*(1.0-0.5)


def test_recruit_absent_channel_is_neutral():
    """When RECRUIT is not in the snapshot (colony off), the modulator is skipped."""
    sw = SwitchNeuron("recruitable", "frontal", threshold=0.5, modulators={"RECRUIT": -0.4})
    assert sw.effective_threshold({"DA": 0.5}) == pytest.approx(0.5)  # RECRUIT absent → no shift


def test_recruit_threshold_clamped():
    sw = SwitchNeuron(
        "recruitable", "frontal", threshold=0.2, modulators={"RECRUIT": -0.9}, min_threshold=0.1
    )
    assert sw.effective_threshold({"RECRUIT": 1.0}) == pytest.approx(0.1)  # clamped to min
