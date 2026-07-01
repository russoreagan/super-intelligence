"""
Machine-readable + human-facing reasons for every autonomy decision.

Replaces the scattered log-string reasons ("routing to local", "budget reached")
with typed enums whose `.human()` returns owner-facing copy. The `reason_code`
persisted on a job record is the enum value; the `reason_human` shown to the owner
is `.human()`.
"""

from __future__ import annotations

from enum import Enum


class RunOutcome(str, Enum):
    """What the gate decided to do with a job or a step."""

    RUN = "run"      # proceed on cloud
    DEFER = "defer"  # requeue with backoff; carries a DeferReason
    ASK = "ask"      # external side-effect → route to the approval ledger
    STOP = "stop"    # hard budget; do not requeue today


class DeferReason(str, Enum):
    """Why an autonomous job was paused instead of run (all recoverable)."""

    BUDGET_SOFT_PAUSE = "budget_soft_pause"   # soft cap hit; awaiting owner 'continue'
    RATE_BUCKET_EMPTY = "rate_bucket_empty"   # background cloud token bucket <= 0
    CLOUD_UNREACHABLE = "cloud_unreachable"   # repeated cloud timeouts / transport down

    def human(self) -> str:
        return {
            DeferReason.BUDGET_SOFT_PAUSE: (
                "Paused autonomous work — reached today's soft spend limit. "
                "Approve to keep going, or it resumes tomorrow."
            ),
            DeferReason.RATE_BUCKET_EMPTY: (
                "Paused briefly — background cloud rate limit reached; will resume shortly."
            ),
            DeferReason.CLOUD_UNREACHABLE: (
                "Paused — the cloud model is temporarily unreachable; will retry."
            ),
        }[self]


class StopReason(str, Enum):
    """Why an autonomous job was stopped outright (not retried today)."""

    BUDGET_HARD_STOP = "budget_hard_stop"

    def human(self) -> str:
        return {
            StopReason.BUDGET_HARD_STOP: (
                "Stopped autonomous work — reached today's hard spend limit. It resumes tomorrow."
            ),
        }[self]
