"""
PostHocScorer — LLM-as-judge for per-turn quality assessment.

Fired by BaselineRunner after the baseline call completes (so both brain and
baseline responses are available for side-by-side scoring).

Three parallel judge calls answer three specific questions:
  1. Quality comparison (original 5 dimensions + delta)
  2. Pipeline efficiency — was the multi-cluster overhead worth it?
  3. Novel behavior — did the architecture produce something a single LLM wouldn't?

Writes eval_patch records via EvalLogger.patch_turn().
Gated by BRAIN_EVAL_SCORE=true env flag.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from brain.utils import safe_json_parse

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)

_QUALITY_JUDGE_SYSTEM = """\
You are an expert evaluator comparing two AI responses to the same user message.

Score BOTH responses on these dimensions (0.0–1.0):
  memory_utilization:      Does the brain response draw on relevant personal history or context?
                           Use 0.5 if no memory was relevant to the question.
  personality_consistency: Does the brain response feel curious, warm, and direct? (per design)
  self_awareness:          On introspective questions, does the brain engage authentically?
                           Use 0.5 if the question was not introspective.
  brain_overall:           Weighted quality score for the brain response (coherence & emotional fit).
  baseline_overall:        Same weighted quality score for the baseline response.
  delta:                   brain_overall minus baseline_overall (can be negative).
  reasoning:               1-2 sentences on the biggest difference between the two responses.

Respond ONLY with valid JSON matching this exact schema:
{
  "memory_utilization": float,
  "personality_consistency": float,
  "self_awareness": float,
  "brain_overall": float,
  "baseline_overall": float,
  "delta": float,
  "reasoning": string
}"""

_PIPELINE_JUDGE_SYSTEM = """\
You evaluate whether a multi-cluster AI brain pipeline justified its computational cost
compared to a single plain LLM call for the same user message.

The brain uses separate clusters: temporal (language understanding), hypothalamus
(emotion/affect), hippocampus (long-term memory recall), frontal (multiple competing
drafts + critic), brainstem (articulation gate). Each cluster may make LLM calls.

Score these dimensions (0.0–1.0):
  pipeline_value_add:  Did the brain response clearly benefit from the multi-cluster
                       pipeline? High (>0.8) = memory, emotion context, or multi-draft
                       selection produced something the baseline couldn't. Low (<0.4) =
                       the baseline was just as good despite far fewer LLM calls.

  calls_justified:     Were the N LLM calls proportionate to the task complexity?
                       High = complex emotional/memory task that needed all those calls.
                       Low = simple factual question that didn't warrant the overhead.
                       Use 0.5 for moderate complexity turns.

  memory_leverage:     Did having persistent long-term memory access materially improve
                       the response? High = response would've been generic without memory.
                       Use 0.5 if no memory was recalled or if it wasn't relevant.

  efficiency_reasoning: 1-2 sentences on whether the pipeline cost was worth it.

Respond ONLY with valid JSON:
{
  "pipeline_value_add": float,
  "calls_justified": float,
  "memory_leverage": float,
  "efficiency_reasoning": string
}"""

_NOVELTY_JUDGE_SYSTEM = """\
You evaluate whether a multi-cluster AI brain produced genuinely novel or emergent
behavior compared to what a standard single-call LLM would produce.

"Novel" means qualitatively different — not just longer or more detailed, but
reflecting something like: emotional authenticity that changes the delivery,
memory-driven specificity about the person, personality continuity across the
conversation, proactive thoughts that weren't directly prompted, or any response
element that feels like it emerged from the interaction between components rather
than from a single generation.

Score these dimensions (0.0–1.0):
  behavioral_novelty:  Does the brain response contain anything that a standard LLM
                       (given the same user message, no memory, no emotion state) would
                       be unlikely to produce? High (>0.8) = clearly yes. Low (<0.3) =
                       the response is indistinguishable from a good single-LLM output.

  emergence_detected:  Does anything in the response seem to have emerged from cluster
                       interactions (e.g., emotion shaping word choice, memory surfacing
                       an unexpected connection, draft competition producing a better
                       synthesis)? High = yes, you can see the seams of something richer.

  personality_continuity: Does the response reflect a consistent, persistent personality
                       that goes beyond prompt-level persona? High = feels like the same
                       entity across turns with accumulated history and affect.

  novelty_reasoning:   1-2 sentences on the most interesting or surprising thing about
                       the brain response vs the baseline, if anything.

Respond ONLY with valid JSON:
{
  "behavioral_novelty": float,
  "emergence_detected": float,
  "personality_continuity": float,
  "novelty_reasoning": string
}"""


class PostHocScorer:
    def __init__(self, eval_logger: "EvalLogger", obs=None) -> None:
        from brain.model_router import ModelRouter
        self._eval_logger = eval_logger
        self._obs = obs
        self._router = ModelRouter(obs=None)
        self._enabled = os.environ.get("BRAIN_EVAL_SCORE", "").lower() in ("1", "true", "yes")

    # ── Public ───────────────────────────────────────────────────────────────

    def fire(self, *, turn_id: str, user_input: str, brain_response: str,
             baseline_response: str, memory_context: str,
             coherence: float, emotional_fit: float,
             trace: "TurnTrace | None" = None) -> None:
        """Schedule all three judge calls in parallel. Non-blocking."""
        if not self._enabled:
            return
        asyncio.create_task(
            self._run_all(turn_id, user_input, brain_response, baseline_response,
                          memory_context, coherence, emotional_fit, trace)
        )

    # ── Private ──────────────────────────────────────────────────────────────

    async def _run_all(self, turn_id: str, user_input: str, brain_response: str,
                       baseline_response: str, memory_context: str,
                       coherence: float, emotional_fit: float,
                       trace: "TurnTrace | None") -> None:
        results = await asyncio.gather(
            self._run_quality(turn_id, user_input, brain_response, baseline_response,
                              memory_context, coherence, emotional_fit),
            self._run_pipeline(turn_id, user_input, brain_response, baseline_response,
                               memory_context, coherence, trace),
            self._run_novelty(turn_id, user_input, brain_response, baseline_response, trace),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                label = ["quality", "pipeline", "novelty"][i]
                logger.warning("PostHocScorer: %s judge raised: %s", label, r)

    async def _run_quality(self, turn_id: str, user_input: str, brain_response: str,
                           baseline_response: str, memory_context: str,
                           coherence: float, emotional_fit: float) -> None:
        prompt = (
            f"User message:\n{user_input}\n\n"
            f"Brain response (multi-cluster pipeline):\n{brain_response}\n\n"
            f"Baseline response (single plain LLM call):\n{baseline_response}\n\n"
            f"Internal critic scores — coherence: {coherence:.2f}, "
            f"emotional_fit: {emotional_fit:.2f}\n\n"
            f"Memory context available to brain (first 500 chars):\n"
            f"{memory_context[:500] if memory_context else '(none)'}\n\n"
            "Score both responses."
        )
        raw = await self._router.call(
            "haiku", _QUALITY_JUDGE_SYSTEM,
            [{"role": "user", "content": prompt}],
            cluster="scorer", cell="quality_judge", turn_id="",
        )
        scores = safe_json_parse(raw)
        if not scores:
            logger.warning("PostHocScorer: quality judge parse failed for turn %s", turn_id)
            return
        self._eval_logger.patch_turn(turn_id, judge_scores=scores)
        if self._obs:
            lf = {f"judge.{k}": v for k, v in scores.items()
                  if k != "reasoning" and isinstance(v, (int, float))}
            self._obs.record_scores(turn_id, lf, comment=scores.get("reasoning", ""))
        logger.debug(
            "PostHocScorer quality: turn=%s brain=%.2f baseline=%.2f delta=%+.2f",
            turn_id, scores.get("brain_overall", 0),
            scores.get("baseline_overall", 0), scores.get("delta", 0),
        )

    async def _run_pipeline(self, turn_id: str, user_input: str, brain_response: str,
                            baseline_response: str, memory_context: str,
                            coherence: float, trace: "TurnTrace | None") -> None:
        llm_calls = getattr(trace, "llm_calls", "?") if trace else "?"
        cluster_tokens = getattr(trace, "cluster_tokens", {}) if trace else {}
        memory_recalled = getattr(trace, "memory_recalled", False) if trace else False
        drafter_count = getattr(trace, "drafter_count", 1) if trace else 1
        llm_calls_saved = getattr(trace, "llm_calls_saved", 0) if trace else 0

        token_summary = (
            "; ".join(
                f"{cl}: {v.get('calls', 0)} calls / {v.get('in', 0)+v.get('out', 0)} tokens"
                for cl, v in cluster_tokens.items()
            ) if cluster_tokens else "(not available)"
        )

        prompt = (
            f"User message:\n{user_input}\n\n"
            f"Brain response (multi-cluster pipeline):\n{brain_response}\n\n"
            f"Baseline response (single plain LLM call):\n{baseline_response}\n\n"
            f"Pipeline stats:\n"
            f"  Total LLM calls this turn: {llm_calls}\n"
            f"  LLM calls saved by gating: {llm_calls_saved}\n"
            f"  Competing drafts generated: {drafter_count}\n"
            f"  Memory recalled: {memory_recalled}\n"
            f"  Per-cluster breakdown: {token_summary}\n\n"
            f"Memory context used (first 400 chars):\n"
            f"{memory_context[:400] if memory_context else '(none)'}\n\n"
            "Evaluate pipeline efficiency."
        )
        raw = await self._router.call(
            "haiku", _PIPELINE_JUDGE_SYSTEM,
            [{"role": "user", "content": prompt}],
            cluster="scorer", cell="pipeline_judge", turn_id="",
        )
        scores = safe_json_parse(raw)
        if not scores:
            logger.warning("PostHocScorer: pipeline judge parse failed for turn %s", turn_id)
            return
        self._eval_logger.patch_turn(turn_id, pipeline_scores=scores)
        if self._obs:
            lf = {f"pipeline.{k}": v for k, v in scores.items()
                  if k != "efficiency_reasoning" and isinstance(v, (int, float))}
            self._obs.record_scores(turn_id, lf,
                                    comment=scores.get("efficiency_reasoning", ""))
        logger.debug(
            "PostHocScorer pipeline: turn=%s value_add=%.2f calls_justified=%.2f memory_leverage=%.2f",
            turn_id, scores.get("pipeline_value_add", 0),
            scores.get("calls_justified", 0), scores.get("memory_leverage", 0),
        )

    async def _run_novelty(self, turn_id: str, user_input: str, brain_response: str,
                           baseline_response: str, trace: "TurnTrace | None") -> None:
        emotion = getattr(trace, "emotion", "neutral") if trace else "neutral"
        emotion_core = getattr(trace, "emotion_core", "neutral") if trace else "neutral"
        llm_calls_saved = getattr(trace, "llm_calls_saved", 0) if trace else 0
        has_anticipations = bool(
            trace and getattr(trace, "fired_path", None) and
            any(p.get("tag") == "anticipator" for p in trace.fired_path)
        )

        prompt = (
            f"User message:\n{user_input}\n\n"
            f"Brain response (multi-cluster pipeline):\n{brain_response}\n\n"
            f"Baseline response (single plain LLM call):\n{baseline_response}\n\n"
            f"Context about the brain this turn:\n"
            f"  Detected emotion: {emotion} (core: {emotion_core})\n"
            f"  Gating saved {llm_calls_saved} LLM calls via prediction\n"
            f"  DMN anticipations surfaced: {has_anticipations}\n\n"
            "Evaluate novelty and emergent behavior."
        )
        raw = await self._router.call(
            "haiku", _NOVELTY_JUDGE_SYSTEM,
            [{"role": "user", "content": prompt}],
            cluster="scorer", cell="novelty_judge", turn_id="",
        )
        scores = safe_json_parse(raw)
        if not scores:
            logger.warning("PostHocScorer: novelty judge parse failed for turn %s", turn_id)
            return
        self._eval_logger.patch_turn(turn_id, novelty_scores=scores)
        if self._obs:
            lf = {f"novelty.{k}": v for k, v in scores.items()
                  if k != "novelty_reasoning" and isinstance(v, (int, float))}
            self._obs.record_scores(turn_id, lf,
                                    comment=scores.get("novelty_reasoning", ""))
        logger.debug(
            "PostHocScorer novelty: turn=%s behavioral=%.2f emergence=%.2f continuity=%.2f",
            turn_id, scores.get("behavioral_novelty", 0),
            scores.get("emergence_detected", 0), scores.get("personality_continuity", 0),
        )
