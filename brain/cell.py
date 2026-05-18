"""
IntegratorCell — LLM-powered, fires only at convergence zones.
Subscribes to topics, threshold-fires, rate-limits, prompt-caches.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from brain.model_router import ModelRouter

logger = logging.getLogger(__name__)


@dataclass
class IntegratorCell:
    name: str
    cluster: str
    model: str                      # logical model key: "haiku", "flash", "flash-lite", "local"
    system_prompt: str
    topics: list[str]               # topics this cell subscribes to
    fire_threshold: float = 0.5     # minimum activation to wake
    max_calls_per_turn: int = 2
    timeout_seconds: float = 20.0

    _router: ModelRouter = field(default=None, init=False, repr=False)
    _calls_this_turn: int = field(default=0, init=False, repr=False)
    _turn_id: str = field(default="", init=False, repr=False)

    def set_router(self, router: ModelRouter) -> None:
        self._router = router

    def reset_turn(self, turn_id: str) -> None:
        self._calls_this_turn = 0
        self._turn_id = turn_id

    def _can_fire(self) -> bool:
        return self._calls_this_turn < self.max_calls_per_turn

    async def call(self, messages: list[dict], extra_context: dict | None = None) -> str:
        if not self._can_fire():
            logger.warning("%s.%s: rate limit hit this turn", self.cluster, self.name)
            return ""
        self._calls_this_turn += 1
        start = time.time()
        try:
            result = await asyncio.wait_for(
                self._router.call(self.model, self.system_prompt, messages),
                timeout=self.timeout_seconds,
            )
            elapsed = time.time() - start
            logger.debug("%s.%s: %.2fs", self.cluster, self.name, elapsed)
            return result
        except asyncio.TimeoutError:
            logger.warning("%s.%s: timeout after %.1fs", self.cluster, self.name, self.timeout_seconds)
            return ""
        except Exception as e:
            logger.error("%s.%s: error: %s", self.cluster, self.name, e)
            return ""
