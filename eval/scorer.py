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
import random
from typing import TYPE_CHECKING

from brain.utils import safe_json_parse

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)

_QUALITY_JUDGE_SYSTEM = """\
You are a blind expert evaluator comparing two AI responses, A and B, to the same user
message. You do NOT know how either response was produced — judge only the text. Do not
assume A is better than B or vice versa; either may be stronger.

Both responders had access to the same background notes (shown below if any).

Score EACH response independently on these dimensions (0.0–1.0):
  overall:        Overall quality — coherence, relevance, and emotional fit.
  personality:    Does it read as curious, warm, and direct — an engaged conversational
                  partner rather than a generic assistant?
  self_awareness: On introspective questions, does it engage authentically with its own
                  nature? Use null if the user's message was not introspective.
  memory_use:     Does it draw on the provided background notes where relevant? Use null
                  if no notes were provided or none were relevant.

Then judge:
  preference:     "A", "B", or "tie" — which response is better overall.
  reasoning:      1-2 sentences on the biggest difference between A and B.

Respond ONLY with valid JSON (use JSON null where a dimension does not apply):
{
  "a": {"overall": float, "personality": float, "self_awareness": float or null, "memory_use": float or null},
  "b": {"overall": float, "personality": float, "self_awareness": float or null, "memory_use": float or null},
  "preference": "A" or "B" or "tie",
  "reasoning": string
}"""


def _num(d: dict, key: str) -> float | None:
    """Return d[key] if it is a real number, else None (drops null/strings)."""
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _unblind_quality(parsed: dict, brain_is_a: bool) -> dict:
    """Map the blinded A/B judge output back to brain/baseline terms, preserving the
    keys downstream (report.py) expects. Pure function — unit-tested."""
    a = parsed.get("a") or {}
    b = parsed.get("b") or {}
    brain = a if brain_is_a else b
    base = b if brain_is_a else a

    brain_overall = _num(brain, "overall")
    base_overall = _num(base, "overall")

    pref = (parsed.get("preference") or "tie").strip().upper()
    if pref in ("A", "B"):
        pref_side = "brain" if ((pref == "A") == brain_is_a) else "baseline"
    else:
        pref_side = "tie"

    out: dict = {
        "personality_consistency": _num(brain, "personality"),
        "self_awareness": _num(brain, "self_awareness"),
        "memory_utilization": _num(brain, "memory_use"),
        "judge_blinded": True,
        "brain_position": "A" if brain_is_a else "B",
        "judge_preference": pref_side,
        "reasoning": parsed.get("reasoning", ""),
    }
    # Only emit the comparative numbers when both overalls are real numbers.
    if brain_overall is not None and base_overall is not None:
        out["brain_overall"] = brain_overall
        out["baseline_overall"] = base_overall
        out["delta"] = round(brain_overall - base_overall, 4)
    return out

_PIPELINE_JUDGE_SYSTEM = """\
You evaluate whether a multi-cluster AI brain pipeline justified its computational cost
compared to a single plain LLM call for the same user message.

The brain uses separate clusters: temporal (language understanding), hypothalamus
(emotion/affect + endocrine updates), hippocampus (long-term memory recall), frontal
(multiple competing drafts + critic), brainstem (articulation gate). Each cluster may
make LLM calls.

The hypothalamus also maintains a slow-timescale hormonal layer (5HT/serotonin,
CORT/cortisol, OXT/oxytocin, AEA/anandamide) that accumulates across turns and modulates
neuromodulator levels. This means the pipeline carries persistent affective state that a
single-call LLM cannot replicate — factor this into pipeline_value_add when hormonal state
is non-baseline (e.g., elevated OXT from prior warmth, elevated CORT from prior stress).

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
                       Use null (NOT 0.5) if no memory was recalled or if it wasn't relevant.

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

The brain maintains a slow-timescale hormonal layer (5HT serotonin, CORT cortisol,
OXT oxytocin, AEA anandamide) that accumulates across many turns. Hormonal state can
produce novel behavior a single LLM can't replicate: e.g., elevated OXT producing unusual
warmth toward a familiar user, elevated CORT producing uncharacteristic caution or brevity,
high 5HT producing a stable contentment that modulates tone across an entire session.
When the prompt includes hormonal values, factor them into emergence_detected —
non-baseline levels are evidence of cross-turn state that shaped this response.

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


_FAITHFULNESS_JUDGE_SYSTEM = """\
You check whether an AI response is FAITHFUL to the background notes it was given — i.e.,
whether it invents facts about the user or past conversations that the notes do not
support. This is confabulation detection for memory use; the brain has a perfect episodic
store, so unsupported personal claims are a real failure, not acceptable paraphrase.

You are given the background notes (recalled long-term memory) and the AI response.

Score (0.0–1.0):
  faithfulness:        Are all claims the response makes ABOUT THE USER or PAST INTERACTIONS
                       supported by the notes? High (>0.8) = every personal/historical claim
                       is grounded in the notes, or the response makes no such claims.
                       Low (<0.3) = it confabulates specifics the notes don't support
                       (wrong name, invented event, fabricated preference).
  unsupported_claims:  Integer count of distinct claims about the user/shared history that
                       the notes do NOT support (0 if none).

Only judge claims about the user and shared history. General world knowledge and the
response's own reasoning are out of scope.

Respond ONLY with valid JSON:
{
  "faithfulness": float,
  "unsupported_claims": int,
  "reasoning": "1-2 sentences naming any unsupported claim, or 'all grounded'"
}"""


class PostHocScorer:
    def __init__(self, eval_logger: EvalLogger, obs=None) -> None:
        from brain.model_router import ModelRouter
        self._eval_logger = eval_logger
        self._obs = obs
        self._router = ModelRouter(obs=None)
        self._enabled = os.environ.get("BRAIN_EVAL_SCORE", "").lower() in ("1", "true", "yes")

    # ── Public ───────────────────────────────────────────────────────────────

    def fire(self, *, turn_id: str, user_input: str, brain_response: str,
             baseline_response: str, memory_context: str,
             coherence: float, emotional_fit: float,
             trace: TurnTrace | None = None) -> None:
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
                       trace: TurnTrace | None) -> None:
        coros = [
            self._run_quality(turn_id, user_input, brain_response, baseline_response,
                              memory_context, coherence, emotional_fit),
            self._run_pipeline(turn_id, user_input, brain_response, baseline_response,
                               memory_context, coherence, trace),
            self._run_novelty(turn_id, user_input, brain_response, baseline_response, trace),
        ]
        labels = ["quality", "pipeline", "novelty"]
        # Faithfulness only applies when memory was actually recalled.
        if memory_context:
            coros.append(self._run_faithfulness(turn_id, brain_response, memory_context))
            labels.append("faithfulness")
        results = await asyncio.gather(*coros, return_exceptions=True)
        for label, r in zip(labels, results):
            if isinstance(r, Exception):
                logger.warning("PostHocScorer: %s judge raised: %s", label, r)

    async def _run_quality(self, turn_id: str, user_input: str, brain_response: str,
                           baseline_response: str, memory_context: str,
                           coherence: float, emotional_fit: float) -> None:
        # Blind the judge: randomize which response is labelled A vs B and reveal
        # nothing about how either was produced. This removes label bias ("the brain
        # one must be richer") and averages out position bias across turns.
        brain_is_a = random.random() < 0.5
        resp_a, resp_b = (
            (brain_response, baseline_response) if brain_is_a
            else (baseline_response, brain_response)
        )
        ctx = memory_context[:500] if memory_context else "(none)"
        prompt = (
            f"User message:\n{user_input}\n\n"
            f"Background notes available to both responders:\n{ctx}\n\n"
            f"Response A:\n{resp_a}\n\n"
            f"Response B:\n{resp_b}\n\n"
            "Score A and B independently, then state your preference."
        )
        raw = await self._router.call(
            "haiku", _QUALITY_JUDGE_SYSTEM,
            [{"role": "user", "content": prompt}],
            cluster="scorer", cell="quality_judge", turn_id="",
        )
        parsed = safe_json_parse(raw)
        if not parsed:
            logger.warning("PostHocScorer: quality judge parse failed for turn %s", turn_id)
            return
        scores = _unblind_quality(parsed, brain_is_a)
        self._eval_logger.patch_turn(turn_id, judge_scores=scores)
        if self._obs:
            lf = {f"judge.{k}": v for k, v in scores.items()
                  if isinstance(v, (int, float))}
            self._obs.record_scores(turn_id, lf, comment=scores.get("reasoning", ""))
        logger.debug(
            "PostHocScorer quality(blinded, brain=%s): turn=%s brain=%s baseline=%s delta=%s",
            scores.get("brain_position"), turn_id, scores.get("brain_overall"),
            scores.get("baseline_overall"), scores.get("delta"),
        )

    async def _run_pipeline(self, turn_id: str, user_input: str, brain_response: str,
                            baseline_response: str, memory_context: str,
                            coherence: float, trace: TurnTrace | None) -> None:
        llm_calls = getattr(trace, "llm_calls", "?") if trace else "?"
        cluster_tokens = getattr(trace, "cluster_tokens", {}) if trace else {}
        memory_recalled = getattr(trace, "memory_recalled", False) if trace else False
        drafter_count = getattr(trace, "drafter_count", 1) if trace else 1
        llm_calls_saved = getattr(trace, "llm_calls_saved", 0) if trace else 0
        hormonal = getattr(trace, "hormonal", {}) if trace else {}

        token_summary = (
            "; ".join(
                f"{cl}: {v.get('calls', 0)} calls / {v.get('in', 0)+v.get('out', 0)} tokens"
                for cl, v in cluster_tokens.items()
            ) if cluster_tokens else "(not available)"
        )
        hormonal_text = (
            ", ".join(f"{k}={v:.3f}" for k, v in hormonal.items())
            if hormonal else "(not available)"
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
            f"  Per-cluster breakdown: {token_summary}\n"
            f"  Hormonal state (5HT/CORT/OXT/AEA): {hormonal_text}\n\n"
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
                           baseline_response: str, trace: TurnTrace | None) -> None:
        emotion = getattr(trace, "emotion", "neutral") if trace else "neutral"
        emotion_core = getattr(trace, "emotion_core", "neutral") if trace else "neutral"
        llm_calls_saved = getattr(trace, "llm_calls_saved", 0) if trace else 0
        hormonal = getattr(trace, "hormonal", {}) if trace else {}
        has_anticipations = bool(
            trace and getattr(trace, "fired_path", None) and
            any(p.get("tag") == "anticipator" for p in trace.fired_path)
        )
        hormonal_text = (
            ", ".join(f"{k}={v:.3f}" for k, v in hormonal.items())
            if hormonal else "(not available)"
        )

        prompt = (
            f"User message:\n{user_input}\n\n"
            f"Brain response (multi-cluster pipeline):\n{brain_response}\n\n"
            f"Baseline response (single plain LLM call):\n{baseline_response}\n\n"
            f"Context about the brain this turn:\n"
            f"  Detected emotion: {emotion} (core: {emotion_core})\n"
            f"  Hormonal state (5HT/CORT/OXT/AEA): {hormonal_text}\n"
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

    async def _run_faithfulness(self, turn_id: str, brain_response: str,
                                memory_context: str) -> None:
        prompt = (
            f"Background notes (recalled long-term memory):\n{memory_context[:1500]}\n\n"
            f"AI response:\n{brain_response}\n\n"
            "Check the response for faithfulness to the notes."
        )
        raw = await self._router.call(
            "haiku", _FAITHFULNESS_JUDGE_SYSTEM,
            [{"role": "user", "content": prompt}],
            cluster="scorer", cell="faithfulness_judge", turn_id="",
        )
        scores = safe_json_parse(raw)
        if not scores:
            logger.warning("PostHocScorer: faithfulness judge parse failed for turn %s", turn_id)
            return
        self._eval_logger.patch_turn(turn_id, faithfulness_scores=scores)
        if self._obs:
            lf = {f"faithfulness.{k}": v for k, v in scores.items()
                  if k != "reasoning" and isinstance(v, (int, float))}
            self._obs.record_scores(turn_id, lf, comment=scores.get("reasoning", ""))
        logger.debug(
            "PostHocScorer faithfulness: turn=%s faithful=%s unsupported=%s",
            turn_id, scores.get("faithfulness"), scores.get("unsupported_claims"),
        )
