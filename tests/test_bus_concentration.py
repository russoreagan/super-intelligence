"""
Phase 2 (colony features): Bus topic concentration / quorum / silence-as-signal.

The crux is the UNARMED → ARMED → QUIET state machine: cold-start silence must
never read as signal, only a topic that was active and then went quiet. Decay is
driven by injected timestamps (no wall-clock) for determinism.
"""

from __future__ import annotations

import pytest

from brain.bus import CONC_ARMED, CONC_QUIET, CONC_UNARMED, Bus, Message
from brain.settings import settings


@pytest.fixture
def colony_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_conc_half_life_s", 10.0)
    monkeypatch.setitem(settings._data, "colony_conc_cap", 10.0)
    monkeypatch.setitem(settings._data, "colony_arm_threshold", 1.0)
    monkeypatch.setitem(settings._data, "colony_quorum_threshold", 1.5)
    monkeypatch.setitem(settings._data, "colony_silence_floor", 0.15)
    monkeypatch.setitem(settings._data, "colony_silence_disarm_s", 100.0)


def _msg(topic="threat", confidence=1.0, payload=None):
    return Message(topic=topic, payload=payload or {}, source="test", confidence=confidence)


def test_no_op_when_colony_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(confidence=5.0), now=0.0)
    assert bus.concentration("threat", now=0.0) == 0.0
    assert bus.topic_status("threat", now=0.0) == CONC_UNARMED


def test_untracked_topic_never_accumulates(colony_on):
    bus = Bus()  # nothing registered
    bus._accumulate(_msg(topic="random", confidence=5.0), now=0.0)
    assert bus.concentration("random", now=0.0) == 0.0


def test_cold_start_is_never_quiet(colony_on):
    """UNARMED topic with zero concentration must NOT report as quiet."""
    bus = Bus()
    bus.track_concentration("threat")
    assert bus.topic_status("threat", now=0.0) == CONC_UNARMED
    assert bus.is_quiet("threat", now=0.0) is False
    # ...even after a long time with no activity
    assert bus.is_quiet("threat", now=50.0) is False


def test_unarmed_to_armed(colony_on):
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(confidence=0.5), now=0.0)
    assert bus.topic_status("threat", now=0.0) == CONC_UNARMED  # below arm threshold
    bus._accumulate(_msg(confidence=0.8), now=0.0)  # cumulative 1.3 ≥ 1.0
    assert bus.topic_status("threat", now=0.0) == CONC_ARMED


def test_quorum_requires_armed_and_threshold(colony_on):
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(confidence=1.2), now=0.0)  # armed but < 1.5
    assert bus.topic_status("threat", now=0.0) == CONC_ARMED
    assert bus.quorum("threat", now=0.0) is False
    bus._accumulate(_msg(confidence=0.5), now=0.0)  # now 1.7 ≥ 1.5
    assert bus.quorum("threat", now=0.0) is True


def test_armed_to_quiet_via_decay(colony_on):
    """ARMED concentration decaying below the silence floor → QUIET (and onset fires once)."""
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(confidence=2.0), now=0.0)
    assert bus.topic_status("threat", now=0.0) == CONC_ARMED
    # half-life 10s: after ~40s, 2.0 → 0.125 < floor 0.15
    assert bus.is_quiet("threat", now=40.0) is True
    # fire-once edge
    assert bus.consume_quiet_onset("threat", now=40.0) is True
    assert bus.consume_quiet_onset("threat", now=40.0) is False


def test_quiet_back_to_armed_on_refire(colony_on):
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(confidence=2.0), now=0.0)
    assert bus.is_quiet("threat", now=40.0) is True
    bus._accumulate(_msg(confidence=2.0), now=41.0)  # re-fires above arm
    assert bus.topic_status("threat", now=41.0) == CONC_ARMED


def test_disarm_after_long_zero_dwell(colony_on):
    """After a long dwell at ~zero, the topic disarms back to UNARMED so stale
    silence stops being meaningful."""
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(confidence=2.0), now=0.0)
    assert bus.is_quiet("threat", now=40.0) is True  # QUIET (below floor, not yet ~zero)
    # zero-dwell clock starts when concentration is first observed at ~zero...
    assert bus.topic_status("threat", now=100.0) == CONC_QUIET  # level ~0.002, clock starts
    # ...and disarms a full disarm window (100s) later.
    assert bus.topic_status("threat", now=220.0) == CONC_UNARMED
    assert bus.is_quiet("threat", now=220.0) is False


def test_magnitude_fn_extracts_payload(colony_on):
    """A registered magnitude_fn weights accumulation by a payload field (GABA prototype)."""
    bus = Bus()
    bus.track_concentration(
        "affect.state",
        lambda p: max(0.0, float((p.get("neuromod") or {}).get("GABA", 0.0)) - 0.2),
    )
    # resting GABA contributes nothing
    bus._accumulate(_msg(topic="affect.state", payload={"neuromod": {"GABA": 0.05}}), now=0.0)
    assert bus.concentration("affect.state", now=0.0) == 0.0
    # elevated GABA accumulates (0.7 - 0.2 = 0.5 each)
    for t in range(4):
        bus._accumulate(
            _msg(topic="affect.state", payload={"neuromod": {"GABA": 0.7}}), now=float(t)
        )
    assert bus.concentration("affect.state", now=3.0) > 1.0


def test_context_ring_captures_tags(colony_on):
    bus = Bus()
    bus.track_concentration("threat")
    bus._accumulate(_msg(payload={"entities": ["intruder", "loud noise"]}), now=0.0)
    ctx = bus.concentration_context("threat")
    assert ctx and ctx[-1]["tags"] == ["intruder", "loud noise"]
    assert "neuromod" in ctx[-1]


def test_concentration_capped(colony_on):
    bus = Bus()
    bus.track_concentration("threat")
    for _ in range(50):
        bus._accumulate(_msg(confidence=5.0), now=0.0)
    assert bus.concentration("threat", now=0.0) == pytest.approx(10.0)  # colony_conc_cap
