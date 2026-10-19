"""Isolated orgs: each persona keeps learning from its own sessions, and nothing it
learns reaches another persona.

Until 2026-10 an isolated org switched lessons/practice, self-written skills and
muscle memory OFF, because their stores were org-wide. Now each persona keeps its
own. These tests run two isolated personas on one volume and check that every
path stays inside the persona that learned it. `isolated_persona_learning: 0`
restores the old behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from brain import cross_learning, org_settings
from brain.cross_learning import LearnOutcome
from brain.hypothesis_store import OCCASIONS
from brain.second_brain.store import bind_persona
from brain.session_turn import _TurnMixin
from brain.settings import settings
from brain.sleep import SleepConsolidation

DAY = 86400.0
A, B, HOME = "the_poet_a1", "the_poet_b2", "home_p"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", HOME)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "isolated_persona_learning", 1)
    return tmp_path


def _trace(persona: str, eu: str, text: str = "hi") -> dict:
    return {"user_input": text, "entity_response": "re", "end_user_id": eu, "persona": persona}


def _sleep() -> SleepConsolidation:
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    s._schema = MagicMock()
    s._schema.read.return_value = ""
    return s


@pytest.fixture
def admit(monkeypatch):
    """Admit one lesson per source, named after the persona it was learned under,
    through the REAL store so placement is exercised end to end."""

    async def _learn(ruminator, store, text, source_id, persona=""):
        hyp = store.add(f"Lesson learned by {persona}.", source_id, persona=persona)
        return LearnOutcome(admitted=True, stage="admitted", hypothesis_id=hyp.id)

    async def _ctx(self, eu, traces, approaches):
        return "ctx"

    monkeypatch.setattr(cross_learning, "learn_from_private", _learn)
    monkeypatch.setattr(SleepConsolidation, "_private_context", _ctx)


def _run_pass(persona: str, traces: list[dict]) -> None:
    with bind_persona(persona):
        asyncio.run(_sleep()._cross_learning_pass(traces))


# ── where lessons land ───────────────────────────────────────────────────────


def test_each_persona_learns_into_its_own_private_store(isolated, admit):
    _run_pass(A, [_trace(A, "buyer-a")])
    _run_pass(B, [_trace(B, "buyer-b")])
    a = cross_learning.load_store(cross_learning.private_store_path(A))
    b = cross_learning.load_store(cross_learning.private_store_path(B))
    assert [h.principle for h in a.all()] == [f"Lesson learned by {A}."]
    assert [h.principle for h in b.all()] == [f"Lesson learned by {B}."]
    assert not cross_learning.org_store_path().exists()  # the shared store is never written
    assert a.corroboration == OCCASIONS  # one person by construction


def test_a_private_store_ignores_another_personas_turns(isolated, admit):
    """sleep_group_by_persona: 0 runs one binding over the whole buffer; the private
    store still learns only from its own persona's turns."""
    _run_pass(A, [_trace(B, "buyer-b")])
    assert not cross_learning.private_store_path(A).exists()


def test_private_learning_off_learns_nothing(isolated, admit, monkeypatch):
    monkeypatch.setitem(settings._data, "isolated_persona_learning", 0)
    _run_pass(A, [_trace(A, "buyer-a")])
    assert cross_learning.private_store_paths() == []
    assert cross_learning.scoped_store_path(A) is None


def test_unread_org_learns_nothing(isolated, admit, monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: org_settings.UNKNOWN)
    _run_pass(A, [_trace(A, "buyer-a")])
    assert cross_learning.private_store_paths() == []
    assert not cross_learning.org_store_path().exists()


def test_consolidated_org_still_uses_the_shared_store(isolated, admit, monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    _run_pass(A, [_trace(A, "buyer-a")])
    assert cross_learning.org_store_path().exists()
    assert cross_learning.private_store_paths() == []


# ── what reaches a turn ──────────────────────────────────────────────────────


class _Brain(_TurnMixin):
    pass


def _established_in(path, persona: str, days: int = 3) -> None:
    from brain.hypothesis_store import HypothesisStore

    clock = {"t": 1_760_000_000.0}
    st = HypothesisStore(now_fn=lambda: clock["t"], corroboration=OCCASIONS)
    for _ in range(days):
        st.add(f"Established for {persona}.", "eu:buyer", persona=persona)
        clock["t"] += DAY
    cross_learning.save_store(st, path)


def test_turn_injects_only_the_bound_personas_private_lessons(isolated, monkeypatch):
    from brain import persona_key

    _established_in(cross_learning.private_store_path(A), A)
    _established_in(cross_learning.org_store_path(), A)  # inert in an isolated org
    who = {"p": A}
    monkeypatch.setattr(persona_key, "active_or_home_persona", lambda: who["p"])
    b = _Brain()
    assert b._established_principles_for_turn() == [f"Established for {A}."]
    who["p"] = B
    assert b._established_principles_for_turn() == []  # B has learned nothing


def test_turn_injects_nothing_when_private_learning_is_off(isolated, monkeypatch):
    from brain import persona_key

    _established_in(cross_learning.private_store_path(A), A)
    monkeypatch.setitem(settings._data, "isolated_persona_learning", 0)
    monkeypatch.setattr(persona_key, "active_or_home_persona", lambda: A)
    assert _Brain()._established_principles_for_turn() == []


def test_practice_is_enabled_per_persona_in_isolated_orgs(isolated, monkeypatch):
    from brain import practice_approaches as pa

    assert pa.enabled() is True
    monkeypatch.setitem(settings._data, "isolated_persona_learning", 0)
    assert pa.enabled() is False
    monkeypatch.setitem(settings._data, "isolated_persona_learning", 1)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: org_settings.UNKNOWN)
    assert pa.enabled() is False  # fail closed on an unread org


def test_idle_practice_seed_reads_only_its_own_store(isolated, monkeypatch):
    from brain import dmn as dmn_mod

    _established_in(cross_learning.private_store_path(B), B)
    dmn = dmn_mod.DefaultModeNetwork.__new__(dmn_mod.DefaultModeNetwork)
    dmn._pstate = {}
    dmn._home = HOME
    dmn._thought_count = 5
    dmn._tick_idle_s = 999.0
    dmn._tick_idle_phase = dmn._idle_phase(999.0)
    monkeypatch.setitem(settings._data, "dmn_practice_seed_every", 5)
    monkeypatch.setattr(dmn, "_engine_lane", lambda: False)
    monkeypatch.setattr(dmn, "_active_persona_name", lambda: A)
    dmn._memory_seed = ""
    dmn._practice_seed = ""
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""  # A never sees B's lesson
    monkeypatch.setattr(dmn, "_active_persona_name", lambda: B)
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed.startswith(f"Established for {B}.")


# ── cascades ─────────────────────────────────────────────────────────────────


def test_end_user_erasure_reaches_every_private_store(isolated):
    _established_in(cross_learning.private_store_path(A), A)
    _established_in(cross_learning.private_store_path(B), B)
    retired = _TurnMixin._purge_cross_learning("buyer")
    assert retired == 2
    for p in (A, B):
        assert cross_learning.load_store(cross_learning.private_store_path(p)).all() == []


def test_private_store_lives_in_the_persona_state_root(isolated):
    from brain.persona_key import persona_state_root

    assert cross_learning.private_store_path(A).parent == persona_state_root(A)
    assert cross_learning.private_store_path(A) != cross_learning.private_store_path(B)
    assert cross_learning.private_store_path(HOME).parent == persona_state_root(HOME)


def test_clone_never_carries_private_lessons():
    from brain import personas

    assert cross_learning.PRIVATE_STORE_NAME not in personas._CURRENT_SEED_FILES


def test_setting_is_on_by_default_and_admin_only():
    from brain.org_permissions import ADMIN_ONLY_KEYS
    from brain.settings import DEFAULTS

    assert DEFAULTS["isolated_persona_learning"] == 1
    assert "isolated_persona_learning" in ADMIN_ONLY_KEYS
