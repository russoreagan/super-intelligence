"""Engine-lane cross-customer scoping (plan §0.7 items 2–3; guide §20 rule 3).

Inside one consolidated persona, three read paths carried one customer's verbatim
material into another customer's context: structural recall renders each hit's
user_input → entity_response into the prompt; SchemaStore.grep matched across every
speaker's profile file; the DMN memory seed sampled episodes across end users. All
three now take an ``end_user_id`` and the hippocampus / DMN pass the bound one on
the agent lane. Companion (owner-lane) turns are byte-identical: they pass None.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from brain.second_brain.store import EpisodicStore, SchemaStore
from brain.settings import settings
from brain.turn_ctx import bind_turn


def _episode_rows():
    return [
        {
            "session_id": "s1",
            "turn_id": "t1",
            "ts": 1.0,
            "user_input": "alice secret",
            "entity_response": "reply a",
            "topic_tags": json.dumps(["approach:probe"]),
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": json.dumps({"DA": 0.5, "NE": 0.5}),
            "end_user_id": "alice",
        },
        {
            "session_id": "s2",
            "turn_id": "t2",
            "ts": 2.0,
            "user_input": "bob secret",
            "entity_response": "reply b",
            "topic_tags": json.dumps(["approach:probe"]),
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": json.dumps({"DA": 0.5, "NE": 0.5}),
            "end_user_id": "bob",
        },
        {
            "session_id": "s3",
            "turn_id": "t3",
            "ts": 3.0,
            "user_input": "owner chat",
            "entity_response": "reply o",
            "topic_tags": "[]",
            "entities": "[]",
            "neuromod_snapshot": "{}",
            "cog_signature": json.dumps({"DA": 0.5, "NE": 0.5}),
            "end_user_id": "",
        },
    ]


def _local_store(rows):
    store = EpisodicStore.__new__(EpisodicStore)
    store._use_supabase = False
    store._ensure_ready = lambda: True
    table = MagicMock()
    table.to_arrow.return_value.to_pylist.return_value = rows
    store._table = table
    return store


def test_recall_structural_scoped_to_end_user():
    store = _local_store(_episode_rows())
    sig = {"DA": 0.5, "NE": 0.5}
    everyone = store.recall_structural(sig, limit=10)
    assert {e["end_user_id"] for e in everyone} == {"alice", "bob", ""}
    alice = store.recall_structural(sig, limit=10, end_user_id="alice")
    assert [e["user_input"] for e in alice] == ["alice secret"]
    owner = store.recall_structural(sig, limit=10, end_user_id="")
    assert [e["user_input"] for e in owner] == ["owner chat"]


def test_sample_random_scoped_to_end_user():
    store = _local_store(_episode_rows())
    assert {e["end_user_id"] for e in store.sample_random(10)} == {"alice", "bob", ""}
    assert [e["user_input"] for e in store.sample_random(10, end_user_id="bob")] == ["bob secret"]
    assert [e["user_input"] for e in store.sample_random(10, end_user_id="")] == ["owner chat"]


class _SbQ:
    def __init__(self, log):
        self.log = log

    def table(self, n):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self.log.append((k, v))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": []})()


def test_supabase_scan_filters_server_side():
    store = EpisodicStore.__new__(EpisodicStore)
    store._use_supabase = True
    log: list = []
    store._sb = lambda: (_SbQ(log), "org-1")
    store._sb_persona = lambda: "p"
    store.recall_structural({"DA": 1.0}, end_user_id="alice")
    assert ("end_user_id", "alice") in log
    log.clear()
    store.sample_random(3, end_user_id="")
    assert ("end_user_id", "") in log
    log.clear()
    store.recall_structural({"DA": 1.0})
    assert not any(k == "end_user_id" for k, _v in log)


def test_grep_hides_other_customers_profiles(tmp_path, monkeypatch):
    import brain.second_brain.store as store_mod

    monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path)
    monkeypatch.setattr(store_mod, "_STORAGE_BACKEND", "local")
    schema = SchemaStore(persona="p")
    schema._use_supabase = False
    (tmp_path / "self.md").write_text("# Self\n- likes cats\n")
    (tmp_path / "user.md").write_text("# User\n- owner likes cats\n")
    (tmp_path / schema.speaker_filename("alice")).write_text("# User: alice\n- alice likes cats\n")
    (tmp_path / schema.speaker_filename("bob")).write_text("# User: bob\n- bob likes cats\n")
    everyone = {f for f, _ in schema.grep("cats")}
    assert everyone == {
        "self.md",
        "user.md",
        schema.speaker_filename("alice"),
        schema.speaker_filename("bob"),
    }
    alice = {f for f, _ in schema.grep("cats", end_user_id="alice")}
    assert alice == {"self.md", schema.speaker_filename("alice")}
    assert schema._grep_visible("open_questions.md", "alice")
    assert not schema._grep_visible("user.md", "alice")


# ── hippocampus passes the bound customer ─────────────────────────────────────


class _Router:
    async def embed(self, *a, **kw):
        return None

    def supports(self, *a, **kw):
        return True


async def _grep_scope_seen(lane_end_user: str | None) -> list:
    """Run a real recall() with the schema grep spied; return the end_user_id
    values the grep received."""
    from brain.bus import Bus
    from brain.clusters.hippocampus import HippocampusCluster

    hippo = HippocampusCluster(Bus(), _Router())
    seen: list = []

    def _grep(keyword, end_user_id=None):
        seen.append(end_user_id)
        return []

    hippo._schema.grep = _grep
    kwargs = {
        "query": "the-quiet-signal",
        "entities": ["signal"],
        "turn_id": "t1",
        "embedding_fn": None,
        "novelty": False,
        "features": {},
    }
    if lane_end_user is None:
        await hippo.recall(**kwargs)
    else:
        with bind_turn("agent", session_id="s", agent_id="p.m", end_user_id=lane_end_user):
            await hippo.recall(**kwargs)
    return seen


async def test_hippocampus_scopes_grep_on_agent_lane(monkeypatch):
    monkeypatch.setitem(settings._data, "engine_lane_scoping", 1)
    assert await _grep_scope_seen("alice") == ["alice"]
    assert await _grep_scope_seen(None) == [None]  # owner lane: unscoped, as before


async def test_hippocampus_grep_kill_switch(monkeypatch):
    monkeypatch.setitem(settings._data, "engine_lane_scoping", 0)
    assert await _grep_scope_seen("alice") == [None]


# ── DMN memory seed ───────────────────────────────────────────────────────────


def _dmn():
    from brain import dmn as dmn_mod

    dmn = dmn_mod.DefaultModeNetwork.__new__(dmn_mod.DefaultModeNetwork)
    dmn._pstate = {}
    dmn._home = "home_p"
    dmn._hippocampus = MagicMock()
    dmn._hippocampus._episodic.sample_random = MagicMock(return_value=[])
    dmn._thought_count = dmn_mod.DMN_MEMORY_SEED_EVERY
    dmn._tick_idle_s = 999.0
    dmn._tick_idle_phase = dmn._idle_phase(999.0)
    dmn._conversation_text = ""
    dmn._spotlight_terms = lambda: ""
    return dmn


def test_memory_seed_samples_owner_lane_only_when_idle(monkeypatch):
    monkeypatch.setitem(settings._data, "engine_lane_scoping", 1)
    dmn = _dmn()
    dmn._maybe_inject_memory_seed()
    dmn._hippocampus._episodic.sample_random.assert_called_once_with(6, end_user_id="")


def test_memory_seed_scoped_to_bound_customer_on_agent_lane(monkeypatch):
    monkeypatch.setitem(settings._data, "engine_lane_scoping", 1)
    dmn = _dmn()
    with bind_turn("agent", session_id="s", agent_id="p.m", end_user_id="alice"):
        dmn._maybe_inject_memory_seed()
    dmn._hippocampus._episodic.sample_random.assert_called_once_with(6, end_user_id="alice")


def test_memory_seed_kill_switch_restores_persona_wide_sample(monkeypatch):
    monkeypatch.setitem(settings._data, "engine_lane_scoping", 0)
    dmn = _dmn()
    dmn._maybe_inject_memory_seed()
    dmn._hippocampus._episodic.sample_random.assert_called_once_with(6, end_user_id=None)


@pytest.mark.parametrize("flag", [0, 1])
def test_defaults_declare_the_switch(flag):
    from brain.settings import DEFAULTS

    assert "engine_lane_scoping" in DEFAULTS
