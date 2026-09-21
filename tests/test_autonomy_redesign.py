"""
Verification suite for the motor-cortex autonomy redesign (brain.autonomy + the
cloud-only / defer / durable-surfacing changes).

Covers the plan's acceptance checks that can be exercised as fast unit tests:
budget tiers + soft-pause approval, external-only classification, the JobOutcome
state model (no silent empty-success), the router's cloud-only defer signal + split
spend pool, the task queue's deferred/backoff, JobStore legacy-state synthesis,
the reporter floor (never empty), and list-tool pagination.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from brain.autonomy import (
    CONTINUE_SPEND_TOOL,
    AutonomousBudget,
    BudgetTier,
    DeferReason,
    JobOutcome,
    JobState,
    RunOutcome,
    SpendRiskGate,
    StopReason,
)


# ── Fakes ──────────────────────────────────────────────────────────────────
class FakeRouter:
    def __init__(self, autonomous_usd=0.0, soft_cleared=False, bucket_empty=False):
        self._usd = autonomous_usd
        self._cleared = soft_cleared
        self._bucket_empty = bucket_empty

    def autonomous_usd_today(self):
        return self._usd

    def autonomous_soft_cleared(self):
        return self._cleared

    def clear_autonomous_soft_pause(self):
        self._cleared = True

    def bg_bucket_empty(self):
        return self._bucket_empty


class FakeApprovals:
    def __init__(self):
        self.recorded = []

    def record(self, tool, tool_input, reason="", turn_id="", end_user_id=""):
        self.recorded.append(tool)
        return {"id": "a1", "tool": tool}


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    # Pin the autonomous caps so tier() is deterministic regardless of settings.json.
    import brain.autonomy.budget as bud

    monkeypatch.setattr(
        bud,
        "_settings",
        type(
            "S",
            (),
            {
                "get": staticmethod(
                    lambda k, d=None: {
                        "autonomous_soft_usd": 30.0,
                        "autonomous_hard_usd": 50.0,
                    }.get(k, d)
                )
            },
        )(),
    )
    yield


# ── JobOutcome state model ─────────────────────────────────────────────────
def test_completed_requires_work_and_summary():
    ok = JobOutcome.completed("j", "goal", productive_steps=2, summary="did it")
    assert ok.state is JobState.COMPLETED and ok.success and ok.summary == "did it"


def test_completed_with_no_work_coerces_to_failed():
    # The CMA "(no output)" path can never masquerade as success.
    for ps, summ in [(0, "x"), (2, ""), (0, "")]:
        o = JobOutcome.completed("j", "goal", productive_steps=ps, summary=summ)
        assert o.state is JobState.FAILED and not o.success
        assert o.reason_human  # never empty


def test_every_terminal_state_has_nonempty_reason_and_summary():
    outs = [
        JobOutcome.deferred("j", "g", reason=DeferReason.RATE_BUCKET_EMPTY, backoff_s=5),
        JobOutcome.stopped_budget("j", "g"),
        JobOutcome.awaiting_approval("j", "g", reason_human="needs approval"),
        JobOutcome.failed("j", "g", reason_code="x", reason_human="broke"),
    ]
    for o in outs:
        assert o.reason_human and o.summary
        assert o.to_record()["state"] == o.state.value
        assert o.success is False


# ── Budget tiers + soft-pause continue-approval ────────────────────────────
@pytest.mark.parametrize(
    "usd,expected",
    [
        (29, BudgetTier.UNDER_SOFT),
        (31, BudgetTier.SOFT_EXCEEDED),
        (49, BudgetTier.SOFT_EXCEEDED),
        (51, BudgetTier.HARD_EXCEEDED),
    ],
)
def test_budget_tiers(usd, expected):
    assert AutonomousBudget(FakeRouter(usd)).tier() is expected


def test_soft_pause_records_one_continue_approval_then_lifts():
    router = FakeRouter(autonomous_usd=31.0)
    budget = AutonomousBudget(router)
    approvals = FakeApprovals()
    gate = SpendRiskGate(budget, approvals, router)

    dec = gate.check_spend()
    assert dec.outcome is RunOutcome.DEFER and dec.defer_reason is DeferReason.BUDGET_SOFT_PAUSE
    assert approvals.recorded == [CONTINUE_SPEND_TOOL]

    # Owner approves → clear the pause → work runs (up to the hard cap).
    budget.clear_soft_pause()
    assert gate.check_spend().outcome is RunOutcome.RUN


def test_hard_cap_stops_without_approval():
    router = FakeRouter(autonomous_usd=51.0)
    approvals = FakeApprovals()
    gate = SpendRiskGate(AutonomousBudget(router), approvals, router)
    dec = gate.check_spend()
    assert dec.outcome is RunOutcome.STOP and dec.stop_reason is StopReason.BUDGET_HARD_STOP
    assert approvals.recorded == []  # a hard stop never asks


def test_rate_bucket_empty_defers():
    router = FakeRouter(autonomous_usd=0.0, bucket_empty=True)
    gate = SpendRiskGate(AutonomousBudget(router), FakeApprovals(), router)
    dec = gate.check_spend()
    assert dec.outcome is RunOutcome.DEFER and dec.defer_reason is DeferReason.RATE_BUCKET_EMPTY


def test_cloud_health_trips_after_consecutive_timeouts(monkeypatch):
    import brain.autonomy.gate as g

    monkeypatch.setattr(
        g,
        "_settings",
        type(
            "S",
            (),
            {
                "get": staticmethod(
                    lambda k, d=None: {
                        "bg_cloud_timeout_trip": 3,
                        "cloud_unreachable_cooldown_s": 60,
                    }.get(k, d)
                )
            },
        )(),
    )
    router = FakeRouter()
    gate = SpendRiskGate(AutonomousBudget(router), FakeApprovals(), router)
    gate.note_cloud_timeout()
    gate.note_cloud_timeout()
    assert not gate.cloud_unreachable()
    gate.note_cloud_timeout()  # third → trip
    assert gate.cloud_unreachable()
    gate.note_cloud_success()  # any success resets
    assert not gate.cloud_unreachable()


# ── External-only classification ───────────────────────────────────────────
@pytest.mark.parametrize(
    "tool,expected",
    [
        ("read_file", RunOutcome.RUN),
        ("write_file", RunOutcome.RUN),
        ("run_command", RunOutcome.RUN),
        ("list_files", RunOutcome.RUN),
        ("get_quote", RunOutcome.RUN),
        ("send_email", RunOutcome.ASK),
        ("gmail.send_message", RunOutcome.ASK),
        ("place_order", RunOutcome.ASK),
        ("delete_record", RunOutcome.ASK),
    ],
)
def test_classify_action_external_only(tool, expected):
    router = FakeRouter()
    gate = SpendRiskGate(AutonomousBudget(router), FakeApprovals(), router)
    assert gate.classify_action(tool, {}).outcome is expected


def test_explicit_recipient_is_external():
    router = FakeRouter()
    gate = SpendRiskGate(AutonomousBudget(router), FakeApprovals(), router)
    assert gate.classify_action("notify_partner", {"to": "a@b.com"}).outcome is RunOutcome.ASK


# ── Router: cloud-only defer + split spend pool ────────────────────────────
def _bare_router():
    import datetime as _dt

    import brain.model_router as mr

    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._cloud_usd_today = 0.0
    # Set to today so _refresh_cloud_usd_today() doesn't reload/wipe mid-test.
    r._cloud_usd_date = _dt.date.today().isoformat()
    r._cloud_usd_autonomous_today = 0.0
    r._autonomous_soft_cleared_date = ""
    r._bg_defer_reason = None
    r._spend_gate = None
    r._bg_cloud_bucket = 100_000.0
    r._bg_cloud_bucket_ts = time.monotonic()
    r._bg_mode = False  # bg depth lives in a ContextVar; the setter zeroes it
    return r


def test_split_spend_pool_charges_autonomous_only_in_bg(monkeypatch, tmp_path):
    import brain.model_router as mr

    r = _bare_router()
    monkeypatch.setattr(mr, "_CLOUD_USAGE_PATH", str(tmp_path / "cloud_usage.json"))
    # Interactive charge → total only.
    r.exit_background_mode()  # ensure interactive
    r._charge_cloud_usd("claude-haiku-4-5-20251001", 1000, 1000, 0)
    assert r.autonomous_usd_today() == 0.0
    assert r._cloud_usd_today > 0
    # Background charge → also the autonomous pool.
    r.enter_background_mode()
    r._charge_cloud_usd("claude-haiku-4-5-20251001", 1000, 1000, 0)
    assert r.autonomous_usd_today() > 0


def test_bg_precheck_defers_on_empty_bucket():
    r = _bare_router()
    # The bucket can go negative (borrowing); a tiny refill drip must not lift it above 0.
    r._bg_cloud_bucket = -100_000.0
    reason = r._bg_precheck("motor", "planner")
    assert reason is DeferReason.RATE_BUCKET_EMPTY


def test_take_bg_defer_is_one_shot():
    r = _bare_router()
    r._bg_defer_reason = DeferReason.CLOUD_UNREACHABLE
    assert r.take_bg_defer() is DeferReason.CLOUD_UNREACHABLE
    assert r.take_bg_defer() is None  # consumed


def test_soft_cleared_roundtrips(monkeypatch, tmp_path):
    import brain.model_router as mr

    r = _bare_router()
    monkeypatch.setattr(mr, "_CLOUD_USAGE_PATH", str(tmp_path / "cloud_usage.json"))
    assert r.autonomous_soft_cleared() is False
    r.clear_autonomous_soft_pause()
    assert r.autonomous_soft_cleared() is True


# ── Task queue: deferred + backoff ─────────────────────────────────────────
def test_task_queue_defer_promote(monkeypatch, tmp_path):
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()
    q.enqueue("do a thing", source="self")
    t = q.take_next()
    assert t is not None and t.status == "running"

    q.mark_deferred(t.id, backoff_s=1000.0, reason="cloud down")
    # Not due → reads as idle, take_next skips it.
    assert q.has_pending() is False
    assert q.take_next() is None

    # Force it due → promoted + returned.
    for task in q._tasks:
        task.not_before = time.time() - 1
    assert q.has_pending() is True
    t2 = q.take_next()
    assert t2 is not None and t2.id == t.id and t2.status == "running"


# ── JobStore legacy-state synthesis ────────────────────────────────────────
def test_jobstore_synthesizes_state_from_legacy(monkeypatch, tmp_path):
    import brain.clusters.job_store as js

    monkeypatch.setattr(js, "JOBS_DIR", tmp_path / "jobs")
    store = js.JobStore()
    # A record written the old way (no state field) still reports a state.
    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    import json

    (tmp_path / "jobs" / "old.json").write_text(
        json.dumps(
            {
                "job_id": "old",
                "goal": "g",
                "success": True,
                "done": True,
                "steps": [],
                "results": [],
            }
        )
    )
    rec = store.get("old")
    assert rec["state"] == "completed"


# ── Reporter floor (never empty) ───────────────────────────────────────────
def test_reporter_floor_never_empty():
    from brain.clusters.follow_through import ResultReporter

    # Deterministic templates don't touch the model.
    assert ResultReporter._deterministic_summary(
        {"goal": "gather data", "productive_steps": 3, "source_links": ["u"]}
    ).strip()
    for st in ("deferred", "stopped_budget", "awaiting_approval", "failed"):
        assert ResultReporter._state_summary({"goal": "g"}, st).strip()


# ── List-tool pagination ───────────────────────────────────────────────────
def test_list_files_pages_with_signal(monkeypatch, tmp_path):
    from brain.clusters import motor_dispatcher as md

    # Small page size so a handful of files paginates.
    monkeypatch.setattr(md, "_page_size", lambda limit=None: int(limit or 3))
    for i in range(7):
        (tmp_path / f"f{i}.txt").write_text("x")
    d = md.ToolDispatcher(allowed_paths=[str(tmp_path)])
    out = d._list_files(str(tmp_path), "*.txt")
    assert "offset=3" in out and "more" in out
    # Second page continues from the offset.
    out2 = d._list_files(str(tmp_path), "*.txt", offset=3)
    assert "offset=6" in out2


# ── Background mode is per-task, not per-router ──────────────────────────────
# One ModelRouter is shared by the DMN, metacognition, motor jobs and live turns,
# all concurrent tasks on one loop. While the flag was a plain instance bool they
# clobbered each other in both directions.


@pytest.mark.asyncio
async def test_background_mode_does_not_leak_between_concurrent_tasks():
    r = _bare_router()
    started = asyncio.Event()
    meta_done = asyncio.Event()
    observed = {}

    async def motor_job():
        r.enter_background_mode()
        try:
            started.set()
            await meta_done.wait()
            # Metacognition has entered AND exited in its own task by now.
            observed["motor_still_bg"] = r._bg_mode
        finally:
            r.exit_background_mode()

    async def metacognition_pass():
        await started.wait()
        r.enter_background_mode()
        r.exit_background_mode()
        meta_done.set()

    await asyncio.gather(motor_job(), metacognition_pass())
    assert observed["motor_still_bg"] is True, "metacognition dropped the motor job's bg mode"


@pytest.mark.asyncio
async def test_a_background_pass_does_not_put_a_live_turn_into_background():
    """The user-visible half: a DMN pass entering bg mode used to truncate the
    user's reply to bg_cloud_max_tokens_per_call and give it the 20s bg timeout."""
    r = _bare_router()
    in_bg = asyncio.Event()
    release = asyncio.Event()
    seen = {}

    async def dmn_pass():
        r.enter_background_mode()
        try:
            in_bg.set()
            await release.wait()
        finally:
            r.exit_background_mode()

    async def live_turn():
        await in_bg.wait()
        seen["turn_bg"] = r._bg_mode
        release.set()

    await asyncio.gather(dmn_pass(), live_turn())
    assert seen["turn_bg"] is False, "a background pass leaked into a live user turn"


@pytest.mark.asyncio
async def test_background_mode_nests():
    r = _bare_router()
    r.enter_background_mode()
    r.enter_background_mode()
    r.exit_background_mode()
    assert r._bg_mode is True, "the inner exit ended background mode early"
    r.exit_background_mode()
    assert r._bg_mode is False
    # Unbalanced extra exits floor at zero rather than going negative.
    r.exit_background_mode()
    r.enter_background_mode()
    assert r._bg_mode is True
    r.exit_background_mode()
    assert r._bg_mode is False


@pytest.mark.asyncio
async def test_a_child_task_inherits_background_mode():
    """Work spawned by a bg job is still bg work."""
    r = _bare_router()
    seen = {}

    async def child():
        seen["bg"] = r._bg_mode

    r.enter_background_mode()
    try:
        await asyncio.create_task(child())
    finally:
        r.exit_background_mode()
    assert seen["bg"] is True


# ── Deferral backoff actually compounds ─────────────────────────────────────
# The old code read `prior = 2 if t.status == "deferred" else 1` — 1 or 2, never
# more — so the docstring's "bounded to ~1h" was unreachable and a
# CLOUD_UNREACHABLE task retried every 60s forever, each retry writing a row to
# Supabase (~1,440 writes/day per stuck task).


def _deferred_wait(q, task_id, n, base=30.0):
    """Defer n times, returning the wait chosen each time."""
    import brain.clusters.task_queue as tq

    waits = []
    for _ in range(n):
        now = time.time()
        q.mark_deferred(task_id, backoff_s=base, reason="cloud down")
        t = next(x for x in q._tasks if x.id == task_id)
        waits.append(t.not_before - now)
        if t.status != "deferred":
            break
        t.status = "running"  # simulate promotion + another failed attempt
    del tq
    return waits


def _queue(tmp_path, monkeypatch):
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "QUEUE_PATH", str(tmp_path / "q.json"), raising=False)
    q = tq.PersistentTaskQueue()
    q._tasks = []
    return q


def test_deferral_backoff_doubles_and_reaches_its_documented_ceiling(tmp_path, monkeypatch):
    import brain.clusters.task_queue as tq

    q = _queue(tmp_path, monkeypatch)
    q.enqueue("do a thing", source="self")
    t = q.take_next()

    waits = _deferred_wait(q, t.id, 10, base=30.0)
    # 30, 60, 120, 240, ... doubling, not a flat 60.
    assert waits[0] == pytest.approx(30.0, abs=1)
    assert waits[1] == pytest.approx(60.0, abs=1)
    assert waits[2] == pytest.approx(120.0, abs=1)
    assert waits[3] == pytest.approx(240.0, abs=1)
    # ...and it actually gets to the ceiling the docstring promised.
    assert waits[-1] == pytest.approx(tq.MAX_DEFER_BACKOFF_S, abs=1)
    # `waits` is measured as not_before - now, so it carries the elapsed time of the
    # call itself; compare with a tolerance rather than an exact ceiling.
    assert max(waits) <= tq.MAX_DEFER_BACKOFF_S + 1


def test_a_very_long_outage_saturates_instead_of_overflowing(tmp_path, monkeypatch):
    import brain.clusters.task_queue as tq

    q = _queue(tmp_path, monkeypatch)
    q.enqueue("do a thing", source="self")
    t = q.take_next()
    task = next(x for x in q._tasks if x.id == t.id)
    task.defer_count = 100_000
    now = time.time()
    q.mark_deferred(t.id, backoff_s=30.0, reason="still down")
    assert task.not_before - now <= tq.MAX_DEFER_BACKOFF_S + 1


def test_a_task_that_is_never_approved_eventually_gives_up(tmp_path, monkeypatch):
    """MAX_TASKS trimming only evicts completed/failed, so without this a task the
    gate will never approve stays queued for the life of the process."""
    import brain.clusters.task_queue as tq

    q = _queue(tmp_path, monkeypatch)
    q.enqueue("never allowed", source="self")
    t = q.take_next()
    task = next(x for x in q._tasks if x.id == t.id)
    task.defer_count = tq.MAX_DEFER_ATTEMPTS - 1
    q.mark_deferred(t.id, backoff_s=30.0, reason="refused again")
    assert task.status == "failed" and task.success is False
    assert task.completed_at


def test_a_successful_run_clears_the_backoff(tmp_path, monkeypatch):
    q = _queue(tmp_path, monkeypatch)
    q.enqueue("flaky", source="self")
    t = q.take_next()
    task = next(x for x in q._tasks if x.id == t.id)
    q.mark_deferred(t.id, backoff_s=30.0, reason="blip")
    q.mark_deferred(t.id, backoff_s=30.0, reason="blip")
    assert task.defer_count == 2
    task.status = "running"
    q.mark_done(t.id, success=True)
    assert task.defer_count == 0, "a cleared outage must not keep punishing the task"


def test_defer_count_survives_a_reload(tmp_path, monkeypatch):
    """The count has to be persisted or the backoff resets on every restart."""
    import brain.clusters.task_queue as tq

    q = _queue(tmp_path, monkeypatch)
    q.enqueue("thing", source="self")
    t = q.take_next()
    q.mark_deferred(t.id, backoff_s=30.0, reason="down")
    q.mark_deferred(t.id, backoff_s=30.0, reason="down")
    restored = tq.Task.from_dict(next(x for x in q._tasks if x.id == t.id).to_dict())
    assert restored.defer_count == 2
    # A row written before the field existed still loads.
    old = {k: v for k, v in restored.to_dict().items() if k != "defer_count"}
    assert tq.Task.from_dict(old).defer_count == 0
