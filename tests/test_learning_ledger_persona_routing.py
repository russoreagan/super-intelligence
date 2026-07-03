"""Per-persona routing of the learning ledger + Learning-surface reads.

SECOND_BRAIN_PATH is frozen at boot to the HOME persona's root, but records are
stamped with the TURN-BOUND persona (agent lanes, round-robin DMN). The ledger
must route each record to the stamped persona's own file — otherwise every
bound persona's learning lands in the home persona's ledger, the Learning
workspace never lists them, and the home view double-counts their rewards
(found 2026-07-03: the Analyst — the Scheduler App's agent persona — was
invisible in the Learning tab).
"""

import json

import pytest

from brain.observability import learning_ledger, learning_reader


@pytest.fixture
def home_root(tmp_path, monkeypatch):
    root = tmp_path / "tenant" / "personas" / "the_companion"
    root.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_companion")
    monkeypatch.setattr(learning_ledger, "_line_counts", {})
    return root


def _emit(persona, **fields):
    learning_ledger.append(
        {
            "type": "decision",
            "decision": "reward_emission",
            "persona": persona,
            "signal_type": "self_graded",
            "turn_id": fields.pop("turn_id", "t"),
            **fields,
        }
    )


def test_records_route_to_stamped_personas_file(home_root):
    _emit("the_companion", turn_id="t1")
    _emit("the_analyst", turn_id="t2")
    _emit("The Analyst", turn_id="t3")  # raw display name normalizes to the same slug

    analyst = home_root.parent / "the_analyst" / "learning_ledger.jsonl"
    assert analyst.exists()
    assert len(analyst.read_text().splitlines()) == 2
    assert len((home_root / "learning_ledger.jsonl").read_text().splitlines()) == 1


def test_routed_persona_appears_in_learning_surface(home_root):
    _emit("the_analyst")
    assert "the_analyst" in learning_reader.list_personas()
    mix = learning_reader._reward_mix("the_analyst")
    assert mix["by_signal_type"] == {"self_graded": 1}


def test_legacy_mixed_home_file_filtered_by_stamp(home_root):
    # Pre-routing history: an analyst-stamped record sitting in the HOME file
    # must not count toward the home persona's view...
    _emit("the_companion")
    with (home_root / "learning_ledger.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "decision",
                    "decision": "reward_emission",
                    "persona": "the_analyst",
                    "signal_type": "self_graded",
                    "turn_id": "legacy",
                }
            )
            + "\n"
        )
        # ...while unstamped (pre-ledger) records are kept.
        f.write(
            json.dumps(
                {
                    "type": "decision",
                    "decision": "reward_emission",
                    "signal_type": "self_graded",
                    "turn_id": "prestamp",
                }
            )
            + "\n"
        )
    mix = learning_reader._reward_mix("the_companion")
    assert mix["by_signal_type"] == {"self_graded": 2}


def test_rotation_is_per_file(home_root, monkeypatch):
    monkeypatch.setattr(learning_ledger, "_MAX_LINES", 20)
    monkeypatch.setattr(learning_ledger, "_KEEP_LINES", 10)
    _emit("the_companion")
    for i in range(25):
        _emit("the_analyst", turn_id=f"t{i}")
    analyst = home_root.parent / "the_analyst" / "learning_ledger.jsonl"
    assert len(analyst.read_text().splitlines()) <= 20
    assert len((home_root / "learning_ledger.jsonl").read_text().splitlines()) == 1


def test_bare_root_routes_non_home_under_personas_dir(tmp_path, monkeypatch):
    bare = tmp_path / "second_brain"
    bare.mkdir()
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(bare))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_companion")
    monkeypatch.setattr(learning_ledger, "_line_counts", {})
    _emit("the_companion")
    _emit("the_analyst")
    assert (bare / "learning_ledger.jsonl").exists()
    assert (bare / "personas" / "the_analyst" / "learning_ledger.jsonl").exists()


def test_persona_state_root_is_the_canonical_rule(home_root, monkeypatch):
    """persona_state_root: home/empty → active root; others → sibling dir;
    raw display names normalize."""
    from brain.persona_key import persona_state_root

    assert persona_state_root("") == home_root
    assert persona_state_root("the_companion") == home_root
    assert persona_state_root("The Analyst") == home_root.parent / "the_analyst"


def test_sequence_predictor_routes_per_persona(home_root, monkeypatch):
    """A predictor constructed under a bound persona persists to that persona's
    own sequence_weights.json; unbound (home) keeps the root file. Round-trips."""
    from brain.second_brain.store import bind_persona
    from brain.sequence_predictor import SequencePredictor

    home_p = SequencePredictor()  # unbound → home
    home_p.record("alpha-one")
    home_p.record("beta-two")
    home_p.save()
    assert (home_root / "sequence_weights.json").exists()

    with bind_persona("the_analyst"):
        analyst_p = SequencePredictor()
    analyst_p.record("gamma-three")
    analyst_p.record("delta-four")
    analyst_p.save()  # binding captured at construction — no active binding needed
    analyst_file = home_root.parent / "the_analyst" / "sequence_weights.json"
    assert analyst_file.exists()

    import json

    assert "gamma-three" not in (home_root / "sequence_weights.json").read_text()
    assert "alpha-one" not in analyst_file.read_text()

    with bind_persona("the_analyst"):
        p2 = SequencePredictor()
    p2.load()
    assert list(p2._history) == ["gamma-three", "delta-four"]
    # load() is idempotent — a second load must not duplicate history.
    p2.load()
    assert list(p2._history) == ["gamma-three", "delta-four"]


def test_chunk_mining_groups_jobs_by_persona_stamp(home_root, monkeypatch):
    """Sleep's chunk mining writes each persona's chunks.json from ITS jobs;
    unstamped legacy records attribute to home."""
    import asyncio

    import brain.clusters.job_store as job_store_mod
    from brain.sleep import SleepConsolidation

    jobs_dir = home_root / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_store_mod, "JOBS_DIR", jobs_dir)

    def _job(i, persona):
        steps = [
            {"tool": "fetch_url", "args": {"url": f"https://x/{i}"}},
            {"tool": "write_file", "args": {"path": "out.md", "content": "c"}},
        ]
        rec = {"job_id": f"j_{persona or 'home'}_{i}", "goal": f"goal {i}",
               "steps": steps, "results": ["ok", "ok"]}
        if persona:
            rec["persona"] = persona
        (jobs_dir / f"{rec['job_id']}.json").write_text(json.dumps(rec))

    for i in range(8):  # meets _CHUNK_MIN_JOBS per group
        _job(i, "the_analyst")
        _job(i, "")  # unstamped → home

    sc = SleepConsolidation.__new__(SleepConsolidation)
    asyncio.run(sc.chunk_mining_pass("s1"))

    analyst_chunks = home_root.parent / "the_analyst" / "chunks.json"
    home_chunks = home_root / "chunks.json"
    assert analyst_chunks.exists() and home_chunks.exists()
    a = json.loads(analyst_chunks.read_text())["chunks"]
    h = json.loads(home_chunks.read_text())["chunks"]
    # Both mined the same shape of sequence from their OWN 8 jobs.
    assert a and h
    assert all(c["distinct_jobs"] == 8 for c in a.values())
    assert all(c["distinct_jobs"] == 8 for c in h.values())


def test_chunk_memory_reads_bound_personas_file(home_root):
    """The runtime chunk consumer resolves the bound persona's chunks.json —
    each persona is primed with its own routines."""
    import asyncio

    from brain.clusters.chunk_memory import ChunkMemorySubsystem
    from brain.second_brain.store import bind_persona

    def _chunks_file(root, tool):
        root.mkdir(parents=True, exist_ok=True)
        (root / "chunks.json").write_text(
            json.dumps(
                {
                    "chunks": {
                        f"{tool}|a→{tool}|b": {
                            "sequence": [{"tool": tool, "arg_keys": ["a"]},
                                         {"tool": tool, "arg_keys": ["b"]}],
                            "occurrences": 5,
                            "state": "active",
                        }
                    }
                }
            )
        )

    _chunks_file(home_root, "home_tool")
    _chunks_file(home_root.parent / "the_analyst", "analyst_tool")

    sub = ChunkMemorySubsystem()
    home_priming = asyncio.run(sub.before_plan("t", None))
    with bind_persona("the_analyst"):
        analyst_priming = asyncio.run(sub.before_plan("t", None))
    assert "home_tool" in home_priming and "analyst_tool" not in home_priming
    assert "analyst_tool" in analyst_priming and "home_tool" not in analyst_priming


def test_job_store_stamps_bound_persona(home_root, monkeypatch):
    import brain.clusters.job_store as job_store_mod
    from brain.clusters.job_store import JobStore
    from brain.second_brain.store import bind_persona

    jobs_dir = home_root / "jobs"
    monkeypatch.setattr(job_store_mod, "JOBS_DIR", jobs_dir)
    store = JobStore()
    with bind_persona("The Analyst"):
        store.save("j1", "goal", [{"tool": "t", "args": {}}], ["ok"], True)
    rec = json.loads((jobs_dir / "j1.json").read_text())
    assert rec["persona"] == "the_analyst"


def test_intent_bank_routes_per_persona(home_root):
    """Each persona's LLM-taught intent exemplars persist to its own
    intent_bank.json and don't leak into other personas' matching."""
    import asyncio

    from brain.intent_detector import IntentDetector
    from brain.second_brain.store import bind_persona

    async def embed(text):
        return [1.0, 0.0] if "alpha" in text else [0.0, 1.0]

    det = IntentDetector(home_root / "intent_bank.json", {"stop_work": ["halt now"]})

    with bind_persona("the_analyst"):
        asyncio.run(det.detect_all("alpha request", embed))
        det.learn_from_llm({"stop_work": True})
    analyst_bank = home_root.parent / "the_analyst" / "intent_bank.json"
    assert analyst_bank.exists()
    assert "alpha request" in analyst_bank.read_text()
    # Home's bank file doesn't carry the analyst's exemplar.
    home_bank = home_root / "intent_bank.json"
    assert not home_bank.exists() or "alpha request" not in home_bank.read_text()
    # And home's in-memory bank stays seed-only.
    assert all("alpha request" not in json.dumps(det._bank) for _ in [0])
