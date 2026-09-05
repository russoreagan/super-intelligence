"""Background jobs run under the AGENT that owns them — the fix that closes the gap.

_run_task used to bind persona and turn but never the agent, and the agent bind was
gated on the agent LANE (origin_channel == "agent" AND a session id) — which a DMN
self-task or project step never has. So every background job ran with
current_agent() None: the org permission ceiling, for exactly the work that runs
unsupervised. These tests assert the bind for each enqueue path AND that an
enforcement layer actually narrows on it — a set contextvar proves nothing on its own.
"""

from __future__ import annotations

import pytest

from brain.agent_ctx import current_agent
from brain.clusters.motor_dispatcher import ToolDispatcher
from brain.clusters.task_queue import PersistentTaskQueue, Task
from brain.second_brain.store import active_persona
from brain.session_turn import _TurnMixin
from brain.turn_ctx import current_turn

AGENT = "the_analyst.day_trading_analyst"


@pytest.fixture
def perms(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(
        "brain.agents.permissions", lambda aid: {"motor_allowed_dirs": str(allowed)}
    )
    return tmp_path, allowed


def _host(capture: dict):
    host = _TurnMixin.__new__(_TurnMixin)

    async def _body(task):
        capture["agent"] = current_agent()
        capture["persona"] = active_persona()
        capture["turn"] = dict(current_turn())
        disp = ToolDispatcher.__new__(ToolDispatcher)
        disp._allowed_paths = capture["ceiling"]
        capture["eff_paths"] = disp._eff_allowed_paths()

    host._run_task_body = _body
    return host


@pytest.mark.asyncio
async def test_dmn_project_step_runs_as_its_agent(perms):
    tmp_path, allowed = perms
    cap: dict = {"ceiling": [str(tmp_path)]}
    task = Task(
        id="t1",
        goal="read the journal",
        source="self",
        origin_persona="the_analyst",
        origin_agent_id=AGENT,
    )
    await _host(cap)._run_task(task)
    assert cap["agent"]["agent_id"] == AGENT
    assert cap["persona"] == "the_analyst"
    assert cap["turn"]["channel"] == "owner"  # idle work stays on the owner feed
    # Enforcement, not just binding: the org ceiling narrowed to the agent's dir.
    assert cap["eff_paths"] == [str(allowed.resolve())]


@pytest.mark.asyncio
async def test_agent_lane_job_gets_turn_and_agent_binds(perms):
    tmp_path, allowed = perms
    cap: dict = {"ceiling": [str(tmp_path)]}
    task = Task(
        id="t2",
        goal="x",
        source="user",
        origin_channel="agent",
        origin_session_id="sess-1",
        origin_agent_id=AGENT,
        origin_partner_id="pt",
    )
    await _host(cap)._run_task(task)
    assert cap["agent"]["agent_id"] == AGENT
    assert cap["turn"]["channel"] == "agent" and cap["turn"]["session_id"] == "sess-1"
    assert cap["persona"] == "the_analyst"  # derived from the agent id
    assert cap["eff_paths"] == [str(allowed.resolve())]


@pytest.mark.asyncio
async def test_bare_owner_task_is_unchanged(perms):
    tmp_path, _ = perms
    cap: dict = {"ceiling": [str(tmp_path)]}
    await _host(cap)._run_task(Task(id="t3", goal="x", source="self"))
    assert cap["agent"] is None
    assert cap["turn"]["channel"] == "owner"
    assert cap["eff_paths"] == [str(tmp_path)]  # the ceiling, verbatim


@pytest.mark.asyncio
async def test_kill_switch_restores_the_ceiling(perms, monkeypatch):
    tmp_path, _ = perms
    monkeypatch.setenv("BRAIN_BIND_AGENT_ON_JOBS", "0")
    cap: dict = {"ceiling": [str(tmp_path)]}
    await _host(cap)._run_task(Task(id="t4", goal="x", source="self", origin_agent_id=AGENT))
    assert cap["agent"] is None
    assert cap["eff_paths"] == [str(tmp_path)]


def test_enqueue_accepts_an_explicit_agent_outside_an_agent_lane(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.clusters.task_queue.TASK_QUEUE_PATH", tmp_path / "q.json")
    q = PersistentTaskQueue()
    t = q.enqueue(
        "scan the watchlist",
        source="self",
        priority=2,
        origin_persona="the_analyst",
        origin_agent_id=AGENT,
    )
    assert t is not None
    assert (
        t.origin_agent_id == AGENT
        and t.origin_channel == "owner"
        and t.origin_persona == "the_analyst"
    )


def test_owning_agent_id_shapes_persona_dot_mandate(monkeypatch):
    from brain import agents

    monkeypatch.setattr(agents, "owning_mandate", lambda p: "day_trading_analyst")
    assert agents.owning_agent_id("The Analyst") == AGENT
    monkeypatch.setattr(agents, "owning_mandate", lambda p: "")
    assert agents.owning_agent_id("The Analyst") == ""
