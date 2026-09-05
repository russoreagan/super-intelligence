"""
DMN project scheduler glue — projects run as a track PARALLEL to rumination:
  - next_project() selects via the pure ranker and CLAIMS the row (compare-and-set)
  - one step in flight at a time (capacity), per process not per persona
  - completion / block / failure all free the slot and advance the row's lifecycle
  - a deduplicated enqueue releases the claim so no turn is burned
  - the prompt digest is rebuilt from the table
"""

from __future__ import annotations

import pytest

from brain import agent_projects_store as store
from brain.dmn import DefaultModeNetwork


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    store.invalidate_cache()
    yield
    store.invalidate_cache()


def _make_dmn():
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._ensure_runtime_state()
    return dmn


def _seed(dmn, *titles, **kw):
    persona = dmn._project_personas()[0]
    return [store.add(persona, "", t, f"work on {t}", **kw) for t in titles]


def test_next_project_selects_and_claims():
    dmn = _make_dmn()
    (pid,) = _seed(dmn, "Self-code review", priority=1)
    row = dmn.next_project()
    assert row is not None and row["id"] == pid
    assert store.get(pid)["state"] == store.RUNNING  # claimed
    assert row["agent_id"] == ""  # no mandate locally → no agent bind


def test_one_project_at_a_time():
    dmn = _make_dmn()
    _seed(dmn, "A", "B")
    row = dmn.next_project()
    dmn.note_project_started(row["id"], "task-1", "", row["task"])
    assert dmn.next_project() is None
    assert dmn.is_project_task("task-1") and not dmn.is_project_task("other")


@pytest.mark.asyncio
async def test_completion_advances_lifecycle_and_frees_slot():
    dmn = _make_dmn()
    (pid,) = _seed(dmn, "A")
    row = dmn.next_project()
    dmn.note_project_started(pid, "task-1", "", row["task"])
    await dmn.note_project_complete("task-1", success=True, summary="read run.py")
    assert not dmn._project_in_flight
    r = store.get(pid)
    assert r["state"] == store.DONE and r["runs"] == 1 and r["last_job_id"] == "job_task_task-1"
    assert "read run.py" in r["status_note"]
    assert dmn.next_project() is None  # nothing left


@pytest.mark.asyncio
async def test_failure_backs_off_and_frees_slot():
    dmn = _make_dmn()
    (pid,) = _seed(dmn, "A")
    row = dmn.next_project()
    dmn.note_project_started(pid, "task-1", "", row["task"])
    await dmn.note_project_complete("task-1", success=False, summary="boom")
    assert store.get(pid)["state"] == store.PENDING
    assert dmn.next_project() is None  # backing off, not eligible yet


@pytest.mark.asyncio
async def test_blocked_frees_slot_and_marks_row():
    dmn = _make_dmn()
    (pid,) = _seed(dmn, "A")
    row = dmn.next_project()
    dmn.note_project_started(pid, "task-1", "", row["task"])
    await dmn.note_project_blocked("task-1", "which directory should I start in?")
    assert not dmn._project_in_flight
    r = store.get(pid)
    assert r["state"] == store.BLOCKED and "directory" in r["blocked_reason"]
    assert dmn.next_project() is None


@pytest.mark.asyncio
async def test_unrelated_task_completion_is_a_no_op():
    dmn = _make_dmn()
    (pid,) = _seed(dmn, "A")
    row = dmn.next_project()
    dmn.note_project_started(pid, "task-1", "", row["task"])
    await dmn.note_project_complete("some-other-task", success=True)
    assert dmn.is_project_task("task-1")
    assert store.get(pid)["state"] == store.RUNNING


def test_dedup_release_undoes_the_claim():
    dmn = _make_dmn()
    (pid,) = _seed(dmn, "A")
    row = dmn.next_project()
    dmn.release_project(row["id"])
    assert store.get(pid)["state"] == store.READY
    assert dmn.next_project()["id"] == pid  # selectable again


def test_kill_switch(monkeypatch):
    from brain import dmn as dmn_module

    dmn = _make_dmn()
    _seed(dmn, "A")
    monkeypatch.setattr(
        dmn_module.settings, "get", lambda k, d=None: 0 if k == "project_scheduler_enabled" else d
    )
    assert dmn.next_project() is None


def test_digest_is_built_from_the_table_priority_first():
    dmn = _make_dmn()
    _seed(dmn, "Later", priority=3)
    _seed(dmn, "First", priority=0)
    dmn.set_projects_context("")
    lines = dmn._last_projects.splitlines()
    assert lines[0].startswith("- **First** (P0)")
    assert "Later" in lines[1]


def test_markdown_in_the_ledger_is_imported_on_refresh():
    dmn = _make_dmn()
    dmn.set_projects_context(
        "## Projects assigned by Russ\n\n### Hand authored\n- **Task**: do it\n- **Status**: Not started\n"
    )
    persona = dmn._project_personas()[0]
    assert any(r["title"] == "Hand authored" for r in store.list_for_personas([persona]))
    assert "Hand authored" in dmn._last_projects
