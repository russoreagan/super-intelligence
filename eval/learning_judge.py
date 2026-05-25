"""
LearningJudge — LLM-as-judge for self-learning and behavioral adaptation.

Runs once at session end. Answers three questions:
  1. Is behavior measurably changing across this session beyond accumulated context?
  2. Do the predictor/gating systems show signs of improvement during this session?
  3. Do the Hebbian weight changes make structural sense as learning?

Requires at least 4 turns to run (otherwise too little signal).
Gated by BRAIN_EVAL_LEARNING=true env flag.

Scores posted to Langfuse as a standalone session-summary span (not tied to any
single turn). Also written to eval JSONL as a "learning_summary" record.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from brain.utils import safe_json_parse

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)

_MIN_TURNS = 4  # below this, no meaningful trend to judge

_JUDGE_SYSTEM = """\
You evaluate whether an AI brain is exhibiting genuine self-learning during a session,
as opposed to merely accumulating conversational context.

The brain has two structural learning mechanisms you should evaluate separately:

  PREDICT-AND-SURPRISE GATING: Each cluster predicts what will happen next. Correct
  predictions allow skipping LLM calls (saving cost). As predictors learn from the
  session, accuracy should rise, surprise should fall, and calls_saved should grow.

  HEBBIAN WEIGHT UPDATES: At session end, firing paths that produced good outcomes
  (high DA, high critic score) have their edge weights increased. Paths on poor turns
  decay. Over time this biases the network toward circuits that worked well.

You are given:
  - A sample of EARLY turns (first third of session)
  - A sample of LATE turns (last third of session)
  - Predictor statistics: accuracy/surprise/gating trends (early vs. late half)
  - A Hebbian summary: which edges gained or lost weight, by how much

Score these dimensions (0.0–1.0):

  intra_session_adaptation:
    Do late-session responses show detectably different stylistic or behavioral
    patterns vs. early turns in ways that CANNOT be explained by simply having more
    context? "More specific because it remembers things" does not count. Look for:
    changes in response length calibration, tone shifts, proactive connection-making,
    different pacing, less hedging, more direct engagement. High (>0.7) = clear
    behavioral shift. Low (<0.3) = consistent style throughout (may be fine if
    already high quality). Use 0.5 if the session was too short to tell.

  predictor_maturation:
    Does the gating data show the predictors improving during this session?
    High (>0.7) = accuracy rose, surprise fell, OR calls_saved grew toward the
    session end. Low (<0.3) = no trend, or predictors got worse. Use 0.5 if the
    session had too few turns to detect a trend.

  hebbian_coherence:
    Do the Hebbian weight changes make structural sense as learning?
    High (>0.7) = the edges that gained weight correspond to paths that intuitively
    should be reinforced (e.g., temporal→frontal gaining when understanding drove good
    responses; hippocampus→frontal gaining when memory recall was rewarded).
    Low (<0.3) = all edges moved in the same direction regardless of quality, or the
    changes look like noise. Use 0.5 if the magnitude was too small to assess.

  structural_vs_context:
    Is any improvement during this session due to structural adaptation (gating more
    efficient, response style shifting, predictor calibration improving) vs. simply
    having more memory context to draw on? Score 1.0 = clearly structural. Score 0.0
    = all gains are attributable to accumulated context alone. Score 0.5 if mixed.

  learning_reasoning:
    2-3 sentences. What is the strongest evidence of genuine learning (or its
    absence)? Is what you observe learning, adaptation, or just context accumulation?

Respond ONLY with valid JSON:
{
  "intra_session_adaptation": float,
  "predictor_maturation": float,
  "hebbian_coherence": float,
  "structural_vs_context": float,
  "learning_reasoning": string
}"""


class LearningJudge:
    def __init__(self, eval_logger: "EvalLogger", obs=None) -> None:
        from brain.model_router import ModelRouter
        self._eval_logger = eval_logger
        self._obs = obs
        self._router = ModelRouter(obs=None)
        self._enabled = os.environ.get("BRAIN_EVAL_LEARNING", "").lower() in ("1", "true", "yes")

    async def evaluate(self, session_id: str, full_traces: list["TurnTrace"],
                       session_metrics: dict) -> None:
        """
        Run the session-end learning judge. Non-blocking — call with await at shutdown.
        session_metrics should come from LearningMonitor.session_metrics().
        """
        if not self._enabled:
            return
        if len(full_traces) < _MIN_TURNS:
            logger.debug("LearningJudge: only %d turns — skipping (need %d)",
                         len(full_traces), _MIN_TURNS)
            return

        prompt = self._build_prompt(full_traces, session_metrics)
        try:
            raw = await self._router.call(
                "haiku",
                _JUDGE_SYSTEM,
                [{"role": "user", "content": prompt}],
                cluster="learning_judge",
                cell="judge",
                turn_id="",
            )
        except Exception as e:
            logger.warning("LearningJudge: LLM call failed: %s", e)
            return

        scores = safe_json_parse(raw)
        if not scores:
            logger.warning("LearningJudge: could not parse judge output for session %s", session_id)
            return

        # Write to eval JSONL
        self._eval_logger._append({
            "type": "learning_summary",
            "session_id": session_id,
            "ts": time.time(),
            "turn_count": len(full_traces),
            "structural_metrics": session_metrics,
            "judge_scores": scores,
        })

        # Post to Langfuse as a standalone session-summary span
        if self._obs:
            self._obs.record_session_learning(session_id, scores, session_metrics)

        logger.info(
            "LearningJudge: session=%s adaptation=%.2f predictor=%.2f hebbian=%.2f structural=%.2f",
            session_id,
            scores.get("intra_session_adaptation", 0),
            scores.get("predictor_maturation", 0),
            scores.get("hebbian_coherence", 0),
            scores.get("structural_vs_context", 0),
        )

    # ── Private ──────────────────────────────────────────────────────────────

    def _build_prompt(self, full_traces: list["TurnTrace"], metrics: dict) -> str:
        n = len(full_traces)
        third = max(1, n // 3)

        early = full_traces[:third]
        late = full_traces[-third:]

        def _turn_summary(t: "TurnTrace", idx: int) -> str:
            saved = t.llm_calls_saved or 0
            outcomes = t.predictor_outcomes or []
            correct = sum(1 for o in outcomes if o.get("correct"))
            acc = f"{correct}/{len(outcomes)}" if outcomes else "n/a"
            return (
                f"  Turn {idx+1}: user={t.user_input[:80]!r}\n"
                f"    response={t.response[:120]!r}\n"
                f"    emotion={t.emotion} | llm_calls={t.llm_calls} "
                f"saved={saved} | predictor_acc={acc}"
            )

        early_text = "\n".join(_turn_summary(t, i) for i, t in enumerate(early))
        late_text = "\n".join(_turn_summary(t, n - third + i) for i, t in enumerate(late))

        # Predictor trend section
        trend_lines = []
        for key, label in [
            ("predictor_accuracy_trend", "Accuracy trend (late − early)"),
            ("gating_efficiency_trend", "Gating efficiency trend"),
            ("surprise_trend", "Avg surprise trend (neg = improving)"),
            ("total_llm_calls_saved", "Total LLM calls saved this session"),
        ]:
            val = metrics.get(key)
            if val is not None:
                trend_lines.append(f"  {label}: {val:+.4f}" if isinstance(val, float)
                                   else f"  {label}: {val}")

        # Hebbian section
        hebbian_lines = []
        wiring_deltas = metrics.get("wiring_deltas", [])
        if wiring_deltas:
            hebbian_lines.append(f"  Edges changed: {metrics.get('wiring_edges_changed', 0)}")
            hebbian_lines.append(f"  Total |delta|: {metrics.get('wiring_delta_magnitude', 0):.4f}")
            gainers = [d for d in wiring_deltas if d["delta"] > 0]
            losers = [d for d in wiring_deltas if d["delta"] < 0]
            if gainers:
                g_text = ", ".join(f"{d['src']}→{d['tgt']} (+{d['delta']:.4f})"
                                   for d in gainers[:5])
                hebbian_lines.append(f"  Top gainers: {g_text}")
            if losers:
                l_text = ", ".join(f"{d['src']}→{d['tgt']} ({d['delta']:.4f})"
                                   for d in losers[:5])
                hebbian_lines.append(f"  Top losers: {l_text}")
        else:
            hebbian_lines.append("  (No Hebbian data available — wiring may not have run yet)")

        cross_drift = metrics.get("cross_session_drift")
        if cross_drift is not None:
            hebbian_lines.append(f"  Cross-session RMS weight drift from oldest snapshot: {cross_drift:.4f}")

        prompt = (
            f"SESSION: {metrics.get('turns_recorded', n)} turns total\n\n"
            f"EARLY TURNS (first third):\n{early_text}\n\n"
            f"LATE TURNS (last third):\n{late_text}\n\n"
            f"PREDICTOR / GATING TREND:\n" + "\n".join(trend_lines or ["  (insufficient data)"]) + "\n\n"
            f"HEBBIAN WEIGHT CHANGES (this session):\n" + "\n".join(hebbian_lines) + "\n\n"
            "Evaluate learning."
        )
        return prompt
