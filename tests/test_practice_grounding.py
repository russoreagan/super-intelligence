"""A line of practice has to rest on what real sessions taught.

In prod the Analyst's "How I approach sessions" was 8/8 idle filler ("The quiet
period has provided more bandwidth…") and the Empath's had 2 such lines among 3
real ones. Two holes let it in and kept it there:

  • any thread opened on a practice tick is tagged practice, so when the idle mind
    wandered off the lesson to its own quiet period, that musing concluded straight
    into a settled line;
  • review() only ran when a lesson was admitted, and kept every line the evidence
    did not mention.

Now grounding() admits a line only when it puts one of the persona's lessons to
work, and review() runs every pass with each line naming its lesson: a line that
names none decays (settled → [trying] → dropped). `practice_grounding: 0` restores
the earlier behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.open_threads as ot
from brain import cross_learning
from brain import practice_approaches as pa
from brain.cross_learning import LearnOutcome
from brain.hypothesis_store import HypothesisStore
from brain.settings import settings
from tests.test_dmn_practice_approaches import _dmn_with, _section
from tests.test_dmn_practice_approaches import _Schema as _DmnSchema
from tests.test_dmn_threads import _meta
from tests.test_practice_review import _Schema, _sleep

FILLER = [
    "The quiet period has provided more bandwidth for system background processes.",
    "Reflecting on system stability can inform broader strategic decisions.",
    "Idle stretches are a chance to consolidate what I know.",
]
REAL = "Offer a grounding step before any problem-solving."


@pytest.fixture
def on(monkeypatch):
    from brain import deid_gate, org_settings

    monkeypatch.setattr(org_settings, "is_isolated", lambda: False)
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "dmn_practice_seed_every", 5)
    monkeypatch.setitem(settings._data, "dmn_practice_approaches_max", 8)
    monkeypatch.setitem(settings._data, "practice_grounding", 1)
    scrub = AsyncMock(side_effect=lambda text, src="": text)
    monkeypatch.setattr(deid_gate.DeidGate, "scrub_passage", lambda self, t, s="": scrub(t, s))
    return scrub


def _store():
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


# ── grounding(): admission ───────────────────────────────────────────────────


async def test_grounding_names_the_lesson(on):
    router = _router('{"basis": 1}')
    got = await pa.grounding(router, REAL, _store(), persona="me")
    assert got.startswith("Grounding first")
    prompt = router.call.call_args.args[2][0]["content"]
    assert REAL in prompt and "stop distance" not in prompt  # own lessons only
    assert router.call.call_args.kwargs["cell"] == "approach_grounding"


@pytest.mark.parametrize("reply", ['{"basis": 0}', '{"basis": 9}', "not json", '{"basis": "x"}'])
async def test_grounding_fails_closed(on, reply):
    assert await pa.grounding(_router(reply), FILLER[0], _store(), persona="me") == ""


async def test_grounding_without_lessons_makes_no_call(on):
    router = _router('{"basis": 1}')
    assert await pa.grounding(router, REAL, HypothesisStore(), persona="me") == ""
    assert await pa.grounding(router, REAL, _store(), persona="the_stoic") == ""
    router.call.assert_not_called()


async def test_grounding_call_error_fails_closed(on):
    router = MagicMock()
    router.call = AsyncMock(side_effect=RuntimeError("down"))
    assert await pa.grounding(router, REAL, _store(), persona="me") == ""


# ── the idle mind: an ungrounded practice conclusion never lands ─────────────


async def _conclude(dmn, line: str, confidence: str):
    dmn._open_threads, t = ot.open_thread([], "the quiet period", bears_on=["practice"])
    await dmn._process_thought(
        "Settling it.",
        _meta(conclude_thread_id=t.id, conclusion=line, conclusion_confidence=confidence),
        "t",
    )
    await asyncio.sleep(0.01)
    return t


@pytest.mark.parametrize("confidence", ["confident", "uncertain"])
async def test_ungrounded_conclusion_is_an_ordinary_one(on, monkeypatch, confidence):
    grounding = AsyncMock(return_value="")
    monkeypatch.setattr(pa, "grounding", grounding)
    schema = _DmnSchema()
    dmn = _dmn_with(schema)
    t = await _conclude(dmn, FILLER[0], confidence)
    grounding.assert_awaited_once()
    assert pa.SECTION not in schema.read("self.md")
    # Not a question for the owner either: kept as an ordinary conclusion, retired.
    dmn._append_deferred_thought.assert_not_called()
    dmn._hippocampus.encode_conclusion.assert_awaited()
    assert ot.find(dmn._open_threads, t.id) is None
    on.assert_not_awaited()  # no de-id call spent on a line that won't be written


async def test_grounded_conclusion_lands(on, monkeypatch):
    monkeypatch.setattr(pa, "grounding", AsyncMock(return_value="Grounding first …"))
    schema = _DmnSchema()
    dmn = _dmn_with(schema)
    await _conclude(dmn, REAL, "uncertain")
    assert _section(schema) == [f"- [trying] {REAL}"]


async def test_grounding_uses_this_personas_lessons(on, monkeypatch):
    grounding = AsyncMock(return_value="x")
    monkeypatch.setattr(pa, "grounding", grounding)
    store = _store()
    monkeypatch.setattr(cross_learning, "load_store", lambda path=None: store)
    dmn = _dmn_with(_DmnSchema())
    dmn._active_persona_name = lambda: "The Analyst"
    await _conclude(dmn, REAL, "confident")
    assert grounding.call_args.args[2] is store
    assert grounding.call_args.kwargs["persona"] == "the_analyst"


async def test_kill_switch_skips_grounding(on, monkeypatch):
    monkeypatch.setitem(settings._data, "practice_grounding", 0)
    grounding = AsyncMock(return_value="")
    monkeypatch.setattr(pa, "grounding", grounding)
    schema = _DmnSchema()
    dmn = _dmn_with(schema)
    await _conclude(dmn, FILLER[0], "confident")
    grounding.assert_not_awaited()
    assert _section(schema) == [f"- {FILLER[0]}"]


# ── review(): every line must name its lesson ────────────────────────────────


async def test_filler_decays_out_in_two_reviews(on):
    """The prod Analyst case: a full section of settled filler, plus one real line."""
    schema = _Schema([(False, t) for t in FILLER] + [(False, REAL)])
    reply = (
        '{"decisions": ['
        + ",".join(f'{{"n": {i + 1}, "basis": 0, "action": "keep"}}' for i in range(3))
        + ', {"n": 4, "basis": 1, "action": "keep"}]}'
    )
    out = await pa.review(_router(reply), schema, _store(), persona="me")
    assert out["demoted"] == 3 and out["dropped"] == 0
    assert pa.read(schema.read("self.md")) == [(True, t) for t in FILLER] + [(False, REAL)]

    out = await pa.review(_router(reply), schema, _store(), persona="me")
    assert out["dropped"] == 3
    assert pa.read(schema.read("self.md")) == [(False, REAL)]


async def test_demoted_lines_go_first_when_trying_overflows(on):
    """Demoting a full section would overflow the [trying] cap; the lines that just
    lost their footing go before the ones being tested on purpose."""
    tested = "Try naming the feeling before reflecting content."
    filler = [f"{FILLER[i % 3]} ({i})" for i in range(4)]
    schema = _Schema([(True, tested)] + [(False, t) for t in filler])
    reply = (
        '{"decisions": [{"n": 1, "basis": 1, "action": "keep"},'
        + ",".join(f'{{"n": {i + 2}, "basis": 0, "action": "keep"}}' for i in range(4))
        + "]}"
    )
    out = await pa.review(_router(reply), schema, _store(), persona="me")
    entries = pa.read(schema.read("self.md"))
    assert (True, tested) in entries
    assert sum(1 for tr, _ in entries if tr) == pa.TRYING_MAX
    assert out["dropped"] == 2 and out["demoted"] == 2
    assert entries == [(True, tested), (True, filler[2]), (True, filler[3])]


async def test_settling_needs_a_lesson_seen_with_several_people(on):
    schema = _Schema([(True, REAL), (True, "Keep breathing exercises short in text.")])
    reply = (
        '{"decisions": [{"n": 1, "basis": 1, "action": "settle"},'
        '{"n": 2, "basis": 2, "action": "settle"}]}'
    )
    out = await pa.review(_router(reply), schema, _store(), persona="me")
    assert out["settled"] == 1  # lesson 2 was seen with one person
    assert pa.read(schema.read("self.md")) == [
        (False, REAL),
        (True, "Keep breathing exercises short in text."),
    ]


async def test_unmentioned_lines_are_left_alone(on):
    """A truncated answer must not wipe the section."""
    schema = _Schema([(False, REAL), (False, FILLER[0])])
    before = schema.read("self.md")
    out = await pa.review(_router('{"decisions": []}'), schema, _store(), persona="me")
    assert out["reviewed"] is True and schema.read("self.md") == before


async def test_review_prompt_numbers_the_lessons(on):
    router = _router('{"decisions": []}')
    await pa.review(router, _Schema([(False, REAL)]), _store(), persona="me")
    assert router.call.call_args.args[1] is pa._REVIEW_GROUNDED_SYS
    prompt = router.call.call_args.args[2][0]["content"]
    assert "L1. Grounding first" in prompt and "L2. Long breathing" in prompt


async def test_review_kill_switch_keeps_unmentioned_evidence(on, monkeypatch):
    monkeypatch.setitem(settings._data, "practice_grounding", 0)
    schema = _Schema([(False, FILLER[0])])
    router = _router('{"decisions": [{"n": 1, "action": "keep"}]}')
    out = await pa.review(router, schema, _store(), persona="me")
    assert router.call.call_args.args[1] is pa._REVIEW_SYS
    assert out["demoted"] == 0 and pa.read(schema.read("self.md")) == [(False, FILLER[0])]


# ── sleep: review every pass, not only when a lesson was admitted ────────────


@pytest.fixture
def nothing_admitted(monkeypatch):
    store = HypothesisStore(salt="t")

    async def _learn(ruminator, st, text, source_id, persona="", seen_at=0.0):
        return LearnOutcome(admitted=False, stage="gate")

    monkeypatch.setattr(cross_learning, "learn_from_private", _learn)
    monkeypatch.setattr(cross_learning, "load_store", lambda path=None: store)
    monkeypatch.setattr(cross_learning, "save_store", lambda st, path=None: None)
    review = AsyncMock(return_value={})
    monkeypatch.setattr(pa, "review", review)
    return review


def test_sleep_reviews_even_when_nothing_was_admitted(on, nothing_admitted):
    s = _sleep(_Schema([(False, FILLER[0])]))
    asyncio.run(s._cross_learning_pass([{"user_input": "x", "end_user_id": "a"}]))
    nothing_admitted.assert_awaited_once()


def test_sleep_kill_switch_reviews_only_on_admission(on, nothing_admitted, monkeypatch):
    monkeypatch.setitem(settings._data, "practice_grounding", 0)
    s = _sleep(_Schema([(False, FILLER[0])]))
    asyncio.run(s._cross_learning_pass([{"user_input": "x", "end_user_id": "a"}]))
    nothing_admitted.assert_not_awaited()


def test_setting_is_declared_on_by_default():
    from brain.settings import DEFAULTS  # declared, not JSON-only

    assert DEFAULTS["practice_grounding"] == 1
