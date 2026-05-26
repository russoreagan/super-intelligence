"""
PersistentTaskQueue — disk-backed queue for autonomous and user-directed tasks.

Tasks survive page refreshes and system restarts. On boot, any task left in
state 'pending' or 'running' (interrupted mid-execution) is recovered and
re-queued with priority 0 (highest) so the brain picks up where it left off.

The queue is a flat JSON file next to the schema markdown files. Writes are
atomic (temp-file → rename) to prevent corruption on hard shutdown.

Sources:
  user      — extracted from a spoken commitment by FollowThrough
  self      — self-initiated by the DMN based on memory / idle reasoning
  recovery  — re-queued at boot from an interrupted previous session
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from brain.second_brain.store import SECOND_BRAIN_ROOT

logger = logging.getLogger(__name__)

TASK_QUEUE_PATH = SECOND_BRAIN_ROOT / "task_queue.json"

Status = Literal["pending", "running", "completed", "failed", "blocked"]
Source = Literal["user", "self", "recovery"]

# Cap total stored tasks (completed + failed entries are trimmed when over limit)
MAX_TASKS = 40
# Rough word-overlap threshold for deduplication of pending tasks
DEDUP_THRESHOLD = 0.70


@dataclass
class Task:
    id: str
    goal: str
    status: Status = "pending"
    source: Source = "user"
    priority: int = 1        # lower number = higher priority
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    success: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class PersistentTaskQueue:
    """
    Disk-backed FIFO task queue with priority, deduplication, and boot recovery.

    Not thread-safe — designed for single-threaded asyncio use. All public
    methods are synchronous (disk I/O is fast enough for a small JSON file);
    callers that need async behaviour can wrap with run_in_executor if needed.
    """

    def __init__(self) -> None:
        self._tasks: list[Task] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if TASK_QUEUE_PATH.exists():
                raw = json.loads(TASK_QUEUE_PATH.read_text())
                self._tasks = [Task.from_dict(t) for t in raw]
                logger.debug("[TaskQueue] Loaded %d task(s) from disk", len(self._tasks))
        except Exception as e:
            logger.warning("[TaskQueue] Failed to load — starting empty: %s", e)
            self._tasks = []

    def _save(self) -> None:
        try:
            TASK_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = TASK_QUEUE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([t.to_dict() for t in self._tasks], indent=2))
            os.replace(tmp, TASK_QUEUE_PATH)
        except Exception as e:
            logger.warning("[TaskQueue] Failed to save: %s", e)

    # ── Boot recovery ─────────────────────────────────────────────────────────

    def recover_interrupted(self) -> list[Task]:
        """
        Called once at boot. Returns any tasks that were pending or running
        when the brain last shut down, re-marking them as pending with
        priority 0 and source 'recovery'.
        """
        # Blocked tasks are intentionally excluded — they need user input, not retry.
        interrupted = [t for t in self._tasks if t.status in ("pending", "running")]
        if not interrupted:
            return []
        for task in interrupted:
            task.status = "pending"
            task.source = "recovery"
            task.priority = 0
            task.started_at = None
        self._save()
        logger.info("[TaskQueue] Recovered %d interrupted task(s) from previous session",
                    len(interrupted))
        return interrupted

    # ── Queue operations ──────────────────────────────────────────────────────

    def enqueue(self, goal: str, source: Source = "user", priority: int = 1) -> Task | None:
        """
        Add a task. Returns the new Task, or None if it was deduplicated.
        Trims oldest completed/failed entries when over MAX_TASKS.
        """
        goal = goal.strip()
        if not goal:
            return None
        # Deduplicate against pending/running tasks
        for t in self._tasks:
            if t.status in ("pending", "running") and _word_overlap(t.goal, goal) >= DEDUP_THRESHOLD:
                logger.debug("[TaskQueue] Deduplicated task (overlap): %r", goal[:60])
                return None
        task = Task(id=str(uuid.uuid4())[:8], goal=goal, source=source, priority=priority)
        self._tasks.append(task)
        # Trim oldest completed/failed if over limit
        if len(self._tasks) > MAX_TASKS:
            for i, t in enumerate(self._tasks):
                if t.status in ("completed", "failed"):
                    self._tasks.pop(i)
                    break
        self._save()
        logger.info("[TaskQueue] Enqueued [%s] source=%s priority=%d: %s",
                    task.id, source, priority, goal[:80])
        return task

    def take_next(self) -> Task | None:
        """
        Pop the highest-priority pending task, marking it 'running'.
        Returns None if the queue is empty.
        """
        pending = sorted(
            [t for t in self._tasks if t.status == "pending"],
            key=lambda t: (t.priority, t.created_at),
        )
        if not pending:
            return None
        task = pending[0]
        task.status = "running"
        task.started_at = time.time()
        self._save()
        logger.info("[TaskQueue] Starting task [%s] source=%s: %s",
                    task.id, task.source, task.goal[:80])
        return task

    def mark_done(self, task_id: str, success: bool) -> None:
        """Mark a running task as completed or failed."""
        for t in self._tasks:
            if t.id == task_id:
                t.status = "completed" if success else "failed"
                t.completed_at = time.time()
                t.success = success
                self._save()
                logger.info("[TaskQueue] Task [%s] → %s", task_id, t.status)
                return
        logger.warning("[TaskQueue] mark_done: task %r not found", task_id)

    def mark_blocked(self, task_id: str, reason: str = "") -> None:
        """Park a task as blocked — waiting for user input before it can continue.
        Blocked tasks are preserved (not failed) and excluded from has_pending()
        so the brain treats them as idle, not as work to retry."""
        for t in self._tasks:
            if t.id == task_id:
                t.status = "blocked"
                # Store the blocking question/reason in the goal so it's visible
                # in the task list and can be shown to the user.
                if reason and reason not in t.goal:
                    t.goal = f"{t.goal}\n[BLOCKED: {reason}]"
                self._save()
                logger.info("[TaskQueue] Task [%s] blocked: %s", task_id, reason[:80])
                return
        logger.warning("[TaskQueue] mark_blocked: task %r not found", task_id)

    def unblock(self, task_id: str) -> bool:
        """Re-queue a blocked task as pending so it runs on the next idle cycle.
        Returns True if the task was found and unblocked."""
        for t in self._tasks:
            if t.id == task_id and t.status == "blocked":
                # Strip the [BLOCKED: ...] annotation before re-running
                import re
                t.goal = re.sub(r"\n\[BLOCKED:.*?\]$", "", t.goal, flags=re.DOTALL).strip()
                t.status = "pending"
                t.started_at = None
                self._save()
                logger.info("[TaskQueue] Task [%s] unblocked and re-queued", task_id)
                return True
        return False

    # ── Introspection ─────────────────────────────────────────────────────────

    def has_pending(self) -> bool:
        return any(t.status == "pending" for t in self._tasks)

    def pending_count(self) -> int:
        return sum(1 for t in self._tasks if t.status == "pending")

    def is_running(self) -> bool:
        return any(t.status == "running" for t in self._tasks)

    def pending_summary(self) -> str:
        """One-line summary of pending tasks for logging / DMN context."""
        pending = [t for t in self._tasks if t.status == "pending"]
        if not pending:
            return "no pending tasks"
        return "; ".join(f"[{t.id}] {t.goal[:60]}" for t in pending[:3])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _word_overlap(a: str, b: str) -> float:
    """Symmetric word-set overlap in [0, 1]."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))
