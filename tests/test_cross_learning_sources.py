"""Cross-learning counts PEOPLE, and idle thinking puts what it learned to work.

2026-10-11 (Haven, a therapy listener on a consolidated partner org): every
consolidation keyed its cross-learning source on the org's user (BRAIN_USER_ID),
so all of a partner's customers counted as ONE source and no principle could ever
reach the distinct-source threshold — learned lessons were never established and
never reached a turn. Sleep now reflects once per source (each end user, or the
owner) and keys corroboration on that person.

The idle mind (DMN) never samples customer episodes on the owner lane, so the
practice seed is how off-time thinking works with what was learned across
customers: it surfaces one de-identified principle from the hypothesis store.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from brain import cross_learning
from brain.cross_learning import LearnOutcome
from brain.hypothesis_store import HypothesisStore
from brain.settings import settings
from brain.sleep import SleepConsolidation
from brain.turn_ctx import bind_turn

# ── sleep: one reflection per source ─────────────────────────────────────────


def _trace(eu: str, text: str) -> dict:
    return {"user_input": text, "entity_response": f"re {text}", "end_user_id": eu}


@pytest.fixture
def capture(monkeypatch):
    """Record each learn_from_private call; admit with the real store so promotion
    is exercised end-to-end."""
    calls: list[tuple[str, str]] = []
    store = HypothesisStore(salt="t")
    saved: list[int] = []

    async def _learn(ruminator, st, text, source_id, persona=""):
        calls.append((source_id, text))
        hyp = st.add("Naming a feeling often loosens its grip.", source_id, persona=persona)
        return LearnOutcome(admitted=True, stage="admitted", hypothesis_id=hyp.id)

    monkeypatch.setattr(cross_learning, "learn_from_private", _learn)
    monkeypatch.setattr(cross_learning, "load_store", lambda path=None: store)
    monkeypatch.setattr(
        cross_learning, "save_store", lambda st, path=None: saved.append(len(st.all()))
    )
    monkeypatch.setenv("BRAIN_USER_ID", "org-owner")
    return calls, store, saved


def _sleep() -> SleepConsolidation:
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    return s


def test_each_customer_is_its_own_source_and_never_shares_a_prompt(capture):
    calls, _, saved = capture
    traces = [
        _trace("alice@therapy.a1", "ALICE-ONLY"),
        _trace("bob@therapy.b1", "BOB-ONLY"),
        _trace("alice@therapy.a1", "ALICE-AGAIN"),
    ]
    asyncio.run(_sleep()._cross_learning_pass(traces))

    assert [c[0] for c in calls] == ["eu:alice@therapy.a1", "eu:bob@therapy.b1"]
    alice_text, bob_text = calls[0][1], calls[1][1]
    assert "ALICE-ONLY" in alice_text and "ALICE-AGAIN" in alice_text
    assert "BOB" not in alice_text
    assert "BOB-ONLY" in bob_text and "ALICE" not in bob_text
    assert saved == [1]  # one save for the whole pass


def test_owner_turns_keep_the_brain_user_source(capture):
    calls, _, _ = capture
    asyncio.run(_sleep()._cross_learning_pass([_trace("", "companion talk")]))
    assert [c[0] for c in calls] == ["org-owner"]


def test_three_customers_establish_a_principle(capture):
    _, store, _ = capture
    for who in ("a", "b", "c"):
        asyncio.run(_sleep()._cross_learning_pass([_trace(f"{who}@therapy.x", "t")]))
    assert len(store.established()) == 1


def test_one_customer_many_sessions_stays_provisional(capture):
    _, store, _ = capture
    for _ in range(4):
        asyncio.run(_sleep()._cross_learning_pass([_trace("a@therapy.x", "t")]))
    assert store.established() == [] and len(store.provisional()) == 1


def test_source_cap_keeps_the_most_recent(capture, monkeypatch):
    calls, _, _ = capture
    monkeypatch.setitem(settings._data, "cross_learning_max_sources", 2)
    traces = [_trace(f"u{i}", f"t{i}") for i in range(5)]
    asyncio.run(_sleep()._cross_learning_pass(traces))
    assert [c[0] for c in calls] == ["eu:u3", "eu:u4"]


def test_nothing_admitted_saves_nothing(monkeypatch):
    saved: list[int] = []

    async def _refuse(ruminator, st, text, source_id):
        return LearnOutcome(admitted=False, stage="reid")

    monkeypatch.setattr(cross_learning, "learn_from_private", _refuse)
    monkeypatch.setattr(cross_learning, "load_store", lambda path=None: HypothesisStore())
    monkeypatch.setattr(cross_learning, "save_store", lambda st, path=None: saved.append(1))
    asyncio.run(_sleep()._cross_learning_pass([_trace("a", "t")]))
    assert saved == []


# ── DMN: the practice seed ───────────────────────────────────────────────────


def _dmn():
    from brain import dmn as dmn_mod

    dmn = dmn_mod.DefaultModeNetwork.__new__(dmn_mod.DefaultModeNetwork)
    dmn._pstate = {}
    dmn._home = "home_p"
    dmn._thought_count = 5
    dmn._tick_idle_s = 999.0
    dmn._tick_idle_phase = dmn._idle_phase(999.0)
    return dmn


@pytest.fixture
def practice(monkeypatch):
    from brain import org_settings

    store = HypothesisStore(salt="t")
    monkeypatch.setattr(cross_learning, "load_store", lambda path=None: store)
    monkeypatch.setattr(org_settings, "is_isolated", lambda: False)
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "dmn_practice_seed_every", 5)
    return store


def test_prefers_established_and_labels_it(practice):
    practice.add("Tentative idea.", "a", persona="home_p")
    for src in ("a", "b", "c"):
        practice.add("Thought records help when worry loops.", src, persona="home_p")
    dmn = _dmn()
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed.startswith("Thought records help when worry loops.")
    assert "several people" in dmn._practice_seed


def test_falls_back_to_provisional_marked_tentative(practice):
    practice.add("Grounding before problem-solving.", "a", persona="home_p")
    dmn = _dmn()
    dmn._maybe_inject_practice_seed()
    assert "Grounding before problem-solving." in dmn._practice_seed
    assert "tentative" in dmn._practice_seed


def test_rotates_instead_of_repeating(practice):
    for p in ("One.", "Two.", "Three."):
        practice.add(p, "a", persona="home_p")
    dmn = _dmn()
    seen = []
    for _ in range(3):
        dmn._maybe_inject_practice_seed()
        seen.append(dmn._practice_seed.split(" (")[0])
        dmn._practice_seed = ""
    assert sorted(seen) == ["One.", "Three.", "Two."]


def test_empty_store_seeds_nothing(practice):
    dmn = _dmn()
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""


def test_not_in_an_isolated_org(practice, monkeypatch):
    from brain import org_settings

    practice.add("Anything.", "a", persona="home_p")
    monkeypatch.setattr(org_settings, "is_isolated", lambda: True)
    dmn = _dmn()
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""


def test_not_on_an_engine_turn(practice):
    practice.add("Anything.", "a", persona="home_p")
    dmn = _dmn()
    with bind_turn("agent", session_id="s", agent_id="p.m", end_user_id="alice"):
        dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""


def test_not_while_engaged_or_off_cadence(practice, monkeypatch):
    from brain.dmn import IdlePhase

    practice.add("Anything.", "a", persona="home_p")
    dmn = _dmn()
    dmn._tick_idle_phase = IdlePhase.ENGAGED
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""
    dmn = _dmn()
    dmn._thought_count = 4
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""
    monkeypatch.setitem(settings._data, "dmn_practice_seed_every", 0)
    dmn = _dmn()
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""


def test_yields_to_a_pending_memory_seed(practice):
    practice.add("Anything.", "a", persona="home_p")
    dmn = _dmn()
    dmn._memory_seed = "From an earlier conversation: ..."
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""


def test_only_this_personas_lessons(practice):
    practice.add("A trading lesson.", "x", persona="the_analyst")
    dmn = _dmn()
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed == ""
    practice.add("A listening lesson.", "y", persona="home_p")
    dmn._maybe_inject_practice_seed()
    assert dmn._practice_seed.startswith("A listening lesson.")


def test_sleep_attributes_lessons_to_the_bound_persona(capture):
    from brain.second_brain.store import bind_persona

    _, store, _ = capture
    with bind_persona("the_empath"):
        asyncio.run(_sleep()._cross_learning_pass([_trace("a@x", "t")]))
    assert store.all()[0].personas == {"the_empath"}
