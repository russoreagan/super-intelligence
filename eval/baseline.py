"""
BaselineRunner — fires a plain single-LLM call in the background for comparison.

Runs every 20th turn (sampled mode) or every turn (intensive mode).
Intensive mode is toggled via BRAIN_EVAL_INTENSIVE env var or live WebSocket
message {"type": "eval_mode", "intensive": true/false}.

Background tasks write eval_patch records via EvalLogger.patch_turn().
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eval.scorer import PostHocScorer
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)

BASELINE_SYSTEM = (
    "You are a helpful AI assistant. Answer the user's message clearly and concisely."
)


class BaselineRunner:
    def __init__(self, eval_logger: EvalLogger) -> None:
        from brain.model_router import ModelRouter
        self._eval_logger = eval_logger
        self._router = ModelRouter(obs=None)   # dedicated instance — no shared state
        self._turn_counter = 0
        self._sample_every = int(os.environ.get("BRAIN_EVAL_SAMPLE_EVERY", "20"))
        self._intensive = os.environ.get("BRAIN_EVAL_INTENSIVE", "").lower() in ("1", "true", "yes")
        self._enabled = os.environ.get("BRAIN_EVAL_BASELINE", "").lower() in ("1", "true", "yes")
        self._scorer: PostHocScorer | None = None   # injected by run.py after construction

    # ── Public ──────────────────────────────────────────────────────────────

    def set_intensive(self, value: bool) -> None:
        self._intensive = value
        logger.info("BaselineRunner: intensive mode %s", "ON" if value else "OFF")

    def fire(self, turn_id: str, user_input: str, brain_response: str,
             memory_context: str, coherence: float, emotional_fit: float,
             trace=None) -> None:
        """Schedule a baseline call if this turn is sampled. Non-blocking."""
        if not self._enabled:
            return
        self._turn_counter += 1
        should_run = self._intensive or (self._turn_counter % self._sample_every == 0)
        if not should_run:
            return
        asyncio.create_task(
            self._run(turn_id, user_input, brain_response, memory_context,
                      coherence, emotional_fit, trace)
        )

    # ── Private ─────────────────────────────────────────────────────────────

    async def _run(self, turn_id: str, user_input: str, brain_response: str,
                   memory_context: str, coherence: float, emotional_fit: float,
                   trace=None) -> None:
        start = time.time()
        try:
            baseline_response = await self._router.call(
                "haiku",
                BASELINE_SYSTEM,
                [{"role": "user", "content": user_input}],
                cluster="baseline",
                cell="baseline",
                turn_id="",   # don't pollute brain's obs
            )
        except Exception as e:
            logger.warning("BaselineRunner: LLM call failed: %s", e)
            return

        latency = time.time() - start
        self._eval_logger.patch_turn(
            turn_id,
            baseline_response=baseline_response,
            baseline_model="haiku",
            baseline_latency_s=round(latency, 3),
        )
        logger.debug("BaselineRunner: patch written for turn %s (%.2fs)", turn_id, latency)

        # Hand off to post-hoc scorer (it needs the baseline response)
        if self._scorer:
            self._scorer.fire(
                turn_id=turn_id,
                user_input=user_input,
                brain_response=brain_response,
                baseline_response=baseline_response,
                memory_context=memory_context,
                coherence=coherence,
                emotional_fit=emotional_fit,
                trace=trace,
            )
