"""MRI interior rail: every section folds to its header, the two growing sections
are marked to fill spare height, and the running-jobs badge is tied to the
Self-directed work section being folded (not to the long-gone right-hand tab)."""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "brain" / "ui" / "index.html").read_text()


def _section(key: str) -> str:
    m = re.search(rf'<section[^>]*data-sec="{key}"[^>]*>(.*?)</section>', HTML, re.S)
    assert m, f"rail section {key!r} missing"
    return m.group(0)


def test_each_section_has_an_accessible_toggle_for_its_own_body():
    for key in ("chem", "thoughts", "tasks", "plasticity"):
        sec = _section(key)
        assert f'aria-controls="sec-body-{key}"' in sec
        assert 'aria-expanded="true"' in sec
        assert f'id="sec-body-{key}"' in sec


def test_only_tasks_and_plasticity_fill_the_rail():
    fills = re.findall(r'<section[^>]*rail-sec--fill[^>]*data-sec="(\w+)"', HTML)
    assert sorted(fills) == ["plasticity", "tasks"]


def test_running_jobs_badge_lives_in_the_tasks_toggle_and_keys_off_the_fold():
    toggle = re.search(r'aria-controls="sec-body-tasks">(.*?)</button>', HTML, re.S).group(1)
    assert 'id="tasks-badge"' in toggle
    body = re.search(r"function _updateTaskBadge\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert "tasks-section" in body and "collapsed" in body
    assert "right-tab" not in body


def test_fold_state_persists_and_the_rail_refits():
    assert "mri_rail_folded" in HTML
    assert "function _fitRail()" in HTML and "rail-flow" in HTML


def test_waiting_approvals_mark_the_folded_tasks_header():
    toggle = re.search(r'aria-controls="sec-body-tasks">(.*?)</button>', HTML, re.S).group(1)
    assert 'id="approvals-badge"' in toggle
    body = re.search(r"function _updateApprovalBadge\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert "APPROVALS" in body and "collapsed" in body
    render = re.search(r"function renderApprovals\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert "_updateApprovalBadge()" in render
