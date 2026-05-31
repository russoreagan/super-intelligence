"""
RelationshipJudge — LLM-as-judge for the relationship system.

Answers: "Is the relationship system producing the right behaviour, not just firing?"

Catches the failure mode the deterministic monitor cannot: a feature that fires
(`disclosure_fired=True`) but has no effect (the draft didn't actually disclose),
or warmth that's miscalibrated to the relationship stage.

DISABLED BY DEFAULT. Gated by BRAIN_EVAL_RELATIONSHIP=true. Fires per turn when on.

Three scored dimensions:
  relationship.warmth_calibration   — does the response's warmth fit the relationship stage?
  relationship.reciprocity_invitation — when disclosure fired, did the draft actually
                                        share something genuine and invite reciprocation?
  relationship.register_fit         — does phrasing match the user's detected register?

Scores go to Langfuse as `relationship.*` and to eval JSONL as an eval_patch.
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

_JUDGE_SYSTEM = """\
You evaluate whether an AI entity's RELATIONSHIP behaviour is well-calibrated this turn.

The entity tracks two things per person: an affection score (live warmth, band
guarded→cool→neutral→friendly→warm→close) and a familiarity tier (new/acquainted/close,
how well they know each other). It may also, on some turns, decide to proactively share
its own internal state to invite the user to open up, and/or nudge its phrasing toward
the user's communication register.

You will be told the relationship stage, whether a self-disclosure opportunity was
flagged this turn, and the user's detected register. Score three dimensions 0.0–1.0:

  warmth_calibration:     Does the response's warmth/formality fit the stage? High (>0.8)
                          = pitch-perfect (e.g. warm and personal with a close friend;
                          polite-but-measured with someone new). Low (<0.4) = miscalibrated
                          (over-familiar with a stranger, or cold/distant with a close
                          friend). Use 0.5 for neutral turns where stage barely matters.

  reciprocity_invitation: ONLY meaningful when a disclosure opportunity was flagged. High
                          (>0.8) = the response genuinely shares something about the
                          entity's own experience/feeling in a way that naturally invites
                          the user to reciprocate, woven in (not announced). Low (<0.4) =
                          it was flagged but the response disclosed nothing / stayed
                          transactional. If NO disclosure was flagged this turn, return 0.5
                          (not applicable).

  register_fit:           Does the response's phrasing roughly match the user's detected
                          register (casual vs formal, terse vs expansive) WITHOUT abandoning
                          the entity's own voice? High = adapted partway and natural. Low =
                          jarringly mismatched (formal essay to a "yeah ok" user, or
                          vice-versa). Use 0.5 if no register was detected.

Respond ONLY with valid JSON matching this schema exactly:
{
  "warmth_calibration": float,
  "reciprocity_invitation": float,
  "register_fit": float,
  "reasoning": "1-2 sentences on what fits or doesn't in the relationship behaviour this turn"
}"""


class RelationshipJudge:
    def __init__(self, eval_logger: EvalLogger, obs=None) -> None:
        from brain.model_router import ModelRouter

        self._eval_logger = eval_logger
        self._obs = obs
        self._router = ModelRouter(obs=None)
        # DISABLED BY DEFAULT — opt in with BRAIN_EVAL_RELATIONSHIP=true.
        self._enabled = os.environ.get("BRAIN_EVAL_RELATIONSHIP", "").lower() in (
            "1",
            "true",
            "yes",
        )

    def fire(self, trace: TurnTrace) -> None:
        """Schedule a judge call. Non-blocking — creates a background task."""
        if not self._enabled:
            return
        asyncio.create_task(self._run(trace))

    async def _run(self, trace: TurnTrace) -> None:
        stage = (
            f"affection {trace.affection}/100 (band: {trace.affection_label or 'unknown'}), "
            f"familiarity: {trace.familiarity_tier or 'unknown'}, bond: {trace.bond:.0f}"
        )
        disclosure = (
            "YES — a self-disclosure opportunity was flagged this turn"
            if trace.disclosure_fired
            else "no disclosure opportunity flagged this turn"
        )
        register = trace.style_register or "(no register detected)"

        prompt = (
            f"User message:\n{trace.user_input}\n\n"
            f"Entity response:\n{trace.response}\n\n"
            f"Relationship stage: {stage}\n"
            f"Disclosure opportunity: {disclosure}\n"
            f"User's detected register: {register}\n\n"
            "Evaluate the three dimensions."
        )

        try:
            raw = await self._router.call(
                "haiku",
                _JUDGE_SYSTEM,
                [{"role": "user", "content": prompt}],
                cluster="relationship_judge",
                cell="judge",
                turn_id="",
            )
        except Exception as e:
            logger.warning("RelationshipJudge: LLM call failed: %s", e)
            return

        scores = safe_json_parse(raw)
        if not scores:
            logger.warning(
                "RelationshipJudge: could not parse judge output for turn %s", trace.turn_id
            )
            return

        self._eval_logger.patch_turn(trace.turn_id, relationship_judge=scores)

        if self._obs:
            langfuse_scores = {
                f"relationship.{k}": v
                for k, v in scores.items()
                if k != "reasoning" and isinstance(v, (int, float))
            }
            self._obs.record_scores(
                trace.turn_id,
                langfuse_scores,
                comment=scores.get("reasoning", ""),
            )

        logger.debug(
            "RelationshipJudge: turn=%s warmth=%.2f reciprocity=%.2f register=%.2f",
            trace.turn_id,
            scores.get("warmth_calibration", 0),
            scores.get("reciprocity_invitation", 0),
            scores.get("register_fit", 0),
        )
