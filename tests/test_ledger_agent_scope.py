"""The projects ledger is AGENT-scoped (persona × mandate), not persona-scoped.

self.md and user.md are correctly persona-scoped — identity and relationship travel
with the temperament. The projects ledger is not: "what work am I pre-authorized to
run autonomously" is a property of the job. One persona wearing two mandates
(the_analyst is both day_trading_analyst and trading_mispricing in prod) shared one
authorization list, and because load_core_context() folds the whole ledger into the
"self" blob that rides in EVERY turn's prompt, the day-trading projects were also
being read into every mispricing debate round — an agent that is lite, has no DMN,
and could never act on them.

These tests pin the separation and, just as importantly, that it fails CLOSED: an
unresolvable mandate falls back to the shared base ledger, never to another
mandate's file.
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


# ── Filename derivation ─────────────────────────────────────────────────────


def test_no_mandate_keeps_the_base_filename():
    assert ot.ledger_file("") == "open_questions.md"
    assert ot.ledger_file("") == ot.BASE_LEDGER_FILE


def test_mandate_gets_its_own_file():
    assert ot.ledger_file(ANALYST_FULL) == "open_questions__day_trading_analyst.md"
    assert ot.ledger_file(ANALYST_LITE) != ot.ledger_file(ANALYST_FULL)


def test_derived_filename_survives_the_stores_filename_guard():
    """A name the guard rejects is silently unwritable — the ledger would look empty
    forever. Mandate ids allow dashes; the guard allows no dots."""
    s = SchemaStore.__new__(SchemaStore)
    s._use_supabase = False
    s._persona = ""
    for mandate in (ANALYST_FULL, "forecast-contrarian", "app_admin"):
        assert s._FILENAME_RE.match(ot.ledger_file(mandate)), mandate


def test_a_malformed_mandate_cannot_produce_a_rejected_filename():
    """Dots/slashes are folded out rather than passed through to the guard."""
    s = SchemaStore.__new__(SchemaStore)
    s._use_supabase = False
    s._persona = ""
    assert s._FILENAME_RE.match(ot.ledger_file("bad.name/../x"))


# ── Which mandate is active ─────────────────────────────────────────────────


def test_a_bound_turn_supplies_the_mandate():
    """Engine/API turns carry agent_id ("persona.mandate") on turn_ctx."""
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        assert ot.active_mandate() == ANALYST_FULL
        assert ot.active_ledger_file() == "open_questions__day_trading_analyst.md"


def test_the_unbound_dmn_lane_falls_back_to_the_personas_full_agent(monkeypatch):
    """The DMN idle loop binds no turn, so it resolves through agents.owning_mandate.
    Without this the DMN would write its projects to the shared base ledger while the
    turn lane read the mandate-scoped one."""
    monkeypatch.setattr("brain.agents.owning_mandate", lambda p: ANALYST_FULL)
    with bind_persona("the_analyst"):
        assert ot.active_mandate() == ANALYST_FULL


def test_resolution_fails_closed_to_the_base_ledger(monkeypatch):
    """A store error or an agent_id with no mandate half must never resolve to some
    other mandate's file — the shared base ledger is the only safe fallback."""

    def boom(_p):
        raise RuntimeError("supabase down")

    monkeypatch.setattr("brain.agents.owning_mandate", boom)
    with bind_persona("the_analyst"):
        assert ot.active_ledger_file() == ot.BASE_LEDGER_FILE
    with bind_turn("agent", agent_id="no_mandate_half"):
        assert ot.active_ledger_file() == ot.BASE_LEDGER_FILE


# ── The regression: two mandates, one persona ───────────────────────────────


def test_two_mandates_on_one_persona_do_not_share_a_ledger(store):
    store.write(
        ot.ledger_file(ANALYST_FULL), "# Full\n\n## Projects assigned by Russ\n\n### Journal\n"
    )
    store.write(ot.ledger_file(ANALYST_LITE), "# Lite\n\n## Projects assigned by Russ\n")

    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        assert "Journal" in store.read(ot.active_ledger_file())
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_LITE}"):
        assert "Journal" not in store.read(ot.active_ledger_file())


def test_the_lite_mandate_does_not_get_the_full_mandates_projects_in_core_context(store):
    """The actual leak: core context folds the ledger onto self.md and that blob is
    in every turn's prompt."""
    store.write("self.md", "# Self\n")
    store.write(
        ot.ledger_file(ANALYST_FULL),
        "# Full\n\n## Projects assigned by Russ\n\n### Decision-journal review\n",
    )
    store.ensure_open_questions_schema()  # base-name skeleton for the lite side

    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        assert "Decision-journal review" in store.load_core_context()["self"]
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_LITE}"):
        assert "Decision-journal review" not in store.load_core_context()["self"]


def test_core_context_cache_is_keyed_by_mandate_not_just_persona(store, monkeypatch):
    """_active_core_context() cached on persona alone, so whichever mandate booted
    first would serve its ledger to the other for the life of the process."""
    from brain.clusters.hippocampus import HippocampusCluster

    h = HippocampusCluster.__new__(HippocampusCluster)
    h._schema = store
    h._core_context = {}

    store.write("self.md", "# Self\n")
    store.write(ot.ledger_file(ANALYST_FULL), "## Projects assigned by Russ\n\n### OnlyFull\n")
    store.write(ot.ledger_file(ANALYST_LITE), "## Projects assigned by Russ\n")

    with bind_persona("the_analyst"):
        with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
            first = h._active_core_context()["self"]
        with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_LITE}"):
            second = h._active_core_context()["self"]

    assert "OnlyFull" in first
    assert "OnlyFull" not in second


# ── Bootstrap under agent scope ─────────────────────────────────────────────


def test_ensure_creates_the_mandate_scoped_file(store):
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        store.ensure_open_questions_schema()
        assert store.read("open_questions__day_trading_analyst.md")
    assert store.read(ot.BASE_LEDGER_FILE) == ""


def test_ensure_carries_forward_a_hand_authored_base_ledger(store):
    """Local dev (and any persona predating agent scoping) has real content in the
    base file. First boot under a mandate must adopt it, not bury it under an empty
    skeleton."""
    store.write(ot.BASE_LEDGER_FILE, "## Projects assigned by Russ\n\n### Hand authored\n")
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        store.ensure_open_questions_schema()
        assert "Hand authored" in store.read(ot.active_ledger_file())


def test_ensure_does_not_clobber_an_existing_mandate_ledger(store):
    with bind_turn("agent", agent_id=f"the_analyst.{ANALYST_FULL}"):
        store.write(ot.active_ledger_file(), "# Mine\n")
        store.ensure_open_questions_schema()
        assert store.read(ot.active_ledger_file()) == "# Mine\n"
