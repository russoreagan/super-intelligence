"""
Lane gating for speaking proactive messages ALOUD.

A third-party app driving an agent through the engine API runs on the "agent"
lane (bind_turn("agent", ...)). Unprompted results from that run must never be
synthesised to audio in the Elyceum app — someone there is observing the run,
not conversing with it. Direct Elyceum interaction (the owner lane) still speaks.

Covers brain/brain_session.py: _proactive_voice_allowed lane check, which gates
the pns.emit (TTS) call at the reactive-completion site in brain/session_turn.py
without touching partner delivery (emit_proactive_speech still fires).
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.brain_session import BrainSession
from brain.turn_ctx import bind_turn

_voice = BrainSession._proactive_voice_allowed


def test_owner_lane_voices_aloud():
    fake = SimpleNamespace(_proactive_speech_allowed=lambda: True)
    # No bind_turn → owner lane (direct Elyceum interaction / idle inner life).
    assert _voice(fake) is True


def test_agent_lane_stays_silent():
    fake = SimpleNamespace(_proactive_speech_allowed=lambda: True)
    # Engine-API turn: deliver to the partner, but never speak aloud in Elyceum.
    with bind_turn("agent", session_id="A", agent_id="x.y", end_user_id="cust-1"):
        assert _voice(fake) is False


def test_owner_lane_still_respects_listener_gate():
    # The lane check is additive — it never loosens the existing listener/fan-out
    # guard. Owner lane with no connected listener still won't synthesise.
    fake = SimpleNamespace(_proactive_speech_allowed=lambda: False)
    assert _voice(fake) is False
