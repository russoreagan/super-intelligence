"""_deliver_proactive is the single channel every unprompted message flows through
(job result, clarification, failure, background-reactive result). It must, on EVERY
lane, deliver to the owning partner — but speak aloud locally only on the owner lane.
This locks that contract against the real _proactive_voice_allowed gate, so the
task-worker sites can't drift back to the weaker listener-only check (which would let
an agent-lane job result leak into the owner's Labs audio)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from brain.brain_session import BrainSession
from brain.session_turn import _TurnMixin
from brain.turn_ctx import bind_turn

_deliver = _TurnMixin._deliver_proactive


class _RecordingEmitter:
    def __init__(self):
        self.partner_calls = []

    async def emit_proactive_speech(self, text, *, affect=None, partner_target=""):
        self.partner_calls.append((text, affect, partner_target))


class _RecordingPns:
    def __init__(self):
        self.spoke = []

    async def emit(self, text, mood):
        self.spoke.append((text, mood))


def _fake_session():
    """A minimal stand-in wired to the REAL lane gate (a connected listener present)."""
    fake = SimpleNamespace(
        _emitter=_RecordingEmitter(),
        pns=_RecordingPns(),
        _proactive_speech_allowed=lambda: True,
    )
    fake._proactive_voice_allowed = BrainSession._proactive_voice_allowed.__get__(fake)
    return fake


def test_owner_lane_delivers_and_speaks():
    fake = _fake_session()
    # No bind_turn → owner lane (direct Elyceum / idle inner life).
    asyncio.run(_deliver(fake, "done with that", {"emotion": "pleased"}, partner_target="cust-1"))
    assert fake._emitter.partner_calls == [("done with that", {"emotion": "pleased"}, "cust-1")]
    assert fake.pns.spoke == [("done with that", {"emotion": "pleased"})]


def test_agent_lane_delivers_to_partner_but_stays_silent():
    fake = _fake_session()
    with bind_turn("agent", session_id="A", agent_id="x.y", end_user_id="cust-1"):
        asyncio.run(
            _deliver(fake, "done with that", {"emotion": "pleased"}, partner_target="cust-1")
        )
    # Partner still hears about it; local TTS is suppressed on the agent lane.
    assert fake._emitter.partner_calls == [("done with that", {"emotion": "pleased"}, "cust-1")]
    assert fake.pns.spoke == []
