"""Isolated-mode leak gates (guide §20 "Learning mode", rules 1–8).

In an isolated org every persona is a separate individual. These are the exact
channels that were process- or org-wide and now close on org_settings.is_isolated():
established-principle injection into the turn, the cross-learning write at sleep,
the self-authoring pass, muscle memory for non-home personas, the DMN roster and
hydration, and the all-persona sleep passes (bounded to the batch ∪ home).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain import org_settings
from brain.second_brain.store import bind_persona
from brain.session_turn import _TurnMixin
from brain.settings import settings
from brain.sleep import SleepConsolidation


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")


@pytest.fixture
def consolidated(monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")


# ── rule 2: principle injection ──────────────────────────────────────────────


class _Brain(_TurnMixin):
    pass


def test_principles_injected_when_consolidated(consolidated, monkeypatch):
    from brain import cross_learning

    monkeypatch.setattr(cross_learning, "established_principles", lambda: ["p1", "p2"])
    b = _Brain()
    assert b._established_principles_for_turn() == ["p1", "p2"]


def test_principles_withheld_when_isolated(isolated, monkeypatch):
    from brain import cross_learning

    calls = {"n": 0}

    def _load():
        calls["n"] += 1
        return ["p1"]

    monkeypatch.setattr(cross_learning, "established_principles", _load)
    b = _Brain()
    assert b._established_principles_for_turn() == []
    assert calls["n"] == 0  # the store is not even read


def test_principles_withheld_when_org_unknown(monkeypatch):
    """Fail closed: a process that never read the org row withholds."""
    monkeypatch.setattr(org_settings, "learning_mode", lambda: org_settings.UNKNOWN)
    from brain import cross_learning

    monkeypatch.setattr(cross_learning, "established_principles", lambda: ["p1"])
    assert _Brain()._established_principles_for_turn() == []


def test_principles_reload_after_ttl(consolidated, monkeypatch):
    from brain import cross_learning

    vals = iter([["a"], ["b"]])
    monkeypatch.setattr(cross_learning, "established_principles", lambda: next(vals))
    b = _Brain()
    assert b._established_principles_for_turn() == ["a"]
    assert b._established_principles_for_turn() == ["a"]  # cached
    b._established_principles_ts -= 61
    assert b._established_principles_for_turn() == ["b"]  # re-read after the TTL


# ── rule 2 (write side), rule 5, rule 7: sleep ───────────────────────────────


def _make_sleep(rec: dict):
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    s._hebbian = None
    s._episodic = MagicMock()
    s._synthesizer = MagicMock()
    s._synthesizer.reset_turn = MagicMock()
    s._synthesizer.call = AsyncMock(
        return_value=json.dumps({"user_facts": [], "topic_clusters": [], "response_patterns": []})
    )
    s._self_updater = MagicMock()
    s._self_updater.reset_turn = MagicMock()
    s._self_updater.call = AsyncMock(return_value="{}")
    s._personality_observer = MagicMock()
    s._personality_observer.reset_turn = MagicMock()
    s._personality_observer.call = AsyncMock(return_value="{}")
    schema = MagicMock()
    schema.read = MagicMock(return_value="# Self\n")
    schema.ensure_speaker_schema = MagicMock(side_effect=lambda sp: f"speaker_{sp}.md")
    schema.aappend_fact = AsyncMock()
    schema.awrite = AsyncMock()
    s._schema = schema
    s.consolidate_thoughts = AsyncMock()

    async def _angle(session_id, personas=None):
        rec["angle"] = personas

    async def _chunk(session_id, personas=None):
        rec["chunk"] = personas

    async def _story(session_id, personas=None):
        rec["story"] = personas

    s.angle_synonym_pass = _angle
    s.chunk_mining_pass = _chunk
    s.learning_story_pass = _story
    s.authoring_pass = AsyncMock()
    return s


def _trace(persona, speaker="u1"):
    return {
        "user_input": "hi",
        "entity_response": "hello",
        "speaker_name": speaker,
        "persona": persona,
        "end_user_id": speaker,
    }


@pytest.fixture
def sleep_settings(monkeypatch):
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "enable_relationship_stage_progression", 0)
    monkeypatch.setitem(settings._data, "learning_narrator", 1)
    monkeypatch.setitem(settings._data, "sleep_group_by_persona", 1)
    monkeypatch.setitem(settings._data, "sleep_scan_all_personas", 0)
    monkeypatch.setitem(settings._data, "self_model_deid", 0)


def test_cross_learning_write_skipped_when_isolated(isolated, sleep_settings, monkeypatch):
    from brain import cross_learning

    learn = AsyncMock()
    monkeypatch.setattr(cross_learning, "learn_from_private", learn)
    monkeypatch.setattr(cross_learning, "load_store", MagicMock())
    rec: dict = {}
    s = _make_sleep(rec)
    with bind_persona("ahab_b1"):
        asyncio.run(s.consolidate("s1", [_trace("ahab_b1")], [], []))
    learn.assert_not_called()


def test_cross_learning_write_runs_when_consolidated(consolidated, sleep_settings, monkeypatch):
    from brain import cross_learning

    outcome = MagicMock(admitted=False, stage="extract")
    learn = AsyncMock(return_value=outcome)
    monkeypatch.setattr(cross_learning, "learn_from_private", learn)
    monkeypatch.setattr(cross_learning, "load_store", MagicMock())
    rec: dict = {}
    s = _make_sleep(rec)
    with bind_persona("ahab"):
        asyncio.run(s.consolidate("s1", [_trace("ahab")], [], []))
    learn.assert_called_once()


def test_all_persona_passes_bounded_to_batch_and_home(consolidated, sleep_settings):
    rec: dict = {}
    s = _make_sleep(rec)
    with bind_persona("ahab_b1"):
        asyncio.run(s.consolidate("s1", [_trace("ahab_b1"), _trace("ahab_b2")], [], []))
    assert rec["angle"] == rec["chunk"] == rec["story"] == {"ahab_b1", "ahab_b2", "home_p"}


def test_scan_all_personas_escape_hatch(consolidated, sleep_settings, monkeypatch):
    monkeypatch.setitem(settings._data, "sleep_scan_all_personas", 1)
    rec: dict = {}
    s = _make_sleep(rec)
    with bind_persona("ahab_b1"):
        asyncio.run(s.consolidate("s1", [_trace("ahab_b1")], [], []))
    assert rec["angle"] is None and rec["chunk"] is None and rec["story"] is None


def test_in_bounds_treats_empty_as_home():
    assert SleepConsolidation._in_bounds("", {"x"})
    assert SleepConsolidation._in_bounds("x", {"x"})
    assert not SleepConsolidation._in_bounds("y", {"x"})
    assert SleepConsolidation._in_bounds("y", None)


def test_bounded_story_pass_skips_out_of_batch_personas(consolidated, monkeypatch):
    from brain.observability import learning_reader

    monkeypatch.setattr(learning_reader, "list_personas", lambda: ["home_p", "a", "b"])
    s = SleepConsolidation.__new__(SleepConsolidation)
    narrated: list[str] = []

    async def _narrate(session_id, persona=""):
        narrated.append(persona)

    s._narrate_persona = _narrate
    asyncio.run(s.learning_story_pass("s1", personas={"a", "home_p"}))
    assert narrated == ["home_p", "a"]
    narrated.clear()
    asyncio.run(s.learning_story_pass("s1", personas=None))
    assert narrated == ["home_p", "a", "b"]


def test_authoring_pass_skipped_when_isolated(isolated, monkeypatch):
    monkeypatch.setitem(settings._data, "node_self_authoring", 1)
    from brain import node_authoring

    author = AsyncMock()
    monkeypatch.setattr(node_authoring, "author_and_admit", author)
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._hebbian = MagicMock()
    s._hebbian._wiring = MagicMock()
    s._router = MagicMock()
    s._node_architect = MagicMock()
    asyncio.run(s.authoring_pass("s1", trace_count=5))
    author.assert_not_called()


# ── rule 4: DMN roster ───────────────────────────────────────────────────────


def _make_dmn(home="home_p"):
    from brain.dmn import DefaultModeNetwork

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._pstate = {}
    dmn._home = home
    dmn._hydrated_personas = set()
    dmn._roster_cache = []
    dmn._roster_ts = 0.0
    dmn._rr_idx = 0
    return dmn


def test_dmn_roster_is_home_only_when_isolated(isolated, monkeypatch):
    """`dmn_isolated_roster=home` is the kill switch: purchase personas never think
    idle. (The default, `active`, adds personas with a recent human turn — covered
    in tests/test_dmn_round_robin.py §5.)"""
    from brain import agents

    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "home")
    rows = [
        {"persona": "b", "enabled": True, "tier": "full"},
        {"persona": "c", "enabled": True, "tier": "full"},
    ]
    monkeypatch.setattr(agents, "list_agents", lambda: rows)
    dmn = _make_dmn()
    assert dmn._roster() == ["home_p"]
    assert dmn._next_persona() == "home_p"
    assert dmn._next_persona() == "home_p"


def test_dmn_roster_rotates_when_consolidated(consolidated, monkeypatch):
    from brain import agents

    rows = [
        {"persona": "b", "enabled": True, "tier": "full"},
        {"persona": "c", "enabled": True, "tier": "full"},
    ]
    monkeypatch.setattr(agents, "list_agents", lambda: rows)
    monkeypatch.delenv("BRAIN_PERSONA_PINNED", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    assert _make_dmn()._roster() == ["home_p", "b", "c"]


def test_dmn_hydrate_skips_non_home_when_isolated(isolated, monkeypatch):
    """Off the roster → never hydrated (and so never persisted) from this loop."""
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "home")
    dmn = _make_dmn()
    dmn._load_novelty = MagicMock()
    dmn._load_threads = AsyncMock()
    dmn._load_routing_weights = MagicMock()
    dmn._load_projects = MagicMock()
    asyncio.run(dmn._hydrate("purchase_1"))
    assert dmn._hydrated_personas == set()
    dmn._load_novelty.assert_not_called()
    asyncio.run(dmn._hydrate("home_p"))
    assert dmn._hydrated_personas == {"home_p"}


# ── agents.list_agents server-side filters ───────────────────────────────────


class _Q:
    def __init__(self, log):
        self.log = log

    def table(self, name):
        self.log.append(("table", name))
        return self

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self.log.append(("eq", k, v))
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": [{"persona": "p", "mandate_id": "m"}]})()


def test_list_agents_filters_server_side(monkeypatch):
    from brain import agents
    from brain.second_brain import supabase_client

    log: list = []
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _Q(log))
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    out = agents.list_agents(tier="full", enabled=True, persona="The Sage")
    assert out[0]["agent_id"] == "p.m"
    assert ("eq", "tier", "full") in log
    assert ("eq", "enabled", True) in log
    assert ("eq", "persona", "the_sage") in log
    with pytest.raises(Exception, match="tier"):
        agents.list_agents(tier="mega")


# ── rule 6: muscle memory ────────────────────────────────────────────────────


def test_muscle_memory_skips_non_home_when_isolated(isolated):
    from brain.clusters import motor_memory as mm

    with bind_persona("purchase_1"):
        assert mm._isolated_non_home() is True
    with bind_persona("home_p"):
        assert mm._isolated_non_home() is False


def test_muscle_memory_active_when_consolidated(consolidated):
    from brain.clusters import motor_memory as mm

    with bind_persona("purchase_1"):
        assert mm._isolated_non_home() is False


def test_muscle_memory_methods_short_circuit(isolated):
    from brain.clusters.motor_memory import MuscleMemorySubsystem

    sub = MuscleMemorySubsystem.__new__(MuscleMemorySubsystem)
    sub._store = MagicMock()
    router = MagicMock()
    router.embed = AsyncMock(return_value=[0.1] * 3)
    with bind_persona("purchase_1"):
        assert asyncio.run(sub.before_plan("do x", router)) == ""
        assert asyncio.run(sub.recall_procedure("do x", router)) == (None, 0.0)
        asyncio.run(sub.after_job("g", [{"tool": "t"}], ["r"], True, router))
    sub._store.save.assert_not_called()
    sub._store.recall.assert_not_called()
