"""Replayed step records never dispatch a planner placeholder, and only a successful
run becomes a ballistic procedure.

Production 2026-09-14/15: the trading analyst's scheduled job failed at ~17:30 two
days running with `[error] Unknown tool: none`. Muscle memory had stored an earlier
FAILED run (cloud_action against a dead connector → the planner's {"tool": "none"}
stop) verbatim; before_plan bumped its use_count every time it was recalled as
context, which promoted it to open-loop; _execute_open_loop then re-fired the dead
cloud_action and dispatched "none" as a tool. The stop "reason" it replayed — the
connector "exhausted after 16 attempts" — was a stale string from the original job.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.clusters import motor_memory as mm
from brain.clusters.motor_cortex import MotorCortexCluster
from brain.clusters.motor_subsystem import is_motor_step

DEAD = "[error] connector-unavailable: trading-readonly"


def test_placeholders_are_not_motor_steps():
    assert not is_motor_step({"tool": "none"})
    assert not is_motor_step({"tool": " None "})
    assert not is_motor_step({})
    assert is_motor_step({"tool": "cloud_action"})


# ── recording ───────────────────────────────────────────────────────────────


def _store():
    s = mm.ProcedureStore.__new__(mm.ProcedureStore)
    s._ready = True
    s._table = MagicMock()
    return s


def test_save_drops_placeholders_with_their_results():
    s = _store()
    s.save(
        "fetch quotes",
        [{"tool": "fetch_url", "args": {"url": "u"}}, {"tool": "none", "reason": "stop"}],
        ["page body", ""],
        True,
        [0.0],
    )
    row = s._table.add.call_args[0][0][0]
    steps = json.loads(row["steps"])
    assert [x["tool"] for x in steps] == ["fetch_url"]
    assert "_sig" in steps[0]
    assert json.loads(row["results"]) == ["page body"]


def test_save_skips_a_run_with_no_real_tool_call():
    s = _store()
    s.save("g", [{"tool": "none"}, {"tool": ""}], ["", ""], True, [0.0])
    s._table.add.assert_not_called()


# ── recall ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("success, expect_proc", [(False, False), (True, True)])
def test_only_a_successful_run_goes_open_loop(monkeypatch, success, expect_proc):
    monkeypatch.setattr(mm, "_isolated_non_home", lambda: False)
    sub = mm.MuscleMemorySubsystem.__new__(mm.MuscleMemorySubsystem)
    proc = {"id": "p1", "goal": "g", "success": success, "similarity": 0.99, "use_count": 9}
    sub._store = MagicMock()
    sub._store.recall.return_value = [proc]
    router = MagicMock()
    router.embed = AsyncMock(return_value=[0.1])
    got, sim = asyncio.run(sub.recall_procedure("g", router))
    assert sim == pytest.approx(0.99)
    assert (got is proc) is expect_proc


# ── replay ──────────────────────────────────────────────────────────────────


def _cortex(outputs: dict[str, str]):
    mc = MotorCortexCluster.__new__(MotorCortexCluster)
    mc._calls_this_turn = 0
    mc._subsystems = []
    mc._chem_snapshot = lambda: {}
    mc._effective_budget = lambda chem: 20
    mc._notify_job_complete = AsyncMock()
    dispatched: list[str] = []

    async def _dispatch(tool, args, turn_id, reason):
        dispatched.append(tool)
        out = outputs.get(tool, "ok")
        return out, {"tool": tool, "output": out}

    mc._dispatch_tool = _dispatch
    return mc, dispatched


def test_open_loop_never_dispatches_a_stored_placeholder():
    mc, dispatched = _cortex({})
    proc = {
        "id": "p1",
        "steps": [
            {"tool": "fetch_url", "args": {"url": "u"}},
            {"tool": "none", "args": {}, "reason": "exhausted after 16 attempts"},
        ],
    }
    res = asyncio.run(mc._execute_open_loop(proc, "g", "t1"))
    assert dispatched == ["fetch_url"]
    assert res["steps_taken"] == 1


def test_open_loop_stops_at_the_first_failed_step():
    mc, dispatched = _cortex({"cloud_action": DEAD})
    proc = {
        "id": "p1",
        "steps": [
            {"tool": "cloud_action", "args": {"task": "quotes"}},
            {"tool": "write_file", "args": {"path": "r.md"}},
        ],
    }
    res = asyncio.run(mc._execute_open_loop(proc, "g", "t1"))
    assert dispatched == ["cloud_action"], "no paying for the rest of a broken sequence"
    assert res["prediction_errors"] == 1
