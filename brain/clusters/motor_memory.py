"""
MuscleMemorySubsystem — procedural memory for the motor cortex.

Records completed jobs and recalls similar prior procedures when planning.
Stored in a 'procedures' table in the same LanceDB database as episodic memory
— same embedding infrastructure, same vector search, no parallel system.

Procedural and episodic memory are distinct memory types (neuroscientifically
justified) but share underlying storage and retrieval machinery.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from brain.clusters.motor_subsystem import MotorSubsystem
from brain.model_router import ModelRouter

logger = logging.getLogger(__name__)

_EPISODES_DIR = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    str(Path(__file__).parent.parent.parent / "second_brain"),
)) / "episodes"

_EMBEDDING_DIM = 768
_SIMILARITY_THRESHOLD = 0.75
_OPEN_LOOP_THRESHOLD = 0.90  # similarity required to run a procedure without LLM planning
_OPEN_LOOP_MIN_USES = 2      # must have succeeded this many times before going open-loop
_MAX_RECALL = 3


class ProcedureStore:
    """LanceDB-backed procedural memory. One table alongside the episodes table."""

    def __init__(self) -> None:
        self._db = None
        self._table = None
        self._ready = False

    def _ensure_ready(self) -> bool:
        if self._ready:
            return True
        try:
            import lancedb
            import pyarrow as pa
            _EPISODES_DIR.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(_EPISODES_DIR))
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("goal", pa.string()),
                pa.field("steps", pa.string()),       # JSON
                pa.field("results", pa.string()),     # JSON
                pa.field("success", pa.bool_()),
                pa.field("recorded_at", pa.string()),
                pa.field("use_count", pa.int32()),
                pa.field("vector", pa.list_(pa.float32(), _EMBEDDING_DIM)),
            ])
            if "procedures" in self._db.table_names():
                self._table = self._db.open_table("procedures")
            else:
                self._table = self._db.create_table("procedures", schema=schema)
            self._ready = True
            return True
        except Exception as e:
            logger.warning("[MuscleMemory] LanceDB unavailable — procedures will not persist: %s", e)
            return False

    @staticmethod
    def _compute_signature(result: str) -> dict:
        """Derive a structural signature from a step result for use as a forward model."""
        is_error = result.startswith("[error]") or result.startswith("[blocked]")
        n = len(result)
        slack = max(n // 3, 50)
        return {
            "expected_success": not is_error,
            "length_min": max(0, n - slack),
            "length_max": n + slack,
            "is_empty": len(result.strip()) == 0,
        }

    def save(self, goal: str, steps: list[dict], results: list[str],
             success: bool, embedding: list[float]) -> None:
        if not self._ensure_ready():
            return
        try:
            # Embed a forward-model signature into each step so open-loop execution
            # can validate outcomes without a separate prediction call.
            annotated = []
            for i, step in enumerate(steps):
                s = dict(step)
                if i < len(results):
                    s["_sig"] = self._compute_signature(results[i])
                annotated.append(s)
            row = {
                "id": str(uuid.uuid4())[:8],
                "goal": goal,
                "steps": json.dumps(annotated),
                "results": json.dumps(results),
                "success": success,
                "recorded_at": datetime.now(UTC).isoformat(),
                "use_count": 0,
                "vector": embedding or ([0.0] * _EMBEDDING_DIM),
            }
            self._table.add([row])
            logger.info("[MuscleMemory] Recorded: %s (%d steps, success=%s)",
                        goal, len(steps), success)
        except Exception as e:
            logger.error("[MuscleMemory] Failed to save procedure: %s", e)

    def recall(self, query_vector: list[float], limit: int = _MAX_RECALL) -> list[dict]:
        if not self._ensure_ready():
            return []
        try:
            results = (
                self._table.search(query_vector)
                .metric("cosine")
                .limit(limit)
                .to_list()
            )
            matches = []
            for r in results:
                similarity = 1.0 - float(r.get("_distance", 1.0))
                if similarity < _SIMILARITY_THRESHOLD:
                    continue
                r["steps"] = json.loads(r.get("steps", "[]"))
                r["results"] = json.loads(r.get("results", "[]"))
                r["similarity"] = round(similarity, 3)
                matches.append(r)
            return matches
        except Exception as e:
            logger.debug("[MuscleMemory] Recall failed: %s", e)
            return []

    def increment_use_count(self, proc_id: str) -> None:
        if not self._ensure_ready():
            return
        with contextlib.suppress(Exception):
            self._table.update(
                where=f"id = '{proc_id}'",
                values={"use_count": self._table.search()
                        .where(f"id = '{proc_id}'")
                        .limit(1).to_list()[0].get("use_count", 0) + 1}
            )

    def reset_use_count(self, proc_id: str) -> None:
        """Reset use_count to 0 — drops procedure below open-loop threshold until re-validated."""
        if not self._ensure_ready() or not proc_id:
            return
        try:
            self._table.update(where=f"id = '{proc_id}'", values={"use_count": 0})
            logger.info("[MuscleMemory] Reset use_count for diverged procedure: %s", proc_id)
        except Exception:
            pass  # non-critical

    @property
    def count(self) -> int:
        if not self._ensure_ready():
            return 0
        try:
            return self._table.count_rows()
        except Exception:
            return 0


class MuscleMemorySubsystem(MotorSubsystem):

    def __init__(self) -> None:
        self._store = ProcedureStore()

    @property
    def name(self) -> str:
        return "muscle_memory"

    async def before_plan(self, task_description: str, router: ModelRouter) -> str:
        if not task_description:
            return ""
        embedding = await router.embed(task_description)
        if not embedding:
            return ""

        matches = self._store.recall(embedding)
        if not matches:
            return ""

        lines = ["Relevant prior procedures:"]
        for i, p in enumerate(matches, 1):
            outcome = "success" if p.get("success") else "partial/failed"
            step_names = " → ".join(s.get("tool", "?") for s in p.get("steps", []))
            lines.append(
                f"{i}. \"{p['goal']}\" "
                f"(similarity: {p['similarity']}, used {p.get('use_count', 0)}×, {outcome})\n"
                f"   Steps: {step_names}"
            )
            self._store.increment_use_count(p["id"])

        return "\n".join(lines)

    async def predict_outcome(
        self,
        tool: str,
        args: dict,
        prior_results: list[str],
        router: ModelRouter,
    ) -> dict | None:
        """Search all stored procedures for prior executions of this tool+args pattern.
        Returns the modal signature if enough examples exist, else None.

        This generalises across procedures — even novel tasks benefit from knowing
        what 'git status' or 'read_file' typically produces. As more procedures
        accumulate, predictions become more reliable.
        """
        if not tool or not self._store._ensure_ready():
            return None
        try:
            # Scan stored steps across all procedures for this tool
            all_rows = self._store._table.search().limit(200).to_list()
            matching_sigs: list[dict] = []
            for row in all_rows:
                steps = json.loads(row.get("steps", "[]"))
                for step in steps:
                    if step.get("tool") != tool:
                        continue
                    # Loose args match: same top-level keys
                    if set(step.get("args", {}).keys()) != set(args.keys()):
                        continue
                    sig = step.get("_sig")
                    if sig:
                        matching_sigs.append(sig)
            if len(matching_sigs) < 2:
                # Not enough evidence to make a reliable prediction
                return None
            # Modal prediction: majority vote on success, average on lengths
            success_votes = sum(1 for s in matching_sigs if s.get("expected_success", True))
            expected_success = success_votes > len(matching_sigs) / 2
            length_mins = [s.get("length_min", 0) for s in matching_sigs]
            length_maxes = [s.get("length_max", 1000) for s in matching_sigs]
            logger.debug("[MuscleMemory] predict_outcome: %s — %d examples, success=%.0f%%",
                         tool, len(matching_sigs), 100 * success_votes / len(matching_sigs))
            return {
                "expected_success": expected_success,
                "length_min": int(sum(length_mins) / len(length_mins)),
                "length_max": int(sum(length_maxes) / len(length_maxes)),
                "is_empty": sum(1 for s in matching_sigs if s.get("is_empty")) > len(matching_sigs) / 2,
                "sample_count": len(matching_sigs),
            }
        except Exception as e:
            logger.debug("[MuscleMemory] predict_outcome failed: %s", e)
            return None

    async def recall_procedure(
        self, task: str, router: ModelRouter
    ) -> tuple[dict | None, float]:
        """Return the best matching procedure if it meets the open-loop threshold."""
        if not task:
            return None, 0.0
        embedding = await router.embed(task)
        if not embedding:
            return None, 0.0
        matches = self._store.recall(embedding, limit=1)
        if not matches:
            return None, 0.0
        best = matches[0]
        similarity = best.get("similarity", 0.0)
        use_count = best.get("use_count", 0)
        if similarity >= _OPEN_LOOP_THRESHOLD and use_count >= _OPEN_LOOP_MIN_USES:
            logger.info("[MuscleMemory] Open-loop candidate: %s (sim=%.3f, uses=%d)",
                        best.get("goal", "")[:60], similarity, use_count)
            return best, similarity
        return None, similarity

    def mark_diverged(self, proc_id: str) -> None:
        """Called when open-loop execution produced unexpected results.
        Resets use_count so the procedure must be re-validated before going open-loop again."""
        self._store.reset_use_count(proc_id)

    async def after_job(
        self,
        goal: str,
        steps: list[dict],
        results: list[str],
        success: bool,
        router: ModelRouter | None = None,
    ) -> None:
        if not goal or not steps:
            return
        embedding: list[float] = []
        if router:
            vec = await router.embed(goal)
            if vec:
                embedding = vec
        self._store.save(goal, steps, results, success, embedding)
