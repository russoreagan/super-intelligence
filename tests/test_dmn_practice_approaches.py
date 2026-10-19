"""Practice threads conclude into the self-model, not into a question for the owner.

Idle thinking that started from a learned principle (the practice seed) carries a
`practice` tag on its thread. When that thread concludes, the line lands in
self.md's "How I approach sessions": settled when confident, "[trying]" when
uncertain (sessions, through cross-learning, are what confirm it, so the owner is
not asked). The self-model rides every turn, so this is how the persona's idle
conclusions reach its sessions: as its own practice, never about anyone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import brain.open_threads as ot
from brain import practice_approaches as pa
from brain.second_brain.store import SchemaStore
from brain.settings import settings
from tests.test_dmn_threads import _make_dmn, _meta

SELF_MD = "# Self\n\n## History summary\nTalks with many people.\n"


class _Schema:
    """In-memory self.md with the real section-replace logic."""

    def __init__(self, content: str = SELF_MD):
        self.files = {"self.md": content}

    def read(self, name):
        return self.files.get(name, "")

    async def upsert_section(self, name, section, body):
        cur = self.files.get(name, "")
        if cur:
            self.files[name] = SchemaStore._replace_section_body(cur, section, body)


@pytest.fixture
def practice(monkeypatch):
    from brain import deid_gate, org_settings

    monkeypatch.setattr(org_settings, "is_isolated", lambda: False)
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "dmn_practice_seed_every", 5)
    monkeypatch.setitem(settings._data, "dmn_practice_approaches_max", 8)
    scrub = AsyncMock(side_effect=lambda text, src="": text)
    monkeypatch.setattr(deid_gate.DeidGate, "scrub_passage", lambda self, t, s="": scrub(t, s))
    # Every line here puts a lesson to work; grounding itself is tested in
    # test_practice_grounding.py.
    monkeypatch.setattr(pa, "grounding", AsyncMock(return_value="A lesson."))
    return scrub


def _dmn_with(schema: _Schema):
    dmn = _make_dmn()
    dmn._hippocampus._schema = schema
    dmn._save_threads = AsyncMock()
    return dmn


def _section(schema: _Schema) -> list[str]:
    body = schema.read("self.md").split(f"## {pa.SECTION}", 1)[1]
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


async def _conclude(dmn, line: str, confidence: str, tags=("practice",)):
    dmn._open_threads, t = ot.open_thread([], "how to pace grounding", bears_on=list(tags))
    await dmn._process_thought(
        "Settling how I want to work.",
        _meta(conclude_thread_id=t.id, conclusion=line, conclusion_confidence=confidence),
        "t",
    )
    await asyncio.sleep(0.01)
    return t


async def test_confident_practice_conclusion_settles_into_self_model(practice):
    schema = _Schema()
    dmn = _dmn_with(schema)
    t = await _conclude(dmn, "Offer a grounding step before any problem-solving.", "confident")
    assert _section(schema) == ["- Offer a grounding step before any problem-solving."]
    assert pa.PREFACE in schema.read("self.md")
    assert ot.find(dmn._open_threads, t.id) is None
    outcome = dmn._obs.record_thought.call_args.kwargs["outcome"]
    assert outcome["action"] == "recorded_approach" and outcome["trying"] is False


async def test_uncertain_practice_conclusion_is_tried_not_asked(practice):
    schema = _Schema()
    dmn = _dmn_with(schema)
    t = await _conclude(dmn, "Try naming the feeling before reflecting content.", "uncertain")
    assert _section(schema) == ["- [trying] Try naming the feeling before reflecting content."]
    dmn._append_deferred_thought.assert_not_called()
    dmn._hippocampus.encode_deferred_question.assert_not_awaited()
    assert ot.find(dmn._open_threads, t.id) is None


async def test_settling_a_trying_line_promotes_it_in_place(practice):
    schema = _Schema()
    dmn = _dmn_with(schema)
    await _conclude(dmn, "Name the feeling before reflecting content.", "uncertain")
    await _conclude(dmn, "Name the feeling before reflecting the content.", "confident")
    assert _section(schema) == ["- Name the feeling before reflecting the content."]


async def test_a_settled_line_is_never_demoted(practice):
    schema = _Schema()
    dmn = _dmn_with(schema)
    await _conclude(dmn, "Name the feeling before reflecting the content.", "confident")
    await _conclude(dmn, "Name the feeling before reflecting content.", "uncertain")
    assert _section(schema) == ["- Name the feeling before reflecting content."]


async def test_caps_trying_and_total(practice, monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_practice_approaches_max", 4)
    schema = _Schema()
    dmn = _dmn_with(schema)
    experiments = [
        "Open with a body check-in.",
        "Summarise progress at the halfway mark.",
        "Ask what helped last time.",
        "Slow the pace when replies shorten.",
        "Offer a values question after venting.",
    ]
    for line in experiments:
        await _conclude(dmn, line, "uncertain")
    trying = [ln for ln in _section(schema) if "[trying]" in ln]
    assert len(trying) == pa.TRYING_MAX
    assert "values question" in trying[-1] and not any("body check-in" in ln for ln in trying)
    settled = [
        "Reflect feelings before facts.",
        "Keep exercises to one step per reply.",
        "Name strengths they mention.",
        "Close with one small next step.",
    ]
    for line in settled:
        await _conclude(dmn, line, "confident")
    lines = _section(schema)
    assert len(lines) == 4


async def test_deid_refusal_writes_nothing(practice):
    practice.side_effect = lambda text, src="": None
    schema = _Schema()
    dmn = _dmn_with(schema)
    await _conclude(dmn, "Something it reached for an example on.", "confident")
    assert pa.SECTION not in schema.read("self.md")
    # Falls back to the ordinary conclusion path (owner-lane memory only).
    dmn._hippocampus.encode_conclusion.assert_awaited()


async def test_non_practice_threads_keep_the_old_path(practice):
    schema = _Schema()
    dmn = _dmn_with(schema)
    t = await _conclude(dmn, "Gating probably reduces cost.", "uncertain", tags=("efficiency",))
    assert pa.SECTION not in schema.read("self.md")
    dmn._append_deferred_thought.assert_called_once()
    assert ot.find(dmn._open_threads, t.id).status == ot.STATUS_PENDING


async def test_isolated_org_keeps_the_old_path(practice, monkeypatch):
    from brain import org_settings

    monkeypatch.setattr(org_settings, "is_isolated", lambda: True)
    schema = _Schema()
    dmn = _dmn_with(schema)
    await _conclude(dmn, "Offer a grounding step first.", "uncertain")
    assert pa.SECTION not in schema.read("self.md")
    dmn._append_deferred_thought.assert_called_once()


async def test_no_self_model_means_no_write(practice):
    schema = _Schema(content="")
    dmn = _dmn_with(schema)
    await _conclude(dmn, "Offer a grounding step first.", "confident")
    assert schema.read("self.md") == ""


def test_parse_render_round_trip():
    entries = [(False, "Settled one."), (True, "Trying two.")]
    assert pa.parse(pa.render(entries)) == entries


def _monologue(seed: str, reply: str):
    from tests.test_dmn_prompt_budget import _make_dmn as _prompt_dmn

    dmn = _prompt_dmn()
    dmn._practice_seed = seed
    dmn._monologue_cell.call = AsyncMock(return_value=reply)
    _thought, metadata = asyncio.run(dmn._run_monologue("t1", {}))
    (messages,), _ = dmn._monologue_cell.call.call_args
    return dmn, metadata, messages[0]["content"]


def test_practice_tick_tags_its_thread_and_consumes_the_seed():
    reply = '{"thought": "Ground first, then solve.", "open_thread": true, "bears_on": ["pacing"]}'
    dmn, metadata, prompt = _monologue("Grounding before problem-solving (tentative)", reply)
    assert "Grounding before problem-solving" in prompt
    assert "How I approach" not in prompt  # the section name is never in the idle prompt
    assert metadata["bears_on"] == ["practice", "pacing"]
    assert dmn._practice_seed == ""


def test_ordinary_tick_is_not_tagged():
    reply = '{"thought": "Something else.", "open_thread": true, "bears_on": ["pacing"]}'
    _, metadata, prompt = _monologue("", reply)
    assert metadata["bears_on"] == ["pacing"]
    assert "learned from the people you serve" not in prompt


async def test_practice_tick_concluding_an_unrelated_thread_is_not_practice(practice):
    """The tick's tag goes into the conclusion's metadata, but routing reads the
    THREAD's tags: an idea about token cost concluded on a practice tick must not
    become a line of session practice."""
    schema = _Schema()
    dmn = _dmn_with(schema)
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?", bears_on=["efficiency"])
    await dmn._process_thought(
        "Settled.",
        _meta(
            conclude_thread_id=t.id,
            conclusion="Emotional gating reduces tokens per response.",
            conclusion_confidence="confident",
            bears_on=["practice", "efficiency"],  # what a practice tick stamps
        ),
        "t",
    )
    await asyncio.sleep(0.01)
    assert pa.SECTION not in schema.read("self.md")
    dmn._hippocampus.encode_conclusion.assert_awaited()


def test_practice_threads_are_marked_with_how_to_conclude_them():
    from tests.test_dmn_prompt_budget import _make_dmn as _prompt_dmn

    dmn = _prompt_dmn()
    dmn._open_threads, _ = ot.open_thread([], "pacing grounding", bears_on=["practice"])
    dmn._open_threads, _ = ot.open_thread(dmn._open_threads, "token cost", bears_on=["cost"])
    asyncio.run(dmn._run_monologue("t1", {}))
    (messages,), _ = dmn._monologue_cell.call.call_args
    prompt = messages[0]["content"]
    assert "(practice) pacing grounding" in prompt
    assert "] token cost" in prompt
    assert "A (practice) thread is about how you approach your sessions" in prompt


def test_no_practice_threads_no_extra_instruction():
    from tests.test_dmn_prompt_budget import _make_dmn as _prompt_dmn

    dmn = _prompt_dmn()
    dmn._open_threads, _ = ot.open_thread([], "token cost", bears_on=["cost"])
    asyncio.run(dmn._run_monologue("t1", {}))
    (messages,), _ = dmn._monologue_cell.call.call_args
    assert "(practice) thread" not in messages[0]["content"]


def test_practice_tick_asks_to_open_a_thread():
    reply = '{"thought": "x"}'
    _, _, prompt = _monologue("A lesson (tentative)", reply)
    assert "open a thread for it" in prompt
