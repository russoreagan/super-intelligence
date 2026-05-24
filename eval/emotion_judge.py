"""
EmotionJudge — LLM-as-judge for the emotion system specifically.

Answers: "Is the emotion system working as intended?"

Fires every turn (no baseline needed). Gated by BRAIN_EVAL_EMOTION=true.

Four scored dimensions:
  emotion.coherence        — does response tone match the detected emotion label?
  emotion.appropriateness  — is the detected emotion contextually reasonable?
  emotion.neuromod_plausible — do DA/ACh/GABA/Glu + hormonal levels make sense?
  emotion.expressiveness   — is the emotion visible in the text, not just labeled?

Scores go to Langfuse as `emotion.*` and to eval JSONL as an eval_patch.
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

_NEUROMOD_GUIDE = """\
Neuromodulator roles (reference only — use for plausibility scoring):
  DA  (dopamine)      high → motivation, excitement, confidence, reward-seeking
  ACh (acetylcholine) high → focused attention, curiosity, learning mode
  GABA                high → calm, inhibited, low arousal
  Glu (glutamate)     high → alertness, sensory engagement
Hormonal:
  5HT (serotonin)     high → contentment, stability, social warmth
  CORT (cortisol)     high → stress, urgency, vigilance
  OXT (oxytocin)      high → trust, warmth, social bonding"""

_JUDGE_SYSTEM = f"""\
You evaluate whether an AI brain's emotion system is working as designed.

The brain uses a biologically-inspired neuromodulator system (dopamine, acetylcholine,
GABA, glutamate) and a hormonal layer (serotonin, cortisol, oxytocin) to derive an
emotion label each turn. You assess four dimensions.

{_NEUROMOD_GUIDE}

Score EACH dimension 0.0–1.0:

  emotion_coherence:       Does the response's actual tone, word choice, pacing, and
                           delivery *match* the detected emotion label? High (>0.8) means
                           you can feel the emotion in the text without being told it.
                           Low (<0.4) means the text contradicts or ignores the label.

  emotion_appropriateness: Given only the user's message, is the detected emotion label
                           a reasonable human-like reaction? High = yes, clearly fits.
                           Use 0.5 if the turn was factual/neutral and any calm emotion
                           would be equally appropriate.

  neuromod_plausibility:   Do the neuromodulator levels make neurochemical sense for this
                           emotion? High (>0.8) = the levels are coherent with the label.
                           Low (<0.4) = there's a contradiction (e.g., very high GABA
                           but emotion is "excitement"). Use 0.5 if levels are mid-range
                           and don't give strong signal either way.

  expressiveness:          Is the emotion actually visible in the response text, or only
                           labeled internally? High = reader can sense the mood without
                           being told. Low = response reads flat/generic regardless of label.

Respond ONLY with valid JSON matching this schema exactly:
{{
  "emotion_coherence": float,
  "emotion_appropriateness": float,
  "neuromod_plausible": float,
  "expressiveness": float,
  "reasoning": "1-2 sentences on what works or doesn't in the emotion system this turn"
}}"""


class EmotionJudge:
    def __init__(self, eval_logger: "EvalLogger", obs=None) -> None:
        from brain.model_router import ModelRouter
        self._eval_logger = eval_logger
        self._obs = obs
        self._router = ModelRouter(obs=None)
        self._enabled = os.environ.get("BRAIN_EVAL_EMOTION", "").lower() in ("1", "true", "yes")

    def fire(self, trace: "TurnTrace") -> None:
        """Schedule a judge call. Non-blocking — creates a background task."""
        if not self._enabled:
            return
        asyncio.create_task(self._run(trace))

    async def _run(self, trace: "TurnTrace") -> None:
        nm = trace.neuromod or {}
        hormonal = trace.hormonal or {}

        nm_text = ", ".join(f"{k}={v:.3f}" for k, v in nm.items()) if nm else "(not available)"
        hormonal_text = (
            ", ".join(f"{k}={v:.3f}" for k, v in hormonal.items())
            if hormonal else "(not available)"
        )

        prompt = (
            f"User message:\n{trace.user_input}\n\n"
            f"Brain response:\n{trace.response}\n\n"
            f"Detected emotion: {trace.emotion} (core family: {trace.emotion_core})\n\n"
            f"Neuromodulator snapshot: {nm_text}\n"
            f"Hormonal snapshot: {hormonal_text}\n\n"
            "Evaluate the four dimensions."
        )

        try:
            raw = await self._router.call(
                "haiku",
                _JUDGE_SYSTEM,
                [{"role": "user", "content": prompt}],
                cluster="emotion_judge",
                cell="judge",
                turn_id="",
            )
        except Exception as e:
            logger.warning("EmotionJudge: LLM call failed: %s", e)
            return

        scores = safe_json_parse(raw)
        if not scores:
            logger.warning("EmotionJudge: could not parse judge output for turn %s", trace.turn_id)
            return

        self._eval_logger.patch_turn(
            trace.turn_id,
            emotion_judge=scores,
        )

        if self._obs:
            langfuse_scores = {
                f"emotion.{k}": v
                for k, v in scores.items()
                if k != "reasoning" and isinstance(v, (int, float))
            }
            self._obs.record_scores(
                trace.turn_id,
                langfuse_scores,
                comment=scores.get("reasoning", ""),
            )

        logger.debug(
            "EmotionJudge: turn=%s emotion=%s coherence=%.2f appropriate=%.2f neuromod=%.2f express=%.2f",
            trace.turn_id,
            trace.emotion,
            scores.get("emotion_coherence", 0),
            scores.get("emotion_appropriateness", 0),
            scores.get("neuromod_plausible", 0),
            scores.get("expressiveness", 0),
        )
