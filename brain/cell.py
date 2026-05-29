"""
IntegratorCell — LLM-powered, fires only at convergence zones.
Subscribes to topics, threshold-fires, rate-limits, prompt-caches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from brain.model_router import ModelRouter

logger = logging.getLogger(__name__)


@dataclass
class IntegratorCell:
    name: str
    cluster: str
    model: str  # logical model key: "haiku", "flash", "flash-lite", "local"
    system_prompt: str
    topics: list[str]  # topics this cell subscribes to
    fire_threshold: float = 0.5  # minimum activation to wake
    max_calls_per_turn: int = 2
    timeout_seconds: float = 20.0
    locality: str = "either"  # "local" | "cloud" | "either"
    sensitivity: str = "normal"  # "sensitive" | "normal"
    max_tokens: int = 1024  # upper bound on completion length
    skills: list[str] = field(default_factory=list)  # brain/skills/*.md to inject for local calls
    temperature: float | None = None  # override the router's default sampling temp (local only)

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
            logger.warning(
                "[%s/%s] Per-turn call limit hit — skipping this call. "
                "Increase max_calls_per_turn if responses feel incomplete.",
                self.cluster,
                self.name,
            )
            return ""
        self._calls_this_turn += 1
        start = time.time()
        # Record on the current turn's firing path (no-op if no trace bound)
        try:
            from brain.observability.firing_path import record_integrator_call

            record_integrator_call(self.name, self.cluster)
        except Exception:
            pass
        try:
            from brain.ui.emitter import emitter as _ui_emitter

            asyncio.create_task(
                _ui_emitter.emit_cell(self.cluster, self.name, self.model, self._turn_id)
            )
        except Exception:
            pass
        try:
            result = await asyncio.wait_for(
                self._router.call(
                    self.model,
                    self.system_prompt,
                    messages,
                    cluster=self.cluster,
                    cell=self.name,
                    turn_id=self._turn_id,
                    locality=self.locality,
                    max_tokens=self.max_tokens,
                    skills=self.skills,
                    temperature=self.temperature,
                ),
                timeout=self.timeout_seconds,
            )
            elapsed = time.time() - start
            logger.debug("%s.%s: %.2fs", self.cluster, self.name, elapsed)
            return result
        except TimeoutError:
            logger.warning(
                "[%s/%s] LLM call timed out after %.1fs — returning empty. "
                "If using Ollama, check it is running and the model is loaded ('ollama serve').",
                self.cluster,
                self.name,
                self.timeout_seconds,
            )
            return ""
        except Exception as e:
            logger.error("[%s/%s] LLM call failed: %s", self.cluster, self.name, e)
            return ""
