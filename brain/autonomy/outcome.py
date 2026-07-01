"""
JobOutcome / JobState — the terminal-state model for an autonomous job.

Replaces the old `success = productive_steps > 0` boolean (and the ad-hoc dicts
returned from `execute_internal_job`). Every job path constructs exactly one
`JobOutcome` via a factory classmethod, each of which guarantees a non-empty
`reason_human` and a non-empty `summary` — so no path can return a silent
empty-success. The `.success` property preserves every existing caller that reads
`summary.get("success")`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from brain.autonomy.reasons import DeferReason, StopReason


class JobState(str, Enum):
    RUNNING = "running"                      # in-progress checkpoint (partial results persisted)
    COMPLETED = "completed"                  # produced real, verified-enough work
    DEFERRED = "deferred"                    # paused; will resume (carries a DeferReason)
    AWAITING_APPROVAL = "awaiting_approval"  # external side-effect pending owner approval
    FAILED = "failed"                        # ran but produced nothing usable (carries a reason)
    STOPPED_BUDGET = "stopped_budget"        # hard budget cap; not retried today


@dataclass
class JobOutcome:
    state: JobState
    job_id: str = ""
    goal: str = ""
    reason_code: str = ""           # machine token (DeferReason/StopReason/failure class)
    reason_human: str = ""          # ALWAYS non-empty owner-facing sentence
    summary: str = ""               # ALWAYS non-empty (falls back to reason_human)
    productive_steps: int = 0
    steps: list = field(default_factory=list)
    results: list = field(default_factory=list)
    plan_steps: list = field(default_factory=list)
    source_links: list = field(default_factory=list)
    stories_completed: int = 0
    stories_total: int = 0
    backoff_s: float = 0.0
    clarification: str | None = None
    extra: dict = field(default_factory=dict)  # complexity, attempts, predictions, verification…

    # ── back-compat shim ─────────────────────────────────────────────────────
    @property
    def success(self) -> bool:
        """True only for COMPLETED — keeps every existing `summary.get('success')`
        reader working while the state model rolls out."""
        return self.state is JobState.COMPLETED

    def __post_init__(self) -> None:
        # Structural guarantee: never an empty reason or summary on any terminal path.
        if not self.reason_human:
            self.reason_human = self.reason_code or self.state.value.replace("_", " ")
        if not self.summary:
            self.summary = self.reason_human

    # ── factory constructors (one per terminal path) ─────────────────────────
    @classmethod
    def completed(cls, job_id: str, goal: str, *, productive_steps: int, summary: str, **kw) -> "JobOutcome":
        """COMPLETED requires real work AND a non-empty summary. If either is missing
        this coerces to FAILED('no_productive_output') — so a `"(no output)"` path can
        never masquerade as success."""
        if productive_steps <= 0 or not (summary or "").strip():
            return cls.failed(
                job_id, goal,
                reason_code="no_productive_output",
                reason_human="The job ran but produced no usable output.",
                productive_steps=productive_steps, **kw,
            )
        return cls(
            state=JobState.COMPLETED, job_id=job_id, goal=goal,
            reason_code="completed", reason_human="Completed.",
            summary=summary, productive_steps=productive_steps, **kw,
        )

    @classmethod
    def failed(cls, job_id: str, goal: str, *, reason_code: str, reason_human: str, **kw) -> "JobOutcome":
        return cls(
            state=JobState.FAILED, job_id=job_id, goal=goal,
            reason_code=reason_code, reason_human=reason_human, **kw,
        )

    @classmethod
    def deferred(cls, job_id: str, goal: str, *, reason: DeferReason, backoff_s: float = 0.0, **kw) -> "JobOutcome":
        return cls(
            state=JobState.DEFERRED, job_id=job_id, goal=goal,
            reason_code=reason.value, reason_human=reason.human(),
            backoff_s=backoff_s, **kw,
        )

    @classmethod
    def stopped_budget(cls, job_id: str, goal: str, *, reason: StopReason = StopReason.BUDGET_HARD_STOP, **kw) -> "JobOutcome":
        return cls(
            state=JobState.STOPPED_BUDGET, job_id=job_id, goal=goal,
            reason_code=reason.value, reason_human=reason.human(), **kw,
        )

    @classmethod
    def awaiting_approval(cls, job_id: str, goal: str, *, reason_human: str, clarification: str | None = None, **kw) -> "JobOutcome":
        return cls(
            state=JobState.AWAITING_APPROVAL, job_id=job_id, goal=goal,
            reason_code="awaiting_approval", reason_human=reason_human,
            clarification=clarification, **kw,
        )

    # ── persistence projection ───────────────────────────────────────────────
    def to_record(self) -> dict:
        """Flat dict for JobStore.save and the agent_jobs table upsert."""
        return {
            "job_id": self.job_id,
            "goal": self.goal,
            "state": self.state.value,
            "success": self.success,
            "reason_code": self.reason_code,
            "reason_human": self.reason_human,
            "summary": self.summary,
            "productive_steps": self.productive_steps,
            "steps": self.steps,
            "results": self.results,
            "plan_steps": self.plan_steps,
            "source_links": self.source_links,
            "stories_completed": self.stories_completed,
            "stories_total": self.stories_total,
            "backoff_s": self.backoff_s,
            "clarification": self.clarification,
            **self.extra,
        }
