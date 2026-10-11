"""Consolidation judges what worked.

Three pieces, all at sleep:

  • the private reflection sees the same person's earlier sessions and the persona's
    current approaches, and is asked for outcome lessons ("X tends to help when …");
  • a newly admitted lesson that restates an existing one corroborates it instead of
    landing as a separate paraphrase (the store keys on wording, so two people's
    versions of one lesson never met and nothing was ever established);
  • when the evidence moved, review() settles, narrows or drops each line of the
    persona's "How I approach sessions" against it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain import cross_learning
from brain import practice_approaches as pa
from brain.cross_learning import LearnOutcome, match_existing
from brain.hypothesis_store import HypothesisStore
from brain.private_rumination import _REFLECT_SYS
from brain.second_brain.store import SchemaStore
from brain.settings import settings
from brain.sleep import SleepConsolidation

SELF_MD = "# Self\n\n## History summary\nTalks with many people.\n"


class _Schema:
    def __init__(self, entries=None):
        self.files = {"self.md": SELF_MD}
        if entries is not None:
            self.files["self.md"] = SchemaStore._replace_section_body(
                SELF_MD, pa.SECTION, pa.render(entries)
            )

    def read(self, name):
        return self.files.get(name, "")

    async def upsert_section(self, name, section, body):
        self.files[name] = SchemaStore._replace_section_body(self.files[name], section, body)


@pytest.fixture
def on(monkeypatch):
    from brain import deid_gate, org_settings

    monkeypatch.setattr(org_settings, "is_isolated", lambda: False)
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "dmn_practice_seed_every", 5)
    monkeypatch.setitem(settings._data, "cross_learning_match", 1)
    scrub = AsyncMock(side_effect=lambda text, src="": text)
    monkeypatch.setattr(deid_gate.DeidGate, "scrub_passage", lambda self, t, s="": scrub(t, s))
    return scrub


# ── the reflection is asked about outcomes ───────────────────────────────────


def test_reflection_asks_what_worked():
    assert "WORKED" in _REFLECT_SYS and "earlier sessions" in _REFLECT_SYS
    assert "tends to help when" in _REFLECT_SYS


def _sleep(schema=None, episodic=None):
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    if schema is not None:
        s._schema = schema
    if episodic is not None:
        s._episodic = episodic
    return s


def test_private_context_has_this_persons_history_and_the_practice(on):
    episodic = MagicMock()
    episodic.recall_recent = MagicMock(
        return_value=[  # newest first, as recall_recent returns them
            {"user_input": "today", "entity_response": "r", "ts": 3.0},
            {"user_input": "last week it helped", "entity_response": "good", "ts": 2.0},
            {"user_input": "", "entity_response": "[CONCLUDED] x", "ts": 1.5},
            {"user_input": "first time", "entity_response": "hi", "ts": 1.0},
        ]
    )
    s = _sleep(_Schema([(True, "Offer grounding first.")]), episodic)
    traces = [{"user_input": "today", "entity_response": "r"}]
    text = asyncio.run(s._private_context("alice@x", traces, s._practice_context()))
    episodic.recall_recent.assert_called_once()
    assert episodic.recall_recent.call_args.args[1] == "alice@x"
    assert "[trying] Offer grounding first." in text
    earlier = text.split("Earlier sessions")[1].split("This session")[0]
    assert earlier.index("first time") < earlier.index("last week it helped")
    assert "today" not in earlier and "CONCLUDED" not in earlier
    assert "This session:\nTurn 1: User: today" in text


def test_owner_source_gets_no_history(on):
    episodic = MagicMock()
    s = _sleep(_Schema(), episodic)
    asyncio.run(s._private_context("", [{"user_input": "x"}], ""))
    episodic.recall_recent.assert_not_called()


# ── paraphrases corroborate ──────────────────────────────────────────────────


async def test_paraphrase_corroborates_the_existing_lesson(on, fake_router):
    store = HypothesisStore(promote_k=2)
    store.add("Grounding before problem-solving tends to help when someone is flooded.", "a")
    fake_router.scripted_responses["lesson_match"] = '{"match": 1}'
    out = await match_existing(
        fake_router, "Settling the body first helps overwhelmed people plan.", store
    )
    assert out == store.all()[0].principle
    store.add(out, "b")
    assert len(store.established()) == 1


async def test_a_different_claim_stays_new(on, fake_router):
    store = HypothesisStore()
    store.add("Grounding first tends to help when someone is flooded.", "a")
    fake_router.scripted_responses["lesson_match"] = '{"match": 0}'
    assert await match_existing(fake_router, "Grounding falls flat when rushed.", store) is None


async def test_exact_wording_and_empty_store_skip_the_call(on, fake_router):
    store = HypothesisStore()
    assert await match_existing(fake_router, "Anything.", store) is None
    store.add("Same words.", "a")
    assert await match_existing(fake_router, "same words", store) is None
    assert not [c for c in fake_router.calls if c.get("cell") == "lesson_match"]


async def test_match_kill_switch(on, fake_router, monkeypatch):
    monkeypatch.setitem(settings._data, "cross_learning_match", 0)
    store = HypothesisStore()
    store.add("One lesson.", "a")
    fake_router.scripted_responses["lesson_match"] = '{"match": 1}'
    assert await match_existing(fake_router, "Another phrasing.", store) is None


async def test_bad_match_number_is_new(on, fake_router):
    store = HypothesisStore()
    store.add("One lesson.", "a")
    fake_router.scripted_responses["lesson_match"] = '{"match": 7}'
    assert await match_existing(fake_router, "Another phrasing.", store) is None


# ── review ───────────────────────────────────────────────────────────────────


def _evidence_store():
    store = HypothesisStore(promote_k=3)
    for src in ("a", "b", "c"):
        store.add("Grounding first tends to help when someone is flooded.", src, persona="me")
    store.add("Long breathing scripts tend to fall flat in text.", "d", persona="me")
    store.add("Size positions to the stop distance.", "e", persona="the_analyst")
    return store


def _router(reply: str):
    r = MagicMock()
    r.call = AsyncMock(return_value=reply)
    return r


async def test_review_settles_revises_drops_and_keeps(on):
    schema = _Schema(
        [
            (True, "Offer grounding before problem-solving."),
            (True, "Walk through a long breathing script."),
            (False, "Reflect feelings before facts."),
            (True, "Ask about values early."),
        ]
    )
    reply = (
        '{"decisions": ['
        '{"n": 1, "action": "settle"},'
        '{"n": 2, "action": "drop"},'
        '{"n": 3, "action": "keep"},'
        '{"n": 4, "action": "revise", "text": "Ask about values once they feel heard."}'
        "]}"
    )
    router = _router(reply)
    out = await pa.review(router, schema, _evidence_store(), persona="me")
    assert out == {"reviewed": True, "settled": 1, "revised": 1, "dropped": 1}
    assert pa.read(schema.read("self.md")) == [
        (False, "Offer grounding before problem-solving."),
        (False, "Reflect feelings before facts."),
        (True, "Ask about values once they feel heard."),
    ]
    prompt = router.call.call_args.args[2][0]["content"]
    assert "seen with 3 people; established" in prompt
    assert "seen with 1 person; tentative" in prompt
    assert "stop distance" not in prompt  # another persona's lesson is not evidence


async def test_review_unparseable_answer_changes_nothing(on):
    entries = [(True, "Offer grounding first.")]
    schema = _Schema(entries)
    before = schema.read("self.md")
    out = await pa.review(_router("not json"), schema, _evidence_store(), persona="me")
    assert out["reviewed"] is False and schema.read("self.md") == before


async def test_review_needs_lines_and_evidence(on):
    router = _router('{"decisions": []}')
    await pa.review(router, _Schema([]), _evidence_store(), persona="me")
    await pa.review(router, _Schema([(True, "x")]), HypothesisStore(), persona="me")
    router.call.assert_not_called()


async def test_revision_refused_by_deid_keeps_the_line(on):
    on.side_effect = lambda text, src="": None
    schema = _Schema([(True, "Offer grounding first.")])
    reply = '{"decisions": [{"n": 1, "action": "revise", "text": "Like with that one client"}]}'
    out = await pa.review(_router(reply), schema, _evidence_store(), persona="me")
    assert out["revised"] == 0
    assert pa.read(schema.read("self.md")) == [(True, "Offer grounding first.")]


async def test_settle_on_a_settled_line_and_bad_numbers_are_ignored(on):
    schema = _Schema([(False, "Reflect feelings first.")])
    reply = '{"decisions": [{"n": 1, "action": "settle"}, {"n": 9, "action": "drop"}, "x"]}'
    out = await pa.review(_router(reply), schema, _evidence_store(), persona="me")
    assert out["settled"] == 0 and out["dropped"] == 0
    assert pa.read(schema.read("self.md")) == [(False, "Reflect feelings first.")]


# ── sleep wires it together ──────────────────────────────────────────────────


@pytest.fixture
def admit(monkeypatch):
    store = HypothesisStore(salt="t")

    async def _learn(ruminator, st, text, source_id, persona=""):
        st.add("A lesson.", source_id, persona=persona)
        return LearnOutcome(admitted=True, stage="admitted")

    monkeypatch.setattr(cross_learning, "learn_from_private", _learn)
    monkeypatch.setattr(cross_learning, "load_store", lambda path=None: store)
    monkeypatch.setattr(cross_learning, "save_store", lambda st, path=None: None)
    review = AsyncMock(return_value={})
    monkeypatch.setattr(pa, "review", review)
    return review


def test_sleep_reviews_the_practice_when_evidence_moved(on, admit):
    s = _sleep(_Schema([(True, "Offer grounding first.")]))
    asyncio.run(s._cross_learning_pass([{"user_input": "x", "end_user_id": "a"}]))
    admit.assert_awaited_once()


def test_sleep_skips_review_without_a_practice_section(on, admit):
    s = _sleep(_Schema())
    asyncio.run(s._cross_learning_pass([{"user_input": "x", "end_user_id": "a"}]))
    admit.assert_not_awaited()


async def test_match_stays_within_the_persona(on, fake_router):
    store = HypothesisStore()
    store.add("Another persona's lesson.", "a", persona="the_analyst")
    fake_router.scripted_responses["lesson_match"] = '{"match": 1}'
    assert await match_existing(fake_router, "My lesson.", store, persona="me") is None
    assert not [c for c in fake_router.calls if c.get("cell") == "lesson_match"]


def test_store_round_trips_personas():
    store = HypothesisStore()
    store.add("Lesson.", "a", persona="me")
    again = HypothesisStore.from_dict(store.to_dict())
    assert again.all()[0].personas == {"me"}
    legacy = {"hypotheses": [{"id": "x", "principle": "Old.", "contributors": ["t"]}]}
    assert HypothesisStore.from_dict(legacy).all()[0].personas == set()


def test_self_update_prompt_leaves_the_practice_section_alone():
    from brain.sleep_prompts import SELF_UPDATE_SYSTEM

    assert pa.SECTION in SELF_UPDATE_SYSTEM
