"""
SpendRiskGate — the single policy point for autonomous work.

Consulted by the motor cortex (before a job plans), the CMA executor (before a
managed-agent run and per tool call), and the DMN. Owns both axes:

  Axis A — check_spend():   can this reach cloud right now?
           HARD_EXCEEDED → STOP(budget) │ SOFT paused → DEFER(soft) + record one
           continue-approval │ cloud unreachable → DEFER(cloud) │ bg bucket empty →
           DEFER(rate) │ else RUN.
  Axis B — classify_action(): is this action safe to run unattended?
           external side-effect (comms-out / money movement / outbound API+network
           write leaving the tenant) → ASK; internal read / sandboxed FS write /
           analysis → RUN.

Cloud-health: the router feeds per-call background timeouts here; after
`bg_cloud_timeout_trip` consecutive timeouts the gate parks autonomous work
(CLOUD_UNREACHABLE) for a cooldown instead of hammering the endpoint. Any success
resets the streak.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

from brain.autonomy.budget import AutonomousBudget, BudgetTier
from brain.autonomy.reasons import DeferReason, RunOutcome, StopReason
from brain.settings import settings as _settings

# Sentinel tool name for the owner-level "approve to keep spending today" item. The
# approval ledger stores arbitrary tool names, so the soft-pause flow needs no schema
# or UI change — it rides the same record→approve rails as any action approval.
CONTINUE_SPEND_TOOL = "__continue_autonomous_spend__"

# External side-effect verbs: an action whose name contains one of these AND is a
# mutation (not a read) leaves the tenant, so it needs owner approval. Mirrors the
# comms/money word-lists the CMA executor used, unified in one place.
_EXTERNAL_VERBS = (
    "send",
    "email",
    "mail",
    "message",
    "sms",
    "text",
    "call",
    "notify",
    "dm",
    "post",
    "tweet",
    "publish",
    "share",
    "reply",
    "comment",
    "order",
    "trade",
    "buy",
    "sell",
    "transfer",
    "pay",
    "payment",
    "invoice",
    "checkout",
    "purchase",
    "wire",
    "withdraw",
    "deposit",
    "webhook",
    # Irreversible destructive actions are gated too (align with cma_executor._classify_action).
    "delete",
    "remove",
    "destroy",
    "wipe",
    "purge",
    "drop",
    "erase",
)
# Read verbs that are always safe even if a side-effect verb also appears.
_READ_HINTS = ("get_", "list_", "search_", "read_", "fetch_", "query_", "lookup", "view_", "show_")
# Motor's own tools are internal by construction (sandboxed FS / shell / local grounding).
_INTERNAL_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "append_file",
        "list_files",
        "search_files",
        "run_command",
        "set_mood",
        "recall_memory",
        "recall_jobs",
        "analyze_image",
        "none",
        "world_geocode",
        "world_places",
        "world_directions",
        "world_weather",
        "world_air_quality",
        "world_timezone",
        "fetch_url",
        "query_langfuse",
    }
)


@dataclass
class GateDecision:
    outcome: RunOutcome
    defer_reason: DeferReason | None = None
    stop_reason: StopReason | None = None
    ask_reason: str = ""
    reason_human: str = ""
    backoff_s: float = 0.0

    @property
    def is_run(self) -> bool:
        return self.outcome is RunOutcome.RUN


class SpendRiskGate:
    def __init__(self, budget: AutonomousBudget, approvals, router) -> None:
        self._budget = budget
        self._approvals = approvals
        self._router = router
        self._timeout_streak = 0
        self._unreachable_until = 0.0

    # ── cloud-health signal (fed by the router) ──────────────────────────────
    def note_cloud_timeout(self) -> None:
        self._timeout_streak += 1
        trip = int(_settings.get("bg_cloud_timeout_trip") or 3)
        if self._timeout_streak >= trip:
            cooldown = float(_settings.get("cloud_unreachable_cooldown_s") or 120.0)
            self._unreachable_until = time.monotonic() + cooldown

    def note_cloud_success(self) -> None:
        self._timeout_streak = 0
        self._unreachable_until = 0.0

    def cloud_unreachable(self) -> bool:
        return time.monotonic() < self._unreachable_until

    # ── Axis A: can this reach cloud right now? ───────────────────────────────
    def check_spend(self) -> GateDecision:
        tier = self._budget.tier()
        if tier is BudgetTier.HARD_EXCEEDED:
            return GateDecision(
                RunOutcome.STOP,
                stop_reason=StopReason.BUDGET_HARD_STOP,
                reason_human=StopReason.BUDGET_HARD_STOP.human(),
            )
        if tier is BudgetTier.SOFT_EXCEEDED and self._budget.soft_pause_active():
            self._record_continue_approval()
            return GateDecision(
                RunOutcome.DEFER,
                defer_reason=DeferReason.BUDGET_SOFT_PAUSE,
                reason_human=DeferReason.BUDGET_SOFT_PAUSE.human(),
                backoff_s=self._backoff("soft"),
            )
        if self.cloud_unreachable():
            return GateDecision(
                RunOutcome.DEFER,
                defer_reason=DeferReason.CLOUD_UNREACHABLE,
                reason_human=DeferReason.CLOUD_UNREACHABLE.human(),
                backoff_s=self._backoff("cloud"),
            )
        if self._bg_bucket_empty():
            return GateDecision(
                RunOutcome.DEFER,
                defer_reason=DeferReason.RATE_BUCKET_EMPTY,
                reason_human=DeferReason.RATE_BUCKET_EMPTY.human(),
                backoff_s=self._backoff("rate"),
            )
        return GateDecision(RunOutcome.RUN)

    # ── Axis B: is this action safe to run unattended? ────────────────────────
    def classify_action(
        self, tool: str, tool_input, *, write_allowed: bool = False
    ) -> GateDecision:
        """External side-effect → ASK; everything internal → RUN. Honors the
        `autonomy_approve_external_only` flag: when it's off, callers keep their old
        (broader) classifier — this method always implements the external-only policy."""
        if self._is_external_side_effect(tool, tool_input):
            name = (tool or "action").strip()
            return GateDecision(
                RunOutcome.ASK,
                ask_reason=f"external side-effect ({name})",
                reason_human=f"Needs your approval — this would send/act outside the system ({name}).",
            )
        return GateDecision(RunOutcome.RUN)

    def _is_external_side_effect(self, tool: str, tool_input) -> bool:
        name = (tool or "").strip().lower()
        if not name:
            return False
        if name in _INTERNAL_TOOLS:
            return False
        if any(h in name for h in _READ_HINTS):
            return False
        # An explicit outbound recipient in the args is a strong external signal.
        if isinstance(tool_input, dict):
            for k in ("to", "recipient", "recipients", "email", "phone", "channel"):
                if tool_input.get(k):
                    return True
        return any(v in name for v in _EXTERNAL_VERBS)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _record_continue_approval(self) -> None:
        if self._approvals is None:
            return
        with contextlib.suppress(Exception):
            self._approvals.record(
                CONTINUE_SPEND_TOOL,
                {"soft_cap": self._budget.soft_cap()},
                reason="autonomous daily soft budget reached — approve to keep spending today",
                end_user_id="",
            )

    def _bg_bucket_empty(self) -> bool:
        fn = getattr(self._router, "bg_bucket_empty", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    @staticmethod
    def _backoff(kind: str) -> float:
        base = float(_settings.get("job_defer_backoff_base_s") or 30.0)
        # Soft-budget pauses want a long wait (owner-gated); rate/cloud want short.
        return base * (20.0 if kind == "soft" else 1.0)
