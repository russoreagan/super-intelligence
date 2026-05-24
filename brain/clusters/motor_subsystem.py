"""
MotorSubsystem — ABC for motor cortex subsystems.

Motor subsystems participate in the execution pipeline via two hooks:
  before_plan() — inject additional context into the planner prompt
  after_job()   — called when a job completes, for recording/learning

To add a new motor subsystem:
  1. Create a new file implementing MotorSubsystem
  2. Register it in run.py via motor.register_subsystem(...)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from brain.model_router import ModelRouter


class MotorSubsystem(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier shown in logs."""

    async def before_plan(self, task_description: str, router: ModelRouter) -> str:
        """Return additional context to prepend to the planner prompt.
        Return empty string to contribute nothing."""
        return ""

    async def recall_procedure(
        self, task: str, router: ModelRouter
    ) -> tuple[dict | None, float]:
        """Return (procedure, similarity) if a high-confidence prior procedure exists.
        Motor uses this to decide whether to run open-loop instead of planning reactively.
        Return (None, 0.0) to indicate no suitable procedure."""
        return None, 0.0

    async def predict_outcome(
        self,
        tool: str,
        args: dict,
        prior_results: list[str],
        router: ModelRouter,
    ) -> dict | None:
        """Predict the outcome of a tool call before it executes.

        Return a signature dict or None if no prediction is available:
          {"expected_success": bool, "length_min": int, "length_max": int, "is_empty": bool}

        Implementations should generalise across procedures — e.g. if this tool+args
        pattern has been seen before in *any* stored procedure, return the typical outcome.
        This is the forward model: predict sensory state from motor command.
        """
        return None

    async def after_job(
        self,
        goal: str,
        steps: list[dict],
        results: list[str],
        success: bool,
    ) -> None:
        """Called after a job finishes. Use for recording, learning, etc."""
