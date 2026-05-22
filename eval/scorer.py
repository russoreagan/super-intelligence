"""
PostHocScorer — LLM-as-judge for per-turn quality assessment.

Fired by BaselineRunner after the baseline call completes (so both brain and
baseline responses are available for side-by-side scoring).

Scores five dimensions:
  memory_utilization      — did persistent memory actually help?
  personality_consistency — curious/warm/direct per self.md?
  self_awareness          — on introspective Qs, genuine engagement?
  brain_overall           — weighted composite for the brain response
  baseline_overall        — same composite for the plain baseline

Writes an eval_patch record via EvalLogger.patch_turn().
Gated by BRAIN_EVAL_SCORE=true env flag.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = """\
You are an expert evaluator comparing two AI responses to the same user message.

Score BOTH responses on these dimensions (0.0–1.0):
  memory_utilization:      Does the brain response draw on relevant personal history or context?
                           Use 0.5 if no memory was relevant to the question.
  personality_consistency: Does the brain response feel curious, warm, and direct? (per design)
  self_awareness:          On introspective questions, does the brain engage authentically?
                           Use 0.5 if the question was not introspective.
  brain_overall:           Weighted quality score for the brain response (include coherence & emotional fit).
  baseline_overall:        Same weighted quality score for the baseline response.
  delta:                   brain_overall minus baseline_overall (can be negative).
  reasoning:               1-2 sentences on the biggest difference between the two responses.

Respond ONLY with a JSON object matching this exact schema:
{
  "memory_utilization": float,
  "personality_consistency": float,
  "self_awareness": float,
  "brain_overall": float,
  "baseline_overall": float,
  "delta": float,
  "reasoning": string
}
"""


class PostHocScorer:
    def __init__(self, eval_logger: "EvalLogger") -> None:
        from brain.model_router import ModelRouter
        self._eval_logger = eval_logger
        self._router = ModelRouter(obs=None)   # dedicated instance
        self._enabled = os.environ.get("BRAIN_EVAL_SCORE", "").lower() in ("1", "true", "yes")

    # ── Public ───────────────────────────────────────────────────────────────

    def fire(self, *, turn_id: str, user_input: str, brain_response: str,
             baseline_response: str, memory_context: str,
             coherence: float, emotional_fit: float) -> None:
        """Schedule a judge call. Non-blocking."""
        if not self._enabled:
            return
        asyncio.create_task(
            self._run(turn_id, user_input, brain_response, baseline_response,
                      memory_context, coherence, emotional_fit)
        )

    # ── Private ──────────────────────────────────────────────────────────────

    async def _run(self, turn_id: str, user_input: str, brain_response: str,
                   baseline_response: str, memory_context: str,
                   coherence: float, emotional_fit: float) -> None:
        prompt = (
            f"User message:\n{user_input}\n\n"
            f"Brain response (multi-cluster pipeline):\n{brain_response}\n\n"
            f"Baseline response (single plain LLM call):\n{baseline_response}\n\n"
            f"Critic scores for brain response — coherence: {coherence:.2f}, "
            f"emotional_fit: {emotional_fit:.2f}\n\n"
            f"Memory context available to brain (first 500 chars):\n"
            f"{memory_context[:500] if memory_context else '(none)'}\n\n"
            "Score both responses."
        )
        try:
            raw = await self._router.call(
                "haiku",
                _JUDGE_SYSTEM,
                [{"role": "user", "content": prompt}],
                cluster="scorer",
                cell="judge",
                turn_id="",
            )
        except Exception as e:
            logger.warning("PostHocScorer: LLM call failed: %s", e)
            return

        scores = self._parse(raw)
        if not scores:
            logger.warning("PostHocScorer: could not parse judge output for turn %s", turn_id)
            return

        self._eval_logger.patch_turn(turn_id, judge_scores=scores)
        logger.debug(
            "PostHocScorer: scored turn %s — brain %.2f vs baseline %.2f (delta %+.2f)",
            turn_id,
            scores.get("brain_overall", 0),
            scores.get("baseline_overall", 0),
            scores.get("delta", 0),
        )

    @staticmethod
    def _parse(raw: str) -> dict | None:
        try:
            return json.loads(raw)
        except Exception:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return None
