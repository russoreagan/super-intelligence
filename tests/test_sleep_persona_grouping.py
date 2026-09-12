"""Sleep consolidation must not mix personas.

The trace buffer is process-wide (one process serves many personas via per-turn
binding) while `consolidate()` is triggered under ONE persona binding. Before the
fix every persona's turns were synthesised, personality-observed and folded into
the self-model of whichever persona pulled the trigger. Now the memory passes
group by each summary's `persona` stamp and re-bind per group, so each persona's
files only ever see its own turns and the synthesizer never sees another
persona's text. `sleep_group_by_persona: 0` restores the old single-binding pass.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.second_brain.store import active_persona, bind_persona
from brain.settings import settings
from brain.sleep import SleepConsolidation


class _Recorder:
    """Records (bound persona, payload) for every store write / cell call."""

    def __init__(self):
        self.synth_calls: list[tuple[str, str]] = []
        self.self_calls: list[tuple[str, str]] = []
        self.facts: list[tuple[str, str, str]] = []
        self.self_writes: list[tuple[str, str]] = []
        self.thought_calls: list[tuple[str, int]] = []
        self.reset_ids: list[str] = []


def _make_sleep(rec: _Recorder, persona_facts: dict[str, list[str]]):
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    s._hebbian = None
    s._episodic = MagicMock()

    async def _synth(messages):
        text = messages[0]["content"]
        rec.synth_calls.append((active_persona(), text))
        # Emit a fact naming which persona's batch this was, so the write site is
        # attributable end-to-end.
        facts = persona_facts.get(active_persona(), [])
        return json.dumps(
            {
                "user_facts": facts,
                "topic_clusters": [f"t_{active_persona() or 'trigger'}"],
                "response_patterns": [],
            }
        )

    s._synthesizer = MagicMock()
    s._synthesizer.reset_turn = MagicMock(side_effect=rec.reset_ids.append)
    s._synthesizer.call = AsyncMock(side_effect=_synth)

    async def _self(messages):
        rec.self_calls.append((active_persona(), messages[0]["content"]))
        return '{"history_summary": "seen %s"}' % (active_persona() or "trigger")

    s._self_updater = MagicMock()
    s._self_updater.reset_turn = MagicMock(side_effect=rec.reset_ids.append)
    s._self_updater.call = AsyncMock(side_effect=_self)

    s._personality_observer = MagicMock()
    s._personality_observer.reset_turn = MagicMock(side_effect=rec.reset_ids.append)
    s._personality_observer.call = AsyncMock(return_value="{}")

    schema = MagicMock()
    schema.read = MagicMock(return_value="# Self\n\n## History summary\n\nold\n")
    schema.ensure_speaker_schema = MagicMock(side_effect=lambda sp: f"speaker_{sp}.md")

    async def _append(fname, fact):
        rec.facts.append((active_persona(), fname, fact))

    async def _awrite(fname, content):
        rec.self_writes.append((active_persona(), content))

    schema.aappend_fact = AsyncMock(side_effect=_append)
    schema.awrite = AsyncMock(side_effect=_awrite)
    schema.upsert_section = AsyncMock()
    schema._replace_section_body = lambda existing, section, body: f"{existing}\n{body}"
    s._schema = schema

    async def _thoughts(session_id, session_thoughts, topic_clusters=None):
        rec.thought_calls.append((active_persona(), len(session_thoughts)))

    s.consolidate_thoughts = _thoughts
    s.angle_synonym_pass = AsyncMock()
    s.chunk_mining_pass = AsyncMock()
    s.learning_story_pass = AsyncMock()
    s.authoring_pass = AsyncMock()
    return s


def _trace(persona: str, speaker: str, text: str) -> dict:
    return {
        "user_input": text,
        "entity_response": f"reply to {text}",
        "speaker_name": speaker,
        "persona": persona,
        "end_user_id": speaker,
    }


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setitem(settings._data, "cross_learning", 0)
    monkeypatch.setitem(settings._data, "enable_relationship_stage_progression", 0)
    monkeypatch.setitem(settings._data, "learning_narrator", 0)
    monkeypatch.setitem(settings._data, "sleep_group_by_persona", 1)


def _run(s, traces, thoughts=None, trigger="the_analyst"):
    async def _go():
        with bind_persona(trigger):
            await s.consolidate("s1", traces, full_traces=None, session_thoughts=thoughts)

    asyncio.run(_go())


def test_each_persona_only_sees_its_own_turns(quiet):
    rec = _Recorder()
    s = _make_sleep(rec, {"the_analyst": ["A-fact"], "the_visionary": ["B-fact"]})
    traces = [
        _trace("the_analyst", "u_a", "ANALYST-SECRET-1"),
        _trace("the_visionary", "u_b", "VISIONARY-SECRET-1"),
        _trace("the_analyst", "u_a", "ANALYST-SECRET-2"),
    ]
    _run(s, traces, thoughts=[{"angle": "x", "salient": True}], trigger="the_analyst")

    # One synthesizer batch per (persona, speaker), each bound to its own persona
    # and containing only that persona's text.
    by_persona = {}
    for persona, text in rec.synth_calls:
        by_persona.setdefault(persona, []).append(text)
    assert set(by_persona) == {"the_analyst", "the_visionary"}
    for text in by_persona["the_analyst"]:
        assert "VISIONARY-SECRET" not in text
        assert "ANALYST-SECRET" in text
    for text in by_persona["the_visionary"]:
        assert "ANALYST-SECRET" not in text
        assert "VISIONARY-SECRET" in text

    # Facts landed under the persona whose batch produced them.
    assert ("the_analyst", "speaker_u_a.md", "A-fact") in rec.facts
    assert ("the_visionary", "speaker_u_b.md", "B-fact") in rec.facts
    assert not [f for f in rec.facts if f[0] == "the_analyst" and f[2] == "B-fact"]
    assert not [f for f in rec.facts if f[0] == "the_visionary" and f[2] == "A-fact"]

    # The self-model rewrite ran once per persona, each seeing only its own turns.
    assert sorted(p for p, _ in rec.self_calls) == ["the_analyst", "the_visionary"]
    for persona, ctx in rec.self_calls:
        other = "VISIONARY-SECRET" if persona == "the_analyst" else "ANALYST-SECRET"
        assert other not in ctx
    assert sorted(p for p, _ in rec.self_writes) == ["the_analyst", "the_visionary"]

    # DMN thoughts stay with the trigger persona only.
    assert rec.thought_calls == [("the_analyst", 1)]

    # Cell call caps are namespaced per persona group.
    assert any("the_visionary" in rid for rid in rec.reset_ids)
    assert any("the_analyst" in rid for rid in rec.reset_ids)


def test_unstamped_traces_fold_into_the_trigger_persona(quiet):
    rec = _Recorder()
    s = _make_sleep(rec, {"the_analyst": ["A-fact"]})
    traces = [_trace("", "u_a", "LEGACY-1"), _trace("the_analyst", "u_a", "NEW-1")]
    _run(s, traces, trigger="the_analyst")
    assert len(rec.synth_calls) == 1
    persona, text = rec.synth_calls[0]
    assert persona == "the_analyst"
    assert "LEGACY-1" in text and "NEW-1" in text


def test_thoughts_consolidate_under_trigger_even_with_no_trigger_turns(quiet):
    rec = _Recorder()
    s = _make_sleep(rec, {"the_visionary": ["B-fact"]})
    traces = [_trace("the_visionary", "u_b", "VISIONARY-1")]
    _run(s, traces, thoughts=[{"angle": "x", "salient": True}], trigger="the_analyst")
    assert rec.thought_calls == [("the_analyst", 1)]
    assert [p for p, _ in rec.synth_calls] == ["the_visionary"]


def test_kill_switch_restores_single_binding_pass(quiet, monkeypatch):
    monkeypatch.setitem(settings._data, "sleep_group_by_persona", 0)
    rec = _Recorder()
    s = _make_sleep(rec, {"the_analyst": ["A-fact"]})
    traces = [
        _trace("the_analyst", "u_a", "ANALYST-SECRET-1"),
        _trace("the_visionary", "u_b", "VISIONARY-SECRET-1"),
    ]
    _run(s, traces, trigger="the_analyst")
    # Old behaviour: everything runs under the trigger binding (the leak).
    assert {p for p, _ in rec.synth_calls} == {"the_analyst"}
    assert {p for p, _ in rec.self_calls} == {"the_analyst"}
    assert any("VISIONARY-SECRET" in ctx for _, ctx in rec.self_calls)


def test_journal_replay_rebuilds_stamp_from_full_trace(tmp_path, monkeypatch):
    from brain.observability import trace_journal
    from brain.observability.timeline import TurnTrace

    root = tmp_path / "tenant"
    root.mkdir()
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_visionary")
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    tr = TurnTrace(
        turn_id="t1",
        session_id="s1",
        user_input="hi",
        persona_name="the_analyst",
        end_user_id="u_9",
    )
    # An older build's summary line: no persona / end_user_id stamp.
    trace_journal.append(tr, {"user_input": "hi"})
    _, sums = trace_journal.load_orphans()
    assert sums[0]["persona"] == "the_analyst"
    assert sums[0]["end_user_id"] == "u_9"
