"""
Tests for motor chunking (brain/clusters/chunk_memory.py).

Coverage:
  mine_chunks (offline mining)
    - extracts recurring tool sub-sequences as chunks
    - promotion gate: requires >= _MIN_DISTINCT_JOBS distinct jobs
    - promotion gate: requires >= _MIN_SUCCESS_RATE success rate
    - invariance: a step is invariant only if its args were identical everywhere
    - planner placeholders (tool="none") are never mined, and act as a barrier
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
    fireable_chunk_count,
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


def test_planner_placeholders_are_never_mined():
    """A run of planner failures must not become the corpus's most reliable skill.

    Regression: the motor cortex logs {"tool": "none", "reason": "[planner failed]"}
    with an EMPTY result when no tool ran. `_is_error("")` is False, so mining scored
    those no-ops as 100%-successful, and since their args are always {} the chunk was
    invariant — making `none→none` the single active chunk mined from the real
    93-job corpus, eligible to fire ballistically.
    """
    placeholder = [
        {"tool": "none", "args": {}, "reason": "[planner failed]"},
        {"tool": "none", "args": {}, "reason": "[planner failed]"},
    ]
    jobs = [
        {"job_id": f"j{i}", "steps": list(placeholder), "results": ["", ""]}
        for i in range(_MIN_DISTINCT_JOBS + 2)
    ]
    data = mine_chunks(jobs)
    assert data["chunks"] == {}, "planner placeholders must never be mined as a chunk"


def test_placeholder_is_a_barrier_between_real_tools():
    """A no-op breaks adjacency: tools either side of it were never run as a unit,
    so no chunk may span the placeholder — but real neighbours still mine."""
    jobs = [
        {
            "job_id": f"j{i}",
            "steps": [
                {"tool": "list_files", "args": {"path": "."}, "reason": ""},
                {"tool": "read_file", "args": {"path": "a"}, "reason": ""},
                {"tool": "none", "args": {}, "reason": "[planner failed]"},
                {"tool": "write_file", "args": {"path": "b"}, "reason": ""},
            ],
            "results": ["ok", "ok", "", "ok"],
        }
        for i in range(_MIN_DISTINCT_JOBS)
    ]
    data = mine_chunks(jobs)
    keys = list(data["chunks"])
    assert all("none" not in k for k in keys), f"no chunk may contain a placeholder: {keys}"
    # The genuine adjacent pair before the barrier still mines and promotes.
    real = _chunk_key(
        [
            {"tool": "list_files", "args": {"path": "."}},
            {"tool": "read_file", "args": {"path": "a"}},
        ]
    )
    assert data["chunks"][real]["state"] == "active"
    # Nothing bridges across the barrier (read_file → write_file were not adjacent).
    bridged = _chunk_key(
        [
            {"tool": "read_file", "args": {"path": "a"}},
            {"tool": "write_file", "args": {"path": "b"}},
        ]
    )
    assert bridged not in data["chunks"]


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


# ── realistic-corpus characterization ─────────────────────────────────────────
#
# The unit tests above pin individual gates. These pin the *corpus-level* behaviour
# the 2026-07-17 investigation turned on: the two promotion-blocking bugs (placeholder
# scoring + persona-group collision) meant NO chunk had ever formed, and the prior
# read of that ("the bar has legitimately never been met") came from mining only the
# home/idle jobs dir — ~100% failed single-step exploration. Re-mining the real
# persona corpora (the_visionary: 74 jobs / 69 success → 63 active; the_analyst: 33 →
# 17 active) showed the bar IS readily met by successful multi-step work. The fixture
# below reproduces that product shape so a regression that re-breaks promotion — or a
# threshold change that stops real work from promoting — fails here.


def _realistic_corpus() -> list[dict]:
    """A product-shaped mix: successful multi-step jobs that share recurring tool
    sub-sequences with per-run-varying args (paths/queries differ every job, like
    real list_files/search_files/read_file), a repeated fixed-arg idle read
    (query_langfuse recent_traces — args identical every run), plus the noise the
    real corpus is mostly made of: failed single-step jobs and a planner-placeholder
    job. Only the successful, recurring, multi-step spine should promote."""
    jobs: list[dict] = []
    # 4 successful research jobs sharing list_files → search_files → read_file
    # (args vary per job) and a trailing fixed-arg query_langfuse → query_langfuse.
    for i in range(4):
        jobs.append(
            _job(
                f"research{i}",
                [
                    ("list_files", {"path": f"/proj/mod{i}"}),
                    ("search_files", {"query": f"term{i}"}),
                    ("read_file", {"path": f"/proj/mod{i}/main.py"}),
                    ("query_langfuse", {"operation": "recent_traces", "limit": 50}),
                    ("query_langfuse", {"operation": "recent_traces", "limit": 50}),
                ],
            )
        )
    # Noise that must NOT promote: single-step failures (below _MIN_LEN) …
    for i in range(6):
        jobs.append(
            {
                "job_id": f"probe{i}",
                "steps": [{"tool": "list_files", "args": {"path": f"/x{i}"}, "reason": ""}],
                "results": ["[error] not found"],
            }
        )
    # … and a planner-placeholder job (all no-ops; a mining barrier).
    jobs.append(
        {
            "job_id": "stalled",
            "steps": [{"tool": "none", "args": {}, "reason": "[planner failed]"}] * 3,
            "results": ["", "", ""],
        }
    )
    return jobs


def test_realistic_corpus_promotes_chunks():
    """On a realistic mixed corpus, successful recurring multi-step work promotes —
    the regression guard the earlier corpus-wide 'zero chunks' state would trip."""
    data = mine_chunks(_realistic_corpus())
    active = {k: c for k, c in data["chunks"].items() if c["state"] == "active"}
    assert active, "a realistic successful-multi-step corpus must yield active chunks"

    # The varying-arg research spine promotes (tool+arg-SHAPE recurs across 4 jobs).
    spine = _chunk_key(
        [
            {"tool": "list_files", "args": {"path": "."}},
            {"tool": "search_files", "args": {"query": "."}},
            {"tool": "read_file", "args": {"path": "."}},
        ]
    )
    assert data["chunks"][spine]["state"] == "active"
    assert data["chunks"][spine]["distinct_jobs"] == 4
    assert data["chunks"][spine]["success_rate"] == 1.0

    # Noise contributes nothing: no active chunk contains a placeholder, and the
    # single-step probe failures can't form a 2-gram at all.
    assert all("none" not in k for k in active)


def test_promotion_and_firing_are_different_gates():
    """The investigation's core nuance: a chunk can be ACTIVE (primes the planner)
    yet fire NOTHING, because ballistic firing needs an invariant (fixed-arg) tail.
    On the realistic corpus, the varying-arg research spine is active-but-inert while
    the fixed-arg query_langfuse tail is the only thing actually fireable."""
    data = mine_chunks(_realistic_corpus())

    # Varying-arg spine: active, but every step's args differ across runs → no
    # invariant tail → inert for firing.
    spine = _chunk_key(
        [
            {"tool": "list_files", "args": {"path": "."}},
            {"tool": "search_files", "args": {"query": "."}},
            {"tool": "read_file", "args": {"path": "."}},
        ]
    )
    spine_seq = data["chunks"][spine]["sequence"]
    assert all(s["invariant"] is False for s in spine_seq)

    # Fixed-arg idle read: query_langfuse → query_langfuse with identical args every
    # run → invariant → fireable.
    ql = _chunk_key(
        [
            {"tool": "query_langfuse", "args": {"operation": "recent_traces", "limit": 50}},
            {"tool": "query_langfuse", "args": {"operation": "recent_traces", "limit": 50}},
        ]
    )
    assert data["chunks"][ql]["state"] == "active"
    assert data["chunks"][ql]["sequence"][1]["invariant"] is True

    # fireable_chunk_count counts only the genuinely-fireable ones — strictly fewer
    # than the active count on this corpus (most active chunks are priming-only).
    n_active = sum(1 for c in data["chunks"].values() if c["state"] == "active")
    n_fireable = fireable_chunk_count(data["chunks"])
    assert 0 < n_fireable < n_active


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


async def test_mined_fixed_arg_tail_fires_end_to_end():
    """Full path: mine a corpus whose recurring tail has identical args every run,
    load the mined chunks into the runtime subsystem, and confirm suggest_chunk
    fires that invariant tail ballistically. Proves the two halves (offline mining →
    runtime firing) connect for the case that SHOULD fire."""
    seq = [("gather_context", {"scope": "session"}), ("publish_digest", {"channel": "ops"})]
    data = mine_chunks([_job(f"j{i}", seq) for i in range(_MIN_DISTINCT_JOBS)])
    active = {k: c for k, c in data["chunks"].items() if c["state"] == "active"}
    assert active

    sub = ChunkMemorySubsystem()
    sub._chunks = active  # inject mined chunks (chunks.json absent → _load is a no-op)

    fired = await sub.suggest_chunk(["gather_context"], {})
    assert fired is not None
    assert [s["tool"] for s in fired] == ["publish_digest"]
    assert fired[0]["args"] == {"channel": "ops"}
