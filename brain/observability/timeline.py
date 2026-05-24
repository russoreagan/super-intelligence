"""
Observability — Langfuse tracing + Streamlit dashboard support.
Langfuse: per-call tracing tagged by cluster/cell/turn.
Dashboard: cluster activation heatmap + message timeline data.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

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
    emotion_core: str = "neutral"   # feeling-wheel parent (happy/sad/anger/…)
    neuromod: dict = field(default_factory=dict)
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

    # ── Voice / prosody fields (populated when --ears is active) ─────────────
    speaker_name: str = ""
    speaker_score: float = 0.0   # voiceprint cosine similarity (0–1)
    prosody_tone: str = ""       # calm / stressed / energetic / whisper / monotone
    prosody_f0_hz: float = 0.0   # mean fundamental frequency
    prosody_energy: float = 0.0  # RMS energy
    prosody_jitter: float = 0.0  # pitch period perturbation
    prosody_shimmer: float = 0.0 # amplitude perturbation


_TRACE_WINDOW = 500   # max full TurnTrace objects kept in memory per session
_NEUROMOD_WINDOW = 2000  # lightweight {ts, neuromod} snapshots for history chart


class ObservabilityLayer:
    def __init__(self, session_id: str, eval_logger: "EvalLogger | None" = None) -> None:
        self._session_id = session_id
        self._traces: list[TurnTrace] = []
        self._neuromod_history: deque[dict] = deque(maxlen=_NEUROMOD_WINDOW)
        self._langfuse = None
        self._active_spans: dict[str, Any] = {}
        self._trace_ids: dict[str, str] = {}
        self._eval_logger = eval_logger
        self._init_langfuse()

    def _init_langfuse(self) -> None:
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not pk or not sk:
            logger.debug("Observability: Langfuse not configured — set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY to enable tracing")
            return
        try:
            from langfuse import Langfuse
            self._langfuse = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
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

    def record_turn(self, trace: TurnTrace) -> None:
        self._traces.append(trace)
        if len(self._traces) > _TRACE_WINDOW:
            self._traces = self._traces[-_TRACE_WINDOW:]
        if trace.neuromod:
            self._neuromod_history.append({"ts": trace.ts, **trace.neuromod})
        if self._langfuse:
            try:
                span = self._active_spans.pop(trace.turn_id, None)
                if span:
                    span.update(
                        output={"response": trace.response},
                        metadata={
                            "llm_calls": trace.llm_calls,
                            "elapsed_s": round(trace.elapsed_s, 3),
                            "emotion": trace.emotion,
                            "emotion_core": trace.emotion_core,
                            "memory_recalled": trace.memory_recalled,
                            "memory_hit_count": trace.memory_hit_count,
                            "drafter_count": trace.drafter_count,
                            "llm_calls_saved": trace.llm_calls_saved,
                            # voice / prosody (non-empty only when --ears active)
                            **({"speaker_name": trace.speaker_name} if trace.speaker_name else {}),
                            **({"speaker_score": trace.speaker_score} if trace.speaker_score else {}),
                            **({"prosody_tone": trace.prosody_tone} if trace.prosody_tone else {}),
                            **({"prosody_f0_hz": trace.prosody_f0_hz} if trace.prosody_f0_hz else {}),
                            **({"prosody_energy": trace.prosody_energy} if trace.prosody_energy else {}),
                            **({"prosody_jitter": trace.prosody_jitter} if trace.prosody_jitter else {}),
                            **({"prosody_shimmer": trace.prosody_shimmer} if trace.prosody_shimmer else {}),
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
                            },
                        )
                    span.end()
                    self._trace_ids[trace.turn_id] = span.trace_id

                # Post internal critic scores for the selected draft
                selected = next((d for d in trace.draft_scores if d.get("selected")), None)
                if selected:
                    self._post_scores(trace.turn_id, {
                        "critic.overall": selected.get("overall", 0),
                        "critic.coherence": selected.get("coherence", 0),
                        "critic.tone_fit": selected.get("tone_fit", 0),
                        "critic.empathy": selected.get("empathy_score", 0),
                    })

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

    def record_scores(self, turn_id: str, scores: dict[str, Any], *,
                      comment: str = "") -> None:
        """Post judge scores to Langfuse as trace-level scores.

        Called by PostHocScorer after LLM-as-judge completes.
        Scores appear in the Langfuse UI's Scores view and can be filtered.
        """
        self._post_scores(turn_id, scores, comment=comment)

    def _post_scores(self, turn_id: str, scores: dict[str, Any], *,
                     comment: str = "") -> None:
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

    def record_llm_call(self, turn_id: str, cluster: str, cell: str,
                         model: str, prompt_tokens: int, completion_tokens: int,
                         latency_s: float) -> None:
        if not self._langfuse:
            return
        try:
            parent = self._active_spans.get(turn_id)
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

    def flush(self) -> None:
        if self._langfuse:
            try:
                self._langfuse.flush()
            except Exception:
                pass
