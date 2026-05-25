"""
Sleep consolidation — runs at session end (or between sessions).
Re-indexes recent episodes, compresses for retrieval efficiency,
updates self.md autobiography, extracts facts to user.md.
Uses batch-friendly API calls (no real-time constraint).

v0.2 feature.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict

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

THOUGHT_CONSOLIDATION_SYSTEM = """You are the REM-sleep consolidation process of an AI brain.
During the session, the brain generated private internal thoughts — its stream of consciousness
between turns. You are given the salient ones (those tagged during high dopamine, strong
emotion, or as speech candidates) plus the full list so you can detect recurrence.

Your job mirrors what the hippocampus and neocortex do during REM: find patterns that the
waking brain was too busy to notice, connect internal preoccupations to the episodic record,
and generate insights worth encoding into the self-model.

Look for:
1. PREOCCUPATIONS — topics or questions the brain returned to repeatedly, even if it never
   brought them up in conversation. These reveal what the brain was actually caring about.
2. CROSS-CONNECTIONS — places where a recurring internal thought connects to something that
   DID come up in conversation (a topic cluster). This is where implicit becomes explicit.
3. INSIGHTS — anything that emerges from the pattern that wasn't obvious during the session.
   A new angle on a question, a contradiction noticed, a shift in emotional preoccupation.
4. UNRESOLVED THREADS — questions or concerns the brain kept returning to but never resolved.
   These should be written into Open Questions so the brain can pick them up next session.

Biological principle: only process thoughts that recurred (appeared in multiple angles or
similar wording) OR occurred during high-salience states. Isolated neutral thoughts are
synaptic noise — do not force meaning onto them.

Return JSON:
{
  "preoccupations": [string],    // 0-3 topics the brain kept returning to internally
  "cross_connections": [string], // 0-3 links between internal themes and conversation topics
  "insights": [string],          // 0-2 genuine insights from the pattern
  "open_questions": [string],    // 0-3 unresolved threads worth carrying forward
  "preoccupations_digest": string // 2-3 sentence summary of the session's inner life,
                                  // written in first person as if the brain is reflecting.
                                  // Empty string if nothing significant emerged.
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

        self._thought_consolidator = IntegratorCell(
            name="thought_consolidator",
            cluster="sleep",
            model="local-general",
            system_prompt=THOUGHT_CONSOLIDATION_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._thought_consolidator.set_router(router)

    async def consolidate(self, session_id: str, session_traces: list[dict],
                          full_traces: list | None = None,
                          session_thoughts: list[dict] | None = None) -> None:
        """
        Run full consolidation after a session ends.
        session_traces: list of {user_input, entity_response, emotion, topic_tags} dicts.
        full_traces: list of TurnTrace objects (carry fired_path, neuromod, draft_scores)
                     — used for the Hebbian pass. Pass [] or None to skip Hebbian.
        session_thoughts: list of tagged DMN thought entries from DefaultModeNetwork.
                          session_thoughts(). Used for the REM-style thought pass.
                          Pass [] or None to skip thought consolidation.
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

        # 3. REM-style thought consolidation — process the session's inner life.
        if session_thoughts:
            await self.consolidate_thoughts(
                session_id=session_id,
                session_thoughts=session_thoughts,
                topic_clusters=all_topic_clusters,
            )

        elapsed = time.time() - start
        logger.info("[Memory consolidation] Done in %.2fs", elapsed)

    async def _apply_self_updates(self, updates: dict) -> None:
        existing = self._schema.read("self.md")
        if not existing:
            return

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

    # ── REM-style thought consolidation ──────────────────────────────────────

    async def consolidate_thoughts(self, session_id: str,
                                   session_thoughts: list[dict],
                                   topic_clusters: list[str] | None = None) -> None:
        """REM-style pass: find recurring preoccupations in the session's inner
        monologue, cross-connect them to episodic topics, and write insights +
        open questions into self.md.

        Biological principle: only salient thoughts (high DA, strong emotion,
        speak-flagged) plus any thought whose angle recurred at least twice are
        passed to the LLM — the rest is homeostatic noise that gets discarded,
        mirroring non-REM synaptic downscaling.
        """
        if not session_thoughts:
            return

        # Identify recurring angles (recurrence ≥ 2 occurrences)
        angle_counts: Counter = Counter(
            t["angle"] for t in session_thoughts if t.get("angle")
        )
        recurring_angles: set[str] = {a for a, c in angle_counts.items() if c >= 2}

        # Filter: salient OR recurring-angle thoughts only
        notable = [
            t for t in session_thoughts
            if t.get("salient") or t.get("angle") in recurring_angles
        ]

        if not notable:
            logger.info("[Memory consolidation] Thought pass: no notable thoughts to consolidate")
            return

        logger.info(
            "[Memory consolidation] Thought pass: %d notable / %d total thoughts",
            len(notable), len(session_thoughts),
        )

        # Build prompt
        lines = ["SESSION INNER MONOLOGUE (notable thoughts only):"]
        for i, t in enumerate(notable[-40:], 1):  # cap at 40
            flags = []
            if t.get("salient"):
                flags.append("salient")
            if t.get("speak_flagged"):
                flags.append("speak-candidate")
            if t.get("angle") in recurring_angles:
                flags.append("recurring-angle")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"{i}. [{t.get('direction', 'outward')}]{flag_str} "
                f"angle={t.get('angle', '?')} | {t['thought']}"
            )

        if topic_clusters:
            lines.append("\nCONVERSATION TOPIC CLUSTERS (from episodic synthesis):")
            lines.append(", ".join(topic_clusters[:15]))

        lines.append(
            "\nRecurring angles (appeared ≥2 times): "
            + (", ".join(sorted(recurring_angles)) if recurring_angles else "none")
        )

        prompt = "\n".join(lines)
        self._thought_consolidator.reset_turn(f"sleep_{session_id}_thoughts")
        raw = await self._thought_consolidator.call([{"role": "user", "content": prompt}])
        result: dict = safe_json_parse(raw) or {}

        if not result:
            logger.debug("[Memory consolidation] Thought consolidator returned no parseable output")
            return

        await self._apply_thought_updates(result)

    async def _apply_thought_updates(self, result: dict) -> None:
        """Write thought consolidation output into self.md."""
        preoccupations = result.get("preoccupations") or []
        cross_connections = result.get("cross_connections") or []
        insights = result.get("insights") or []
        open_questions = result.get("open_questions") or []
        digest = (result.get("preoccupations_digest") or "").strip()

        if not any([preoccupations, cross_connections, insights, open_questions, digest]):
            return

        existing = self._schema.read("self.md")
        if not existing:
            return

        updated = existing

        # Append open questions to the "Open Questions" section (if present).
        # These are questions the brain kept circling that were never resolved.
        if open_questions:
            oq_block = "\n".join(f"- {q}" for q in open_questions[:3])
            # Try to append inside an existing Open Questions section
            oq_pattern = r"(## Open [Qq]uestions\n)(.*?)(\n## |\Z)"
            match = re.search(oq_pattern, updated, flags=re.DOTALL)
            if match:
                existing_content = match.group(2).rstrip()
                new_content = f"{existing_content}\n{oq_block}" if existing_content else oq_block
                updated = re.sub(oq_pattern,
                                 lambda m: m.group(1) + new_content + "\n" + m.group(3),
                                 updated, flags=re.DOTALL)
            else:
                # Append a new section at the end
                updated = updated.rstrip() + f"\n\n## Open Questions\n{oq_block}\n"

        # Write the preoccupations digest as a fact into self.md.
        # The sanitize_fact check prevents injection; aappend_fact handles
        # dedup so repeated consolidations don't pile up identical lines.
        if digest:
            fact = sanitize_fact(f"Session inner-life digest: {digest}")
            if fact:
                await self._schema.aappend_fact("self.md", fact)
                logger.info("[Memory consolidation] Thought digest: %s", digest[:120])

        # Log the rest to decisions for observability without writing noise to schema.
        decisions.log(
            "thought_consolidation",
            preoccupations=preoccupations,
            cross_connections=cross_connections,
            insights=insights,
            open_questions=open_questions,
        )

        if preoccupations:
            logger.info("[Memory consolidation] Preoccupations: %s",
                        "; ".join(preoccupations[:3]))
        if insights:
            logger.info("[Memory consolidation] Insights: %s",
                        "; ".join(insights[:2]))

        # Write open-questions changes only if the content was restructured
        if open_questions and updated != existing:
            await self._schema.awrite("self.md", updated)
            logger.debug("[Memory consolidation] Open Questions section updated")

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
        Outcome in roughly [-1, +1].

        Signal sources:
        - DA delta (50%): how much DA changed THIS turn vs start of turn.
          Encodes whether the turn was rewarding (positive) or not (negative).
          Prior DA is captured in trace.prior_neuromod at turn start.
        - Critic score (30%): actual LLM critic assessment; only counted when the
          critic actually ran (critic_ran=True). Single-draft turns contribute 0
          so the DA delta carries the full directional signal for those turns.
        - User emotion valence (20%): positive/negative valence of the user's
          detected emotional state this turn (stored in trace.user_emotion).
        """
        nm = trace.neuromod or {}
        da = float(nm.get("DA", 0.5))

        # Per-turn DA delta: how much did DA change during THIS turn?
        # Typical turn deltas are ±0.05–0.15; scale × 4 → ±0.2–0.6 range.
        prior_nm = getattr(trace, "prior_neuromod", None) or {}
        da_prior = float(prior_nm.get("DA", da))  # fallback to current if missing
        da_delta = (da - da_prior) * 4.0
        da_delta = max(-1.0, min(1.0, da_delta))

        # Critic score — only trust it when the LLM critic actually ran.
        critic_term = 0.0
        for d in (trace.draft_scores or []):
            if d.get("selected") and d.get("critic_ran"):
                critic_term = (float(d.get("overall", 0.5)) - 0.5) * 2.0
                break

        # User emotion valence: read from trace.user_emotion (set by run.py
        # from features.user_emotion after temporal understanding fires).
        user_emotion = getattr(trace, "user_emotion", "") or ""
        user_term = self._emotion_valence(user_emotion)

        outcome = 0.5 * da_delta + 0.3 * critic_term + 0.2 * user_term
        # Clamp
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

    def _apply_drafter_competition(self, trace, outcome: float, plasticity: float,
                                   gainers: list, losers: list) -> None:
        """Extra reinforcement for multi-draft turns: winning drafter edges gain an
        additional bonus; non-winning drafters that ran get a small penalty.
        Applies only when critic_ran=True for the selected draft (real quality signal).
        """
        draft_scores = trace.draft_scores or []
        # Only run when the critic actually compared multiple drafts
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
            # Parse drafter index from "draft_{idx}_{turn_id}" format
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
                # Winner bonus: proportional to margin over median of losers
                loser_scores = [float(x.get("overall", 0.5))
                                for x in real_scored if x.get("draft_id") != winner_id]
                margin = winner_overall - (sum(loser_scores) / len(loser_scores) if loser_scores else 0.5)
                bonus = margin * bonus_scale * 0.5   # half the normal outcome delta as bonus
                self._wiring.hebbian_update([edge[0], edge[1]], bonus)
            else:
                # Loser penalty: small negative nudge proportional to how far below winner
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
                    "drafter_competition_applied", turn_id=trace.turn_id,
                    drafter=drafter_name, won=(did == winner_id),
                    from_weight=round(prev, 4), to_weight=round(now, 4),
                    delta=round(edge_delta, 4),
                    winner_score=round(winner_overall, 3),
                )

    def _drafter_competition_edge_count(self, trace) -> int:
        """Count of drafter competition edge updates (for total_updated accounting)."""
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

            # Competitive drafter reinforcement: when multiple drafters competed,
            # additionally strengthen the winning drafter edge and weaken losers.
            # This is separate from the path-based update above (which gives equal
            # delta to all drafters that ran) and creates genuine drafter divergence.
            self._apply_drafter_competition(trace, outcome, plasticity, gainers, losers)
            total_updated += self._drafter_competition_edge_count(trace)

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

        # Session summary — also report signal quality so we can track how many
        # turns had each component of the outcome signal actually contributing.
        turns_with_critic = sum(
            1 for t in full_traces
            if any(d.get("critic_ran") and d.get("selected") for d in (t.draft_scores or []))
        )
        turns_with_user_emotion = sum(
            1 for t in full_traces if getattr(t, "user_emotion", "")
        )
        turns_with_da_delta = sum(
            1 for t in full_traces
            if abs(float((getattr(t, "prior_neuromod", None) or {}).get("DA",
                   float((t.neuromod or {}).get("DA", 0.5)))
                   ) - float((t.neuromod or {}).get("DA", 0.5))) > 0.01
        )
        top_gainers = sorted(gainers, key=lambda x: x[1], reverse=True)[:5]
        top_losers = sorted(losers, key=lambda x: x[1])[:5]
        decisions.log(
            "session_plasticity_summary", session_id=session_id,
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
        logger.info("[Memory consolidation] Hebbian: plasticity=%.2f edges_updated=%d "
                    "turns_skipped=%d critic_turns=%d/%d",
                    plasticity, total_updated, skipped,
                    turns_with_critic, len(full_traces))
