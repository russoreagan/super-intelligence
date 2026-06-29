"""
Result→reasoning reflex loop: a finished self-directed motor-cortex job is fed back
into DMN reflection (note_job_result → _event_seed), the entity reasons over it, and
the existing speak-gate / self-task pathways carry the decision. A reflex chain is
bounded by REFLEX_MAX_DEPTH so an autonomous follow-up chain can't run away.

Covers brain/dmn.py (note_job_result, commit_candidate_to_speech/take_proactive
from_job tagging) and brain/clusters/task_queue.py (reflex_depth threading).
"""

from __future__ import annotations

from collections import deque

from brain.clusters.task_queue import PersistentTaskQueue, Task
from brain.dmn import REFLEX_MAX_DEPTH, DefaultModeNetwork


def _bare_dmn() -> DefaultModeNetwork:
    # __new__ skeleton; note_job_result only needs _ensure_runtime_state (which seeds
    # the _event_seed* slots) — no bus/router/cells required.
    return DefaultModeNetwork.__new__(DefaultModeNetwork)


def test_note_job_result_seeds_reflection():
    dmn = _bare_dmn()
    dmn.note_job_result("scan NVDA options flow", "3 unusual call sweeps near the ask", True)
    assert "scan NVDA options flow" in dmn._event_seed
    assert "3 unusual call sweeps" in dmn._event_seed
    assert dmn._event_seed_depth == 0


def test_successful_job_with_no_summary_is_not_seeded():
    dmn = _bare_dmn()
    dmn.note_job_result("a routine refresh", "", True)
    assert dmn._event_seed == ""  # nothing worth reasoning over


def test_failure_is_always_seeded_even_without_summary():
    dmn = _bare_dmn()
    dmn.note_job_result("fetch the filing", "", False)
    assert "failed" in dmn._event_seed
    assert "fetch the filing" in dmn._event_seed


def test_depth_cap_seed_forbids_further_tasks():
    dmn = _bare_dmn()
    dmn.note_job_result("dig deeper", "found more", True, depth=REFLEX_MAX_DEPTH)
    assert dmn._event_seed_depth == REFLEX_MAX_DEPTH
    # At the cap the seed instructs the tick not to spawn another task.
    assert "do NOT start another task" in dmn._event_seed
    # Below the cap there is no such instruction.
    dmn2 = _bare_dmn()
    dmn2.note_job_result("dig deeper", "found more", True, depth=0)
    assert "do NOT start another task" not in dmn2._event_seed


def test_already_reported_seed_suppresses_repeat():
    # A user-awaited job's answer is delivered synchronously by _run_task; the reflex
    # seed must tell the entity not to repeat it, only to weigh a follow-up.
    dmn = _bare_dmn()
    dmn.note_job_result("price NVDA", "NVDA is $920", True, already_reported=True)
    assert "do not repeat" in dmn._event_seed
    # A non-reported (autonomous) result carries no such restriction.
    dmn2 = _bare_dmn()
    dmn2.note_job_result("price NVDA", "NVDA is $920", True, already_reported=False)
    assert "do not repeat" not in dmn2._event_seed


def test_proactive_queue_carries_from_job_flag():
    dmn = _bare_dmn()
    dmn._proactive_q = deque(maxlen=2)
    dmn.commit_candidate_to_speech({"spoken": "I found something on NVDA.", "from_job": True})
    dmn.commit_candidate_to_speech({"spoken": "idle musing", "from_job": False})
    a = dmn.take_proactive()
    b = dmn.take_proactive()
    assert a == {"spoken": "I found something on NVDA.", "from_job": True}
    assert b == {"spoken": "idle musing", "from_job": False}
    assert dmn.take_proactive() is None


def test_task_reflex_depth_round_trips():
    t = Task(id="x", goal="g", reflex_depth=2)
    assert Task.from_dict(t.to_dict()).reflex_depth == 2
    # Backward compatible: a queue entry written before this field defaults to 0.
    assert Task.from_dict({"id": "y", "goal": "g"}).reflex_depth == 0


def test_enqueue_threads_reflex_depth(tmp_path, monkeypatch):
    # Isolate from the real on-disk queue (avoid its dedup state and never clobber it).
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "tasks.json")
    q = PersistentTaskQueue()
    t = q.enqueue("follow up on the unusual sweep", source="self", priority=2, reflex_depth=1)
    assert t is not None
    assert t.reflex_depth == 1
    # Survives the disk round-trip (to_dict/from_dict).
    assert PersistentTaskQueue()._tasks[0].reflex_depth == 1


def test_recovery_count_round_trips():
    t = Task(id="r", goal="g", recovery_count=2)
    assert Task.from_dict(t.to_dict()).recovery_count == 2
    # Backward compatible: a queue entry written before this field defaults to 0.
    assert Task.from_dict({"id": "z", "goal": "g"}).recovery_count == 0


def test_interrupted_task_quarantined_after_recovery_cap(tmp_path, monkeypatch):
    # A job that hard-crashes the pod mid-run is left 'running' and recovered at boot.
    # Without a ceiling it re-runs on EVERY boot forever (bounded only by the daily $
    # cap). After MAX_RECOVERY_ATTEMPTS recoveries it must be quarantined instead.
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(tq, "MAX_RECOVERY_ATTEMPTS", 3)

    q = PersistentTaskQueue()
    t = q.enqueue("a job that wedges the pod mid-run", source="self", priority=2)
    assert t is not None
    tid = t.id

    recoveries = 0
    for _ in range(6):
        # Simulate the crash-loop: the recovered task gets picked up, starts, and the
        # pod dies again before it can finish — so it's 'running' at the next boot.
        task = next(x for x in q._tasks if x.id == tid)
        task.status = "running"
        out = q.recover_interrupted()
        if out:
            recoveries += 1
            assert out[0].id == tid and out[0].status == "pending"
        else:
            break

    # Exactly 3 recoveries, then quarantine — not an unbounded loop.
    assert recoveries == 3, f"expected 3 recoveries before quarantine, got {recoveries}"
    task = next(x for x in q._tasks if x.id == tid)
    assert task.status == "failed" and task.success is False
    assert task.recovery_count == 3

    # The counter PERSISTS across a fresh boot (else a real restart would reset it and
    # the cap would never bite): a quarantined task is not resurrected.
    q2 = PersistentTaskQueue()
    task2 = next(x for x in q2._tasks if x.id == tid)
    assert task2.recovery_count == 3 and task2.status == "failed"
    task2.status = "running"  # even if something flips it back, it re-quarantines at once
    assert q2.recover_interrupted() == []


def test_recovery_under_cap_still_re_runs(tmp_path, monkeypatch):
    # Regression guard: a task within its recovery budget is still recovered normally,
    # so a genuine one-off interruption (e.g. a single deploy restart) isn't lost.
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(tq, "MAX_RECOVERY_ATTEMPTS", 3)

    q = PersistentTaskQueue()
    t = q.enqueue("finish the half-done research", source="user", priority=1)
    assert t is not None
    next(x for x in q._tasks if x.id == t.id).status = "running"
    out = q.recover_interrupted()
    assert len(out) == 1 and out[0].id == t.id
    assert out[0].status == "pending" and out[0].source == "recovery"
    assert out[0].recovery_count == 1
