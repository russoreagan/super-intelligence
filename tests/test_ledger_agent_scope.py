"""Scoping split: open threads are PERSONA-scoped; projects are AGENT-scoped.

Open threads are the DMN's unfinished thoughts — learning — and agents share a
persona on purpose so learning pools across jobs. So the open_questions.md ledger is
one file per persona, and cross-job bleed is handled at READ time by the
mandate-domain gate in DMN.route_threads_for_turn. Authorization is different: "what
work am I pre-authorized to run" is a property of the JOB, so projects live in the
agent_projects table keyed (persona, mandate) — see tests/test_agent_projects_store.py.

What survives from the brief file-per-mandate experiment is active_mandate(): the
agent resolver that the projects digest, add_manual_project, and the DMN's self-task
stamping all use — and its fail-CLOSED rule.
"""

from __future__ import annotations

import pytest

from brain import open_threads as ot
from brain.second_brain.store import SchemaStore, bind_persona
from brain.turn_ctx import bind_turn

ANALYST_FULL = "day_trading_analyst"
ANALYST_LITE = "trading_mispricing"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("brain.second_brain.store.SCHEMA_DIR", tmp_path)
    monkeypatch.setattr("brain.second_brain.store._STORAGE_BACKEND", "local")
    return SchemaStore()


# ── The threads ledger is one file per persona ──────────────────────────────


def test_ledger_is_the_base_file_regardless_of_mandate():
    assert ot.active_ledger_file() == ot.BASE_LEDGER_FILE == "open_questions.md"
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        assert ot.active_ledger_file() == ot.BASE_LEDGER_FILE
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_LITE}"):
        assert ot.active_ledger_file() == ot.BASE_LEDGER_FILE


def test_two_mandates_on_one_persona_share_their_threads(store):
    store.write(ot.BASE_LEDGER_FILE, "# Shared\n\n## Open threads\n")
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        a = store.read(ot.active_ledger_file())
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_LITE}"):
        b = store.read(ot.active_ledger_file())
    assert a == b == "# Shared\n\n## Open threads\n"


def test_migration_filename_still_derives_and_passes_the_guard():
    """ledger_file(mandate) survives for the one-time migration of the mandate-suffixed
    files that briefly existed; the store's filename guard must still accept it."""
    s = SchemaStore.__new__(SchemaStore)
    s._use_supabase = False
    s._persona = ""
    assert ot.ledger_file(ANALYST_FULL) == "open_questions__day_trading_analyst.md"
    assert ot.ledger_file("") == ot.BASE_LEDGER_FILE
    for mandate in (ANALYST_FULL, "forecast-contrarian", "bad.name/../x"):
        assert s._FILENAME_RE.match(ot.ledger_file(mandate)), mandate


# ── The agent resolver ──────────────────────────────────────────────────────


def test_a_bound_turn_supplies_the_mandate():
    """Engine/API turns carry agent_id ("persona.mandate") on turn_ctx."""
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        assert ot.active_mandate() == ANALYST_FULL


def test_the_unbound_dmn_lane_falls_back_to_the_personas_full_agent(monkeypatch):
    """The DMN idle loop binds no turn, so it resolves through agents.owning_mandate."""
    monkeypatch.setattr("brain.agents.owning_mandate", lambda p: ANALYST_FULL)
    with bind_persona("the_analyst"):
        assert ot.active_mandate() == ANALYST_FULL


def test_resolution_fails_closed(monkeypatch):
    """A store error or an agent_id with no mandate half resolves to "" — never to
    some other mandate."""

    def boom(_p):
        raise RuntimeError("supabase down")

    monkeypatch.setattr("brain.agents.owning_mandate", boom)
    with bind_persona("the_analyst"):
        assert ot.active_mandate() == ""
    with bind_turn("agent", agent_id="no_mandate_half"):
        assert ot.active_mandate() == ""


# ── Bootstrap ───────────────────────────────────────────────────────────────


def test_ensure_creates_the_base_file_under_a_bound_mandate(store):
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        store.ensure_open_questions_schema()
    assert store.read(ot.BASE_LEDGER_FILE).startswith("# Open Questions & Projects")
    assert store.read(ot.ledger_file(ANALYST_FULL)) == ""


def test_ensure_does_not_clobber_an_existing_ledger(store):
    store.write(ot.BASE_LEDGER_FILE, "# Mine\n")
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        store.ensure_open_questions_schema()
    assert store.read(ot.BASE_LEDGER_FILE) == "# Mine\n"


def test_core_context_carries_the_shared_ledger_for_every_mandate(store):
    store.write("self.md", "# Self\n")
    store.write(ot.BASE_LEDGER_FILE, "## Open threads\n\nSHARED-THREAD\n")
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        assert "SHARED-THREAD" in store.load_core_context()["self"]
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_LITE}"):
        assert "SHARED-THREAD" in store.load_core_context()["self"]
