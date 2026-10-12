"""Metacognition's appraisal (sympathetic, proud, apologetic...) colours the NEXT
turn of the conversation it appraised. It used to ride the bus on the default
30 s message TTL, so in voice sessions (replies > 30 s apart) it never landed, and
because the bus is shared per tenant it could land on another end user's turn
inside the window. It is now filed per conversation lane (parietal.lane_key) and
consumed once, by that lane's next turn."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import MagicMock

import pytest

from brain.bus import DEFAULT_TTL, Bus
from brain.clusters.hypothalamus import META_OVERRIDE_MAX_AGE_S, HypothalamusCluster
from brain.clusters.parietal import lane_key
from brain.turn_ctx import bind_turn

_FEATURES = {
    "sentiment": 0.0,
    "hostility": 0.0,
    "salience": 0.3,
    "surprise_score": 0.0,
    "topic_summary": "test",
}


def _as(end_user: str, partner: str = "p1", agent: str = "haven.therapist"):
    return bind_turn(
        "agent", session_id="s1", agent_id=agent, end_user_id=end_user, partner_id=partner
    )


def _meta(bus):
    from brain.metacognition import MetacognitionCell

    meta = MetacognitionCell.__new__(MetacognitionCell)
    meta._bus = bus
    meta._schema = None
    meta._turn_stats = deque(maxlen=20)
    meta._neuromod_history = deque(maxlen=50)
    meta._override_cooldowns = {}
    meta._cooldown_turns = 3
    return meta


async def _appraise_sympathy(meta, end_user: str) -> None:
    """End of `end_user`'s turn: they said they are struggling."""
    with _as(end_user):
        await meta._appraise_and_emit({"user_emotion": "struggling"}, {}, [], lane=lane_key())


async def _turn(hypo, end_user: str | None) -> dict:
    """The hypothalamus step of a turn on that lane (None = the owner's console)."""
    if end_user is None:
        return await hypo.process(dict(_FEATURES))
    with _as(end_user):
        return await hypo.process(dict(_FEATURES))


def _later(monkeypatch, seconds: float) -> None:
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)


@pytest.fixture
def brain():
    bus = Bus()
    return _meta(bus), HypothalamusCluster(bus)


async def test_override_reaches_the_same_lanes_next_turn_after_30s(brain, monkeypatch):
    meta, hypo = brain
    await _appraise_sympathy(meta, "alice")
    _later(monkeypatch, DEFAULT_TTL + 15)  # a voice reply, well past the bus TTL

    affect = await _turn(hypo, "alice")
    assert affect["emotion"] == "sympathetic"
    assert affect["emotion_source"] == "metacognition"


async def test_override_does_not_reach_another_lane(brain):
    meta, hypo = brain
    await _appraise_sympathy(meta, "alice")

    assert (await _turn(hypo, "bob"))["emotion_source"] == "neuromod"
    assert (await _turn(hypo, None))["emotion_source"] == "neuromod"
    with bind_turn(
        "agent", session_id="s1", agent_id="haven.therapist", end_user_id="alice", partner_id="p2"
    ):
        # Same end-user id under another partner is another person.
        assert (await hypo.process(dict(_FEATURES)))["emotion_source"] == "neuromod"
    # Other lanes' turns drained the bus but left alice's appraisal waiting for her.
    assert (await _turn(hypo, "alice"))["emotion"] == "sympathetic"


async def test_override_is_consumed_once(brain):
    meta, hypo = brain
    await _appraise_sympathy(meta, "alice")

    assert (await _turn(hypo, "alice"))["emotion"] == "sympathetic"
    second = await _turn(hypo, "alice")
    assert second["emotion_source"] == "neuromod"
    assert second["emotion_override_reason"] is None


async def test_newest_appraisal_per_lane_wins(brain):
    meta, hypo = brain
    await _appraise_sympathy(meta, "alice")
    with _as("alice"):
        await meta._appraise_and_emit(
            {}, {}, [{"selected": True, "overall": 0.93}], lane=lane_key()
        )

    assert (await _turn(hypo, "alice"))["emotion"] == "proud"
    assert (await _turn(hypo, "alice"))["emotion_source"] == "neuromod"


async def test_appraisal_past_the_staleness_cap_is_dropped(brain, monkeypatch):
    meta, hypo = brain
    await _appraise_sympathy(meta, "alice")
    _later(monkeypatch, META_OVERRIDE_MAX_AGE_S + 60)

    assert (await _turn(hypo, "alice"))["emotion_source"] == "neuromod"


async def test_self_directed_pride_is_felt_not_voiced_on_its_lane(brain, monkeypatch):
    meta, hypo = brain
    with _as("alice"):
        await meta._appraise_and_emit(
            {}, {}, [{"selected": True, "overall": 0.93}], lane=lane_key()
        )
    _later(monkeypatch, DEFAULT_TTL + 15)

    assert (await _turn(hypo, "bob"))["emotion_self_directed"] is False
    affect = await _turn(hypo, "alice")
    assert affect["emotion"] == "proud"
    assert affect["emotion_self_directed"] is True
    assert affect["expression_basis"] != "proud"


async def test_record_turn_stamps_the_turns_own_lane():
    published: list[dict] = []

    class _Bus:
        neuromod = MagicMock()

        async def publish_dict(self, topic, payload, **kw):
            published.append({"topic": topic, "payload": payload, **kw})

    meta = _meta(_Bus())
    meta._reward_aesthetic = lambda scores: None
    with _as("alice"):
        alice_lane = lane_key()
        meta.record_turn(
            turn_id="t1",
            llm_calls=1,
            elapsed_s=1.0,
            emotion="neutral",
            neuromod={},
            surprise_score=0.5,
            features={"user_emotion": "struggling"},
        )
    for _ in range(5):  # the appraisal is fire-and-forget
        await asyncio.sleep(0)

    [msg] = [p for p in published if p["topic"] == "meta.emotion_override"]
    assert msg["payload"]["lane"] == alice_lane != lane_key()
    assert msg["ttl"] >= META_OVERRIDE_MAX_AGE_S


async def test_lane_scoping_kill_switch_still_lands_turn_based(brain, monkeypatch):
    from brain.settings import settings

    meta, hypo = brain
    real_get = settings.get
    monkeypatch.setattr(
        settings, "get", lambda k, *a: 0 if k == "engine_lane_scoping" else real_get(k, *a)
    )
    await _appraise_sympathy(meta, "alice")
    _later(monkeypatch, DEFAULT_TTL + 15)
    # Switch off = one shared lane (the pre-fix sharing), but still one-shot and
    # not bound by the 30 s bus TTL.
    assert (await _turn(hypo, "bob"))["emotion"] == "sympathetic"
    assert (await _turn(hypo, "bob"))["emotion_source"] == "neuromod"
