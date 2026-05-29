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
from brain.sleep_prompts import (
    EPISODE_SYNTHESIS_SYSTEM,
    PERSONALITY_OBSERVATION_SYSTEM,
    SELF_UPDATE_SYSTEM,
    THOUGHT_CONSOLIDATION_SYSTEM,
)
from brain.utils import safe_json_parse
from brain.wiring import Wiring

logger = logging.getLogger(__name__)


class SleepConsolidation:
    def __init__(
        self,
        router: ModelRouter,
        schema: SchemaStore,
        episodic: EpisodicStore,
        wiring: Wiring | None = None,
    ) -> None:
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

        self._personality_observer = IntegratorCell(
            name="personality_observer",
            cluster="sleep",
            model="local-general",
            system_prompt=PERSONALITY_OBSERVATION_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._personality_observer.set_router(router)

    async def consolidate(
        self,
        session_id: str,
        session_traces: list[dict],
        full_traces: list | None = None,
        session_thoughts: list[dict] | None = None,
    ) -> None:
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

        logger.info(
            "[Memory consolidation] Processing %d turns from session %s",
            len(session_traces),
            session_id,
        )
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
                f"Turn {i + 1}: User: {t.get('user_input', '')[:200]} | "
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
                    logger.debug(
                        "[Memory consolidation] Writing fact to %s: %s", schema_file, fact[:80]
                    )

            all_topic_clusters.extend(s.get("topic_clusters", []))
            all_response_patterns.extend(s.get("response_patterns", []))
            if not synthesis:
                synthesis = s  # use first group's synthesis for self-model update

        synthesis["topic_clusters"] = all_topic_clusters
        synthesis["response_patterns"] = all_response_patterns

        # 1b. Personality observation — per speaker, upsert Communication style.
        await self._observe_personality(session_id, session_traces)

        # Reconstruct batch_text for the self-model update (uses all turns)
        batch_text = "\n".join(
            f"Turn {i + 1}: User: {t.get('user_input', '')[:200]} | "
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

    # ── Personality observation ───────────────────────────────────────────────

    _JOKE_MARKERS = ("haha", "lol", "lmao", "rofl", "😂", "🤣", "😆", "😄")
    _FRUSTRATION_EMOTIONS = frozenset(
        {
            "frustrated",
            "annoyed",
            "angry",
            "disappointed",
            "irritated",
        }
    )
    _APPROVAL_PATTERNS = (
        "want me to",
        "should i",
        "shall i",
        "would you like me to",
        "do you want me to",
        "let me know if",
    )
    _CANCEL_PATTERNS = (
        "no thanks",
        "not now",
        "never mind",
        "nevermind",
        "skip",
        "cancel",
        "stop",
        "don't",
        "do not",
    )

    # Coarse valence for the user_emotion vocabulary the temporal lobe emits.
    # Used to detect mood SHIFTS turn-to-turn — we don't need fine precision,
    # only sign + magnitude. Unknown labels treated as 0.0.
    _USER_EMOTION_VALENCE: dict[str, float] = {
        "happy": 0.7,
        "playful": 0.6,
        "amused": 0.6,
        "warm": 0.6,
        "affectionate": 0.8,
        "excited": 0.7,
        "curious": 0.4,
        "engaged": 0.4,
        "surprised": 0.1,
        "neutral": 0.0,
        "tired": -0.3,
        "confused": -0.3,
        "disappointed": -0.5,
        "annoyed": -0.5,
        "frustrated": -0.6,
        "angry": -0.8,
        "sad": -0.6,
        "anxious": -0.6,
        "distressed": -0.7,
        "struggling": -0.5,
    }

    @classmethod
    def _emotion_valence(cls, label: str) -> float:
        return cls._USER_EMOTION_VALENCE.get((label or "").lower(), 0.0)

    @classmethod
    def _response_tags(cls, response: str) -> list[str]:
        """Coarse tags describing the brain's response — used to attribute
        mood shifts to response style."""
        r = (response or "").lower()
        rlen = len(r)
        tags: list[str] = []
        if rlen < 80:
            tags.append("short_reply")
        elif rlen < 280:
            tags.append("medium_reply")
        else:
            tags.append("long_reply")
        if any(j in r for j in cls._JOKE_MARKERS) or " :)" in r or "haha" in r:
            tags.append("humour")
        if "?" in r:
            tags.append("asked_question")
        if any(p in r for p in cls._APPROVAL_PATTERNS):
            tags.append("asked_for_approval")
        if any(w in r for w in ("sorry", "apologies", "my mistake", "i was wrong")):
            tags.append("apology")
        if any(
            w in r
            for w in ("i did", "i ran", "i checked", "i pulled", "i wrote", "i added", "i removed")
        ):
            tags.append("reported_action")
        return tags

    def _mood_shift_episodes(self, turns: list[dict]) -> dict:
        """For each consecutive (turn N, turn N+1) pair, compute the valence
        delta of the user's emotion. Return aggregate counts by response-tag
        and the strongest positive/negative individual episodes."""
        shifts_by_tag: dict[str, list[float]] = defaultdict(list)
        episodes: list[dict] = []
        for i in range(len(turns) - 1):
            cur, nxt = turns[i], turns[i + 1]
            v0 = self._emotion_valence(cur.get("user_emotion") or "")
            v1 = self._emotion_valence(nxt.get("user_emotion") or "")
            delta = v1 - v0
            if abs(delta) < 0.2:
                continue  # noise — ignore
            tags = self._response_tags(cur.get("entity_response") or "")
            for tag in tags:
                shifts_by_tag[tag].append(delta)
            episodes.append(
                {
                    "delta": round(delta, 2),
                    "from": cur.get("user_emotion") or "neutral",
                    "to": nxt.get("user_emotion") or "neutral",
                    "brain_response": (cur.get("entity_response") or "")[:180].replace("\n", " "),
                    "next_user_input": (nxt.get("user_input") or "")[:140].replace("\n", " "),
                    "response_tags": tags,
                }
            )
        # Aggregate: per-tag mean delta + sample count.
        tag_summary: dict[str, dict] = {}
        for tag, deltas in shifts_by_tag.items():
            if not deltas:
                continue
            tag_summary[tag] = {
                "n": len(deltas),
                "mean_delta": round(sum(deltas) / len(deltas), 2),
                "positive": sum(1 for d in deltas if d > 0),
                "negative": sum(1 for d in deltas if d < 0),
            }
        # Top moments by absolute delta.
        episodes.sort(key=lambda e: abs(e["delta"]), reverse=True)
        return {
            "tag_summary": tag_summary,
            "top_moments": episodes[:5],
        }

    def _personality_stats(self, turns: list[dict]) -> dict:
        """Compress a list of session traces into a small set of counters the
        LLM can reason over without re-reading every turn."""
        n = len(turns)
        if not n:
            return {"turns": 0}
        msg_len_counter: Counter[str] = Counter()
        intent_counter: Counter[str] = Counter()
        register_counter: Counter[str] = Counter()
        user_emotion_counter: Counter[str] = Counter()
        prosody_tone_counter: Counter[str] = Counter()
        pace_counter: Counter[str] = Counter()
        joke_turns = 0
        frustration_turns = 0
        cancel_turns = 0
        action_turns = 0
        hesitant_turns = 0
        approval_asked_turns = 0
        user_lens: list[int] = []
        resp_lens: list[int] = []
        for t in turns:
            ui = (t.get("user_input") or "").lower()
            br = (t.get("entity_response") or "").lower()
            msg_len_counter[t.get("msg_length") or "unknown"] += 1
            intent_counter[t.get("intent") or "unknown"] += 1
            register_counter[t.get("register") or "unknown"] += 1
            if t.get("user_emotion"):
                user_emotion_counter[t["user_emotion"]] += 1
            if t.get("prosody_tone"):
                prosody_tone_counter[t["prosody_tone"]] += 1
            if t.get("pace_label"):
                pace_counter[t["pace_label"]] += 1
            if any(j in ui for j in self._JOKE_MARKERS):
                joke_turns += 1
            if (t.get("user_emotion") or "").lower() in self._FRUSTRATION_EMOTIONS:
                frustration_turns += 1
            if any(p in ui for p in self._CANCEL_PATTERNS):
                cancel_turns += 1
            if t.get("requires_action"):
                action_turns += 1
            if t.get("hesitant_speech"):
                hesitant_turns += 1
            if any(p in br for p in self._APPROVAL_PATTERNS):
                approval_asked_turns += 1
            user_lens.append(len(t.get("user_input") or ""))
            resp_lens.append(t.get("response_chars") or len(t.get("entity_response") or ""))

        def _avg(xs: list[int]) -> int:
            return int(sum(xs) / len(xs)) if xs else 0

        stats: dict = {
            "turns": n,
            "avg_user_chars": _avg(user_lens),
            "avg_response_chars": _avg(resp_lens),
            "msg_length_mix": dict(msg_len_counter),
            "intent_mix": dict(intent_counter),
            "register_mix": dict(register_counter),
            "user_emotion_mix": dict(user_emotion_counter),
            "joke_turns": joke_turns,
            "frustration_turns": frustration_turns,
            "cancel_turns": cancel_turns,
            "action_turns": action_turns,
            "approval_asked_turns": approval_asked_turns,
        }
        if any(prosody_tone_counter.values()):
            stats["prosody_tone_mix"] = dict(prosody_tone_counter)
        if any(pace_counter.values()):
            stats["pace_mix"] = dict(pace_counter)
            stats["hesitant_turns"] = hesitant_turns
        return stats

    @staticmethod
    def _read_section(content: str, section: str) -> str:
        m = re.search(
            r"(?ms)^##[ \t]+" + re.escape(section) + r"[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)",
            content,
        )
        return m.group(1).strip() if m else ""

    async def _observe_personality(self, session_id: str, session_traces: list[dict]) -> None:
        """Aggregate session signals per speaker and upsert the Communication
        style section of that speaker's schema file. Quietly skips on small
        sessions or LLM failures — the section just won't change."""
        if not session_traces:
            return
        # Use a larger window than fact-extraction (personality benefits from
        # more turns); but cap to keep the LLM payload small.
        window = session_traces[-60:]
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in window:
            groups[t.get("speaker_name") or ""].append(t)

        for speaker, turns in groups.items():
            # Require a minimum signal floor — single-turn sessions don't tell
            # us anything new about a person's style.
            if len(turns) < 3:
                continue
            schema_file = self._schema.ensure_speaker_schema(speaker) if speaker else "user.md"
            current_content = self._schema.read(schema_file)
            current_style = (
                self._read_section(current_content, "Communication style") or "(learning…)"
            )
            current_mood_response = (
                self._read_section(current_content, "Mood response patterns") or "(learning…)"
            )
            stats = self._personality_stats(turns)
            mood = self._mood_shift_episodes(turns)
            sample_lines = [
                (t.get("user_input") or "").strip().replace("\n", " ")[:160]
                for t in turns[-12:]
                if (t.get("user_input") or "").strip()
            ]
            payload = (
                f"speaker_name: {speaker or 'primary user'}\n"
                f"current_style:\n{current_style}\n\n"
                f"current_mood_response:\n{current_mood_response}\n\n"
                f"session_stats: {stats}\n\n"
                f"mood_shifts: {mood['tag_summary']}\n\n"
                f"mood_top_moments: {mood['top_moments']}\n\n"
                "sample_turns:\n- " + "\n- ".join(sample_lines)
            )
            turn_id = f"sleep_{session_id}_personality_{speaker or 'primary'}"
            self._personality_observer.reset_turn(turn_id)
            try:
                raw = await self._personality_observer.call([{"role": "user", "content": payload}])
            except Exception as exc:
                logger.warning(
                    "[Personality observer] LLM call failed for %s: %s", speaker or "primary", exc
                )
                continue
            result = safe_json_parse(raw) or {}

            def _clean_bullets(text: str, section_heading: str) -> str:
                text = (text or "").strip()
                if not text:
                    return ""
                text = re.sub(
                    r"(?im)^##[ \t]+" + re.escape(section_heading) + r"[ \t]*\r?\n",
                    "",
                    text,
                ).strip()
                if not text.startswith("-"):
                    text = "- " + text.replace("\n", "\n- ")
                return text

            new_style = _clean_bullets(result.get("communication_style"), "Communication style")
            new_mood = _clean_bullets(
                result.get("mood_response_patterns"), "Mood response patterns"
            )

            if new_style:
                await self._schema.upsert_section(schema_file, "Communication style", new_style)
                logger.info(
                    "[Personality observer] Updated %s ## Communication style (%d turns observed)",
                    schema_file,
                    len(turns),
                )
            if new_mood:
                await self._schema.upsert_section(schema_file, "Mood response patterns", new_mood)
                logger.info(
                    "[Personality observer] Updated %s ## Mood response "
                    "patterns (%d shifts observed)",
                    schema_file,
                    len(mood["top_moments"]),
                )
            if not new_style and not new_mood:
                logger.debug(
                    "[Personality observer] No usable output for %s — skipping",
                    speaker or "primary",
                )

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

    def _apply_drafter_competition(
        self, trace, outcome: float, plasticity: float, gainers: list, losers: list
    ) -> None:
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

    async def consolidate_thoughts(
        self, session_id: str, session_thoughts: list[dict], topic_clusters: list[str] | None = None
    ) -> None:
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
        angle_counts: Counter = Counter(t["angle"] for t in session_thoughts if t.get("angle"))
        recurring_angles: set[str] = {a for a, c in angle_counts.items() if c >= 2}

        # Filter: salient OR recurring-angle thoughts only
        notable = [
            t for t in session_thoughts if t.get("salient") or t.get("angle") in recurring_angles
        ]

        if not notable:
            logger.info("[Memory consolidation] Thought pass: no notable thoughts to consolidate")
            return

        logger.info(
            "[Memory consolidation] Thought pass: %d notable / %d total thoughts",
            len(notable),
            len(session_thoughts),
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
                updated = re.sub(
                    oq_pattern,
                    lambda m: m.group(1) + new_content + "\n" + m.group(3),
                    updated,
                    flags=re.DOTALL,
                )
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
            logger.info("[Memory consolidation] Preoccupations: %s", "; ".join(preoccupations[:3]))
        if insights:
            logger.info("[Memory consolidation] Insights: %s", "; ".join(insights[:2]))

        # Write open-questions changes only if the content was restructured
        if open_questions and updated != existing:
            await self._schema.awrite("self.md", updated)
            logger.debug("[Memory consolidation] Open Questions section updated")

    # ── Hebbian pass ─────────────────────────────────────────────────────────
