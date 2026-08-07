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
    r._bg_mode_val = False
    return r


def test_split_spend_pool_charges_autonomous_only_in_bg(monkeypatch, tmp_path):
    import brain.model_router as mr

    r = _bare_router()
    monkeypatch.setattr(mr, "_CLOUD_USAGE_PATH", str(tmp_path / "cloud_usage.json"))
    # Interactive charge → total only.
    r._bg_mode_val = False
    r._charge_cloud_usd("claude-haiku-4-5-20251001", 1000, 1000, 0)
    assert r.autonomous_usd_today() == 0.0
    assert r._cloud_usd_today > 0
    # Background charge → also the autonomous pool.
    r._bg_mode_val = True
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
