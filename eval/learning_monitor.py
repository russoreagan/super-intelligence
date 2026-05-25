"""
LearningMonitor — structural learning metrics, computed from TurnTrace data.

No LLM required. Answers "are the learning systems moving?" with hard numbers.

Per-turn scores (sent to Langfuse on every turn):
  learning.predictor_accuracy    — fraction of correct predictions this turn
  learning.avg_predictor_confidence — mean predictor confidence (rising = more certain)
  learning.avg_surprise          — mean surprise score (falling = better prediction)
  learning.gating_efficiency     — llm_calls_saved / (calls + saved)
  learning.bypass_rate           — emotion-override fraction of all integrator decisions

Session summary (returned as a dict at session end for use by LearningJudge):
  predictor_accuracy_trend       — late-half accuracy minus early-half accuracy
  gating_efficiency_trend        — late-half efficiency minus early-half
  surprise_trend                 — late-half avg_surprise minus early-half (neg = improving)
  total_llm_calls_saved          — cumulative calls saved by gating all session
  wiring_edges_changed           — how many Hebbian edges moved this session
  wiring_delta_magnitude         — sum |delta| across all moved edges
  cross_session_drift            — RMS weight change vs. oldest wiring history snapshot
"""
from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace
    from brain.wiring import Wiring

logger = logging.getLogger(__name__)


class LearningMonitor:
    def __init__(self, obs=None) -> None:
        self._obs = obs
        self._turn_metrics: list[dict] = []

    def record_turn(self, trace: TurnTrace) -> dict:
        """Compute per-turn structural metrics and post to Langfuse. Returns the metrics dict."""
        outcomes = trace.predictor_outcomes or []

        total_outcomes = len(outcomes)
        correct = sum(1 for o in outcomes if o.get("correct"))
        accuracy = correct / total_outcomes if total_outcomes > 0 else None

        confidences = [o["confidence"] for o in outcomes if o.get("confidence") is not None]
        avg_confidence = sum(confidences) / len(confidences) if confidences else None

        surprises = [o["surprise"] for o in outcomes if o.get("surprise") is not None]
        avg_surprise = sum(surprises) / len(surprises) if surprises else None

        saved = trace.llm_calls_saved or 0
        total_calls = (trace.llm_calls or 0) + saved
        gating_eff = saved / total_calls if total_calls > 0 else 0.0

        bypassed = trace.gating_bypassed_count or 0
        total_integrators = total_outcomes + bypassed
        bypass_rate = bypassed / total_integrators if total_integrators > 0 else 0.0

        metrics = {
            "turn_idx": len(self._turn_metrics),
            "predictor_accuracy": accuracy,
            "avg_predictor_confidence": avg_confidence,
            "avg_surprise": avg_surprise,
            "gating_efficiency": gating_eff,
            "bypass_rate": bypass_rate,
            "llm_calls_saved": saved,
        }
        self._turn_metrics.append(metrics)

        if self._obs:
            lf = {
                f"learning.{k}": v
                for k, v in metrics.items()
                if k != "turn_idx" and isinstance(v, (int, float))
            }
            if lf:
                try:
                    self._obs.record_scores(trace.turn_id, lf)
                except Exception as e:
                    logger.debug("LearningMonitor: record_scores failed: %s", e)

        return metrics

    def session_metrics(self, wiring: Wiring | None = None) -> dict:
        """
        Compute session-level learning summary.
        Call at session end before the Hebbian pass runs (so wiring.session_deltas()
        reflects changes from this session's sleep consolidation).
        """
        n = len(self._turn_metrics)
        if n < 2:
            return {"turns_recorded": n}

        mid = max(1, n // 2)
        early = self._turn_metrics[:mid]
        late = self._turn_metrics[mid:]

        def _avg(lst: list[dict], key: str) -> float | None:
            vals = [x[key] for x in lst if isinstance(x.get(key), (int, float))]
            return sum(vals) / len(vals) if vals else None

        summary: dict = {
            "turns_recorded": n,
            "total_llm_calls_saved": sum(m.get("llm_calls_saved", 0) for m in self._turn_metrics),
        }

        for key in ("predictor_accuracy", "gating_efficiency", "avg_surprise"):
            e_val = _avg(early, key)
            l_val = _avg(late, key)
            summary[f"early_{key}"] = e_val
            summary[f"late_{key}"] = l_val
            if e_val is not None and l_val is not None:
                summary[f"{key}_trend"] = round(l_val - e_val, 4)

        # Wiring deltas (requires sleep consolidation to have run first — caller's responsibility)
        if wiring is not None:
            deltas = wiring.session_deltas()
            summary["wiring_edges_changed"] = len(deltas)
            summary["wiring_delta_magnitude"] = round(sum(abs(d["delta"]) for d in deltas), 4)
            if deltas:
                top = deltas[0]
                summary["wiring_top_gainer"] = f"{top['src']}→{top['tgt']} (+{top['delta']:.4f})"
            losers = [d for d in deltas if d["delta"] < 0]
            if losers:
                bot = losers[-1]
                summary["wiring_top_loser"] = f"{bot['src']}→{bot['tgt']} ({bot['delta']:.4f})"
            summary["wiring_deltas"] = deltas[:10]  # top 10 for judge context

        # Cross-session drift: compare current weights to oldest history snapshot
        if wiring is not None:
            drift = _cross_session_drift(wiring)
            if drift is not None:
                summary["cross_session_drift"] = round(drift, 4)

        return summary


def _cross_session_drift(wiring: Wiring) -> float | None:
    """
    RMS weight change relative to the oldest wiring history snapshot.
    Returns None if no history exists.
    """
    from brain.wiring import WIRING_HISTORY_DIR
    try:
        snapshots = sorted(WIRING_HISTORY_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime)
        if not snapshots:
            return None
        oldest = json.loads(snapshots[0].read_text())
        old_weights = {(e["src"], e["tgt"]): e["w"] for e in oldest.get("edges", [])}
        if not old_weights:
            return None
        sq_sum = 0.0
        count = 0
        for (src, tgt), edge in wiring._edges.items():
            old_w = old_weights.get((src, tgt))
            if old_w is not None:
                sq_sum += (edge.weight - old_w) ** 2
                count += 1
        return math.sqrt(sq_sum / count) if count > 0 else 0.0
    except Exception as e:
        logger.debug("cross_session_drift failed: %s", e)
        return None
