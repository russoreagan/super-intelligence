"""Markdown is an INPUT path, one way: the assigned-projects section
(store.PROJECTS_HEADING) imports into the agent_projects table. Content refreshes;
lifecycle never does. The single-user-era heading still parses."""

from __future__ import annotations

import pytest

from brain import agent_projects_store as store
from brain.dmn import DefaultModeNetwork

_OQ = """# Open Questions & Projects

## Assigned projects

### Self-code review (PRIMARY — do this first)
- **Task**: Review my own codebase for optimization opportunities.
- **Status**: In progress.

### Evolution App review (secondary)
- **Task**: Review the Evolution App project and surface observations.
- **Status**: Not started.

### Old finished thing
- **Task**: something
- **Status**: Done.

### Stuck one
- **Task**: needs a decision
- **Status**: Blocked — waiting on you: which repo?
"""


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    store.invalidate_cache()
    yield
    store.invalidate_cache()


def _dmn():
    return DefaultModeNetwork.__new__(DefaultModeNetwork)


def test_import_maps_priority_and_status_words():
    n = _dmn().import_markdown_projects(_OQ, persona="the_admin", mandate="app_admin")
    assert n == 4
    rows = {r["title"]: r for r in store.list_for_agent("the_admin", "app_admin")}
    assert (
        rows["Self-code review"]["priority"] == 1
        and rows["Self-code review"]["state"] == store.READY
    )
    assert rows["Evolution App review"]["priority"] == 2
    assert rows["Old finished thing"]["state"] == store.DONE
    assert rows["Stuck one"]["state"] == store.BLOCKED
    assert all(r["source"] == "markdown_import" for r in rows.values())


def test_import_is_idempotent():
    d = _dmn()
    d.import_markdown_projects(_OQ, persona="p", mandate="m")
    d.import_markdown_projects(_OQ, persona="p", mandate="m")
    assert len(store.list_for_agent("p", "m")) == 4


def test_reimport_does_not_resurrect_finished_work():
    d = _dmn()
    d.import_markdown_projects(_OQ, persona="p", mandate="m")
    pid = store.project_id("p", "m", "Evolution App review")
    store.claim(pid, "t1")
    assert store.finish(pid, success=True) == store.DONE
    d.import_markdown_projects(_OQ, persona="p", mandate="m")  # markdown still says Not started
    assert store.get(pid)["state"] == store.DONE


def test_reimport_refreshes_content():
    d = _dmn()
    d.import_markdown_projects(_OQ, persona="p", mandate="m")
    edited = _OQ.replace(
        "Review the Evolution App project", "Review the Evolution App project CAREFULLY"
    )
    d.import_markdown_projects(edited, persona="p", mandate="m")
    assert "CAREFULLY" in store.get(store.project_id("p", "m", "Evolution App review"))["task"]


def test_skeleton_imports_nothing():
    from brain.second_brain.store import SchemaStore

    assert (
        _dmn().import_markdown_projects(
            SchemaStore.OPEN_QUESTIONS_SKELETON, persona="p", mandate="m"
        )
        == 0
    )


def test_seed_docs_import_as_finite_ready_projects():
    """The seed script's ledgers must convert to one-shot, ready rows — a standing
    (max_runs=0) row re-runs forever, which is recurring spend by accident."""
    from scripts.seed_open_questions import SEEDS

    d = _dmn()
    for (_org, persona, mandate), doc in SEEDS.items():
        assert d.import_markdown_projects(doc, persona=persona, mandate=mandate) > 0
        for r in store.list_for_agent(persona, mandate):
            assert r["state"] == store.READY and r["max_runs"] == 1 and r["task"], (
                persona,
                r["title"],
            )


def test_legacy_heading_still_imports_and_is_never_written():
    """Ledgers written before 2026-09-13 say "Projects assigned by Russ" — an
    org-neutral product cannot keep naming one person in every tenant's turn
    context, but those entries must not be orphaned by the rename."""
    from brain.second_brain.store import (
        LEGACY_PROJECTS_HEADINGS,
        PROJECTS_HEADING,
        SchemaStore,
    )

    assert PROJECTS_HEADING == "## Assigned projects"
    legacy = _OQ.replace(PROJECTS_HEADING, LEGACY_PROJECTS_HEADINGS[0])
    assert legacy != _OQ
    n = _dmn().import_markdown_projects(legacy, persona="old", mandate="m")
    assert n == 4
    # A heading that merely starts with the words is not the section.
    assert (
        _dmn()._parse_projects("## Assigned projects and other notes\n### X\n- **Task**: t\n") == []
    )
    assert "Russ" not in SchemaStore.OPEN_QUESTIONS_SKELETON
    assert PROJECTS_HEADING in SchemaStore.OPEN_QUESTIONS_SKELETON
