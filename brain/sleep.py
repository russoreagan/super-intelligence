"""
Sleep consolidation — runs at session end (or between sessions).
Re-indexes recent episodes, compresses for retrieval efficiency,
updates self.md autobiography, extracts facts to user.md.
Uses batch-friendly API calls (no real-time constraint).

v0.2 feature.
"""
from __future__ import annotations

import logging
import time

from brain.cell import IntegratorCell
from brain.emotion_hierarchy import CORE_VALENCE, valence_of
from brain.model_router import ModelRouter
from brain.observability.decisions import decisions
from brain.second_brain.store import EpisodicStore, SchemaStore
from brain.security import sanitize_fact
from brain.settings import settings
from brain.utils import safe_json_parse
from brain.wiring import Wiring

logger = logging.getLogger(__name__)

SELF_UPDATE_SYSTEM = """You are the sleep consolidation process of an AI brain.
Given the entity's current self-model and a summary of recent sessions,
rewrite the self-model's "History summary" and "Stable preferences" sections.
Be concise. Base preferences only on observed patterns, not aspirations.
Return JSON: {
  "history_summary": string,    // 2-3 sentences rolling autobiography
  "stable_preferences": string  // bullet list of confirmed behavioral tendencies
}
Return ONLY JSON."""

EPISODE_SYNTHESIS_SYSTEM = """You are consolidating episodic memories for an AI brain.
Given a batch of raw turn records, identify:
- Key facts learned about the user
- Topics of sustained interest
- Any patterns in the entity's responses worth noting

Return JSON: {
  "user_facts": [string],       // factual claims about the user to update user.md
  "topic_clusters": [string],   // recurring topic themes
  "response_patterns": [string] // observed tendencies in the entity's responses
}
Return ONLY JSON."""


class SleepConsolidation:
    def __init__(self, router: ModelRouter, schema: SchemaStore,
                 episodic: EpisodicStore, wiring: Wiring | None = None) -> None:
        self._router = router
        self._schema = schema
        self._episodic = episodic
        self._wiring = wiring

        self._self_updater = IntegratorCell(
            name="self_updater",
            cluster="sleep",
            model="local-general",
            system_prompt=SELF_UPDATE_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._self_updater.set_router(router)

        self._synthesizer = IntegratorCell(
            name="episode_synthesizer",
            cluster="sleep",
            model="local-general",
            system_prompt=EPISODE_SYNTHESIS_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._synthesizer.set_router(router)

    async def consolidate(self, session_id: str, session_traces: list[dict],
                          full_traces: list | None = None) -> None:
        """
        Run full consolidation after a session ends.
        session_traces: list of {user_input, entity_response, emotion, topic_tags} dicts.
        full_traces: list of TurnTrace objects (carry fired_path, neuromod, draft_scores)
                     — used for the Hebbian pass. Pass [] or None to skip Hebbian.
        """
        if not session_traces:
            return

        logger.info("[Memory consolidation] Processing %d turns from session %s",
                    len(session_traces), session_id)
        start = time.time()

        # ── Hebbian pass (independent of LLM consolidation; runs synchronously) ──
        if full_traces and self._wiring is not None:
            self._run_hebbian_pass(session_id, full_traces)

        # 1. Episode synthesis — extract facts per speaker
        # Group the last 20 turns by speaker so facts land in the right schema file.
        # Turns without a speaker_name go to user.md (primary user).
        from collections import defaultdict
        speaker_turns: dict[str, list[dict]] = defaultdict(list)
        for t in session_traces[-20:]:
            key = t.get("speaker_name") or ""
            speaker_turns[key].append(t)

        all_topic_clusters: list[str] = []
        all_response_patterns: list[str] = []
        synthesis: dict = {}

        for speaker, turns in speaker_turns.items():
            turn_id = f"sleep_{session_id}_{speaker or 'primary'}"
            self._synthesizer.reset_turn(turn_id)
            batch_text = "\n".join(
                f"Turn {i+1}: User: {t.get('user_input', '')[:200]} | "
                f"Brain: {t.get('entity_response', '')[:200]}"
                for i, t in enumerate(turns)
            )
            raw = await self._synthesizer.call([{"role": "user", "content": batch_text}])
            s: dict = safe_json_parse(raw) or {}

            schema_file = self._schema.ensure_speaker_schema(speaker) if speaker else "user.md"
            for raw_fact in s.get("user_facts", []):
                fact = sanitize_fact(raw_fact)
                if fact:
                    await self._schema.aappend_fact(schema_file, fact)
                    logger.debug("[Memory consolidation] Writing fact to %s: %s",
                                 schema_file, fact[:80])

            all_topic_clusters.extend(s.get("topic_clusters", []))
            all_response_patterns.extend(s.get("response_patterns", []))
            if not synthesis:
                synthesis = s  # use first group's synthesis for self-model update

        synthesis["topic_clusters"] = all_topic_clusters
        synthesis["response_patterns"] = all_response_patterns

        # Reconstruct batch_text for the self-model update (uses all turns)
        batch_text = "\n".join(
            f"Turn {i+1}: User: {t.get('user_input', '')[:200]} | "
            f"Brain: {t.get('entity_response', '')[:200]}"
            for i, t in enumerate(session_traces[-20:])
        )

        # 2. Self-model update
        self._self_updater.reset_turn(f"sleep_{session_id}_self")
        current_self = self._schema.read("self.md")
        context = (
            f"Current self-model:\n{current_self}\n\n"
            f"Session summary:\n{batch_text[:1000]}\n\n"
            f"Topics: {', '.join(synthesis.get('topic_clusters', []))}\n"
            f"Patterns: {', '.join(synthesis.get('response_patterns', []))}"
        )
        raw_self = await self._self_updater.call([{"role": "user", "content": context}])

        updates: dict = safe_json_parse(raw_self) or {}

        if updates:
            await self._apply_self_updates(updates)

        elapsed = time.time() - start
        logger.info("[Memory consolidation] Done in %.2fs", elapsed)

    async def _apply_self_updates(self, updates: dict) -> None:
        existing = self._schema.read("self.md")
        if not existing:
            return

        import re
        for section_key, content in updates.items():
            # Map JSON key to markdown section name
            section_map = {
                "history_summary": "History summary",
                "stable_preferences": "Stable preferences",
            }
            section_name = section_map.get(section_key)
            if not section_name or not content:
                continue
            pattern = rf"(## {re.escape(section_name)}\n)(.*?)(\n## |\Z)"
            replacement = f"\\1{content.strip()}\n\\3"
            existing = re.sub(pattern, replacement, existing, flags=re.DOTALL)

        await self._schema.awrite("self.md", existing)
        logger.debug("[Memory consolidation] Self-model updated")

    # ── Hebbian pass ─────────────────────────────────────────────────────────

    # Valence per core emotion family — single source of truth now lives in
    # brain/emotion_hierarchy.py. Keep the classmethod alias so existing call
    # sites continue to work without refactor.
    _CORE_VALENCE = CORE_VALENCE  # alias for backward compat with any reader

    @classmethod
    def _emotion_valence(cls, emotion: str | None) -> float:
        return valence_of(emotion)

    def _composite_outcome(self, trace) -> tuple[float, dict]:
        """Return (outcome, breakdown) for a single TurnTrace.
        Outcome in roughly [-1, +1]."""
        nm = trace.neuromod or {}
        # DA delta vs neutral baseline (0.5)
        da = float(nm.get("DA", 0.5))
        da_delta = (da - 0.5) * 2.0   # rescale to [-1, +1] roughly

        # Critic overall for the selected draft
        critic_overall = 0.5
        for d in (trace.draft_scores or []):
            if d.get("selected"):
                critic_overall = float(d.get("overall", 0.5))
                break
        # Map [0, 1] → [-1, +1] so a 0.5 score is neutral
        critic_term = (critic_overall - 0.5) * 2.0

        # User emotion valence delta — proxy: current trace's user_emotion vs neutral
        # (We don't track per-turn deltas yet; use absolute valence as a proxy.)
        # When user_emotion is recorded in features, we'd compare to prior turn.
        user_emotion = ""
        for d in (trace.draft_scores or []):
            if isinstance(d, dict) and d.get("user_emotion"):
                user_emotion = d["user_emotion"]
                break
        user_term = self._emotion_valence(user_emotion)

        outcome = 0.5 * da_delta + 0.3 * critic_term + 0.2 * user_term
        # Clamp
        outcome = max(-1.0, min(1.0, outcome))
        return outcome, {
            "da_delta": round(da_delta, 3),
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
        if abs(outcome) < 0.05:
            return True, "outcome_near_zero"
        # Defuse path: the response started with "Let's slow down" or similar — a marker
        # we don't have explicitly. Use a proxy: very high GABA + low critic.
        gaba = float(trace.neuromod.get("GABA", 0.0))
        if gaba > settings.get("gaba_skip_threshold_high") and len(trace.draft_scores) <= 1:
            return True, "defuse_path"
        # Confused/flat AND user negative
        emotion = (trace.emotion or "").lower()
        if emotion in ("confused", "flat"):
            return True, f"dissociated_emotion={emotion}"
        return False, ""

    def _run_hebbian_pass(self, session_id: str, full_traces: list) -> None:
        """Apply gentle decay then per-turn Hebbian updates along firing paths."""
        # Gentle synaptic homeostasis — every edge drifts 1% toward 1.0
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
                decisions.log("hebbian_update_skipped", turn_id=trace.turn_id,
                              reason=reason, outcome=round(outcome, 3))
                skipped += 1
                continue

            delta = outcome * settings.get("hebbian_outcome_delta") * plasticity
            path_names = [n["name"] for n in trace.fired_path]
            # Snapshot weights before to compute per-edge delta for logging
            before = {(path_names[i], path_names[i+1]):
                       self._wiring.get_edge_weight(path_names[i], path_names[i+1])
                      for i in range(len(path_names) - 1)
                      if self._wiring.has(path_names[i], path_names[i+1])}
            updated = self._wiring.hebbian_update(path_names, delta)
            total_updated += updated
            # Log non-trivial per-edge changes (only when |delta| > tiny)
            for (src, tgt), prev in before.items():
                now = self._wiring.get_edge_weight(src, tgt)
                edge_delta = now - prev
                if abs(edge_delta) > 0.001:
                    if edge_delta > 0:
                        gainers.append((f"{src}→{tgt}", edge_delta))
                    else:
                        losers.append((f"{src}→{tgt}", edge_delta))
                    decisions.log(
                        "hebbian_update_applied", turn_id=trace.turn_id,
                        src=src, tgt=tgt,
                        from_weight=round(prev, 4), to_weight=round(now, 4),
                        delta=round(edge_delta, 4),
                        outcome=round(outcome, 3),
                        breakdown=breakdown,
                    )

        # Persist
        try:
            self._wiring.save()
        except Exception as e:
            logger.warning("[Memory consolidation] Wiring save failed: %s", e)
        # Snapshot for evolution history
        try:
            self._wiring.snapshot_to_history(session_id)
        except Exception as e:
            logger.debug("[Memory consolidation] Wiring snapshot failed: %s", e)

        # Session summary
        top_gainers = sorted(gainers, key=lambda x: x[1], reverse=True)[:5]
        top_losers = sorted(losers, key=lambda x: x[1])[:5]
        decisions.log(
            "session_plasticity_summary", session_id=session_id,
            plasticity_modulator=round(plasticity, 3),
            edges_updated=total_updated,
            turns_skipped=skipped,
            top_gainers=[{"edge": e, "delta": round(d, 4)} for e, d in top_gainers],
            top_losers=[{"edge": e, "delta": round(d, 4)} for e, d in top_losers],
        )
        logger.info("[Memory consolidation] Hebbian: plasticity=%.2f edges_updated=%d turns_skipped=%d",
                    plasticity, total_updated, skipped)
