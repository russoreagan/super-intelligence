"""Grounded reward that arrives after its turn still reaches that turn's learning.

Two outcomes land after the turn that earned them has ended and been journaled:

* a background job the turn started finishes, and the TaskWorker pays
  job_success / job_failure DA outside any turn. With no trace bound, the
  reward-emission chokepoint (Bus._log_reward_emission) attached it to nothing,
  so tagged capture (hebbian) and the allostatic reward rate never saw the most
  grounded outcome the system has;
* an external grade is posted (api_grade_turn) and set on the live trace only,
  so a turn replayed from the journal after a crash came back ungraded.

Both are now credited to the originating turn while it is buffered and
journaled as late lines that boot replay applies, exactly once.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from brain import allostasis
from brain.observability import trace_journal
from brain.observability.timeline import TurnTrace
from brain.settings import settings

SUCCESS_SUMMARY = {"success": True, "complexity": "medium", "productive_steps": 6}
FAILURE_SUMMARY = {"success": False, "complexity": "medium", "productive_steps": 6}


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    r = tmp_path / "tenant"
    r.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(r))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_visionary")
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    return r


@pytest.fixture
def notes(monkeypatch):
    from brain.observability.decisions import decisions

    seen: list[tuple[str, dict]] = []
    real = decisions.log

    def _log(kind, **fields):
        seen.append((kind, fields))
        return real(kind, **fields)

    monkeypatch.setattr(decisions, "log", _log)
    return seen


def _trace(turn_id="t1", session_id="s1"):
    return TurnTrace(
        turn_id=turn_id,
        session_id=session_id,
        user_input="please research this",
        persona_name="the_analyst",
        end_user_id="buyer",
        fired_path=[{"name": "temporal.curiosity", "cluster": "temporal", "kind": "switch"}],
    )


def _session(*traces):
    from brain.bus import Bus
    from brain.session_turn import _TurnMixin

    class _Session(_TurnMixin):
        pass

    s = object.__new__(_Session)
    s.bus = Bus()
    s._session_traces_full = list(traces)
    return s


def _task(origin_turn_key="s1:t1"):
    return SimpleNamespace(id="abcd1234", origin_turn_key=origin_turn_key)


def _job_emissions(trace):
    return [e for e in trace.reward_emissions if e["reason"] in ("job_success", "job_failure")]


# ── the job remembers the turn that started it ───────────────────────────────


def test_a_job_enqueued_during_a_turn_carries_that_turn(tmp_path, monkeypatch):
    from brain.clusters import task_queue as tq
    from brain.observability.firing_path import reset_current_trace, set_current_trace

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(tq, "SELF_LEDGER_PATH", tmp_path / "ledger.json")
    q = tq.PersistentTaskQueue()
    token = set_current_trace(_trace())
    try:
        in_turn = q.enqueue("research the supplier list", source="user")
    finally:
        reset_current_trace(token)
    idle = q.enqueue("tidy the open threads ledger", source="self")
    assert in_turn.origin_turn_key == "s1:t1"
    assert idle.origin_turn_key == ""  # the DMN's own work: no turn started it
    # It survives the on-disk queue (a job outlives a restart).
    again = tq.PersistentTaskQueue()
    assert {t.goal: t.origin_turn_key for t in again._tasks}["research the supplier list"] == (
        "s1:t1"
    )


# ── job outcomes reach the originating turn ──────────────────────────────────


def test_job_success_is_credited_to_the_turn_that_started_it():
    trace = _trace()
    s = _session(trace)
    s._credit_job_outcome(_task(), SUCCESS_SUMMARY)
    [e] = _job_emissions(trace)
    assert e["reason"] == "job_success" and e["delta"] > 0
    # Grounded reward for the sleep-time learning that reads it.
    assert allostasis.grounded_reward(trace) == pytest.approx(e["delta"])


def test_job_failure_is_credited_too():
    trace = _trace()
    _session(trace)._credit_job_outcome(_task(), FAILURE_SUMMARY)
    [e] = _job_emissions(trace)
    assert e["reason"] == "job_failure" and e["delta"] < 0
    assert allostasis.grounded_reward(trace) < 0


def test_the_credit_binds_only_for_the_emission():
    from brain.observability.firing_path import get_current_trace

    _session(_trace())._credit_job_outcome(_task(), SUCCESS_SUMMARY)
    assert get_current_trace() is None


def test_a_crash_before_consolidation_replays_the_job_credit(root):
    trace = _trace()
    trace_journal.append(trace, {"turn_id": "t1", "session_id": "s1"})
    _session(trace)._credit_job_outcome(_task(), SUCCESS_SUMMARY)
    [credited] = _job_emissions(trace)

    fulls, _ = trace_journal.load_orphans()  # the process died; next boot
    assert len(fulls) == 1
    assert _job_emissions(fulls[0]) == [credited]
    assert allostasis.grounded_reward(fulls[0]) == pytest.approx(credited["delta"])
    # A second crash before consolidation replays it once more, not twice.
    fulls2, _ = trace_journal.load_orphans()
    assert _job_emissions(fulls2[0]) == [credited]


def test_a_committed_batch_never_replays_the_credit(root):
    trace = _trace()
    trace_journal.append(trace, {"turn_id": "t1", "session_id": "s1"})
    _session(trace)._credit_job_outcome(_task(), SUCCESS_SUMMARY)
    token = trace_journal.rotate_inflight()  # the consolidation pass snapshots …
    trace_journal.clear_inflight(token)  # … and commits
    assert trace_journal.load_orphans() == ([], [])


def test_a_job_whose_turn_already_left_the_buffer_is_dropped_with_a_note(root, notes):
    s = _session()  # consolidated: the buffer no longer has it
    da_before = s.bus.neuromod.get("DA")
    s._credit_job_outcome(_task(), SUCCESS_SUMMARY)
    assert s.bus.neuromod.get("DA") > da_before  # the chemistry still moves
    [(_, note)] = [n for n in notes if n[0] == "late_credit_dropped"]
    assert note["turn_id"] == "t1" and note["reason"] == "turn_not_live"
    assert note["credit"] == "job_outcome"
    assert not (root / "pending_traces.jsonl").exists()  # nothing journaled


def test_a_turn_both_learning_stages_committed_cannot_take_it(root, notes):
    trace = _trace()  # replayed after a cut-short pass that committed its learning
    trace_journal.mark_applied(trace_journal.STAGE_HEBBIAN, [trace])
    trace_journal.mark_applied(trace_journal.STAGE_ALLOSTASIS, [trace])
    _session(trace)._credit_job_outcome(_task(), SUCCESS_SUMMARY)
    assert _job_emissions(trace) == []
    assert [n[1]["reason"] for n in notes if n[0] == "late_credit_dropped"] == ["turn_consolidated"]


def test_the_dmns_own_work_pays_but_notes_nothing(notes):
    trace = _trace()
    _session(trace)._credit_job_outcome(_task(origin_turn_key=""), SUCCESS_SUMMARY)
    assert _job_emissions(trace) == []
    assert not [n for n in notes if n[0] == "late_credit_dropped"]


def test_the_credited_job_tags_the_turns_path_at_sleep(monkeypatch, tmp_path):
    """End to end into tagged capture: a strong job result credited late tags the
    turn's winning skill like an in-turn tool result does."""
    from tests.test_fragment_wiring import _isolated_wiring, _sc, _win_trace

    monkeypatch.setitem(settings._data, "accomplishment_base", 0.3)
    monkeypatch.setitem(settings._data, "job_intrinsic_da_cap", 1.0)

    def _weight(tagged: int, sub: str) -> float:
        monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / sub))
        monkeypatch.setitem(settings._data, "tagged_consolidation", tagged)
        w = _isolated_wiring(monkeypatch, tmp_path / sub)
        sc = _sc(monkeypatch, tmp_path / sub, w)
        t = _win_trace("t1", winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})
        t.session_id = "s1"
        _session(t)._credit_job_outcome(_task(), SUCCESS_SUMMARY)
        assert allostasis.grounded_reward(t) >= float(settings.get("tag_threshold", 0.05))
        sc._run_hebbian_pass("s1", [t])
        return dict(w.attached_fragments("frontal.drafter_C")).get("alpha", 1.0)

    assert _weight(1, "b") > _weight(0, "a") + 0.05


# ── late grades survive a crash replay ───────────────────────────────────────


def _loops(*traces):
    from brain.bus import Bus
    from brain.session_loops import _LoopsMixin

    stub = SimpleNamespace(bus=Bus(), _session_traces_full=list(traces), _eval_logger=None)
    return lambda tid, g, **kw: _LoopsMixin.api_grade_turn(stub, tid, g, **kw)


def test_a_grade_posted_before_a_crash_is_replayed(root):
    trace = _trace()
    trace_journal.append(trace, {"turn_id": "t1", "session_id": "s1"})
    grade = _loops(trace)
    assert grade("t1", 1.0, source="validator")["applied_live"] is True

    fulls, _ = trace_journal.load_orphans()
    assert fulls[0].external_grade == pytest.approx(1.0)
    assert fulls[0].external_grade_source == "validator"
    assert allostasis.grounded_reward(fulls[0]) == pytest.approx(0.2)


def test_a_regrade_replays_as_the_last_grade(root):
    trace = _trace()
    trace_journal.append(trace, {"turn_id": "t1", "session_id": "s1"})
    grade = _loops(trace)
    grade("t1", 1.0)
    grade("t1", -1.0)
    fulls, _ = trace_journal.load_orphans()
    assert fulls[0].external_grade == pytest.approx(-1.0)


def test_a_grade_for_a_turn_not_buffered_journals_nothing(root):
    out = _loops()("t1", 1.0)
    assert out["applied_live"] is False and out["reason"] == "turn_not_live"
    assert not (root / "pending_traces.jsonl").exists()
