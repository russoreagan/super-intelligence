"""The MRI chat box talks to the selected agent.

A user_message carrying agent_id runs as that agent's turn (its persona + role, on
the agent lane, as the console user), gated by the same read rule as the agent's
history; without agent_id it still goes to the running persona."""

from __future__ import annotations

import asyncio
import json

import pytest

from brain import org_settings
from brain.settings import settings


class _WS:
    """Yields queued frames, then raises to end the receive loop."""

    def __init__(self, frames: list[dict]):
        self._frames = [{"text": json.dumps(f)} for f in frames]
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        if self._frames:
            return self._frames.pop(0)
        raise RuntimeError("closed")

    async def send_text(self, s: str) -> None:
        self.sent.append(json.loads(s))


def _server(on_agent=None, on_user=None):
    from brain.ui.server import UIServer

    return UIServer(asyncio.Queue(), on_user_message=on_user, on_agent_message=on_agent)


async def _drive(srv, ws, actor):
    srv._client_actor[ws] = actor
    srv._client_end_user[ws] = "u-1@console"
    await srv._receive_loop(ws)
    # The agent turn runs off the receive loop; let it finish.
    while srv._agent_chat_tasks:
        await asyncio.gather(*list(srv._agent_chat_tasks))


ADMIN = {"source": "console", "user": "a", "org_admin": True}
MEMBER = {"source": "console", "user": "m", "org_admin": False}


@pytest.fixture
def consolidated(monkeypatch):
    monkeypatch.setitem(settings._data, "content_read_policy", 1)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")


def test_message_with_agent_id_runs_as_that_agent(consolidated):
    agent_calls, owner_calls = [], []

    async def on_agent(text, agent_id, end_user):
        agent_calls.append((text, agent_id, end_user))
        return "ok"

    async def on_user(text):
        owner_calls.append(text)

    srv = _server(on_agent, on_user)
    ws = _WS([{"type": "user_message", "text": "hi", "agent_id": "the_empath.listener"}])
    asyncio.run(_drive(srv, ws, ADMIN))
    assert agent_calls == [("hi", "the_empath.listener", "u-1@console")]
    assert owner_calls == []  # never leaks onto the running persona
    assert ws.sent == []


def test_message_without_agent_id_still_goes_to_running_persona(consolidated):
    agent_calls, owner_calls = [], []

    async def on_agent(text, agent_id, end_user):
        agent_calls.append(text)

    async def on_user(text):
        owner_calls.append(text)

    srv = _server(on_agent, on_user)
    asyncio.run(_drive(srv, _WS([{"type": "user_message", "text": "hi"}]), ADMIN))
    assert owner_calls == ["hi"] and agent_calls == []


def test_viewer_who_cannot_read_the_agent_cannot_chat(consolidated):
    calls = []

    async def on_agent(*a):
        calls.append(a)

    srv = _server(on_agent)
    ws = _WS([{"type": "user_message", "text": "hi", "agent_id": "the_empath.listener"}])
    asyncio.run(_drive(srv, ws, MEMBER))
    assert calls == []
    assert ws.sent and ws.sent[0]["type"] == "agent_chat_error"
    assert ws.sent[0]["agent_id"] == "the_empath.listener"


def test_isolated_org_customer_persona_refused(monkeypatch):
    monkeypatch.setitem(settings._data, "content_read_policy", 1)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setattr(org_settings, "is_home", lambda slug: False)
    calls = []

    async def on_agent(*a):
        calls.append(a)

    srv = _server(on_agent)
    ws = _WS([{"type": "user_message", "text": "hi", "agent_id": "the_empath.listener"}])
    asyncio.run(_drive(srv, ws, ADMIN))
    assert calls == [] and ws.sent[0]["type"] == "agent_chat_error"


def test_turn_failure_is_reported_to_the_sender(consolidated):
    from brain.agents import AgentNotFound

    async def on_agent(*a):
        raise AgentNotFound("unknown or disabled agent 'x.y'")

    srv = _server(on_agent)
    ws = _WS([{"type": "user_message", "text": "hi", "agent_id": "x.y"}])
    asyncio.run(_drive(srv, ws, ADMIN))
    assert ws.sent[0]["type"] == "agent_chat_error"
    assert "unknown or disabled" in ws.sent[0]["detail"]


def test_console_end_user_shape():
    from brain.ids import END_USER_ID_RE
    from brain.ui.server import _console_end_user

    eu = _console_end_user({"sub": "742c0dfd-d59a-426c-8d1e-d4473411954a"})
    assert eu == "742c0dfd-d59a-426c-8d1e-d4473411954a@console"
    assert END_USER_ID_RE.match(eu)
    assert _console_end_user(None) == "owner@console"
    assert _console_end_user({"sub": "has space"}) == "owner@console"


def test_console_agent_turn_binds_the_agent_lane(monkeypatch):
    """The turn runs with the agent's persona + role on the agent lane (so its
    events go to the observed view, not the owner chat) and is logged."""
    from brain import agent_log, agents
    from brain.clusters import parietal
    from brain.session_turn import _TurnMixin
    from brain.turn_ctx import current_turn

    monkeypatch.setattr(agents, "resolve", lambda aid: ("the_empath", "therapy_listener"))
    logged = []
    monkeypatch.setattr(agent_log, "record", lambda **kw: logged.append(kw))
    seen = {}

    class _S(_TurnMixin):
        async def api_turn(self, message, end_user_id, mandate_id=None, persona=None):
            seen.update(
                turn=dict(current_turn()),
                lane=parietal.lane_key(),
                args=(message, end_user_id, mandate_id, persona),
            )
            return "[mood:calm] I hear you.", {"turn_id": "t9"}

    out = asyncio.run(
        _S().console_agent_turn("hello", "the_empath.therapy_listener", "u-1@console")
    )
    assert out == "I hear you."
    assert seen["args"] == ("hello", "u-1@console", "therapy_listener", "the_empath")
    t = seen["turn"]
    assert t["channel"] == "agent" and t["agent_id"] == "the_empath.therapy_listener"
    assert t["end_user_id"] == "u-1@console" and t["partner_id"] == "console"
    assert t["session_id"].startswith("console.")
    assert seen["lane"] != parietal.OWNER_LANE
    assert logged and logged[0]["prompt"] == "hello" and logged[0]["response"] == "I hear you."
    assert logged[0]["persona"] == "the_empath" and logged[0]["turn_id"] == "t9"
    assert current_turn().get("channel", "owner") != "agent"  # binding released
