"""
The thalamus as the global-workspace spotlight (GWT).

These pin the core the consumers depend on: ignition from sustained accumulation,
cross-turn persistence, the real attention.focus subscriber, the emotion-veto wake,
and the hard requirement that the disabled path is a neutral no-op.
"""

from __future__ import annotations

import asyncio

import pytest

from brain.bus import Bus
from brain.clusters.thalamus import ThalamusCluster, _neutral_verdict
from brain.predictor import should_bypass_gating
from brain.settings import settings


@pytest.fixture
def bus() -> Bus:
    """A bus with the workspace topics registered exactly as session_setup does."""
    b = Bus()
    b.track_concentration("affect.state", lambda p: max(0.0, p.get("GABA", 0.0) - 0.2))
    b.track_concentration("temporal.features", lambda p: max(0.0, p.get("salience", 0.0) - 0.3))
    b.track_concentration("mem.recall", lambda p: 1.0)
    return b


@pytest.fixture(autouse=True)
def _restore_settings():
    keep = {
        k: settings.get(k) for k in ("thalamus_workspace_enabled", "workspace_ignition_threshold")
    }
    yield
    settings.update(keep)


async def _flood(bus: Bus, topic: str, payload: dict, n: int = 12) -> None:
    for _ in range(n):
        await bus.publish_dict(topic, payload, source="test")


def test_cold_start_not_ignited(bus):
    th = ThalamusCluster(bus)
    v = asyncio.run(th.route({"salience": 0.3}, {"emotion": "neutral"}))
    assert v["ignited"] is False
    assert v["focus"] is None
    assert v["hot_entities"] == []


def test_sustained_threat_ignites_with_content(bus):
    th = ThalamusCluster(bus)

    async def scenario():
        await _flood(bus, "affect.state", {"GABA": 0.9, "tags": ["audit", "deadline"]})
        return await th.route({"salience": 0.3}, {"emotion": "calm"})

    v = asyncio.run(scenario())
    assert v["ignited"] is True
    assert v["focus"] == "affect.state"
    assert v["coalition"] == "threat"
    assert v["salience"] >= settings.get("workspace_ignition_threshold")
    # Content, not just a channel: the hot entities the coalition carried.
    assert "audit" in v["hot_entities"] and "deadline" in v["hot_entities"]


def test_sustained_turns_accumulates_then_resets(bus):
    th = ThalamusCluster(bus)

    async def scenario():
        await _flood(bus, "affect.state", {"GABA": 0.9, "tags": ["x"]})
        streaks = []
        for _ in range(3):
            # keep it hot so it stays ignited across turns
            await bus.publish_dict("affect.state", {"GABA": 0.9, "tags": ["x"]}, source="t")
            v = await th.route({"salience": 0.3}, {"emotion": "calm"})
            streaks.append(v["sustained_turns"])
        # let it go cold: a long-enough silence decays it below threshold
        v_cold = await th.route(
            {"salience": 0.3},
            {"emotion": "calm"},
        )
        return streaks, v_cold

    streaks, _ = asyncio.run(scenario())
    assert streaks == [1, 2, 3], streaks


def test_attention_focus_has_a_real_subscriber(bus):
    """The paper's claim: promotion to attention.focus makes it available
    system-wide. A subscriber must actually receive the broadcast on ignition."""
    th = ThalamusCluster(bus)
    inbox = bus.subscribe("attention.focus")

    async def scenario():
        await _flood(bus, "affect.state", {"GABA": 0.9, "tags": ["audit"]})
        await th.route({"salience": 0.3}, {"emotion": "calm"})

    asyncio.run(scenario())
    msg = inbox.get_nowait()
    assert msg.payload["cluster"] == "affect.state"
    assert msg.payload["coalition"] == "threat"
    assert "audit" in msg.payload["hot_entities"]


def test_current_spotlight_reflects_latest(bus):
    th = ThalamusCluster(bus)

    async def scenario():
        await _flood(bus, "affect.state", {"GABA": 0.9, "tags": ["audit"]})
        await th.route({"salience": 0.3}, {"emotion": "calm"})

    asyncio.run(scenario())
    assert th.current_spotlight()["ignited"] is True
    assert th.current_spotlight()["coalition"] == "threat"


def test_threat_ignition_wakes_the_integrator(bus):
    """Ignition pulls in a specialist: a threat coalition forces the gate open even
    when this turn's own affect would not have. Non-threat ignition does not."""
    th = ThalamusCluster(bus)

    async def scenario():
        await _flood(bus, "affect.state", {"GABA": 0.9, "tags": ["audit"]})
        return await th.route({"salience": 0.3}, {"emotion": "calm"})

    threat = asyncio.run(scenario())
    # current turn's affect is calm — only the workspace ignition trips it
    woke, reason = should_bypass_gating({"emotion": "calm", "spotlight": threat}, {})
    assert woke is True and reason == "workspace_ignition"

    # a salience coalition ignites but must NOT force the wake (conservative)
    salience_verdict = dict(threat, coalition="salience")
    woke2, _ = should_bypass_gating({"emotion": "calm", "spotlight": salience_verdict}, {})
    assert woke2 is False


def test_disabled_is_a_neutral_no_op(bus):
    settings.update({"thalamus_workspace_enabled": 0})
    th = ThalamusCluster(bus)
    inbox = bus.subscribe("attention.focus")

    async def scenario():
        # even under heavy accumulation, disabled yields the neutral verdict
        await _flood(bus, "affect.state", {"GABA": 0.95, "tags": ["audit"]})
        return await th.route({"salience": 0.95}, {"emotion": "calm"})

    v = asyncio.run(scenario())
    assert v == _neutral_verdict()
    # and it never broadcasts
    with pytest.raises(asyncio.QueueEmpty):
        inbox.get_nowait()
    # a consumer reading the neutral spotlight is never woken by it
    woke, _ = should_bypass_gating({"emotion": "calm", "spotlight": v}, {})
    assert woke is False
