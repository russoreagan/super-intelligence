"""Org-wide answer_only and the three API-layer guards.

The effective flag is org setting OR turn declaration OR agent permission — any
one restricts, none widens. Three holes existed even with the flag set:

  Guard 1  api_turn lifted whatever sat on the process-global pending slot onto
           the turn's response, so a stray parked write from another session could
           surface as this answer-only turn's confirmation.
  Guard 2  the three confirmation emitters (POST, SSE done, WS done) had no
           explicit answer_only check.
  Guard 3  a background job minted by an EARLIER normal turn re-binds its origin
           session's lane when it runs (session_turn._run_task), so its
           stream_thought{from_job} / proactive_speech / task_outcome carry the
           session's route_sid and passed the SSE/WS filters during a later
           answer-only turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import agents
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.session_turn import _effective_answer_only
from brain.settings import settings
from brain.turn_ctx import current_turn

_AUTH = {"Authorization": "Bearer k"}


@pytest.fixture
def org_off(monkeypatch):
    monkeypatch.setitem(settings._data, "answer_only", 0)


@pytest.fixture
def org_on(monkeypatch):
    monkeypatch.setitem(settings._data, "answer_only", 1)


# ── the OR fold ──────────────────────────────────────────────────────────────


def test_org_setting_restricts_every_turn(org_on, monkeypatch):
    monkeypatch.setattr(agents, "answer_only", lambda aid: False)
    assert current_turn()["answer_only"] is False  # owner lane, nothing declared
    assert _effective_answer_only({}) is True
    assert _effective_answer_only({"agent_id": "p.m"}) is True


def test_org_setting_off_keeps_the_old_resolution(org_off, monkeypatch):
    monkeypatch.setattr(agents, "answer_only", lambda aid: aid == "p.restricted")
    assert _effective_answer_only({}) is False
    assert _effective_answer_only({"agent_id": "p.restricted"}) is True


def test_effective_permissions_or_folds_answer_only():
    assert agents.effective_permissions({"answer_only": 0}, {})["answer_only"] == 0
    assert agents.effective_permissions({"answer_only": 1}, {})["answer_only"] == 1
    assert (
        agents.effective_permissions({"answer_only": 0}, {"answer_only": True})["answer_only"] == 1
    )
    # An agent cannot lift the org switch.
    assert (
        agents.effective_permissions({"answer_only": 1}, {"answer_only": False})["answer_only"] == 1
    )


# ── guard 1: api_turn never lifts a stray pending onto an answer-only turn ────


class _Cloud:
    def __init__(self):
        self.has_pending = True
        self.cleared = 0

    def get_pending(self):
        return {"description": "stray write"}

    def clear_pending(self):
        self.has_pending = False
        self.cleared += 1


def _brain(answer_only: bool):
    from brain.session_turn import _TurnMixin

    class _B(_TurnMixin):
        def __init__(self):
            self.motor = type("M", (), {"_cloud": _Cloud()})()

        async def process_turn(self, *a, **k):
            return "answer", {
                "emotion": "neutral",
                **({"answer_only": True} if answer_only else {}),
            }

    return _B()


def test_api_turn_lifts_pending_on_a_normal_turn(org_off):
    b = _brain(answer_only=False)
    _, affect = asyncio.run(b.api_turn("hi", "u"))
    assert affect["pending"] == {"description": "stray write"}
    assert b.motor._cloud.cleared == 1


def test_api_turn_never_lifts_pending_on_an_answer_only_turn(org_off):
    b = _brain(answer_only=True)
    _, affect = asyncio.run(b.api_turn("hi", "u"))
    assert "pending" not in affect
    assert b.motor._cloud.cleared == 1  # discarded, never left for another session


# ── guard 2 + 3 on the transports ────────────────────────────────────────────


class _Runner:
    """Returns a pending write on EVERY turn — the stray the guards must swallow —
    and records the answer_only the transport bound on the turn context."""

    def __init__(self):
        self.bound: list[bool] = []

    async def __call__(self, message, end_user_id, mandate_id=None, persona=None, **kw):
        self.bound.append(bool(current_turn().get("answer_only")))
        affect = {"emotion": "neutral", "turn_id": "t1", "pending": {"description": "write x"}}
        if self.bound[-1]:
            affect["answer_only"] = True
        return "reply", affect


class _Source:
    def __init__(self):
        self.taps = []

    def add_tap(self, q):
        self.taps.append(q)

    def remove_tap(self, q):
        pass


def _client(runner, source=None):
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: h == "Bearer k",
            event_source=source,
        )
    )
    return TestClient(app)


def test_post_turn_confirmation_is_suppressed_under_org_answer_only(org_on):
    runner = _Runner()
    c = _client(runner)
    sid = c.post("/v1/sessions", headers=_AUTH, json={"end_user_id": "u"}).json()["session_id"]
    # A body `false` cannot widen the org switch.
    r = c.post(
        f"/v1/sessions/{sid}/turns", headers=_AUTH, json={"message": "hi", "answer_only": False}
    )
    assert r.status_code == 200
    assert "confirmation" not in r.json()
    assert runner.bound == [True]


def test_post_turn_confirmation_flows_when_nothing_restricts(org_off):
    runner = _Runner()
    c = _client(runner)
    sid = c.post("/v1/sessions", headers=_AUTH, json={"end_user_id": "u"}).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/turns", headers=_AUTH, json={"message": "hi"})
    assert r.json()["confirmation"] == {"required": True, "description": "write x"}


def test_sse_drops_job_thoughts_and_confirmation_under_answer_only(org_off):
    """A stream_thought{from_job} carrying THIS session's route_sid (what
    _run_task's lane rebinding produces) must not be forwarded on an answer-only
    turn, while an ordinary thought still is; and done carries no confirmation."""
    runner = _Runner()
    src = _Source()
    c = _client(runner, source=src)
    sid = c.post(
        "/v1/sessions", headers=_AUTH, json={"end_user_id": "u", "answer_only": True}
    ).json()["session_id"]

    # Feed the tap as soon as the stream registers it (the tap is added before the
    # turn task starts), from a side task on the same loop.
    orig_add = src.add_tap

    def _add(q):
        orig_add(q)
        q.put_nowait(
            {"type": "stream_thought", "route_sid": sid, "thought": "job", "from_job": True}
        )
        q.put_nowait({"type": "stream_thought", "route_sid": sid, "thought": "mine"})
        q.put_nowait({"type": "turn_end", "route_sid": sid, "turn_id": "t1"})

    src.add_tap = _add
    with c.stream(
        "POST", f"/v1/sessions/{sid}/turns/stream", headers=_AUTH, json={"message": "hi"}
    ) as r:
        text = "".join(r.iter_text())
    frames = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    thoughts = [f.get("thought") for f in frames if f.get("type") == "stream_thought"]
    assert thoughts == ["mine"]
    done = next(f for f in frames if "response" in f and "affect" in f)
    assert "confirmation" not in done


def test_sse_forwards_job_thoughts_when_not_answer_only(org_off):
    runner = _Runner()
    src = _Source()
    c = _client(runner, source=src)
    sid = c.post("/v1/sessions", headers=_AUTH, json={"end_user_id": "u"}).json()["session_id"]
    orig_add = src.add_tap

    def _add(q):
        orig_add(q)
        q.put_nowait(
            {"type": "stream_thought", "route_sid": sid, "thought": "job", "from_job": True}
        )
        q.put_nowait({"type": "turn_end", "route_sid": sid, "turn_id": "t1"})

    src.add_tap = _add
    with c.stream(
        "POST", f"/v1/sessions/{sid}/turns/stream", headers=_AUTH, json={"message": "hi"}
    ) as r:
        text = "".join(r.iter_text())
    frames = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    assert [f.get("thought") for f in frames if f.get("type") == "stream_thought"] == ["job"]
    assert next(f for f in frames if "response" in f)["confirmation"]["required"] is True


# ── WS ────────────────────────────────────────────────────────────────────────


def _ws_session(answer_only: bool):
    from brain.api.sessions import ApiSession
    from brain.api.ws import WsSession

    class _S(ApiSession):
        pass

    s = ApiSession(session_id="s1", end_user_id="u", agent_id=None, mandate_id=None)
    s.answer_only = answer_only
    sent: list[dict] = []
    sess = WsSession.__new__(WsSession)
    sess._session = s
    sess._active_turn_id = "t1"
    sess._audio_opts = None
    sess._registry = None

    async def _send(payload):
        sent.append(payload)

    sess._send = _send
    return sess, sent


def _drive(sess, events):
    async def _go():
        q: asyncio.Queue = asyncio.Queue()
        for ev in events:
            q.put_nowait(ev)
        task = asyncio.create_task(sess._emitter_loop(q))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    asyncio.run(_go())


_WS_EVENTS = [
    {
        "type": "stream_thought",
        "route_sid": "s1",
        "turn_id": "t1",
        "thought": "job",
        "from_job": True,
    },
    {"type": "stream_thought", "route_sid": "s1", "turn_id": "t1", "thought": "mine"},
    {"type": "proactive_speech", "route_sid": "s1", "turn_id": "bg_t0", "text": "later"},
    {"type": "task_outcome", "route_sid": "s1", "turn_id": "t1", "job_id": "j"},
]


def test_ws_drops_job_events_under_answer_only(org_off):
    sess, sent = _ws_session(answer_only=True)
    _drive(sess, _WS_EVENTS)
    assert [(p["type"], p.get("thought")) for p in sent] == [("thought", "mine")]


def test_ws_drops_job_events_under_org_answer_only(org_on):
    sess, sent = _ws_session(answer_only=False)
    _drive(sess, _WS_EVENTS)
    assert [(p["type"], p.get("thought")) for p in sent] == [("thought", "mine")]


def test_ws_forwards_everything_when_not_answer_only(org_off):
    sess, sent = _ws_session(answer_only=False)
    _drive(sess, _WS_EVENTS)
    assert [p["type"] for p in sent] == ["thought", "thought", "proactive", "task_outcome"]
