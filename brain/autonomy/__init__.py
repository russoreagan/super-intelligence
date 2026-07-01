"""
brain.autonomy — the motor-cortex autonomy policy core.

Two orthogonal concerns that used to be smeared across `if self._bg_mode`,
`_enforce_cloud_budget`, `cloud_budget_exhausted`, and per-branch degrade logic
live here as one policy point and one outcome model:

- Axis A — can this call reach cloud right now?  (budget tier · rate bucket · cloud health)
- Axis B — is this action safe to run unattended?  (external side-effect → approval)

`SpendRiskGate` owns both axes and emits a single `GateDecision`
(RUN | DEFER | ASK | STOP). `JobOutcome`/`JobState` is the terminal-state model
every job path must produce — no more silent empty-success. Imported by the motor
cortex, the CMA executor, and the DMN alike; it takes the router + approvals ledger
as injected dependencies so it stays free of import cycles.
"""

from __future__ import annotations

from brain.autonomy.budget import AutonomousBudget, BudgetTier
from brain.autonomy.gate import CONTINUE_SPEND_TOOL, GateDecision, SpendRiskGate
from brain.autonomy.outcome import JobOutcome, JobState
from brain.autonomy.reasons import DeferReason, RunOutcome, StopReason

__all__ = [
    "AutonomousBudget",
    "BudgetTier",
    "SpendRiskGate",
    "GateDecision",
    "CONTINUE_SPEND_TOOL",
    "JobOutcome",
    "JobState",
    "RunOutcome",
    "DeferReason",
    "StopReason",
]
