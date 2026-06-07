"""
Tests for motor chunking (brain/clusters/chunk_memory.py).

Coverage:
  mine_chunks (offline mining)
    - extracts recurring tool sub-sequences as chunks
    - promotion gate: requires >= _MIN_DISTINCT_JOBS distinct jobs
    - promotion gate: requires >= _MIN_SUCCESS_RATE success rate
    - invariance: a step is invariant only if its args were identical everywhere
  ChunkMemorySubsystem (runtime consumer)
    - suggest_chunk: prefix match returns remaining invariant steps
    - suggest_chunk: stops at the first variable-arg step
    - suggest_chunk: returns None when no active chunk matches
    - suggest_chunk: suppressed chunks no longer fire
    - before_plan: lists active routines
"""

from __future__ import annotations

from brain.clusters.chunk_memory import (
    _MIN_DISTINCT_JOBS,
    ChunkMemorySubsystem,
    _chunk_key,
    mine_chunks,
)


def _job(job_id: str, tools_args: list[tuple[str, dict]], errors: set[int] | None = None) -> dict:
    errors = errors or set()
    steps = [{"tool": t, "args": a, "reason": ""} for t, a in tools_args]
    results = [("[error] boom" if i in errors else f"ok output {i}") for i in range(len(steps))]
    return {"job_id": job_id, "steps": steps, "results": results}


# ── mine_chunks ───────────────────────────────────────────────────────────────


def test_mine_extracts_recurring_subsequence():
    seq = [("git_status", {}), ("git_diff", {"path": "."})]
    jobs = [_job(f"j{i}", seq) for i in range(_MIN_DISTINCT_JOBS)]
    data = mine_chunks(jobs)
    key = _chunk_key(
        [{"tool": "git_status", "args": {}}, {"tool": "git_diff", "args": {"path": "."}}]
    )
    assert key in data["chunks"]
    c = data["chunks"][key]
    assert c["distinct_jobs"] == _MIN_DISTINCT_JOBS
    assert c["success_rate"] == 1.0
    assert c["state"] == "active"


def test_promotion_requires_enough_distinct_jobs():
    seq = [("git_status", {}), ("git_diff", {"path": "."})]
    jobs = [_job(f"j{i}", seq) for i in range(_MIN_DISTINCT_JOBS - 1)]
    data = mine_chunks(jobs)
    c = next(iter(data["chunks"].values()))
    assert c["distinct_jobs"] == _MIN_DISTINCT_JOBS - 1
    assert c["state"] == "candidate"


def test_promotion_requires_success_rate():
    seq = [("read_file", {"path": "x"}), ("run_command", {"cmd": "y"})]
    # 4 distinct jobs, half fail the second step → success_rate 0.5
    jobs = [
        _job("j0", seq),
        _job("j1", seq),
        _job("j2", seq, errors={1}),
        _job("j3", seq, errors={1}),
    ]
    data = mine_chunks(jobs)
    c = next(iter(data["chunks"].values()))
    assert c["distinct_jobs"] == 4
    assert c["success_rate"] == 0.5
    assert c["state"] == "candidate"


def test_invariance_detection():
    # git_status args always {}; git_diff path varies across jobs.
    jobs = [
        _job("j0", [("git_status", {}), ("git_diff", {"path": "a"})]),
        _job("j1", [("git_status", {}), ("git_diff", {"path": "b"})]),
        _job("j2", [("git_status", {}), ("git_diff", {"path": "c"})]),
    ]
    data = mine_chunks(jobs)
    c = next(iter(data["chunks"].values()))
    s0, s1 = c["sequence"]
    assert s0["tool"] == "git_status" and s0["invariant"] is True and s0["args"] == {}
    assert s1["tool"] == "git_diff" and s1["invariant"] is False and s1["args"] is None


# ── ChunkMemorySubsystem ──────────────────────────────────────────────────────


def _sub_with_chunk(sequence: list[dict]) -> tuple[ChunkMemorySubsystem, str]:
    """Construct a subsystem with a single active chunk injected in memory.
    (chunks.json does not exist in the test dir, so _load() is a no-op.)"""
    sub = ChunkMemorySubsystem()
    key = "|".join(s["tool"] for s in sequence)  # any stable key
    sub._chunks = {
        key: {
            "sequence": sequence,
            "occurrences": 9,
            "successes": 9,
            "distinct_jobs": 3,
            "success_rate": 1.0,
            "jobs": ["a", "b", "c"],
            "state": "active",
        }
    }
    return sub, key


async def test_suggest_chunk_prefix_match():
    seq = [
        {"tool": "a", "arg_keys": [], "args": {}, "invariant": True},
        {"tool": "b", "arg_keys": [], "args": {"k": 1}, "invariant": True},
        {"tool": "c", "arg_keys": [], "args": {}, "invariant": True},
    ]
    sub, _ = _sub_with_chunk(seq)
    out = await sub.suggest_chunk(["a"], {})
    assert [s["tool"] for s in out] == ["b", "c"]
    assert out[0]["args"] == {"k": 1}
    # also matches when 'a' is the tail of a longer recent history
    out2 = await sub.suggest_chunk(["x", "a"], {})
    assert [s["tool"] for s in out2] == ["b", "c"]


async def test_suggest_chunk_stops_at_variable_step():
    seq = [
        {"tool": "a", "arg_keys": [], "args": {}, "invariant": True},
        {"tool": "b", "arg_keys": [], "args": {}, "invariant": True},
        {"tool": "c", "arg_keys": ["path"], "args": None, "invariant": False},
    ]
    sub, _ = _sub_with_chunk(seq)
    out = await sub.suggest_chunk(["a"], {})
    assert [s["tool"] for s in out] == ["b"]


async def test_suggest_chunk_no_match_returns_none():
    seq = [
        {"tool": "a", "arg_keys": [], "args": {}, "invariant": True},
        {"tool": "b", "arg_keys": [], "args": {}, "invariant": True},
    ]
    sub, _ = _sub_with_chunk(seq)
    assert await sub.suggest_chunk(["z"], {}) is None
    assert await sub.suggest_chunk([], {}) is None


async def test_suggest_chunk_suppressed():
    seq = [
        {"tool": "a", "arg_keys": [], "args": {}, "invariant": True},
        {"tool": "b", "arg_keys": [], "args": {}, "invariant": True},
    ]
    sub, key = _sub_with_chunk(seq)
    assert await sub.suggest_chunk(["a"], {}) is not None
    sub.suppress(f"chunk:{key}")
    assert await sub.suggest_chunk(["a"], {}) is None


async def test_before_plan_lists_active_routines():
    seq = [
        {"tool": "git_status", "arg_keys": [], "args": {}, "invariant": True},
        {"tool": "git_diff", "arg_keys": [], "args": {}, "invariant": True},
    ]
    sub, _ = _sub_with_chunk(seq)
    out = await sub.before_plan("anything", router=None)
    assert "git_status → git_diff" in out
