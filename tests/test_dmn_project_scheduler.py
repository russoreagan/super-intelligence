"""
Project scheduler — projects run as a track PARALLEL to rumination:
  - next_project_goal starts one project at a time, PRIMARY first, round-robin
  - eligibility skips done / user-blocked projects
  - completion updates the project's Status in open_questions.md and frees the slot
  - failure / blocked paths also free the slot (no permanent stall)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.dmn import DefaultModeNetwork
from brain.sequence_predictor import SequencePredictor

_OQ = """# Open Questions & Projects

## Projects assigned by Russ

### Self-code review (PRIMARY — do this first)
- **Task**: Review my own codebase for optimization opportunities.
- **Status**: In progress.

### Academic research scan (PRIMARY)
- **Task**: Search recent neuroscience/AI papers for design ideas.
- **Status**: Not started.

### Evolution App review (secondary)
- **Task**: Review the Evolution App project and surface observations.
- **Status**: Not started.

### Old finished thing
- **Task**: something
- **Status**: Done.
"""


def _make_dmn(oq_text=_OQ):
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._seq_predictor = SequencePredictor()
    schema = MagicMock()
    schema.read = MagicMock(return_value=oq_text)
    schema.awrite = AsyncMock()
    hip = MagicMock()
    hip._schema = schema
    dmn._hippocampus = hip
    dmn._projects = []
    dmn._project_in_flight = None
    dmn._project_task_id = None
    dmn._project_rotation_idx = 0
    dmn._last_projects = ""
    dmn.set_projects_context(oq_text)
    return dmn


def test_parses_projects_with_priority_and_status():
    dmn = _make_dmn()
    names = {p["name"]: p for p in dmn._projects}
    assert "Self-code review" in names
    assert names["Self-code review"]["priority"] == "PRIMARY"
    assert "Done" in names["Old finished thing"]["status"]


def test_eligibility_skips_done():
    dmn = _make_dmn()
    done = next(p for p in dmn._projects if p["name"] == "Old finished thing")
    assert not dmn._project_eligible(done)


def test_next_goal_prefers_primary_and_round_robins():
    dmn = _make_dmn()
    n1, _ = dmn.next_project_goal()
    dmn._project_in_flight = None  # simulate completion between picks
    n2, _ = dmn.next_project_goal()
    # Both PRIMARY, rotating between the two primaries (not the secondary/done).
    assert {n1, n2} == {"Self-code review", "Academic research scan"}


def test_one_project_at_a_time():
    dmn = _make_dmn()
    name, goal = dmn.next_project_goal()
    dmn.note_project_started(name, "task-123")
    # While one is in flight, no new project is started.
    assert dmn.next_project_goal() is None


@pytest.mark.asyncio
async def test_completion_updates_status_and_frees_slot():
    dmn = _make_dmn()
    name, goal = dmn.next_project_goal()
    dmn.note_project_started(name, "task-123")
    await dmn.note_project_complete("task-123", success=True, summary="read run.py")
    assert dmn._project_in_flight is None
    # Status line rewritten in the file.
    dmn._hippocampus._schema.awrite.assert_awaited()
    written = dmn._hippocampus._schema.awrite.await_args.args[1]
    assert "last worked" in written
    # Slot freed → next project can start.
    assert dmn.next_project_goal() is not None


@pytest.mark.asyncio
async def test_blocked_frees_slot_and_marks_status():
    dmn = _make_dmn()
    name, goal = dmn.next_project_goal()
    dmn.note_project_started(name, "task-123")
    await dmn.note_project_blocked("task-123", "which directory should I start in?")
    assert dmn._project_in_flight is None
    written = dmn._hippocampus._schema.awrite.await_args.args[1]
    assert "Blocked" in written


@pytest.mark.asyncio
async def test_is_project_task_guards_unrelated_tasks():
    dmn = _make_dmn()
    dmn.note_project_started("Self-code review", "task-123")
    assert dmn.is_project_task("task-123")
    assert not dmn.is_project_task("some-other-task")
    # A non-project task completion is a no-op.
    await dmn.note_project_complete("some-other-task", success=True)
    assert dmn._project_in_flight == "Self-code review"  # unchanged
