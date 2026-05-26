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
from brain.hebbian import HebbianUpdater
from brain.model_router import ModelRouter
from brain.observability.decisions import decisions
from brain.second_brain.store import EpisodicStore, SchemaStore
from brain.security import sanitize_fact
from brain.settings import settings
from brain.sleep_prompts import (
    EPISODE_SYNTHESIS_SYSTEM,
    SELF_UPDATE_SYSTEM,
    THOUGHT_CONSOLIDATION_SYSTEM,
)
from brain.utils import safe_json_parse
from brain.wiring import Wiring

logger = logging.getLogger(__name__)


class SleepConsolidation:
    def __init__(self, router: ModelRouter, schema: SchemaStore,
                 episodic: EpisodicStore, wiring: Wiring | None = None) -> None:
        self._router = router
        self._schema = schema
        self._episodic = episodic
        self._hebbian = HebbianUpdater(wiring) if wiring is not None else None

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
        if full_traces and self._hebbian is not None:
            self._hebbian.run(session_id, full_traces)

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

    # ── Hebbian delegation (preserve public API for callers and tests) ────────

    def _composite_outcome(self, trace) -> tuple[float, dict]:
        assert self._hebbian is not None, "Wiring required for Hebbian methods"
        return self._hebbian._composite_outcome(trace)

    def _plasticity_modulator(self, full_traces: list) -> float:
        assert self._hebbian is not None, "Wiring required for Hebbian methods"
        return self._hebbian._plasticity_modulator(full_traces)

    def _should_skip_hebbian(self, trace, outcome: float) -> tuple[bool, str]:
        assert self._hebbian is not None, "Wiring required for Hebbian methods"
        return self._hebbian._should_skip_hebbian(trace, outcome)

    def _apply_drafter_competition(self, trace, outcome: float, plasticity: float,
                                   gainers: list, losers: list) -> None:
        assert self._hebbian is not None, "Wiring required for Hebbian methods"
        self._hebbian._apply_drafter_competition(trace, outcome, plasticity, gainers, losers)

    def _run_hebbian_pass(self, session_id: str, full_traces: list) -> None:
        assert self._hebbian is not None, "Wiring required for Hebbian methods"
        self._hebbian.run(session_id, full_traces)

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
