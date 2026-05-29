"""Hebbian learning pass — runs at session end as part of sleep consolidation."""

from __future__ import annotations

import logging

from brain.emotion_hierarchy import CORE_VALENCE, valence_of
from brain.observability.decisions import decisions
from brain.settings import settings
from brain.wiring import Wiring

logger = logging.getLogger(__name__)


class HebbianUpdater:
    """Apply Hebbian weight updates to the wiring graph after a session."""

    _CORE_VALENCE = CORE_VALENCE

    def __init__(self, wiring: Wiring) -> None:
        self._wiring = wiring

    @classmethod
    def _emotion_valence(cls, emotion: str | None) -> float:
        return valence_of(emotion)

    def _composite_outcome(self, trace) -> tuple[float, dict]:
        """Return (outcome, breakdown) for a single TurnTrace. Outcome in [-1, +1].

        Signal sources:
        - DA delta (50%): how much DA changed THIS turn vs start of turn.
        - Critic score (30%): actual LLM critic assessment; only when critic_ran=True.
        - User emotion valence (20%): valence of the user's detected emotional state.
        """
        nm = trace.neuromod or {}
        da = float(nm.get("DA", 0.5))

        prior_nm = getattr(trace, "prior_neuromod", None) or {}
        da_prior = float(prior_nm.get("DA", da))
        da_delta = (da - da_prior) * 4.0
        da_delta = max(-1.0, min(1.0, da_delta))

        critic_term = 0.0
        for d in trace.draft_scores or []:
            if d.get("selected") and d.get("critic_ran"):
                critic_term = (float(d.get("overall", 0.5)) - 0.5) * 2.0
                break

        user_emotion = getattr(trace, "user_emotion", "") or ""
        user_term = self._emotion_valence(user_emotion)

        outcome = 0.5 * da_delta + 0.3 * critic_term + 0.2 * user_term
        outcome = max(-1.0, min(1.0, outcome))
        return outcome, {
            "da_delta": round(da_delta, 3),
            "da_prior": round(da_prior, 3),
            "da_current": round(da, 3),
            "critic": round(critic_term, 3),
            "user_emotion": round(user_term, 3),
        }

    def _plasticity_modulator(self, full_traces: list) -> float:
        """Session-averaged DA + ACh → plasticity scalar in [0.3, 1.2]."""
        if not full_traces:
            return 1.0
        da_avg = sum(float(t.neuromod.get("DA", 0.5)) for t in full_traces) / len(full_traces)
        ach_avg = sum(float(t.neuromod.get("ACh", 0.3)) for t in full_traces) / len(full_traces)
        mod = 0.5 + da_avg + 0.5 * ach_avg
        return max(0.3, min(1.2, mod))

    def _should_skip_hebbian(self, trace, outcome: float) -> tuple[bool, str]:
        """Skip Hebbian for turns where the entity wasn't in a state worth learning from."""
        if abs(outcome) < 0.02:
            return True, "outcome_near_zero"
        gaba = float(trace.neuromod.get("GABA", 0.0))
        if gaba > settings.get("gaba_skip_threshold_high") and len(trace.draft_scores) <= 1:
            return True, "defuse_path"
        emotion = (trace.emotion or "").lower()
        if emotion in ("confused", "flat"):
            return True, f"dissociated_emotion={emotion}"
        return False, ""

    def _apply_drafter_competition(
        self, trace, outcome: float, plasticity: float, gainers: list, losers: list
    ) -> None:
        """Extra reinforcement for multi-draft turns: winning drafter edges gain a bonus;
        non-winning drafters get a small penalty. Applies only when critic_ran=True."""
        draft_scores = trace.draft_scores or []
        real_scored = [d for d in draft_scores if d.get("critic_ran")]
        if len(real_scored) < 2:
            return

        selected = next((d for d in real_scored if d.get("selected")), None)
        if selected is None:
            return

        winner_id = selected.get("draft_id", "")
        winner_overall = float(selected.get("overall", 0.5))

        bonus_scale = settings.get("hebbian_outcome_delta") * plasticity
        for d in real_scored:
            did = d.get("draft_id", "")
            parts = did.split("_")
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[1])
            except ValueError:
                continue
            drafter_name = f"frontal.drafter_{chr(65 + idx)}"
            edge = ("frontal.executive", drafter_name)
            if not self._wiring.has(*edge):
                continue

            prev = self._wiring.get_edge_weight(*edge)
            if did == winner_id:
                loser_scores = [
                    float(x.get("overall", 0.5))
                    for x in real_scored
                    if x.get("draft_id") != winner_id
                ]
                margin = winner_overall - (
                    sum(loser_scores) / len(loser_scores) if loser_scores else 0.5
                )
                bonus = margin * bonus_scale * 0.5
                self._wiring.hebbian_update([edge[0], edge[1]], bonus)
            else:
                loser_score = float(d.get("overall", 0.5))
                penalty_mag = (winner_overall - loser_score) * bonus_scale * 0.25
                self._wiring.hebbian_update([edge[0], edge[1]], -penalty_mag)

            now = self._wiring.get_edge_weight(*edge)
            edge_delta = now - prev
            if abs(edge_delta) > 0.001:
                label = f"{edge[0]}→{edge[1]}"
                if edge_delta > 0:
                    gainers.append((label, edge_delta))
                else:
                    losers.append((label, edge_delta))
                decisions.log(
                    "drafter_competition_applied",
                    turn_id=trace.turn_id,
                    drafter=drafter_name,
                    won=(did == winner_id),
                    from_weight=round(prev, 4),
                    to_weight=round(now, 4),
                    delta=round(edge_delta, 4),
                    winner_score=round(winner_overall, 3),
                )

    def _drafter_competition_edge_count(self, trace) -> int:
        """Count drafter competition edge updates (for total_updated accounting)."""
        draft_scores = trace.draft_scores or []
        real_scored = [d for d in draft_scores if d.get("critic_ran")]
        if len(real_scored) < 2:
            return 0
        count = 0
        for d in real_scored:
            parts = d.get("draft_id", "").split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                drafter_name = f"frontal.drafter_{chr(65 + int(parts[1]))}"
                if self._wiring.has("frontal.executive", drafter_name):
                    count += 1
        return count

    def run(self, session_id: str, full_traces: list) -> None:
        """Apply gentle decay then per-turn Hebbian updates along firing paths."""
        self._wiring.decay_toward_rest(rest=1.0, rate=0.01)

        plasticity = self._plasticity_modulator(full_traces)
        gainers: list[tuple[str, float]] = []
        losers: list[tuple[str, float]] = []
        total_updated = 0
        skipped = 0

        for trace in full_traces:
            if not trace.fired_path:
                skipped += 1
                continue

            outcome, breakdown = self._composite_outcome(trace)
            skip, reason = self._should_skip_hebbian(trace, outcome)
            if skip:
                decisions.log(
                    "hebbian_update_skipped",
                    turn_id=trace.turn_id,
                    reason=reason,
                    outcome=round(outcome, 3),
                )
                skipped += 1
                continue

            delta = outcome * settings.get("hebbian_outcome_delta") * plasticity
            path_names = [n["name"] for n in trace.fired_path]
            before = {
                (path_names[i], path_names[i + 1]): self._wiring.get_edge_weight(
                    path_names[i], path_names[i + 1]
                )
                for i in range(len(path_names) - 1)
                if self._wiring.has(path_names[i], path_names[i + 1])
            }
            updated = self._wiring.hebbian_update(path_names, delta)
            total_updated += updated
            for (src, tgt), prev in before.items():
                now = self._wiring.get_edge_weight(src, tgt)
                edge_delta = now - prev
                if abs(edge_delta) > 0.001:
                    if edge_delta > 0:
                        gainers.append((f"{src}→{tgt}", edge_delta))
                    else:
                        losers.append((f"{src}→{tgt}", edge_delta))
                    decisions.log(
                        "hebbian_update_applied",
                        turn_id=trace.turn_id,
                        src=src,
                        tgt=tgt,
                        from_weight=round(prev, 4),
                        to_weight=round(now, 4),
                        delta=round(edge_delta, 4),
                        outcome=round(outcome, 3),
                        breakdown=breakdown,
                    )

            self._apply_drafter_competition(trace, outcome, plasticity, gainers, losers)
            total_updated += self._drafter_competition_edge_count(trace)

        try:
            self._wiring.save()
        except Exception as e:
            logger.warning("[Memory consolidation] Wiring save failed: %s", e)
        try:
            self._wiring.snapshot_to_history(session_id)
        except Exception as e:
            logger.debug("[Memory consolidation] Wiring snapshot failed: %s", e)

        turns_with_critic = sum(
            1
            for t in full_traces
            if any(d.get("critic_ran") and d.get("selected") for d in (t.draft_scores or []))
        )
        turns_with_user_emotion = sum(1 for t in full_traces if getattr(t, "user_emotion", ""))
        turns_with_da_delta = sum(
            1
            for t in full_traces
            if abs(
                float(
                    (getattr(t, "prior_neuromod", None) or {}).get(
                        "DA", float((t.neuromod or {}).get("DA", 0.5))
                    )
                )
                - float((t.neuromod or {}).get("DA", 0.5))
            )
            > 0.01
        )
        top_gainers = sorted(gainers, key=lambda x: x[1], reverse=True)[:5]
        top_losers = sorted(losers, key=lambda x: x[1])[:5]
        decisions.log(
            "session_plasticity_summary",
            session_id=session_id,
            plasticity_modulator=round(plasticity, 3),
            edges_updated=total_updated,
            turns_skipped=skipped,
            signal_quality={
                "turns_with_critic_score": turns_with_critic,
                "turns_with_user_emotion": turns_with_user_emotion,
                "turns_with_da_delta": turns_with_da_delta,
                "total_turns": len(full_traces),
            },
            top_gainers=[{"edge": e, "delta": round(d, 4)} for e, d in top_gainers],
            top_losers=[{"edge": e, "delta": round(d, 4)} for e, d in top_losers],
        )
        logger.info(
            "[Memory consolidation] Hebbian: plasticity=%.2f edges_updated=%d "
            "turns_skipped=%d critic_turns=%d/%d",
            plasticity,
            total_updated,
            skipped,
            turns_with_critic,
            len(full_traces),
        )
