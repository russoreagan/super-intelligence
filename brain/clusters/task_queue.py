"""
PersistentTaskQueue — disk-backed queue for autonomous and user-directed tasks.

Tasks survive page refreshes and system restarts. On boot, any task left in
state 'pending' or 'running' (interrupted mid-execution) is recovered and
re-queued with priority 0 (highest) so the brain picks up where it left off.

The queue is a flat JSON file next to the schema markdown files. Writes are
atomic (temp-file → rename) to prevent corruption on hard shutdown.

Sources:
  user       — the user explicitly asked and is awaiting the result (task-mode
               turns). Bypasses the autonomy rate caps and the spend gate.
  commitment — extracted by FollowThrough from a commitment the assistant
               volunteered in its OWN reply on a non-task turn ("I'll look into
               that"). Self-directed in spirit: subject to the autonomy rate
               caps, the spend/risk gate, and self-task dedup — the 2026-07-03
               debate incident showed these enqueues cascading uncapped.
  self       — self-initiated by the DMN based on memory / idle reasoning
  recovery   — re-queued at boot from an interrupted previous session
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

Status = Literal["pending", "running", "completed", "failed", "blocked", "deferred"]
Source = Literal["user", "commitment", "self", "recovery"]

# Cap total stored tasks (completed + failed entries are trimmed when over limit)
MAX_TASKS = 40
# Word-overlap threshold for deduplication of pending/running tasks
DEDUP_THRESHOLD = 0.70
# Stricter threshold applied to self-initiated tasks (DMN) against recent completions
SELF_DEDUP_THRESHOLD = 0.55
# How many seconds back to check completed tasks when deduplicating self-tasks
SELF_DEDUP_RECENCY = 2 * 60 * 60  # 2 hours
# Boot-recovery ceiling. A task left in pending/running at boot was interrupted
# mid-run and is re-queued by recover_interrupted(). But a job that HARD-crashes or
# wedges the pod mid-execution (so it never marks itself failed) would otherwise be
# re-run on EVERY boot forever — a crash-loop bounded only by the daily cloud-USD cap.
# After this many automatic recoveries we QUARANTINE the task (mark it failed) instead
# of re-running it, so a poison job stops itself. One retry is the cost/safety balance:
# a genuine pod blip gets its second chance, while a poison job buys at most one extra
# run of cloud spend (three re-runs of a spendy job proved too generous). Override via
# BRAIN_MAX_JOB_RECOVERIES.
MAX_RECOVERY_ATTEMPTS = int(os.environ.get("BRAIN_MAX_JOB_RECOVERIES", "1"))


@dataclass
class Task:
    id: str
    goal: str
    status: Status = "pending"
    source: Source = "user"
    priority: int = 1  # lower number = higher priority
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    success: bool | None = None
    job_id: str | None = None  # linked JobStore entry (set when execution starts)
    # Reflex chain depth: 0 for user/DMN-originated work; a job whose completion
    # seeds a reflection that spawns a follow-up task tags that follow-up depth+1,
    # so the result→reasoning→act loop is bounded (see DMN.note_job_result).
    reflex_depth: int = 0
    # Boot-recovery counter: how many times recover_interrupted() has re-queued this
    # task after an interrupted run. Capped at MAX_RECOVERY_ATTEMPTS, after which the
    # task is quarantined (status='failed') instead of re-run — so a job that crashes
    # the pod mid-execution can't re-run on every boot indefinitely. Defaults to 0 for
    # entries written before this field existed (see from_dict).
    recovery_count: int = 0
    # Deferral backoff: when a job DEFERS (autonomous cloud unavailable — soft-budget
    # pause / rate / cloud-unreachable), it is parked as status='deferred' with a
    # not_before wall-clock time. take_next() auto-promotes it back to 'pending' once
    # due; before that it reads as idle (not busy-work). Default 0.0 = due immediately.
    not_before: float = 0.0
    # Routing origin: the lane this task descended from, captured at enqueue time
    # from the turn context (brain.turn_ctx). When a job is deferred DURING an
    # agent-lane turn, it carries that agent so its later execution can run on the
    # same lane — its events, result delivery, and reflection are attributed to
    # (and observable as) that agent's self-directed work. Empty / "owner" → the
    # brain's OWN idle reasoning, which stays on the owner lane (its private life).
    origin_channel: str = "owner"
    origin_session_id: str = ""
    origin_agent_id: str = ""
    origin_end_user_id: str = ""
    # Job-scope approval grant (approvals.grant_for): set when this task is the
    # re-queue of a user-approved action, so the whole re-run is pre-authorized —
    # one approval clears the task instead of one approval per action.
    approval_token: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
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
        Called once at boot. Re-queues tasks that were pending or running when the
        brain last shut down (interrupted mid-execution) as pending, priority 0,
        source 'recovery' — so the brain picks up where it left off.

        A task that has already been recovered MAX_RECOVERY_ATTEMPTS times is
        QUARANTINED instead (status='failed'): a job that hard-crashes or wedges the
        pod mid-run would otherwise re-run on every boot forever, bounded only by the
        daily cloud-USD cap. Quarantining converts that crash-loop into a clean
        give-up. Returns only the tasks actually re-queued (quarantined ones excluded).
        """
        # Blocked tasks are intentionally excluded — they need user input, not retry.
        interrupted = [t for t in self._tasks if t.status in ("pending", "running")]
        if not interrupted:
            return []
        recovered: list[Task] = []
        quarantined: list[Task] = []
        for task in interrupted:
            if task.recovery_count >= MAX_RECOVERY_ATTEMPTS:
                # Exhausted its recovery budget — stop re-running it. Marking it failed
                # lets the MAX_TASKS trim eventually drop it, and the self-dedup recency
                # window suppresses an immediate identical re-spawn by the DMN.
                task.status = "failed"
                task.success = False
                task.completed_at = time.time()
                quarantined.append(task)
                continue
            task.recovery_count += 1
            task.status = "pending"
            task.source = "recovery"
            task.priority = 0
            task.started_at = None
            recovered.append(task)
        self._save()
        if quarantined:
            logger.warning(
                "[TaskQueue] Quarantined %d task(s) past the %d-recovery cap "
                "(probable crash-loop): %s",
                len(quarantined),
                MAX_RECOVERY_ATTEMPTS,
                "; ".join(t.goal[:60] for t in quarantined),
            )
        if recovered:
            logger.info(
                "[TaskQueue] Recovered %d interrupted task(s) from previous session",
                len(recovered),
            )
        return recovered

    # ── Queue operations ──────────────────────────────────────────────────────

    def enqueue(
        self,
        goal: str,
        source: Source = "user",
        priority: int = 1,
        reflex_depth: int = 0,
        approval_token: str = "",
    ) -> Task | None:
        """
        Add a task. Returns the new Task, or None if it was deduplicated.
        Trims oldest completed/failed entries when over MAX_TASKS.
        """
        goal = goal.strip()
        if not goal:
            return None
        # Deduplicate against pending/running tasks
        for t in self._tasks:
            if (
                t.status in ("pending", "running")
                and _word_overlap(t.goal, goal) >= DEDUP_THRESHOLD
            ):
                logger.debug("[TaskQueue] Deduplicated task (overlap): %r", goal[:60])
                return None
        # For self-initiated tasks: also deduplicate against recently completed/failed
        # tasks within the recency window. Prevents the DMN from re-running work it
        # just finished because it rephrased the goal slightly on the next tick.
        # Commitment tasks get the same treatment — a repetitive conversation (e.g.
        # debate rounds) re-extracts near-identical goals turn after turn.
        if source in ("self", "commitment"):
            cutoff = time.time() - SELF_DEDUP_RECENCY
            for t in self._tasks:
                if (
                    t.status in ("completed", "failed")
                    and t.completed_at is not None
                    and t.completed_at >= cutoff
                    and _word_overlap(t.goal, goal) >= SELF_DEDUP_THRESHOLD
                ):
                    logger.info(
                        "[TaskQueue] Self-task deduplicated against recent completion "
                        "(overlap=%.2f): %r",
                        _word_overlap(t.goal, goal),
                        goal[:60],
                    )
                    return None
        # Capture the lane this task is being deferred from. A job spawned during an
        # agent-lane turn (the partner/agent path) is tagged with that agent so its
        # eventual execution runs bound to the same lane; the brain's own idle work
        # enqueues on the owner lane and carries no agent identity.
        from brain.turn_ctx import current_turn

        octx = current_turn()
        _is_agent = octx.get("channel") == "agent"
        task = Task(
            id=str(uuid.uuid4())[:8],
            goal=goal,
            source=source,
            priority=priority,
            reflex_depth=reflex_depth,
            origin_channel="agent" if _is_agent else "owner",
            origin_session_id=octx.get("session_id", "") if _is_agent else "",
            origin_agent_id=octx.get("agent_id", "") if _is_agent else "",
            origin_end_user_id=octx.get("end_user_id", "") if _is_agent else "",
            approval_token=approval_token,
        )
        self._tasks.append(task)
        # Trim oldest completed/failed if over limit
        if len(self._tasks) > MAX_TASKS:
            for i, t in enumerate(self._tasks):
                if t.status in ("completed", "failed"):
                    self._tasks.pop(i)
                    break
        self._save()
        logger.info(
            "[TaskQueue] Enqueued [%s] source=%s priority=%d: %s",
            task.id,
            source,
            priority,
            goal[:80],
        )
        return task

    def _promote_due_deferred(self) -> None:
        """Return any deferred task whose backoff has elapsed to the pending pool."""
        now = time.time()
        promoted = 0
        for t in self._tasks:
            if t.status == "deferred" and t.not_before <= now:
                t.status = "pending"
                t.not_before = 0.0
                promoted += 1
        if promoted:
            self._save()
            logger.info("[TaskQueue] Promoted %d due deferred task(s) → pending", promoted)

    def take_next(self) -> Task | None:
        """
        Pop the highest-priority pending task, marking it 'running'.
        Returns None if the queue is empty. Deferred tasks whose backoff has elapsed
        are promoted to pending first, so a requeued (deferred) job resumes when due.
        """
        self._promote_due_deferred()
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
        logger.info(
            "[TaskQueue] Starting task [%s] source=%s: %s", task.id, task.source, task.goal[:80]
        )
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

    def mark_deferred(self, task_id: str, backoff_s: float = 30.0, reason: str = "") -> None:
        """Park a running task as deferred with an exponential-ish backoff. The task
        keeps its job_id so it RESUMES from its last checkpoint when promoted. Unlike
        'blocked' (awaiting the user) a deferred task auto-promotes once not_before
        elapses — no user action needed. Excluded from has_pending() until due."""
        now = time.time()
        for t in self._tasks:
            if t.id == task_id:
                # Compound the backoff on repeated defers so a persistently-unreachable
                # cloud parks the queue instead of spinning (bounded to ~1h).
                prior = 2 if t.status == "deferred" else 1
                t.status = "deferred"
                t.not_before = now + min(3600.0, max(1.0, backoff_s) * prior)
                t.started_at = None
                self._save()
                logger.info(
                    "[TaskQueue] Task [%s] deferred %.0fs (%s)",
                    task_id,
                    t.not_before - now,
                    reason[:60],
                )
                return
        logger.warning("[TaskQueue] mark_deferred: task %r not found", task_id)

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

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending or blocked task. Running tasks cannot be cancelled mid-execution.
        Returns True if the task was found and cancelled."""
        for t in self._tasks:
            if t.id == task_id and t.status in ("pending", "blocked"):
                t.status = "failed"
                t.completed_at = time.time()
                t.success = False
                self._save()
                logger.info("[TaskQueue] Task [%s] cancelled", task_id)
                return True
        return False

    def clear_all(self) -> int:
        """Kill switch for the Self-directed work panel: fail every pending /
        blocked / running task. The running task's asyncio execution must be
        cancelled separately by the caller — this only settles the ledger.
        Returns the number of tasks cleared."""
        cleared = 0
        for t in self._tasks:
            if t.status in ("pending", "blocked", "running"):
                t.status = "failed"
                t.completed_at = time.time()
                t.success = False
                cleared += 1
        if cleared:
            self._save()
            logger.info("[TaskQueue] Cleared %d task(s) (user kill switch)", cleared)
        return cleared

    def update_goal(self, task_id: str, new_goal: str) -> bool:
        """Update the goal of a pending task. Returns True if found and updated."""
        new_goal = new_goal.strip()
        if not new_goal:
            return False
        for t in self._tasks:
            if t.id == task_id and t.status == "pending":
                t.goal = new_goal
                self._save()
                logger.info("[TaskQueue] Task [%s] goal updated: %s", task_id, new_goal[:80])
                return True
        return False

    def all_tasks(self) -> list[Task]:
        """Return all tasks (any status), newest first."""
        return list(reversed(self._tasks))

    # ── Introspection ─────────────────────────────────────────────────────────

    def _is_ready(self, t: Task, now: float) -> bool:
        """Pending, or a deferred task whose backoff has elapsed (ready to resume).
        A not-yet-due deferred task reads as idle so the brain doesn't busy-wait on it."""
        return t.status == "pending" or (t.status == "deferred" and t.not_before <= now)

    def has_pending(self) -> bool:
        now = time.time()
        return any(self._is_ready(t, now) for t in self._tasks)

    def pending_count(self) -> int:
        now = time.time()
        return sum(1 for t in self._tasks if self._is_ready(t, now))

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
