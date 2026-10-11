"""brain/admin_sweep — The Admin watches the org and fixes only what is safely fixable.

2026-10-10: the therapy org's Admin (shell, network and writes all off) spent an
afternoon on self-invented "run a performance audit" jobs that failed at step one. The
redesign: a deterministic sweep, one bounded fix per issue type, a model call only when
something new is wrong, and an org switch that keeps idle ideas from becoming jobs.
"""

from __future__ import annotations

import asyncio
import collections
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain import admin_sweep as sw
from brain.settings import settings

NOW = 1_800_000_000.0


def _codes(issues):
    return {i["code"]: i for i in issues}


# ── find_issues ────────────────────────────────────────────────────────────────


def test_healthy_org_has_no_issues():
    assert sw.find_issues(signals={"breaker": {}, "dmn": {"dormant": True}}, jobs=[], now=NOW) == []


def test_breaker_is_fixable_only_while_held_and_probe_known():
    issues = sw.find_issues(
        signals={
            "breaker": {
                "anthropic": {"kind": "billing", "until": NOW + 600, "since": NOW - 60},
                "google": {"kind": "auth", "until": NOW + 600},
            }
        },
        jobs=[],
        now=NOW,
    )
    by_subject = {i["subject"]: i for i in issues}
    assert by_subject["anthropic"]["fix"] == "reset_breaker"
    assert by_subject["google"]["fix"] == "", "no probe model for google → report only"
    # An expired hold already lets the next call through as the probe: nothing to reset.
    expired = sw.find_issues(
        signals={"breaker": {"anthropic": {"kind": "auth", "until": NOW - 1}}}, jobs=[], now=NOW
    )
    assert expired[0]["fix"] == ""


def test_stuck_running_job_is_killed_but_approval_wait_is_only_reported():
    jobs = [
        {"job_id": "job_task_r", "state": "running", "updated_at": NOW - 45 * 60},
        {"job_id": "job_task_a", "state": "awaiting_approval", "updated_at": NOW - 2 * 86400},
        {"job_id": "job_task_ok", "state": "running", "updated_at": NOW - 60},
    ]
    c = _codes(sw.find_issues(signals={}, jobs=jobs, now=NOW))
    assert c["stuck_job"]["subject"] == "job_task_r"
    assert c["stuck_job"]["fix"] == "kill_job"
    assert c["waiting_on_human"]["subject"] == "job_task_a"
    assert c["waiting_on_human"]["fix"] == "", "a job waiting on a person is never touched"


def test_repeated_failures_are_reported_by_reason_without_job_goals():
    jobs = [
        {
            "job_id": f"j{n}",
            "state": "failed",
            "reason_code": "provider_error",
            "reason_human": "The model provider refused the call.",
            "goal": "SECRET GOAL",
            "updated_at": NOW - 600,
        }
        for n in range(3)
    ] + [
        {"job_id": "old", "state": "failed", "reason_code": "x", "updated_at": NOW - 5 * 3600},
        {"job_id": "once", "state": "failed", "reason_code": "y", "updated_at": NOW - 60},
        {
            "job_id": "reaped",
            "state": "failed",
            "reason_code": "stale_running",
            "updated_at": NOW - 60,
        },
        {
            "job_id": "reaped2",
            "state": "failed",
            "reason_code": "stale_running",
            "updated_at": NOW - 60,
        },
    ]
    issues = sw.find_issues(signals={}, jobs=jobs, now=NOW)
    assert [i["subject"] for i in issues] == ["provider_error"]
    assert issues[0]["evidence"]["count"] == 3
    assert "SECRET GOAL" not in repr(issues), "evidence is content-free"


def test_project_rules():
    projects = [
        {
            "id": "p-orphan",
            "state": "running",
            "last_started_at": NOW - 3600,
            "in_flight_task_id": "gone",
        },
        {
            "id": "p-live",
            "state": "running",
            "last_started_at": NOW - 3600,
            "in_flight_task_id": "t-live",
        },
        {
            "id": "p-fresh",
            "state": "running",
            "last_started_at": NOW - 60,
            "in_flight_task_id": "gone",
        },
        {"id": "p-blocked", "state": "blocked", "updated_at": NOW - 2 * 86400},
        {"id": "p-failed", "state": "failed", "consecutive_failures": 3},
    ]
    issues = sw.find_issues(
        signals={}, jobs=[], projects=projects, live_task_ids={"t-live"}, now=NOW
    )
    got = {(i["code"], i["subject"], i["fix"]) for i in issues}
    assert got == {
        ("stuck_project", "p-orphan", "release_project"),
        ("project_waiting", "p-blocked", ""),
        ("project_failed", "p-failed", ""),
    }


def test_connector_errors_from_registry_and_breaker_merge_into_one_issue():
    issues = sw.find_issues(
        signals={},
        jobs=[],
        connectors=[{"name": "gmail", "status": "error"}, {"name": "drive", "status": "ready"}],
        connector_health={"slack": {"disabled": True}, "drive": {"disabled": False}},
        now=NOW,
    )
    assert len(issues) == 1
    assert issues[0]["fix"] == "reload_connectors"
    assert issues[0]["evidence"]["connectors"] == ["gmail", "slack"]


def test_every_fix_named_is_a_known_fix():
    issues = sw.find_issues(
        signals={"breaker": {"anthropic": {"until": NOW + 1}}, "pod_budget": {"exhausted": True}},
        jobs=[{"job_id": "j", "state": "running", "updated_at": NOW - 7200}],
        projects=[{"id": "p", "state": "running", "last_started_at": NOW - 7200}],
        connectors=[{"name": "c", "status": "error"}],
        now=NOW,
    )
    assert {i["fix"] for i in issues} - {""} <= set(sw.FIXES)


# ── Cooldown ───────────────────────────────────────────────────────────────────


def test_cooldown_reports_once_and_again_after_recovery():
    cd = sw.Cooldown()
    a = {"key": "stuck_job:j1"}
    assert cd.fresh([a], 3600, now=NOW) == [a]
    assert cd.fresh([a], 3600, now=NOW + 60) == [], "still broken, inside the window"
    assert cd.fresh([a], 3600, now=NOW + 3601) == [a], "still broken, window passed"
    assert cd.fresh([], 3600, now=NOW + 3700) == []  # it cleared
    assert cd.fresh([a], 3600, now=NOW + 3710) == [a], "a recurrence is news"


# ── Sweep log ─────────────────────────────────────────────────────────────────


def test_log_round_trip_newest_first_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_log_path", lambda: tmp_path / sw.LOG_FILENAME)
    monkeypatch.setattr(sw, "LOG_KEEP", 3)
    for n in range(5):
        sw.record({"ts": n})
    assert [r["ts"] for r in sw.recent(10)] == [4, 3, 2]


def test_fallback_text_says_what_was_done():
    issues = [
        {"key": "a", "detail": "job stuck", "fix": "kill_job"},
        {"key": "b", "detail": "key rejected", "fix": "reset_breaker"},
        {"key": "c", "detail": "waiting on you", "fix": ""},
    ]
    text = sw.fallback_text(
        issues,
        {
            "a": {"applied": True, "note": "stopped the stuck job"},
            "b": {"applied": False, "note": "still rejects the key"},
        },
    )
    assert "job stuck — fixed: stopped the stuck job" in text
    assert "key rejected — not fixed: still rejects the key" in text
    assert "- waiting on you" in text


# ── The session side: fixes and the sweep ─────────────────────────────────────


def _session():
    from brain.session_loops import _LoopsMixin

    s = _LoopsMixin.__new__(_LoopsMixin)
    s.router = MagicMock()
    s.router.__dict__["_provider_outage"] = {}
    s.router.call = AsyncMock(return_value="A job stalled, so I stopped it.")
    s.router.provider_outages = MagicMock(return_value={})
    s._task_queue = None
    s.dmn = None
    s.motor = None
    return s


def test_breaker_fix_restores_the_hold_when_the_probe_fails():
    s = _session()
    held = {"kind": "billing", "until": NOW + 999, "strikes": 2, "since": NOW - 60}
    s.router.__dict__["_provider_outage"]["anthropic"] = dict(held)

    def _reset(p):
        s.router.__dict__["_provider_outage"].pop(p, None)

    s.router.reset_provider_breaker = MagicMock(side_effect=_reset)
    s.router.call = AsyncMock(side_effect=RuntimeError("402 credit balance too low"))
    res = asyncio.run(s._admin_fix({"fix": "reset_breaker", "subject": "anthropic"}))
    assert res["applied"] is False
    assert s.router.__dict__["_provider_outage"]["anthropic"] == held, (
        "a failed probe must not lengthen the outage it found"
    )

    s.router.call = AsyncMock(return_value="OK")
    res = asyncio.run(s._admin_fix({"fix": "reset_breaker", "subject": "anthropic"}))
    assert res["applied"] is True
    assert "anthropic" not in s.router.__dict__["_provider_outage"]


def test_kill_job_falls_back_to_closing_the_orphaned_record(monkeypatch):
    s = _session()
    s.kill_task = MagicMock(return_value={"killed_running": False, "cancelled_pending": False})
    from brain import agent_jobs_store

    monkeypatch.setattr(agent_jobs_store, "reap_stale", lambda: 1)
    res = asyncio.run(s._admin_fix({"fix": "kill_job", "subject": "job_task_x"}))
    assert res == {"applied": True, "note": "closed the orphaned job record"}


def test_report_only_issue_is_never_acted_on():
    s = _session()
    s.kill_task = MagicMock()
    res = asyncio.run(s._admin_fix({"fix": "", "subject": "job_task_a"}))
    assert res["applied"] is False
    s.kill_task.assert_not_called()


def _wire_sweep(s, monkeypatch, tmp_path, inputs):
    monkeypatch.setattr(sw, "_log_path", lambda: tmp_path / sw.LOG_FILENAME)
    s._admin_sweep_inputs = lambda: inputs
    from brain import personas

    monkeypatch.setattr(personas, "_read_self_md", lambda slug: "")


def test_healthy_sweep_makes_no_model_call(monkeypatch, tmp_path):
    s = _session()
    _wire_sweep(s, monkeypatch, tmp_path, {"signals": {}, "jobs": [], "projects": []})
    entry = asyncio.run(s.run_admin_sweep())
    assert entry["issues"] == 0 and entry["new"] == []
    s.router.call.assert_not_called()
    assert sw.recent(1)[0]["issues"] == 0


def test_sweep_fixes_explains_and_then_stays_quiet(monkeypatch, tmp_path):
    import time as _t

    s = _session()
    stale = _t.time() - 3600
    _wire_sweep(
        s,
        monkeypatch,
        tmp_path,
        {
            "signals": {},
            "jobs": [{"job_id": "job_task_r", "state": "running", "updated_at": stale}],
        },
    )
    s.kill_task = MagicMock(return_value={"killed_running": True})
    entry = asyncio.run(s.run_admin_sweep())
    assert [i["code"] for i in entry["new"]] == ["stuck_job"]
    assert entry["new"][0]["result"]["applied"] is True
    assert entry["text"] == "A job stalled, so I stopped it."
    assert s.router.call.await_count == 1

    # Same issue on the next sweep: inside the cooldown, so no fix and no model call.
    again = asyncio.run(s.run_admin_sweep())
    assert again["new"] == [] and again["issues"] == 1
    assert s.kill_task.call_count == 1
    assert s.router.call.await_count == 1


def test_fixes_off_reports_without_acting(monkeypatch, tmp_path):
    import time as _t

    monkeypatch.setitem(settings._data, "admin_sweep_fixes", 0)
    s = _session()
    _wire_sweep(
        s,
        monkeypatch,
        tmp_path,
        {
            "signals": {},
            "jobs": [{"job_id": "j", "state": "running", "updated_at": _t.time() - 3600}],
        },
    )
    s.kill_task = MagicMock()
    s.router.call = AsyncMock(side_effect=RuntimeError("no model"))
    entry = asyncio.run(s.run_admin_sweep())
    s.kill_task.assert_not_called()
    assert entry["new"][0]["result"] is None
    assert "running with no progress" in entry["text"], "falls back to the plain report"


def test_sweep_settings_are_owner_only_and_monitor_by_default():
    from brain.org_permissions import ADMIN_ONLY_KEYS
    from brain.settings import DEFAULTS

    for k in (
        "dmn_freeform_self_tasks",
        "admin_sweep_interval_s",
        "admin_sweep_fixes",
        "admin_sweep_cooldown_s",
    ):
        assert k in ADMIN_ONLY_KEYS
        assert k in DEFAULTS
    # Since 2026-10-10 the sweep runs by default; idle ideas may still become jobs
    # (the agent's mandate steers which, see test_dmn_role_steers_tasks).
    assert DEFAULTS["admin_sweep_interval_s"] == 900.0
    assert DEFAULTS["dmn_freeform_self_tasks"] == 1


def test_existing_org_takes_the_sweep_default_once(tmp_path, monkeypatch):
    """A tenant settings.json still pinning the old defaults takes the new ones on
    load; once the update id is saved, an owner's deliberate old value sticks."""
    import json

    import brain.settings as bs

    path = tmp_path / "settings.json"
    monkeypatch.setattr(bs, "SETTINGS_PATH", path)
    path.write_text(json.dumps({"admin_sweep_interval_s": 0.0, "dmn_freeform_self_tasks": 1}))
    s = bs.Settings()
    assert s.get("admin_sweep_interval_s") == 900.0
    assert s.get("dmn_freeform_self_tasks") == 1  # untouched
    s.save({"admin_sweep_interval_s": 0.0})  # the owner turns the sweep off on purpose
    again = bs.Settings()
    assert again.get("admin_sweep_interval_s") == 0.0
    assert "2026-10-10-admin-monitor" in again.get("settings_updates_applied")


def test_one_time_update_leaves_a_non_default_choice_alone(tmp_path, monkeypatch):
    import json

    import brain.settings as bs

    path = tmp_path / "settings.json"
    monkeypatch.setattr(bs, "SETTINGS_PATH", path)
    path.write_text(json.dumps({"admin_sweep_interval_s": 300.0}))
    assert bs.Settings().get("admin_sweep_interval_s") == 300.0


# ── The DMN gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_freeform_off_drops_idle_ideas_before_the_queue(monkeypatch):
    from tests.test_dmn_round_robin import _make_dmn, _meta

    dmn = _make_dmn(home="the_admin")
    dmn._self_task_q = collections.deque(maxlen=8)

    monkeypatch.setitem(settings._data, "dmn_freeform_self_tasks", 0)
    await dmn._process_thought(
        "I should run a performance audit against the current system.",
        _meta(task_goal="Run a performance audit against the current system."),
        "t1",
    )
    assert dmn.take_self_task() is None

    monkeypatch.setitem(settings._data, "dmn_freeform_self_tasks", 1)
    await dmn._process_thought(
        "I should check whether the ingest job duplicates rows.",
        _meta(task_goal="Check the ingest job for duplicate rows."),
        "t2",
    )
    assert dmn.take_self_task() is not None


def test_admin_idle_focus_leads_the_idle_prompt():
    """The idle loop is persona-based: what The Admin thinks about and starts on its
    own comes from its self.md, and the Idle focus section must survive the
    snippet budget ahead of everything else."""
    from brain.dmn import DefaultModeNetwork
    from scripts.seed_persona_selfmd import composed_docs

    docs = composed_docs()
    assert "## Idle focus" in docs["the_admin"]
    assert "## Idle focus" not in docs["the_empath"]  # optional, per persona

    class _D:
        _SELF_MODEL_SECTIONS = DefaultModeNetwork._SELF_MODEL_SECTIONS
        _last_self_schema = docs["the_admin"]

    snippet = DefaultModeNetwork.self_model_snippet(_D())
    assert snippet.startswith("## Idle focus")
    assert "Work I don't start" in snippet
