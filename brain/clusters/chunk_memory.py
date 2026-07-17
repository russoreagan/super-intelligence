"""
ChunkMemorySubsystem — motor chunking for the motor cortex.

The automatization tier *below* whole-procedure open-loop (motor_memory.py).
Where muscle memory recalls a whole job by goal similarity, chunks are recurring
*tool sub-sequences* that show up across many different jobs — the basal-ganglia
analogue of motor chunking. Once a sub-sequence has fired reliably across enough
distinct contexts, its over-learned, fixed-parameter tail can run ballistically
without per-step planning, even inside an otherwise novel job.

Two-tier learning:
  - Mining + promotion happens offline, during sleep consolidation
    (brain/sleep.py calls mine_chunks() over second_brain/jobs/*.json and writes
    second_brain/chunks.json). Skills consolidate during rest; the hot path is
    untouched.
  - This subsystem is a read-only consumer of chunks.json at runtime:
      before_plan()   — surface active chunks to prime the planner (Phase 1)
      suggest_chunk() — fire an over-learned tail ballistically (Phase 2)

Storage is a plain JSON file (like sequence_weights.json / dmn_routing_weights.json),
not LanceDB — chunks are keyed by exact tool-sequence strings, so vector search
would be the wrong tool.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from brain.clusters.motor_subsystem import MotorSubsystem
from brain.model_router import ModelRouter

logger = logging.getLogger(__name__)

# Mining parameters
_MIN_LEN = 2  # shortest sub-sequence worth tracking
_MAX_LEN = 4  # longest sub-sequence worth tracking
_JOBS_RING = 25  # cap on stored contributing job ids per chunk

# Promotion gate — what makes a sub-sequence a *skill* rather than one memorised job
_MIN_DISTINCT_JOBS = 3  # must appear across this many different parent goals
_MIN_SUCCESS_RATE = 0.9  # of observed occurrences, this fraction must have succeeded

# How many active chunks to surface to the planner as priming context.
_MAX_PRIMING = 6


# Planner placeholders, not motor acts. The motor cortex logs {"tool": "none"}
# into the step record both when the planner deliberately stops and when it fails
# outright ({"reason": "[planner failed]"}); either way no tool ran, and the empty
# result it leaves behind reads as SUCCESS to _is_error. Mining them promotes a run
# of planner failures into a perfect-success "skill" — and because their args are
# always {} it is invariant, so suggest_chunk would fire "none" ballistically,
# burning turn budget and appending yet more placeholders to the job record for the
# next pass to count. A no-op also breaks genuine adjacency between real tools, so
# a placeholder acts as a barrier: n-grams are mined only within the spans of real
# tool calls between them.
_NON_MOTOR_TOOLS = frozenset({"none", "", "?"})


def _is_motor_step(step: dict) -> bool:
    return str(step.get("tool") or "").strip().lower() not in _NON_MOTOR_TOOLS


def _is_error(result: str) -> bool:
    return result.startswith("[error]") or result.startswith("[blocked]")


def _arg_keys(step: dict) -> list[str]:
    return sorted((step.get("args") or {}).keys())


def _step_sig(step: dict) -> str:
    """Tool + arg-key shape (abstracts over arg *values*)."""
    return f"{step.get('tool', '?')}|{','.join(_arg_keys(step))}"


def _chunk_key(steps: list[dict]) -> str:
    return "→".join(_step_sig(s) for s in steps)


def _args_json(step: dict) -> str:
    return json.dumps(step.get("args") or {}, sort_keys=True)


def mine_chunks(jobs: list[dict]) -> dict:
    """Build the chunks.json structure from a list of job records.

    Pure and deterministic — recomputed from scratch each pass over the current
    jobs window (the jobs dir is capped, so counts are naturally self-correcting:
    a chunk that stops recurring rolls off and demotes).

    Each job record is expected to carry ``steps`` (list of {tool, args, ...}),
    ``results`` (parallel list of str), and an id (``job_id`` or ``id``).

    A chunk's success in a given job means *every step in the window* succeeded
    (not merely that the whole job did) — that is the unit we'd run ballistically.
    """
    # accum[key] = {
    #   "sequence": [{"tool","arg_keys"}...],
    #   "occurrences": int, "successes": int,
    #   "jobs": [job ids...],
    #   "arg_values": [set per position]  (only from successful occurrences)
    # }
    accum: dict[str, dict] = {}

    for job in jobs:
        steps = job.get("steps") or []
        results = job.get("results") or []
        job_id = job.get("job_id") or job.get("id") or ""
        n = len(steps)
        if n < _MIN_LEN:
            continue

        # Count each distinct chunk key at most once per job, so a job that
        # repeats a sub-sequence doesn't inflate its standing.
        seen_in_job: set[str] = set()
        for length in range(_MIN_LEN, _MAX_LEN + 1):
            for start in range(0, n - length + 1):
                window = steps[start : start + length]
                # Placeholders are a barrier — a window spanning one is not a
                # motor sequence and never becomes a chunk.
                if not all(_is_motor_step(s) for s in window):
                    continue
                key = _chunk_key(window)
                if key in seen_in_job:
                    continue
                seen_in_job.add(key)

                window_results = results[start : start + length]
                succeeded = len(window_results) == length and not any(
                    _is_error(r) for r in window_results
                )

                entry = accum.get(key)
                if entry is None:
                    entry = {
                        "sequence": [
                            {"tool": s.get("tool", "?"), "arg_keys": _arg_keys(s)} for s in window
                        ],
                        "occurrences": 0,
                        "successes": 0,
                        "jobs": [],
                        "arg_values": [set() for _ in window],
                    }
                    accum[key] = entry

                entry["occurrences"] += 1
                if job_id and job_id not in entry["jobs"]:
                    entry["jobs"].append(job_id)
                if succeeded:
                    entry["successes"] += 1
                    for i, s in enumerate(window):
                        entry["arg_values"][i].add(_args_json(s))

    # Collapse accumulators into the serialisable chunk records.
    chunks: dict[str, dict] = {}
    now = datetime.now(UTC).isoformat()
    for key, entry in accum.items():
        occ = entry["occurrences"]
        succ = entry["successes"]
        distinct_jobs = len(entry["jobs"])
        success_rate = (succ / occ) if occ else 0.0

        # Per-step invariance: a step is "invariant" (ballistic-eligible) only if
        # its concrete args were identical across every successful occurrence.
        sequence = []
        for i, step_sig in enumerate(entry["sequence"]):
            values = entry["arg_values"][i]
            invariant = len(values) == 1
            args = json.loads(next(iter(values))) if invariant else None
            sequence.append(
                {
                    "tool": step_sig["tool"],
                    "arg_keys": step_sig["arg_keys"],
                    "args": args,
                    "invariant": invariant,
                }
            )

        active = distinct_jobs >= _MIN_DISTINCT_JOBS and success_rate >= _MIN_SUCCESS_RATE
        chunks[key] = {
            "sequence": sequence,
            "occurrences": occ,
            "successes": succ,
            "distinct_jobs": distinct_jobs,
            "success_rate": round(success_rate, 3),
            "jobs": entry["jobs"][-_JOBS_RING:],
            "last_seen": now,
            "state": "active" if active else "candidate",
        }

    return {"chunks": chunks, "ts": now}


def fireable_chunk_count(chunks: dict) -> int:
    """How many ACTIVE chunks suggest_chunk could actually fire ballistically.

    Promotion (distinct_jobs + success_rate) makes a chunk ``active`` and eligible
    for before_plan() priming, but Phase-2 firing needs an INVARIANT tail: a chunk
    fires >=1 step only if some position after the first has identical concrete args
    across every successful occurrence (suggest_chunk stops at the first variable
    step). On this product's workload most active chunks are file-nav / search /
    query sequences whose paths and queries vary every run, so they promote but are
    inert for firing — real, but priming-only. Surfacing this count alongside the
    active count keeps that promotion-vs-firing gap visible in the mining log instead
    of a bare "N active" that reads as "N reflexes will fire".
    """
    n = 0
    for c in chunks.values():
        if c.get("state") != "active":
            continue
        seq = c.get("sequence", [])
        if any(
            seq[j].get("invariant") and seq[j].get("args") is not None
            for j in range(1, len(seq))
        ):
            n += 1
    return n


class _ChunkState:
    """One persona's runtime chunk state: loaded chunks + session-local
    suppression/reinforcement."""

    __slots__ = ("chunks", "mtime", "suppressed", "session_success")

    def __init__(self) -> None:
        self.chunks: dict[str, dict] = {}
        self.mtime: float = 0.0
        # Chunks that diverged this session are suppressed until the next sleep
        # pass re-derives their success rate from the (now lower-quality) history.
        self.suppressed: set[str] = set()
        # Session-local count of clean ballistic completions per chunk — live
        # reinforcement that biases ranking until the next mining pass folds the
        # successes into the durable counts.
        self.session_success: dict[str, int] = {}


class ChunkMemorySubsystem(MotorSubsystem):
    """Runtime consumer of the per-persona chunks.json files. Read-only; sleep
    owns the writes. One subsystem instance serves every persona: state is kept
    per persona and resolved from the binding at each access (jobs run inside
    bind_persona), so each persona is primed with ITS OWN automatized routines,
    not everyone's pooled habits."""

    def __init__(self) -> None:
        self._by_persona: dict[str, _ChunkState] = {}
        self._load()  # warm the home persona's state at boot

    def _state(self) -> _ChunkState:
        try:
            from brain.persona_key import persona_slug
            from brain.second_brain.store import active_persona

            key = persona_slug(active_persona() or "")
        except Exception:
            key = ""
        st = self._by_persona.get(key)
        if st is None:
            st = self._by_persona[key] = _ChunkState()
        return st

    def _chunks_path(self) -> Path:
        try:
            from brain.persona_key import persona_state_root
            from brain.second_brain.store import active_persona

            return persona_state_root(active_persona() or "") / "chunks.json"
        except Exception:
            return Path(
                os.environ.get(
                    "SECOND_BRAIN_PATH",
                    str(Path(__file__).parent.parent.parent / "second_brain"),
                )
            ) / "chunks.json"

    # Legacy attribute names — method bodies and tests address the ACTIVE
    # persona's state through these, exactly as before the per-persona split.
    @property
    def _chunks(self) -> dict[str, dict]:
        return self._state().chunks

    @_chunks.setter
    def _chunks(self, value: dict[str, dict]) -> None:
        self._state().chunks = value

    @property
    def _mtime(self) -> float:
        return self._state().mtime

    @_mtime.setter
    def _mtime(self, value: float) -> None:
        self._state().mtime = value

    @property
    def _suppressed(self) -> set[str]:
        return self._state().suppressed

    @_suppressed.setter
    def _suppressed(self, value: set[str]) -> None:
        self._state().suppressed = value

    @property
    def _session_success(self) -> dict[str, int]:
        return self._state().session_success

    @_session_success.setter
    def _session_success(self, value: dict[str, int]) -> None:
        self._state().session_success = value

    @property
    def name(self) -> str:
        return "chunk_memory"

    # ── loading ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            path = self._chunks_path()
            if not path.exists():
                return
            mtime = path.stat().st_mtime
            if mtime == self._mtime and self._chunks:
                return
            with open(path) as f:
                data = json.load(f)
            self._chunks = data.get("chunks", {}) or {}
            had_prior = self._mtime > 0.0
            self._mtime = mtime
            # A fresh mining pass re-derived every chunk's success rate from the
            # job history (which now includes any divergences), so session
            # suppression has served its purpose — let demotion do the gating.
            if had_prior and self._suppressed:
                logger.info(
                    "[ChunkMemory] New mining pass — lifting %d session suppression(s)",
                    len(self._suppressed),
                )
                self._suppressed.clear()
            n_active = sum(1 for c in self._chunks.values() if c.get("state") == "active")
            logger.info("[ChunkMemory] Loaded %d chunks (%d active)", len(self._chunks), n_active)
        except Exception as e:
            logger.warning("[ChunkMemory] Could not load chunks.json: %s", e)

    def _active_chunks(self) -> list[tuple[str, dict]]:
        self._load()  # cheap mtime check; picks up the latest sleep pass
        return [
            (k, c)
            for k, c in self._chunks.items()
            if c.get("state") == "active" and k not in self._suppressed
        ]

    # ── Phase 1: prime the planner ───────────────────────────────────────────

    async def before_plan(self, task_description: str, router: ModelRouter) -> str:
        active = self._active_chunks()
        if not active:
            return ""
        # Surface the most-exercised routines first; clean firings this session
        # count alongside the mined history.
        active.sort(
            key=lambda kc: kc[1].get("occurrences", 0) + self._session_success.get(kc[0], 0),
            reverse=True,
        )
        lines = ["Familiar tool routines (you often run these as a unit):"]
        for _, c in active[:_MAX_PRIMING]:
            seq = " → ".join(s.get("tool", "?") for s in c.get("sequence", []))
            lines.append(f"  {seq}  ({c.get('occurrences', 0)}× seen)")
        return "\n".join(lines)

    # ── Phase 2: ballistic firing ────────────────────────────────────────────

    async def suggest_chunk(self, recent_tools: list[str], last_args: dict) -> list[dict] | None:
        """If the just-executed tool tail matches the prefix of an active chunk,
        return the remaining *invariant* steps to fire ballistically (concrete
        args included). Stops at the first variable-arg step — a reflex has fixed
        parameters; anything context-dependent stays deliberate.

        Returns a list of {tool, args, reason} or None.
        """
        if not recent_tools:
            return None

        best: list[dict] | None = None
        best_matched = 0
        for key, c in self._active_chunks():
            seq = c.get("sequence", [])
            tools = [s.get("tool") for s in seq]
            # Longest prefix k (1 <= k < len) such that the recent tail ends with tools[:k].
            max_k = min(len(recent_tools), len(tools) - 1)
            for k in range(max_k, 0, -1):
                if recent_tools[-k:] != tools[:k]:
                    continue
                # Take invariant remaining steps, stopping at the first variable one.
                remaining: list[dict] = []
                for step in seq[k:]:
                    if not step.get("invariant") or step.get("args") is None:
                        break
                    remaining.append(
                        {
                            "tool": step["tool"],
                            "args": step["args"],
                            "reason": f"chunk:{key}",
                        }
                    )
                if remaining and k > best_matched:
                    best, best_matched = remaining, k
                break  # only the longest prefix for this chunk matters
        return best

    def suppress(self, reason_tag: str) -> None:
        """Stop firing a chunk until the next mining pass (reason_tag is 'chunk:<key>')."""
        if reason_tag.startswith("chunk:"):
            key = reason_tag.split("chunk:", 1)[1]
            if key and key not in self._suppressed:
                self._suppressed.add(key)
                self._session_success.pop(key, None)
                logger.info("[ChunkMemory] Suppressed diverged chunk for session: %s", key)

    def reinforce(self, reason_tag: str) -> None:
        """Record a clean ballistic completion (reason_tag is 'chunk:<key>') — the
        success side of the loop suppress() is the failure side of. Biases ranking
        live; the durable counts catch up at the next mining pass."""
        if reason_tag.startswith("chunk:"):
            key = reason_tag.split("chunk:", 1)[1]
            if key:
                self._session_success[key] = self._session_success.get(key, 0) + 1
