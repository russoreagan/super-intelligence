"""
FrontalSubsystem — ABC for distinct functional subsystems within the frontal cortex.

The frontal lobe handles more than conversation: task planning, future subsystems, etc.
Each subsystem declares what it handles and processes it independently.
The FrontalCluster dispatches to the first matching subsystem after the executive runs;
if none match, the existing conversational path (drafter/critic) fires as the fallback.

To add a new subsystem:
  1. Create a new file (e.g. frontal_reasoning.py)
  2. Implement FrontalSubsystem
  3. Register it in FrontalCluster.__init__ via self._subsystems.append(...)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SubsystemResult:
    response: str                              # spoken response returned to the user
    job_steps: list[dict] = field(default_factory=list)  # ordered tool calls for motor cortex
    metadata: dict = field(default_factory=dict)         # subsystem-specific extras


class FrontalSubsystem(ABC):
    """Base class for a frontal cortex subsystem."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier shown in logs and observability."""

    @abstractmethod
    def can_handle(self, response_type: str, features: dict) -> bool:
        """Return True if this subsystem should handle the current turn."""

    @abstractmethod
    async def process(
        self,
        features: dict,
        affect: dict,
        memory: dict,
        parietal_context: str,
        instruction: dict,
        turn_id: str,
    ) -> SubsystemResult:
        """Execute the subsystem and return a result."""
