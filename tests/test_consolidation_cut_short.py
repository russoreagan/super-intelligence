"""A consolidation pass cut short must not re-apply what it already committed.

Prod 2026-10-11: the Sleep button's 90 s wait ran out while the learning narrator
was mid-stream to the pod, then five redeploys in a row gave each tenant 3 s
between SIGTERM and SIGKILL. Every kill landed after the Hebbian pass had
committed but before the trace journal was cleared, so each boot replayed the
same 24 turns and the next shutdown re-applied their Hebbian updates — seven
passes over one batch before an idle pass finally completed it.

Covered here: the stage ledger makes Hebbian and the memory passes exactly-once
per turn across replays; the journal is cleared once the core stages commit,
before the optional tail; the shutdown pass bounds that tail; and the gateway
stops its tenants in parallel with a real grace on a redeploy.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.observability import trace_journal
from brain.observability.timeline import TurnTrace
from brain.sleep import SleepConsolidation


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "tenant"
    r.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(r))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_admin")
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    return r


def _trace(turn_id, session="s1", persona="the_admin"):
    return TurnTrace(
        turn_id=turn_id,
        session_id=session,
        user_input="hi",
        persona_name=persona,
        fired_path=[{"name": "temporal.curiosity", "cluster": "temporal", "kind": "switch"}],
    )


def _summary(turn_id, session="s1", persona="the_admin"):
    return {"user_input": "hi", "persona": persona, "turn_id": turn_id, "session_id": session}


def _journal(*turn_ids):
    for tid in turn_ids:
        trace_journal.append(_trace(tid), _summary(tid))


def _sleep():
    """A SleepConsolidation whose stages are recorders: hebbian.run, the memory
    batch and the optional tail."""
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._hebbian = MagicMock()
    s._consolidate_persona_batch = AsyncMock(return_value=[])
    s._consolidate_tail = AsyncMock(return_value=None)
    return s


def _hebbian_ids(s):
    return [[t.turn_id for t in c.args[1]] for c in s._hebbian.run.call_args_list]


def _memory_ids(s):
    return [[t["turn_id"] for t in c.args[1]] for c in s._consolidate_persona_batch.call_args_list]


# ── stage ledger ───────────────────────────────────────────────────────────────


def test_turn_key_reads_a_trace_and_its_summary_alike(root):
    assert trace_journal.turn_key(_trace("t1", "s9")) == "s9:t1"
    assert trace_journal.turn_key(_summary("t1", "s9")) == "s9:t1"
    assert trace_journal.turn_key({"user_input": "no id"}) == ""
    assert trace_journal.turn_key(MagicMock()) == ""  # a mock's attrs are not ids


def test_marked_turns_drop_out_of_that_stage_only(root):
    a, b = _trace("a"), _trace("b")
    trace_journal.mark_applied(trace_journal.STAGE_HEBBIAN, [a])
    assert trace_journal.unapplied(trace_journal.STAGE_HEBBIAN, [a, b]) == [b]
    assert trace_journal.unapplied(trace_journal.STAGE_MEMORY, [a, b]) == [a, b]
    # Same turn id in another session is a different turn.
    other = _trace("a", session="s2")
    assert trace_journal.unapplied(trace_journal.STAGE_HEBBIAN, [other]) == [other]
    # Unkeyed items always pass.
    assert trace_journal.unapplied(trace_journal.STAGE_HEBBIAN, [{"x": 1}]) == [{"x": 1}]


def test_ledger_is_bounded(root, monkeypatch):
    monkeypatch.setattr(trace_journal, "_APPLIED_KEEP", 3)
    trace_journal.mark_applied("hebbian", [_trace(f"t{i}") for i in range(5)])
    left = trace_journal.unapplied("hebbian", [_trace(f"t{i}") for i in range(5)])
    assert [t.turn_id for t in left] == ["t0", "t1"]


def test_ledger_off_with_the_journal(root, monkeypatch):
    monkeypatch.setenv("BRAIN_TRACE_JOURNAL", "false")
    a = _trace("a")
    trace_journal.mark_applied("hebbian", [a])
    assert trace_journal.unapplied("hebbian", [a]) == [a]


def test_replayed_summaries_carry_their_turn_key(root):
    # An older line: the summary has no ids, the full trace does.
    trace_journal.append(_trace("old", "s7"), {"user_input": "hi"})
    _, sums = trace_journal.load_orphans()
    assert trace_journal.turn_key(sums[0]) == "s7:old"


# ── consolidate: exactly-once per stage across replays ─────────────────────────


def test_pass_killed_after_hebbian_does_not_reapply_it_on_replay(root):
    _journal("t1", "t2")
    token = trace_journal.rotate_inflight()

    # Pass 1: the memory stage is where the kill lands (prod: 3 s after SIGTERM).
    s1 = _sleep()
    s1._consolidate_persona_batch.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            s1.consolidate(
                "sess1",
                [_summary("t1"), _summary("t2")],
                full_traces=[_trace("t1"), _trace("t2")],
                journal_token=token,
            )
        )
    assert _hebbian_ids(s1) == [["t1", "t2"]]

    # Next boot: the batch replays (nothing committed the journal) ...
    fulls, sums = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["t1", "t2"]
    # ... alongside a new turn.
    fulls.append(_trace("t3", "s2"))
    sums.append(_summary("t3", "s2"))

    s2 = _sleep()
    asyncio.run(s2.consolidate("sess2", sums, full_traces=fulls))
    assert _hebbian_ids(s2) == [["t3"]], "replayed turns' Hebbian must not apply twice"
    assert _memory_ids(s2) == [["t1", "t2", "t3"]], "memory never committed — it runs"


def test_memory_stage_is_exactly_once_per_persona_group(root):
    s1 = _sleep()
    calls = {"n": 0}

    async def _batch(session_id, traces, thoughts, scope=""):
        calls["n"] += 1
        if calls["n"] == 2:  # the second persona's batch is cut
            raise asyncio.CancelledError
        return []

    s1._consolidate_persona_batch.side_effect = _batch
    sums = [_summary("a1", persona="the_admin"), _summary("b1", persona="the_analyst")]
    fulls = [_trace("a1", persona="the_admin"), _trace("b1", persona="the_analyst")]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s1.consolidate("sess1", sums, full_traces=fulls))

    s2 = _sleep()
    asyncio.run(s2.consolidate("sess2", sums, full_traces=fulls))
    assert _memory_ids(s2) == [["b1"]], "only the group that never committed runs again"
    assert _hebbian_ids(s2) == []


def test_completed_pass_replayed_by_a_late_crash_is_a_noop(root):
    """The window between a pass committing and its journal clear: a replay of a
    fully committed batch re-runs nothing."""
    sums, fulls = [_summary("t1")], [_trace("t1")]
    asyncio.run(_sleep().consolidate("sess1", sums, full_traces=fulls))
    s2 = _sleep()
    asyncio.run(s2.consolidate("sess2", sums, full_traces=fulls))
    assert _hebbian_ids(s2) == [] and _memory_ids(s2) == []


def test_journal_cleared_before_the_tail(root):
    """A kill in the optional tail (prod: the learning narrator) must not stage
    the committed batch for another pass."""
    _journal("t1")
    token = trace_journal.rotate_inflight()
    s = _sleep()
    s._consolidate_tail.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            s.consolidate("sess", [_summary("t1")], full_traces=[_trace("t1")], journal_token=token)
        )
    assert trace_journal.load_orphans() == ([], [])


def test_journal_kept_when_the_core_fails(root):
    _journal("t1")
    token = trace_journal.rotate_inflight()
    s = _sleep()
    s._consolidate_persona_batch.side_effect = RuntimeError("llm down")
    with pytest.raises(RuntimeError):
        asyncio.run(
            s.consolidate("sess", [_summary("t1")], full_traces=[_trace("t1")], journal_token=token)
        )
    fulls, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["t1"]


def test_tail_budget_stops_a_slow_tail_without_failing_the_pass(root):
    s = _sleep()
    finished = {"tail": False}

    async def _slow_tail(session_id, session_traces):
        await asyncio.sleep(5)
        finished["tail"] = True

    s._consolidate_tail = _slow_tail
    t0 = time.monotonic()
    asyncio.run(
        s.consolidate("sess", [_summary("t1")], full_traces=[_trace("t1")], tail_budget_s=0.05)
    )
    assert time.monotonic() - t0 < 2
    assert not finished["tail"]
    assert _memory_ids(s) == [["t1"]]


def test_unbounded_tail_runs_to_completion(root):
    s = _sleep()
    asyncio.run(s.consolidate("sess", [_summary("t1")], full_traces=[_trace("t1")]))
    s._consolidate_tail.assert_awaited_once()


# ── shutdown pass ──────────────────────────────────────────────────────────────


def _shutdown_session(fulls, sums, sleep):
    from brain.brain_session import BrainSession

    s = BrainSession.__new__(BrainSession)
    s._session_traces = list(sums)
    s._session_traces_full = list(fulls)
    s._sleep = sleep
    s.dmn = None
    s.session_id = "sess"
    s.persona_name = "the_admin"
    return s


def test_shutdown_pass_hands_the_journal_token_and_a_tail_budget(root):
    from brain import brain_session

    _journal("t1")
    fulls, sums = trace_journal.load_orphans()
    sleep = MagicMock()
    sleep.consolidate = AsyncMock(return_value=None)
    asyncio.run(_shutdown_session(fulls, sums, sleep)._shutdown_consolidate())
    kw = sleep.consolidate.await_args.kwargs
    assert kw["journal_token"] and kw["journal_token"].startswith("pending_traces.inflight")
    assert kw["tail_budget_s"] == brain_session.SHUTDOWN_TAIL_BUDGET_S


def test_shutdown_killed_mid_pass_keeps_uncommitted_turns_and_skips_committed(root):
    """End to end through the real shutdown path: the second SIGTERM cancels the
    pass after Hebbian; the next boot's pass runs memory but not Hebbian."""
    _journal("t1", "t2")
    fulls, sums = trace_journal.load_orphans()
    s1 = _sleep()
    s1._consolidate_persona_batch.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_shutdown_session(fulls, sums, s1)._shutdown_consolidate())
    assert _hebbian_ids(s1) == [["t1", "t2"]]

    fulls2, sums2 = trace_journal.load_orphans()
    s2 = _sleep()
    asyncio.run(_shutdown_session(fulls2, sums2, s2)._shutdown_consolidate())
    assert _hebbian_ids(s2) == []
    assert _memory_ids(s2) == [["t1", "t2"]]
    assert trace_journal.load_orphans() == ([], [])


# ── gateway shutdown gives tenants a grace, in parallel ────────────────────────


class _ExitsAfter:
    """A tenant process that exits `after_s` seconds after SIGTERM."""

    def __init__(self, after_s: float):
        self.after_s = after_s
        self.term_at: float | None = None
        self.killed = False
        self.pid = 1

    def terminate(self):
        self.term_at = time.monotonic()

    def kill(self):
        self.killed = True

    def poll(self):
        if self.killed:
            return -9
        if self.term_at is not None and time.monotonic() - self.term_at >= self.after_s:
            return 0
        return None


def _provisioner_with(procs: dict):
    from brain import provisioner as pv

    prov = pv.Provisioner()
    for key, proc in procs.items():
        entry = pv._Proc(proc, port=1000 + len(prov._procs), api_port=None)
        entry.booting = False
        prov._procs[key] = entry
    return prov


def test_gateway_shutdown_waits_for_consolidating_tenants_in_parallel(monkeypatch):
    from brain import provisioner as pv

    monkeypatch.setattr(pv, "SHUTDOWN_GRACE_S", 3.0)
    a, b = _ExitsAfter(1.0), _ExitsAfter(1.0)
    prov = _provisioner_with({"org-a": a, "org-b": b})
    t0 = time.monotonic()
    asyncio.run(prov.stop())
    took = time.monotonic() - t0
    # Both exited cleanly on their own (the old fixed 3 s × N sequential reap
    # killed anything slower) — and concurrently, not one after the other.
    assert not a.killed and not b.killed
    assert took < 1.9
    assert prov._procs == {}


def test_gateway_shutdown_still_kills_a_tenant_past_the_grace(monkeypatch):
    from brain import provisioner as pv

    monkeypatch.setattr(pv, "SHUTDOWN_GRACE_S", 0.4)
    stuck = _ExitsAfter(60.0)
    prov = _provisioner_with({"org-a": stuck})
    asyncio.run(prov.stop())
    assert stuck.killed


def test_single_stop_keeps_the_short_reap():
    stuck = _ExitsAfter(60.0)
    prov = _provisioner_with({"org-a": stuck})
    t0 = time.monotonic()
    asyncio.run(prov.stop_user("org-a"))
    assert stuck.killed and time.monotonic() - t0 < 5
