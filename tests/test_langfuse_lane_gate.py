"""Langfuse export skips partner (engine-API) turns unless the deployment opts in.

The export carries the customer's verbatim prompt and response to a third-party
host with its own retention, outside anything DELETE /v1/end_users/{id} can
reach. So a turn on the agent lane opens no root span and closes none; the owner
lane (interactive UI, idle loop) exports exactly as before, and
BRAIN_LANGFUSE_AGENT_LANE=true restores the export for a deployment that wants
partner traces in Langfuse and has documented it as a sub-processor.
"""

from __future__ import annotations

import pytest

from brain.observability.timeline import ObservabilityLayer, TurnTrace
from brain.turn_ctx import bind_turn


class _Span:
    trace_id = "tr-1"

    def __init__(self):
        self.updated = None
        self.ended = False

    def update(self, **kw):
        self.updated = kw

    def end(self):
        self.ended = True


class _Langfuse:
    def __init__(self):
        self.started: list[dict] = []
        self.scores: list[dict] = []

    def start_observation(self, **kw):
        self.started.append(kw)
        return _Span()

    def create_score(self, **kw):
        self.scores.append(kw)


@pytest.fixture
def obs(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("BRAIN_LANGFUSE_AGENT_LANE", raising=False)
    # `propagate_attributes` is imported lazily inside the methods; stub the module.
    import contextlib
    import sys
    import types

    fake = types.ModuleType("langfuse")
    fake.propagate_attributes = lambda **kw: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "langfuse", fake)
    o = ObservabilityLayer("s1")
    o._langfuse = _Langfuse()
    return o


def _trace(turn_id, api_session_id=""):
    t = TurnTrace(turn_id=turn_id, session_id="s1", user_input="hi", api_session_id=api_session_id)
    t.response = "yo"
    return t


def test_owner_lane_exports(obs):
    obs.begin_turn("t1", "hi")
    assert len(obs._langfuse.started) == 1
    obs.record_turn(_trace("t1"))
    assert "t1" in obs._trace_ids


def test_agent_lane_is_skipped_by_default(obs):
    with bind_turn("agent", session_id="api-1", end_user_id="u_1"):
        obs.begin_turn("t2", "my secret")
    assert obs._langfuse.started == []  # no root span, no input text sent
    obs.record_turn(_trace("t2", api_session_id="api-1"))
    assert obs._langfuse.started == []  # and no flat fallback trace either
    assert "t2" not in obs._trace_ids
    # Scores posted later find no trace and go nowhere.
    obs.record_scores("t2", {"judge.x": 1.0})
    assert obs._langfuse.scores == []
    # The in-memory trace window is unaffected — the eval path still sees the turn.
    assert [t.turn_id for t in obs._traces] == ["t2"]


def test_agent_lane_exports_when_opted_in(obs, monkeypatch):
    monkeypatch.setenv("BRAIN_LANGFUSE_AGENT_LANE", "true")
    with bind_turn("agent", session_id="api-1", end_user_id="u_1"):
        obs.begin_turn("t3", "hi")
    assert len(obs._langfuse.started) == 1
    obs.record_turn(_trace("t3", api_session_id="api-1"))
    assert "t3" in obs._trace_ids
