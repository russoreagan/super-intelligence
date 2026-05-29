"""
Observability — Langfuse tracing + Streamlit dashboard support.
Langfuse: per-call tracing tagged by cluster/cell/turn.
Dashboard: cluster activation heatmap + message timeline data.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)


@dataclass
class TurnTrace:
    turn_id: str
    session_id: str
    user_input: str
    response: str = ""
    llm_calls: int = 0
    elapsed_s: float = 0.0
    emotion: str = "neutral"
    emotion_core: str = "neutral"  # feeling-wheel parent (happy/sad/anger/…)
    neuromod: dict = field(default_factory=dict)
    hormonal: dict = field(default_factory=dict)  # 5HT, CORT, OXT snapshot
    cluster_activations: dict[str, float] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    # ── Eval fields (all have defaults — fully backward compatible) ──────────

    # Draft scores from frontal critic (free — already computed each turn)
    draft_scores: list[dict] = field(default_factory=list)
    # e.g. [{"draft_id": "draft_0_abc", "coherence": 0.8, "relevance": 0.9,
    #         "tone_fit": 0.7, "empathy_score": 0.85, "overall": 0.82, "selected": True}]
    selected_draft_id: str = ""
    drafter_count: int = 0

    # Per-cluster token usage (derived from model_router._call_log after enrichment)
    cluster_tokens: dict[str, dict] = field(default_factory=dict)
    # e.g. {"frontal": {"in": 1200, "out": 340, "calls": 5}, ...}

    # Memory recall signal
    memory_recalled: bool = False
    memory_hit_count: int = 0

    # Baseline comparison (populated async by BaselineRunner — may be empty)
    baseline_response: str = ""
    baseline_model: str = ""
    baseline_latency_s: float = 0.0

    # Post-hoc judge scores (populated async by PostHocScorer — may be empty)
    judge_scores: dict = field(default_factory=dict)
    # e.g. {"memory_utilization": 0.7, "personality_consistency": 0.8,
    #        "self_awareness": 0.6, "brain_overall": 0.78,
    #        "baseline_overall": 0.55, "delta": +0.23, "reasoning": "..."}

    # ── Predict-and-surprise / Hebbian fields ─────────────────────────────

    # Each entry: {"cluster", "predicted", "actual", "confidence",
    #              "surprise", "integrator_woken", "bypass_reason", "correct"}
    predictor_outcomes: list[dict] = field(default_factory=list)
    # Count of integrators that were gated off this turn (cost saved)
    llm_calls_saved: int = 0
    # How many times the emotion-aware veto forced an integrator to wake
    gating_bypassed_count: int = 0

    # Ordered list of switches + integrators that fired this turn.
    # Each entry: {"name", "cluster", "kind": "switch"|"integrator",
    #              "level", "tag", "ts"}
    fired_path: list[dict] = field(default_factory=list)

    # Neuromod state at the start of the turn (before any processing).
    # Used by the Hebbian pass to compute per-turn DA delta rather than
    # comparing DA to an arbitrary neutral baseline.
    prior_neuromod: dict = field(default_factory=dict)

    # User's detected emotional state this turn (from temporal understanding).
    # Stored here so the Hebbian sleep pass can use it without re-parsing features.
    user_emotion: str = ""

    # Modulation counters (incremented by SwitchNeuron.fire / should_fire)
    modulated_switch_count: int = 0  # switches where |mod_delta| > 0.01
    suppressed_switch_count: int = 0  # near-misses: level >= base but < effective

    # ── Deliberate emotion expression (set_mood tool + inline markup) ─────────
    # Each entry: {"emotion": str, "source": "tool"|"inline", "preview": str}
    # Empty list means purely reactive emotional state this turn.
    deliberate_emotions: list[dict] = field(default_factory=list)

    # ── Mid-turn neuromod updates (new injection points within a single turn) ──
    # Each entry: {"trigger": str, "snapshot": dict[str, float]}
    # Triggers: "hippocampus_recall", "tool_success", "tool_failure",
    #           "tool_exception", "draft_quality_low", "draft_quality_high"
    neuromod_midturn: list[dict] = field(default_factory=list)

    # ── Voice / prosody fields (populated when --ears is active) ─────────────
    speaker_name: str = ""
    speaker_score: float = 0.0  # voiceprint cosine similarity (0–1)
    prosody_tone: str = ""  # calm / stressed / energetic / whisper / monotone
    prosody_f0_hz: float = 0.0  # mean fundamental frequency
    prosody_energy: float = 0.0  # RMS energy
    prosody_jitter: float = 0.0  # pitch period perturbation
    prosody_shimmer: float = 0.0  # amplitude perturbation


_TRACE_WINDOW = 500  # max full TurnTrace objects kept in memory per session
_NEUROMOD_WINDOW = 2000  # lightweight {ts, neuromod} snapshots for history chart


class ObservabilityLayer:
    def __init__(self, session_id: str, eval_logger: EvalLogger | None = None) -> None:
        self._session_id = session_id
        self._traces: list[TurnTrace] = []
        self._neuromod_history: deque[dict] = deque(maxlen=_NEUROMOD_WINDOW)
        self._langfuse = None
        self._active_spans: dict[str, Any] = {}
        self._active_cluster_spans: dict[str, Any] = {}  # "{turn_id}:{cluster}" → span
        self._trace_ids: dict[str, str] = {}
        self._eval_logger = eval_logger
        self._init_langfuse()

    def _init_langfuse(self) -> None:
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not pk or not sk:
            logger.debug(
                "Observability: Langfuse not configured — set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY to enable tracing"
            )
            return
        try:
            from langfuse import Langfuse

            self._langfuse = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=os.environ.get("LANGFUSE_HOST")
                or os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
            )
            logger.info("Observability: Langfuse tracing connected")
        except Exception as e:
            logger.debug("Observability: Langfuse init failed: %s", e)

    def begin_turn(self, turn_id: str, user_input: str) -> None:
        """Call at the start of each turn to open the root trace span."""
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            with propagate_attributes(session_id=self._session_id, trace_name="brain-turn"):
                span = self._langfuse.start_observation(
                    name="brain-turn",
                    as_type="span",
                    input={"user": user_input},
                )
            self._active_spans[turn_id] = span
            self._trace_ids[turn_id] = span.trace_id
        except Exception as e:
            logger.debug("Langfuse begin_turn failed: %s", e)

    def begin_cluster(self, turn_id: str, cluster: str, note: str = "") -> None:
        """Open a child span for one cluster's execution within a turn."""
        if not self._langfuse:
            return
        parent = self._active_spans.get(turn_id)
        if not parent:
            return
        key = f"{turn_id}:{cluster}"
        try:
            span = parent.start_observation(
                name=cluster,
                as_type="span",
                input={"note": note} if note else {},
                metadata={"cluster": cluster},
            )
            self._active_cluster_spans[key] = (span, time.time())
        except Exception as e:
            logger.debug("Langfuse begin_cluster(%s) failed: %s", cluster, e)

    def end_cluster(self, turn_id: str, cluster: str) -> None:
        """Close the cluster span opened by begin_cluster."""
        if not self._langfuse:
            return
        key = f"{turn_id}:{cluster}"
        entry = self._active_cluster_spans.pop(key, None)
        if entry:
            span, started = entry
            try:
                span.update(
                    metadata={"cluster": cluster, "latency_s": round(time.time() - started, 3)}
                )
                span.end()
            except Exception as e:
                logger.debug("Langfuse end_cluster(%s) failed: %s", cluster, e)

    def record_deliberate_emotion(
        self, turn_id: str, emotion: str, source: str, preview: str = ""
    ) -> None:
        """Record one deliberate emotion expression within a turn.

        Called by:
          - MotorCortexCluster._set_mood()  → source="tool"
          - session_turn draining meta.mood_expression → source="inline"

        Appends to the active span's pending metadata so it's included when
        record_turn() finalises the span.  Safe to call with no Langfuse config.
        """
        entry = {
            "emotion": emotion,
            "source": source,
            **({"preview": preview[:80]} if preview else {}),
        }
        # Stash on the span object itself so record_turn() can pick it up
        # without needing a separate dict.  Falls back gracefully if the span
        # doesn't support arbitrary attribute assignment.
        span = self._active_spans.get(turn_id)
        if span is not None:
            pending: list = getattr(span, "_deliberate_emotions", None)
            if pending is None:
                try:
                    span._deliberate_emotions = []
                    pending = span._deliberate_emotions
                except AttributeError:
                    pending = None
            if pending is not None:
                pending.append(entry)
        logger.debug("Observability: deliberate_emotion turn=%s %s/%s", turn_id, source, emotion)

    def record_turn(self, trace: TurnTrace) -> None:
        self._traces.append(trace)
        if len(self._traces) > _TRACE_WINDOW:
            self._traces = self._traces[-_TRACE_WINDOW:]
        if trace.neuromod:
            entry = {"ts": trace.ts, **trace.neuromod}
            if trace.hormonal:
                entry["hormonal"] = trace.hormonal
            self._neuromod_history.append(entry)
        if self._langfuse:
            try:
                span = self._active_spans.pop(trace.turn_id, None)
                if span:
                    # Pull any deliberate emotion entries stashed by record_deliberate_emotion()
                    stashed = getattr(span, "_deliberate_emotions", None)
                    if stashed:
                        trace.deliberate_emotions.extend(stashed)
                    switches_fired = sum(1 for e in trace.fired_path if e.get("kind") == "switch")
                    try:
                        from brain.settings import settings as _s

                        _gain = float(_s.get("modulation_gain", 1.0))
                    except Exception:
                        _gain = 1.0
                    span.update(
                        output={"response": trace.response},
                        metadata={
                            # ── core turn stats ───────────────────────────
                            "llm_calls": trace.llm_calls,
                            "elapsed_s": round(trace.elapsed_s, 3),
                            "llm_calls_saved": trace.llm_calls_saved,
                            "gating_bypassed_count": trace.gating_bypassed_count,
                            # ── emotion ───────────────────────────────────
                            "emotion": trace.emotion,
                            "emotion_core": trace.emotion_core,
                            **(
                                {"deliberate_emotions": trace.deliberate_emotions}
                                if trace.deliberate_emotions
                                else {}
                            ),
                            "deliberate_emotion_count": len(trace.deliberate_emotions),
                            **({"user_emotion": trace.user_emotion} if trace.user_emotion else {}),
                            # ── neuromod + hormonal snapshots ─────────────
                            **({"neuromod": trace.neuromod} if trace.neuromod else {}),
                            **(
                                {"prior_neuromod": trace.prior_neuromod}
                                if trace.prior_neuromod
                                else {}
                            ),
                            **({"hormonal": trace.hormonal} if trace.hormonal else {}),
                            **(
                                {"neuromod_midturn": trace.neuromod_midturn}
                                if trace.neuromod_midturn
                                else {}
                            ),
                            # ── memory ────────────────────────────────────
                            "memory_recalled": trace.memory_recalled,
                            "memory_hit_count": trace.memory_hit_count,
                            # ── frontal / drafting ────────────────────────
                            "drafter_count": trace.drafter_count,
                            **(
                                {"selected_draft_id": trace.selected_draft_id}
                                if trace.selected_draft_id
                                else {}
                            ),
                            **({"draft_scores": trace.draft_scores} if trace.draft_scores else {}),
                            # ── token + cluster breakdown ─────────────────
                            **(
                                {"cluster_tokens": trace.cluster_tokens}
                                if trace.cluster_tokens
                                else {}
                            ),
                            **(
                                {"cluster_activations": trace.cluster_activations}
                                if trace.cluster_activations
                                else {}
                            ),
                            # ── switch / modulation summary ───────────────
                            "switches_fired": switches_fired,
                            "modulated_switch_count": trace.modulated_switch_count,
                            "suppressed_switch_count": trace.suppressed_switch_count,
                            "modulation_gain": _gain,
                            # ── predict-and-surprise / wiring ─────────────
                            **(
                                {"predictor_outcomes": trace.predictor_outcomes}
                                if trace.predictor_outcomes
                                else {}
                            ),
                            **({"fired_path": trace.fired_path} if trace.fired_path else {}),
                            # ── voice / prosody (non-empty when --ears active)
                            **({"speaker_name": trace.speaker_name} if trace.speaker_name else {}),
                            **(
                                {"speaker_score": trace.speaker_score}
                                if trace.speaker_score
                                else {}
                            ),
                            **({"prosody_tone": trace.prosody_tone} if trace.prosody_tone else {}),
                            **(
                                {"prosody_f0_hz": trace.prosody_f0_hz}
                                if trace.prosody_f0_hz
                                else {}
                            ),
                            **(
                                {"prosody_energy": trace.prosody_energy}
                                if trace.prosody_energy
                                else {}
                            ),
                            **(
                                {"prosody_jitter": trace.prosody_jitter}
                                if trace.prosody_jitter
                                else {}
                            ),
                            **(
                                {"prosody_shimmer": trace.prosody_shimmer}
                                if trace.prosody_shimmer
                                else {}
                            ),
                        },
                    )
                    span.end()
                    self._trace_ids[trace.turn_id] = span.trace_id
                else:
                    # begin_turn wasn't called; create a flat trace as fallback
                    from langfuse import propagate_attributes

                    with propagate_attributes(session_id=self._session_id, trace_name="brain-turn"):
                        span = self._langfuse.start_observation(
                            name="brain-turn",
                            as_type="span",
                            input={"user": trace.user_input},
                            output={"response": trace.response},
                            metadata={
                                "llm_calls": trace.llm_calls,
                                "elapsed_s": round(trace.elapsed_s, 3),
                                "emotion": trace.emotion,
                                "emotion_core": trace.emotion_core,
                                **({"neuromod": trace.neuromod} if trace.neuromod else {}),
                                **({"hormonal": trace.hormonal} if trace.hormonal else {}),
                                "memory_recalled": trace.memory_recalled,
                                "drafter_count": trace.drafter_count,
                                "llm_calls_saved": trace.llm_calls_saved,
                                **(
                                    {"cluster_tokens": trace.cluster_tokens}
                                    if trace.cluster_tokens
                                    else {}
                                ),
                                **(
                                    {"draft_scores": trace.draft_scores}
                                    if trace.draft_scores
                                    else {}
                                ),
                                **({"fired_path": trace.fired_path} if trace.fired_path else {}),
                            },
                        )
                    span.end()
                    self._trace_ids[trace.turn_id] = span.trace_id

                # Post internal critic scores for the selected draft
                selected = next((d for d in trace.draft_scores if d.get("selected")), None)
                if selected:
                    self._post_scores(
                        trace.turn_id,
                        {
                            "critic.overall": selected.get("overall", 0),
                            "critic.coherence": selected.get("coherence", 0),
                            "critic.tone_fit": selected.get("tone_fit", 0),
                            "critic.empathy": selected.get(
                                "empathy_score"
                            ),  # None when empathy check didn't run → dropped by _post_scores
                        },
                    )

                # Trim old trace_ids to avoid unbounded growth in long sessions
                if len(self._trace_ids) > 200:
                    oldest = list(self._trace_ids.keys())[:50]
                    for k in oldest:
                        self._trace_ids.pop(k, None)

            except Exception as e:
                logger.debug("Langfuse record_turn failed: %s", e)
        if self._eval_logger:
            try:
                self._eval_logger.log_turn(trace)
            except Exception as e:
                logger.debug("EvalLogger.log_turn failed: %s", e)

    def record_scores(self, turn_id: str, scores: dict[str, Any], *, comment: str = "") -> None:
        """Post judge scores to Langfuse as trace-level scores.

        Called by PostHocScorer after LLM-as-judge completes.
        Scores appear in the Langfuse UI's Scores view and can be filtered.
        """
        self._post_scores(turn_id, scores, comment=comment)

    def _post_scores(self, turn_id: str, scores: dict[str, Any], *, comment: str = "") -> None:
        if not self._langfuse:
            return
        trace_id = self._trace_ids.get(turn_id)
        if not trace_id:
            logger.debug("Langfuse _post_scores: no trace_id for turn %s", turn_id)
            return
        for name, value in scores.items():
            if not isinstance(value, (int, float)):
                continue
            try:
                self._langfuse.create_score(
                    trace_id=trace_id,
                    name=name,
                    value=float(value),
                    data_type="NUMERIC",
                    comment=comment or None,
                )
            except Exception as e:
                logger.debug("Langfuse create_score(%s) failed: %s", name, e)

    def record_baseline(
        self,
        turn_id: str,
        user_input: str,
        baseline_response: str,
        baseline_model: str,
        latency_s: float,
    ) -> None:
        """Create a sibling Langfuse generation for the baseline comparison call.

        Called by BaselineRunner after the plain LLM call completes. Creates a
        separate generation span under the same session (not nested under the
        brain-turn span, which is already closed) tagged with turn_id so it can
        be correlated in the Langfuse UI.
        """
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            with propagate_attributes(session_id=self._session_id, trace_name="baseline-call"):
                gen = self._langfuse.start_observation(
                    name="baseline-call",
                    as_type="generation",
                    model=baseline_model,
                    input={"user": user_input},
                    output={"response": baseline_response},
                    metadata={
                        "turn_id": turn_id,
                        "latency_s": round(latency_s, 3),
                    },
                )
            gen.end()
        except Exception as e:
            logger.debug("Langfuse record_baseline failed: %s", e)

    def record_thought(
        self,
        thought: str,
        direction: str,
        angle: str | None,
        count: int,
        neuromod: dict | None = None,
    ) -> None:
        """Create a standalone Langfuse trace for one DMN internal thought."""
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            with propagate_attributes(session_id=self._session_id, trace_name="dmn-thought"):
                span = self._langfuse.start_observation(
                    name="dmn-thought",
                    as_type="span",
                    input={"thought": thought},
                    metadata={
                        "direction": direction,
                        "angle": angle or "",
                        "count": count,
                        **({"neuromod": neuromod} if neuromod else {}),
                    },
                )
            span.end()
        except Exception as e:
            logger.debug("Langfuse record_thought failed: %s", e)

    def record_modulation_event(
        self,
        switch_name: str,
        cluster: str,
        suppressed: bool,
        chem: dict | None = None,
        level: float = 0.0,
        effective_threshold: float = 0.0,
    ) -> None:
        """Standalone Langfuse event for out-of-turn chemistry gate outcomes
        (DMN idle_gate, metacognition self_monitor_trigger, etc.)."""
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            outcome = "suppressed" if suppressed else "allowed"
            with propagate_attributes(
                session_id=self._session_id, trace_name=f"modulation-{outcome}"
            ):
                span = self._langfuse.start_observation(
                    name=f"{cluster}.{switch_name}.{outcome}",
                    as_type="span",
                    input={"level": level, "effective_threshold": effective_threshold},
                    metadata={
                        "cluster": cluster,
                        "switch": switch_name,
                        "suppressed": suppressed,
                        **(
                            {"chem": {k: round(float(v), 3) for k, v in chem.items()}}
                            if chem
                            else {}
                        ),
                    },
                )
            span.end()
        except Exception as e:
            logger.debug("Langfuse record_modulation_event failed: %s", e)

    def record_dmn_failure(
        self, *, step: str, error: str = "", consecutive: int = 0, backoff: float = 1.0
    ) -> None:
        """Record a DMN step/tick failure so dark degradation is visible.

        Always increments the in-process counters (cheap, queryable via
        dmn_failure_counts()); also emits a Langfuse span when configured. The
        DMN itself only skips-and-backs-off on failure — this is the signal that
        makes that backoff observable rather than silent."""
        self._dmn_failures[step] = self._dmn_failures.get(step, 0) + 1
        self._dmn_failure_total += 1
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            with propagate_attributes(session_id=self._session_id, trace_name="dmn-failure"):
                span = self._langfuse.start_observation(
                    name=f"dmn.{step}.failure",
                    as_type="span",
                    input={"error": error},
                    metadata={
                        "step": step,
                        "consecutive": consecutive,
                        "backoff": backoff,
                    },
                )
            span.end()
        except Exception as e:
            logger.debug("Langfuse record_dmn_failure failed: %s", e)

    def dmn_failure_counts(self) -> dict:
        """Session rollup of DMN failures by step + total."""
        return {"by_step": dict(self._dmn_failures), "total": self._dmn_failure_total}

    def begin_job(self, job_id: str, goal: str, chem: dict | None = None) -> None:
        """Open a Langfuse trace for a background internal job.

        Creates an entry in _active_spans[job_id] so that subsequent
        record_llm_call(job_id, ...) calls automatically nest under it —
        the same mechanism used for per-turn tracing.
        """
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            with propagate_attributes(session_id=self._session_id, trace_name="brain-job"):
                span = self._langfuse.start_observation(
                    name="brain-job",
                    as_type="span",
                    input={"goal": goal},
                    metadata={
                        "job_id": job_id,
                        **(
                            {"neuromod": {k: round(float(v), 3) for k, v in chem.items()}}
                            if chem
                            else {}
                        ),
                    },
                )
            self._active_spans[job_id] = span
        except Exception as e:
            logger.debug("Langfuse begin_job(%s) failed: %s", job_id, e)

    def end_job(
        self,
        job_id: str,
        *,
        success: bool,
        steps_completed: int,
        steps_planned: int,
        total_attempts: int = 0,
    ) -> None:
        """Close the Langfuse trace opened by begin_job.

        total_attempts counts every tool dispatch across all stories + retries
        (including appropriateness-gate re-plans and criteria-check retries),
        so it's always >= steps_completed. A gap > 0 means retries fired.
        """
        if not self._langfuse:
            return
        span = self._active_spans.pop(job_id, None)
        if span:
            try:
                span.update(
                    output={"success": success, "steps_completed": steps_completed},
                    metadata={
                        "success": success,
                        "steps_completed": steps_completed,
                        "steps_planned": steps_planned,
                        "total_attempts": total_attempts,
                        "retries": max(0, total_attempts - steps_completed),
                    },
                )
                span.end()
            except Exception as e:
                logger.debug("Langfuse end_job(%s) failed: %s", job_id, e)

    def record_llm_call(
        self,
        turn_id: str,
        cluster: str,
        cell: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_s: float,
    ) -> None:
        if not self._langfuse:
            return
        try:
            # Prefer the active cluster span so the generation nests inside it.
            cluster_entry = self._active_cluster_spans.get(f"{turn_id}:{cluster}")
            parent = cluster_entry[0] if cluster_entry else self._active_spans.get(turn_id)
            if parent:
                gen = parent.start_observation(
                    name=f"{cluster}.{cell}",
                    as_type="generation",
                    model=model,
                    usage_details={"input": prompt_tokens, "output": completion_tokens},
                    metadata={"latency_s": round(latency_s, 3)},
                )
                gen.end()
        except Exception as e:
            logger.debug("Langfuse record_llm_call failed: %s", e)

    def dashboard_data(self) -> dict:
        """Data structure for Streamlit dashboard."""
        return {
            "session_id": self._session_id,
            "turn_count": len(self._traces),
            "traces": [
                {
                    "turn_id": t.turn_id,
                    "ts": t.ts,
                    "user": t.user_input[:80],
                    "response": t.response[:80],
                    "llm_calls": t.llm_calls,
                    "elapsed_s": t.elapsed_s,
                    "emotion": t.emotion,
                    "neuromod": t.neuromod,
                }
                for t in self._traces[-20:]  # last 20 turns
            ],
            "neuromod_history": list(self._neuromod_history),
        }

    def record_session_learning(
        self, session_id: str, judge_scores: dict, structural_metrics: dict
    ) -> None:
        """Create a standalone Langfuse span for session-level learning scores.

        Called by LearningJudge at session end. Scores appear in Langfuse tagged
        with the session_id so they're co-located with all other turns.
        """
        if not self._langfuse:
            return
        try:
            from langfuse import propagate_attributes

            with propagate_attributes(session_id=session_id, trace_name="learning-summary"):
                span = self._langfuse.start_observation(
                    name="learning-summary",
                    as_type="span",
                    input={
                        "session_id": session_id,
                        "turns": structural_metrics.get("turns_recorded", 0),
                    },
                    output={"reasoning": judge_scores.get("learning_reasoning", "")},
                    metadata={
                        **{
                            k: v
                            for k, v in structural_metrics.items()
                            if k not in ("wiring_deltas",) and isinstance(v, (int, float, str))
                        },
                    },
                )
            # Post judge scores as numeric Langfuse scores on this span's trace
            span.end()
            tid = span.trace_id
            lf_scores = {
                f"learning.{k}": v
                for k, v in judge_scores.items()
                if k != "learning_reasoning" and isinstance(v, (int, float))
            }
            # Also include key structural metrics as scores for charting
            for key in (
                "predictor_accuracy_trend",
                "gating_efficiency_trend",
                "surprise_trend",
                "wiring_delta_magnitude",
                "cross_session_drift",
            ):
                val = structural_metrics.get(key)
                if isinstance(val, (int, float)):
                    lf_scores[f"learning.{key}"] = val
            for name, value in lf_scores.items():
                try:
                    self._langfuse.create_score(
                        trace_id=tid,
                        name=name,
                        value=float(value),
                        data_type="NUMERIC",
                    )
                except Exception as e:
                    logger.debug("Langfuse create_score(%s) failed: %s", name, e)
        except Exception as e:
            logger.debug("record_session_learning failed: %s", e)

    def flush(self) -> None:
        if self._langfuse:
            with contextlib.suppress(Exception):
                self._langfuse.flush()
