"""The projects ledger must exist.

Hosted tenants never had an open_questions.md row: nothing seeded it, and
add_manual_project() bailed when it was absent. So `_parse_projects` always saw ""
→ `_last_projects` was permanently empty → the PRE-AUTHORIZED PROJECTS block was
never injected and the project scheduler never fired, while the monologue prompt
still told the model "you will receive a list of active projects with their paths".
That gap is what let the prompt's own worked EXAMPLES stand in as the only concrete
projects the model could see.

These tests pin the two halves of the fix: the file gets created at boot, and the
skeleton it gets is parseable-but-empty (structure without authorization).
"""

from __future__ import annotations

import re

import pytest

from brain.dmn import DefaultModeNetwork
from brain.second_brain.store import SchemaStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.second_brain.store.SCHEMA_DIR", tmp_path)
    monkeypatch.setattr("brain.second_brain.store._STORAGE_BACKEND", "local")
    return SchemaStore()


# ── Creation ────────────────────────────────────────────────────────────────


def test_ensure_creates_the_ledger_when_absent(store):
    assert store.read("open_questions.md") == ""
    store.ensure_open_questions_schema()
    assert store.read("open_questions.md").startswith("# Open Questions & Projects")


def test_ensure_never_clobbers_an_existing_ledger(store):
    """The DMN writes its own progress here (status rewrites, `## Open threads`),
    so a second boot must not reset the file."""
    store.write("open_questions.md", "# Mine\n\n## Projects assigned by Russ\n\n### Real\n")
    store.ensure_open_questions_schema()
    assert store.read("open_questions.md") == (
        "# Mine\n\n## Projects assigned by Russ\n\n### Real\n"
    )


# ── The skeleton is structure WITHOUT authorization ──────────────────────────


def test_skeleton_carries_the_header_the_parser_contracts_on():
    """`## Projects assigned by Russ` is matched verbatim by _parse_projects and by
    add_manual_project. Rewording the skeleton's header orphans every entry."""
    assert "## Projects assigned by Russ" in SchemaStore.OPEN_QUESTIONS_SKELETON


def test_skeleton_pre_authorizes_nothing():
    """A project entry IS the authorization to auto-run work. A fresh tenant must
    start with none — a seeded placeholder would hand out authorization silently."""
    assert DefaultModeNetwork._parse_projects(SchemaStore.OPEN_QUESTIONS_SKELETON) == []


def test_skeleton_comment_cannot_be_mistaken_for_a_project():
    """The format hint inside the section is an HTML comment, and its `### <name>`
    line must not parse as a real project block."""
    parsed = DefaultModeNetwork._parse_projects(SchemaStore.OPEN_QUESTIONS_SKELETON)
    assert not any("<name>" in p["name"] for p in parsed)


def test_skeleton_stays_small():
    """load_core_context() concatenates the WHOLE file onto self.md and that blob
    rides in every turn's context — bulk here is paid per turn, forever."""
    assert len(SchemaStore.OPEN_QUESTIONS_SKELETON) < 1200


# ── The write path that was unreachable ─────────────────────────────────────


@pytest.mark.asyncio
async def test_add_manual_project_works_with_no_existing_ledger(store, monkeypatch):
    """ "work on X" in chat must assign a project even on a tenant that has never
    had the file — the old `if not text: return False` made this a silent no-op."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._ensure_runtime_state = lambda: None
    dmn._projects = []
    dmn._last_projects = ""
    dmn._schema_store = lambda: store

    ok = await dmn.add_manual_project("Engine API review", "Read api/ and summarise the routes.")

    assert ok is True
    names = [p["name"] for p in DefaultModeNetwork._parse_projects(store.read("open_questions.md"))]
    assert "Engine API review" in names


@pytest.mark.asyncio
async def test_added_project_is_eligible_and_reaches_the_prompt(store):
    """End to end: assign → parsed → eligible → rendered into the digest that the
    PRE-AUTHORIZED PROJECTS block is built from."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._ensure_runtime_state = lambda: None
    dmn._projects = []
    dmn._last_projects = ""
    dmn._project_in_flight = None
    dmn._project_rotation_idx = 0
    dmn._schema_store = lambda: store

    await dmn.add_manual_project("Engine API review", "Read api/ and summarise the routes.")

    assert "Engine API review" in dmn._last_projects
    goal = dmn.next_project_goal()
    assert goal is not None and goal[0] == "Engine API review"


# ── The seeded ledgers ──────────────────────────────────────────────────────


def test_seeded_ledgers_parse_and_are_finite():
    """Every seeded project must parse, and none may be open-ended: _project_eligible
    excludes only done/blocked, so a standing status re-runs forever — recurring
    cloud spend that has to be a deliberate choice, not a seeding accident."""
    from scripts.seed_open_questions import SEEDS

    assert SEEDS, "no seeds defined"
    for (org_id, persona, _mandate), doc in SEEDS.items():
        projects = DefaultModeNetwork._parse_projects(doc)
        assert projects, f"{persona} @ {org_id} seeded no parseable projects"
        for p in projects:
            assert p["task"], f"{persona}: {p['name']} has no **Task**"
            assert re.match(r"not started", p["status"], re.I), (
                f"{persona}: {p['name']} has a non-finite status {p['status']!r} — "
                "it would be re-picked by the scheduler indefinitely"
            )


def test_seeded_ledgers_stay_small():
    """Same per-turn context cost as the skeleton."""
    from scripts.seed_open_questions import SEEDS

    for (_org, persona, _mandate), doc in SEEDS.items():
        assert len(doc) < 2500, f"{persona} ledger is {len(doc)} chars"
