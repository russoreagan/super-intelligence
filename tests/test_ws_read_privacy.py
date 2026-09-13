"""Console WebSocket under the read policy: agent-lane events and thoughts are
forwarded in full or projected PER CLIENT, the connect replays obey the same
rule, and thoughts carry the persona that thought them."""

from __future__ import annotations

import asyncio
import json

import pytest

from brain import learning_mode, org_settings
from brain.settings import settings


class _WS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, s: str):
        self.sent.append(json.loads(s))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setitem(settings._data, "content_read_policy", 1)
    monkeypatch.setattr(learning_mode, "audit_log_path", lambda: tmp_path / "g.jsonl")
    from brain.ui.server import UIServer

    s = UIServer(asyncio.Queue())
    admin, member = _WS(), _WS()
    s._clients.update({admin, member})
    s._client_actor[admin] = {"source": "console", "user": "a", "org_admin": True}
    s._client_actor[member] = {"source": "console", "user": "m", "org_admin": False}
    return s, admin, member


START = {
    "type": "turn_start",
    "channel": "agent",
    "route_sid": "A",
    "agent_id": "ahab.m",
    "end_user_id": "cust-1",
    "user_input": "buy AAPL",
    "turn_id": "t1",
    "ts": 1,
}
END = {**START, "type": "turn_end", "response": "bought 10", "elapsed_s": 0.2}


def test_agent_event_full_for_admin_projected_for_member_consolidated(srv, monkeypatch):
    s, admin, member = srv
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    asyncio.run(s._handle_agent_event(START))
    asyncio.run(s._handle_agent_event(END))
    a, m = admin.sent[-1]["event"], member.sent[-1]["event"]
    assert a["response"] == "bought 10"
    assert "response" not in m and "user_input" not in m and m["response_len"] == 9
    assert "end_user_id" not in m and m["end_user_hash"]


def test_agent_event_isolated_non_home_projected_for_admin(srv, monkeypatch):
    s, admin, _ = srv
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    asyncio.run(s._handle_agent_event(START))
    ev = admin.sent[-1]["event"]
    assert "user_input" not in ev and ev["input_len"] == 8 and ev["content"] is False


def test_history_replay_filtered_per_client(srv, monkeypatch):
    s, admin, member = srv
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    asyncio.run(s._handle_agent_event(START))
    asyncio.run(s._handle_agent_event(END))
    assert s._agent_history and s._agent_history[0]["response"] == "bought 10"
    full = s._policy_view(
        admin, {"type": "turn_end", **s._agent_history[0]}, "turns", s._agent_history[0]
    )
    proj = s._policy_view(
        member, {"type": "turn_end", **s._agent_history[0]}, "turns", s._agent_history[0]
    )
    assert full["response"] == "bought 10"
    assert "response" not in proj and proj["response_len"] == 9


def test_thoughts_withheld_for_non_home_persona_in_isolated(srv, monkeypatch):
    s, admin, member = srv
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    th = {
        "type": "stream_thought",
        "thought": "a buyer's secret",
        "persona": "ahab",
        "ts": 1,
        "salience": 0.3,
    }
    asyncio.run(s._fan_out(th, "thoughts"))
    for c in (admin, member):
        ev = c.sent[-1]
        assert (
            ev["type"] == "stream_thought_withheld"
            and "thought" not in ev
            and ev["persona"] == "ahab"
        )
    home = {"type": "stream_thought", "thought": "home musing", "persona": "home_p", "ts": 2}
    asyncio.run(s._fan_out(home, "thoughts"))
    assert admin.sent[-1]["thought"] == "home musing"
    assert member.sent[-1]["type"] == "stream_thought_withheld"  # member: never content


def test_thought_carries_the_bound_persona(monkeypatch):
    """DMN publish → forwarder → emitter: the persona rides on the event."""
    from brain.second_brain.store import bind_persona
    from brain.ui.emitter import ActivationEmitter

    em = ActivationEmitter()
    q = em.get_queue()
    asyncio.run(em.emit_stream_thought("t", persona="ahab"))
    assert q.get_nowait()["persona"] == "ahab"
    # Fallback: an event emitted INSIDE a persona binding is stamped by _stamp_lane.
    with bind_persona("luna"):
        asyncio.run(em.emit_stream_thought("u"))
    assert q.get_nowait()["persona"] == "luna"


def test_project_event_drops_speech_and_keeps_affect(srv, monkeypatch):
    s, admin, member = srv
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    speech = {"type": "proactive_speech", "channel": "agent", "agent_id": "ahab.m", "text": "hi"}
    asyncio.run(s._fan_out(speech, "turns", wrap="agent_event"))
    assert not admin.sent and not member.sent
    emo = {"type": "emotion", "channel": "agent", "agent_id": "ahab.m", "emotion": "calm"}
    asyncio.run(s._fan_out(emo, "turns", wrap="agent_event"))
    assert admin.sent[-1]["event"]["emotion"] == "calm"
