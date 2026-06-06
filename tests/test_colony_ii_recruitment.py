"""Colony Layer II — C3 (satisfaction/stop), C4 (quorum slope), N2 (softmax allocation)."""

from __future__ import annotations

import pytest

from brain.bus import Bus, Message
from brain.settings import settings


@pytest.fixture
def colony_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_conc_half_life_s", 1e9)  # freeze decay in-test


# ── C3: satisfaction / stop signal ────────────────────────────────────────────


def test_satisfy_lowers_recruitment_faster_than_decay(colony_on, monkeypatch):
    monkeypatch.setitem(settings._data, "colony_satisfy_rate", 0.5)
    bus = Bus()
    bus.recruit("frontal", 1.0, now=0.0)
    assert bus.recruitment_level("frontal", now=0.0) == pytest.approx(1.0)
    bus.satisfy("frontal", 1.0, now=0.0)  # full satisfaction removes 50% (rate)
    assert bus.recruitment_level("frontal", now=0.0) == pytest.approx(0.5)


def test_satisfy_noop_when_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    bus = Bus()
    bus.satisfy("frontal", 1.0, now=0.0)  # no error, no effect
    assert bus.recruitment_level("frontal", now=0.0) == 0.0


def test_recruit_then_satisfy_converges_no_oscillation(colony_on):
    """A recruit→satisfy loop on success settles, rather than oscillating up/down."""
    bus = Bus()
    levels = []
    for t in range(20):
        bus.recruit("frontal", 0.3, now=float(t))
        bus.satisfy("frontal", 0.8, now=float(t))  # strong satisfaction each success
        levels.append(bus.recruitment_level("frontal", now=float(t)))
    assert max(levels) <= 1.0
    assert levels[-1] < 0.6  # satisfaction holds it down — not pinned high


# ── C4: rate-of-change quorum ─────────────────────────────────────────────────


@pytest.fixture
def slope_cfg(colony_on, monkeypatch):
    monkeypatch.setitem(settings._data, "colony_arm_threshold", 0.5)
    monkeypatch.setitem(
        settings._data, "colony_quorum_threshold", 5.0
    )  # high → level alone won't trip
    monkeypatch.setitem(settings._data, "colony_quorum_slope_threshold", 0.2)


def _acc(bus, conf, now, topic="threat"):
    bus._accumulate(Message(topic=topic, payload={}, source="t", confidence=conf), now=now)


def test_fast_rise_trips_quorum_via_slope(slope_cfg):
    bus = Bus()
    bus.track_concentration("threat")
    _acc(bus, 0.6, now=0.0)  # armed (≥0.5), below level threshold
    _acc(bus, 0.6, now=1.0)  # +0.6 in 1s → slope 0.6 ≥ 0.2
    assert bus.concentration("threat", now=1.0) < 5.0  # not via level
    assert bus.quorum("threat", now=1.0) is True  # via slope


def test_slow_rise_does_not_trip_quorum(slope_cfg):
    bus = Bus()
    bus.track_concentration("threat")
    _acc(bus, 0.6, now=0.0)
    _acc(bus, 0.6, now=100.0)  # +0.6 over 100s → slope ~0.006 < 0.2
    assert bus.quorum("threat", now=100.0) is False


# ── N2: softmax multi-need allocation ─────────────────────────────────────────


def test_softmax_allocates_more_to_higher_need(colony_on, monkeypatch):
    monkeypatch.setitem(settings._data, "colony_recruit_budget", 1.0)
    monkeypatch.setitem(settings._data, "colony_recruit_softmax_temp", 0.5)
    bus = Bus()
    bus.allocate_recruitment({"frontal": 1.0, "hippocampus": 0.0}, now=0.0)
    f = bus.recruitment_level("frontal", now=0.0)
    h = bus.recruitment_level("hippocampus", now=0.0)
    assert f > h
    assert f + h == pytest.approx(1.0, abs=1e-6)  # budget × saturation(=1.0)


def test_softmax_zero_need_is_noop(colony_on):
    bus = Bus()
    bus.allocate_recruitment({"frontal": 0.0, "hippocampus": 0.0}, now=0.0)
    assert bus.recruitment_level("frontal", now=0.0) == 0.0
    assert bus.recruitment_level("hippocampus", now=0.0) == 0.0


def test_softmax_temperature_sharpens(colony_on, monkeypatch):
    monkeypatch.setitem(settings._data, "colony_recruit_budget", 1.0)
    needs = {"a": 1.0, "b": 0.5}
    bus_hot = Bus()
    monkeypatch.setitem(settings._data, "colony_recruit_softmax_temp", 2.0)  # softer
    bus_hot.allocate_recruitment(needs, now=0.0)
    soft_gap = bus_hot.recruitment_level("a", now=0.0) - bus_hot.recruitment_level("b", now=0.0)
    bus_cold = Bus()
    monkeypatch.setitem(settings._data, "colony_recruit_softmax_temp", 0.2)  # sharper
    bus_cold.allocate_recruitment(needs, now=0.0)
    sharp_gap = bus_cold.recruitment_level("a", now=0.0) - bus_cold.recruitment_level("b", now=0.0)
    assert sharp_gap > soft_gap
