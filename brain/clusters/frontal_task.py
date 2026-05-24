"""
FrontalTaskSubsystem — task intent within the frontal cortex.

When the executive classifies a turn as requires_action=True, this subsystem
extracts the goal and deposits it into PendingTask for the motor cortex to pick up.
Motor then owns the full execution loop, adapting step by step.

Frontal sets the intent. Motor decides how to achieve it. No duplicate planning.
"""
from __future__ import annotations

import logging

from brain.clusters.frontal_subsystem import FrontalSubsystem, SubsystemResult

logger = logging.getLogger(__name__)


class PendingTask:
    """Shared mutable state: frontal deposits a goal, motor picks it up and clears it."""

    def __init__(self) -> None:
        self._goal: str | None = None

    def set(self, goal: str) -> None:
        if self._goal:
            logger.warning("[PendingTask] Overwriting unstarted goal: %s", self._goal[:60])
        self._goal = goal
        logger.info("[PendingTask] Goal queued: %s", goal[:80])

    def take(self) -> str | None:
        """Retrieve and clear the pending goal. Returns None if nothing queued."""
        goal, self._goal = self._goal, None
        return goal

    def has_pending(self) -> bool:
        return self._goal is not None


class FrontalTaskSubsystem(FrontalSubsystem):
    """
    Detects action-oriented turns and deposits the goal for motor to execute.
    Returns no response so the conversational drafter path fires and writes
    a natural spoken acknowledgment (response_type="task").
    """

    def __init__(self, pending_task: PendingTask) -> None:
        self._pending = pending_task

    @property
    def name(self) -> str:
        return "task"

    def can_handle(self, response_type: str, features: dict) -> bool:
        return response_type == "task" and bool(features.get("requires_action"))

    async def process(
        self,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        instruction: dict,
        turn_id: str,
    ) -> SubsystemResult:
        goal = features.get("raw_text") or features.get("topic_summary", "")
        self._pending.set(goal)
        # Empty response → falls through to the drafter, which writes the acknowledgment.
        return SubsystemResult(response="")
