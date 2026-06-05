"""Phase 6 (colony features): silence-triggered recall in the DMN."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.dmn as dmn_mod
from brain.bus import Bus, Message
from brain.settings import settings
from brain.sequence_predictor import SequencePredictor


@pytest.fixture
def colony_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_conc_half_life_s", 10.0)
    monkeypatch.setitem(settings._data, "colony_arm_threshold", 1.0)
    monkeypatch.setitem(settings._data, "colony_silence_floor", 0.15)
    monkeypatch.setitem(settings._data, "colony_silence_disarm_s", 1e12)  # never disarm in-test
    # idle so the silence step is eligible
    monkeypatch.setattr(dmn_mod, "get_idle_seconds", lambda: 60.0)


def _make_dmn(bus):
    dmn = dmn_mod.DefaultModeNetwork.__new__(dmn_mod.DefaultModeNetwork)
    dmn._seq_predictor = SequencePredictor()
    dmn._bus = bus
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=[0.0] * 16)
    dmn._hippocampus = MagicMock()
    dmn._hippocampus.recall = AsyncMock(
        return_value={"episodes": "they said X; I replied Y", "recall_affect": {"ACh": 0.1}}
    )
    dmn._memory_seed = ""
    return dmn


def _drive_to_quiet(bus, topic="threat"):
    bus.track_concentration(topic)
    bus._accumulate(Message(topic=topic, payload={"entities": ["deadline", "sarah"]}, source="t",
                            confidence=2.0), now=0.0)
    assert bus.is_quiet(topic, now=40.0) is True  # ARMED→QUIET, onset flagged


async def test_silence_recall_fires_on_quiet_onset(colony_on):
    bus = Bus()
    _drive_to_quiet(bus)
    dmn = _make_dmn(bus)
    ach_before = bus.neuromod.get("ACh")

    await dmn._run_silence_recall("dmn_1")

    dmn._hippocampus.recall.assert_awaited_once()
    # cue built from the captured context ring
    _, kwargs = dmn._hippocampus.recall.call_args
    assert "deadline" in kwargs["query"] and "sarah" in kwargs["query"]
    # recalled episodes surfaced as a monologue seed
    assert "they said X" in dmn._memory_seed
    # recall_affect recolored chemistry
    assert bus.neuromod.get("ACh") > ach_before


async def test_silence_recall_debounced_fires_once(colony_on):
    bus = Bus()
    _drive_to_quiet(bus)
    dmn = _make_dmn(bus)
    await dmn._run_silence_recall("dmn_1")
    await dmn._run_silence_recall("dmn_2")  # no fresh onset
    assert dmn._hippocampus.recall.await_count == 1


async def test_silence_recall_suppressed_when_active(colony_on, monkeypatch):
    """Mid-exchange (OS not idle) → no recall even on a quiet onset."""
    monkeypatch.setattr(dmn_mod, "get_idle_seconds", lambda: 5.0)
    bus = Bus()
    _drive_to_quiet(bus)
    dmn = _make_dmn(bus)
    await dmn._run_silence_recall("dmn_1")
    dmn._hippocampus.recall.assert_not_awaited()


async def test_no_recall_without_quiet_onset(colony_on):
    bus = Bus()
    bus.track_concentration("threat")
    # Armed and still hot (accumulate at real time so it hasn't decayed to quiet
    # by the time the DMN reads it at wall-clock time).
    bus._accumulate(Message(topic="threat", payload={}, source="t", confidence=2.0))
    assert bus.topic_status("threat") == "armed"
    dmn = _make_dmn(bus)
    await dmn._run_silence_recall("dmn_1")
    dmn._hippocampus.recall.assert_not_awaited()
