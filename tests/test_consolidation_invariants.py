"""
Consolidation invariant harness — runs a synthetic 20-turn session through the
REAL Sleep.consolidate() (scripted LLM, tmp second_brain) and asserts structural
invariants rather than exact outputs. This is the boundary where Hebbian
updates, fact extraction, the self-model, the synonym pass, and chunk mining
all funnel — and where multi-writer/file-shape bugs have historically lived.
"""

from __future__ import annotations

import json

import pytest

from brain.observability.timeline import TurnTrace
from brain.wiring import WEIGHT_MAX, WEIGHT_MIN, Wiring


class ScriptedCellRouter:
    """FakeRouter variant that tolerates IntegratorCell's full kwarg surface."""

    def __init__(self) -> None:
        self.scripted: dict[str, str] = {}
        self.calls: list[dict] = []
        self._call_log: list[dict] = self.calls

    async def call(self, model_key, system_prompt, messages, **kwargs) -> str:
        cell = kwargs.get("cell", "")
        self.calls.append({"cell": cell, "cluster": kwargs.get("cluster", "")})
        for key in (cell, model_key):
            if key in self.scripted:
                return self.scripted[key]
        return "{}"

    async def embed(self, text: str) -> list[float] | None:
        return [0.1] * 768


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect every second_brain artifact the consolidation touches to tmp."""
    import brain.second_brain.store as store_mod
    import brain.wiring as wiring_mod

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(store_mod, "SCHEMA_DIR", schema_dir)
    monkeypatch.setattr(store_mod, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(wiring_mod, "WIRING_PATH", tmp_path / "wiring.json")
    monkeypatch.setattr(wiring_mod, "WIRING_HISTORY_DIR", tmp_path / "wiring_history")
    # sequence_predictor + chunk paths resolve per persona at CALL time from
    # SECOND_BRAIN_PATH (persona_key.persona_state_root) — route the root to tmp.
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    return tmp_path


def _wiring_with_path(names: list[str]) -> Wiring:
    w = Wiring()
    for a, b in zip(names, names[1:], strict=False):
        w.add(a, b)
    return w


def _trace(i: int, names: list[str], da: float, da_prior: float) -> TurnTrace:
    t = TurnTrace(turn_id=f"t{i}", session_id="harness", user_input=f"input {i}")
    t.fired_path = [{"name": n} for n in names]
    t.neuromod = {"DA": da, "GABA": 0.2, "ACh": 0.4, "Glu": 0.3, "NE": 0.3}
    t.prior_neuromod = {"DA": da_prior, "GABA": 0.2, "ACh": 0.4, "Glu": 0.3, "NE": 0.3}
    t.draft_scores = [
        {"draft_id": f"draft_0_t{i}", "overall": 0.8, "selected": True, "critic_ran": True}
    ]
    t.user_emotion = "happy" if da > da_prior else ""
    return t


def _session_traces(n: int = 20) -> list[dict]:
    return [
        {
            "user_input": f"tell me about topic {i}",
            "entity_response": f"here is what I think about topic {i}",
            "emotion": "engaged",
            "topic_tags": [f"topic_{i % 3}"],
            "speaker_name": "Russ" if i % 2 else "",
            "user_emotion": "happy",
            "user_tone_toward_ai": "warm",
            "msg_length": "medium",
            "intent": "question",
            "requires_action": False,
        }
        for i in range(n)
    ]


def _scripted_sleep(router: ScriptedCellRouter, schema, episodic, wiring):
    from brain.sleep import SleepConsolidation as Sleep

    router.scripted.update(
        episode_synthesizer=json.dumps(
            {
                "user_facts": ["Enjoys discussing topics in depth."],
                "topic_clusters": ["topics"],
                "response_patterns": ["thorough"],
            }
        ),
        self_updater=json.dumps({}),
        personality_observer=json.dumps({}),
        thought_consolidator=json.dumps({}),
        angle_synonym_clusterer=json.dumps({"mappings": []}),
    )
    return Sleep(router, schema, episodic, wiring=wiring)


async def test_consolidation_end_to_end_invariants(sandbox, fake_schema_store):
    from brain.second_brain.store import EpisodicStore

    names = ["temporal.gate", "frontal.drafter_A", "brainstem.select"]
    wiring = _wiring_with_path(names)
    before = {k: wiring.get_edge_weight(*k) for k in [(names[0], names[1]), (names[1], names[2])]}

    router = ScriptedCellRouter()
    sleep = _scripted_sleep(router, fake_schema_store, EpisodicStore(), wiring)

    # Alternating reward/punish DA so updates flow both directions.
    full = [_trace(i, names, da=0.6 if i % 2 else 0.4, da_prior=0.45) for i in range(20)]
    await sleep.consolidate("harness", _session_traces(20), full_traces=full)

    # 1. Every weight stays inside the structural clamp.
    for (src, tgt), _ in before.items():
        w = wiring.get_edge_weight(src, tgt)
        assert WEIGHT_MIN <= w <= WEIGHT_MAX

    # 2. Learning happened: at least one edge moved off its starting weight.
    moved = any(
        abs(wiring.get_edge_weight(src, tgt) - prev) > 1e-6 for (src, tgt), prev in before.items()
    )
    assert moved, "20 outcome-bearing turns produced zero wiring movement"

    # 3. Wiring persisted and parses; weights on disk are in bounds too.
    import brain.wiring as wiring_mod

    edges = json.loads(wiring_mod.WIRING_PATH.read_text())
    assert edges, "wiring.json saved with no edges"
    for e in edges:
        assert WEIGHT_MIN <= float(e["w"]) <= WEIGHT_MAX

    # 4. Facts landed in schema files (primary user → user.md; named → speaker file).
    schema_files = {p.name for p in (sandbox / "schema").glob("*.md")}
    assert any(f for f in schema_files), "no schema files written by consolidation"

    # 5. No temp-file litter anywhere under the sandbox.
    litter = list(sandbox.rglob("*.tmp"))
    assert not litter, f"atomic-write litter left behind: {litter}"

    # 6. No file ballooned (a 20-turn session should stay well under 1 MB/file).
    for p in sandbox.rglob("*"):
        if p.is_file():
            assert p.stat().st_size < 1_000_000, f"{p} grew suspiciously large"


async def test_consolidation_empty_session_is_noop(sandbox, fake_schema_store):
    from brain.second_brain.store import EpisodicStore

    wiring = _wiring_with_path(["a.x", "b.y"])
    router = ScriptedCellRouter()
    sleep = _scripted_sleep(router, fake_schema_store, EpisodicStore(), wiring)
    await sleep.consolidate("harness-empty", [], full_traces=[])
    assert not router.calls, "empty session must not spend LLM calls"


async def test_consolidation_survives_llm_garbage(sandbox, fake_schema_store):
    """A consolidation pass where every LLM returns junk must not raise and
    must not corrupt the wiring file."""
    from brain.second_brain.store import EpisodicStore

    names = ["a.x", "b.y", "c.z"]
    wiring = _wiring_with_path(names)
    router = ScriptedCellRouter()
    router.scripted = dict.fromkeys(
        [
            "episode_synthesizer",
            "self_updater",
            "personality_observer",
            "thought_consolidator",
            "angle_synonym_clusterer",
        ],
        "NOT JSON {{{",
    )
    from brain.sleep import SleepConsolidation as Sleep

    sleep = Sleep(router, fake_schema_store, EpisodicStore(), wiring=wiring)
    full = [_trace(i, names, da=0.55, da_prior=0.45) for i in range(5)]
    await sleep.consolidate("harness-garbage", _session_traces(5), full_traces=full)

    import brain.wiring as wiring_mod

    for e in json.loads(wiring_mod.WIRING_PATH.read_text()):
        assert WEIGHT_MIN <= float(e["w"]) <= WEIGHT_MAX


async def test_chunk_mining_promotion_gates(sandbox):
    """Mined chunks only activate with >=3 distinct jobs and >=0.9 success."""
    from brain.clusters.chunk_memory import mine_chunks

    def job(jid, ok=True):
        return {
            "job_id": jid,
            "steps": [
                {"tool": "read", "args": {"p": jid}},
                {"tool": "grep", "args": {"q": "x"}},
            ],
            "results": ["ok", "ok" if ok else "[error] boom"],
        }

    promoted = mine_chunks([job("a"), job("b"), job("c")])
    active = [k for k, c in promoted["chunks"].items() if c["state"] == "active"]
    assert active, "3 distinct successful jobs should promote the sub-sequence"

    not_enough = mine_chunks([job("a"), job("b")])
    assert not any(c["state"] == "active" for c in not_enough["chunks"].values())

    flaky = mine_chunks([job("a"), job("b"), job("c", ok=False)])
    two_step = [c for c in flaky["chunks"].values() if len(c["sequence"]) == 2]
    assert all(c["state"] != "active" for c in two_step), "66% success must not promote"
