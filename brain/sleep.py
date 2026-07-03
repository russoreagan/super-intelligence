"""
Sleep consolidation — runs at session end (or between sessions).
Re-indexes recent episodes, compresses for retrieval efficiency,
updates self.md autobiography, extracts facts to user.md.
Uses batch-friendly API calls (no real-time constraint).

v0.2 feature.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
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
    ANGLE_SYNONYM_SYSTEM,
    EPISODE_SYNTHESIS_SYSTEM,
    LEARNING_NARRATOR_SYSTEM,
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
            model="runpod-general",
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
            model="runpod-general",
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
            model="runpod-general",
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
            model="runpod-general",
            system_prompt=PERSONALITY_OBSERVATION_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._personality_observer.set_router(router)

        self._angle_synonym_cell = IntegratorCell(
            name="angle_synonym_clusterer",
            cluster="sleep",
            model="runpod-general",
            system_prompt=ANGLE_SYNONYM_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="low",
        )
        self._angle_synonym_cell.set_router(router)

        # Learning narrator — phrases the session's numeric learning digest as
        # first-person stories. sensitivity=low: the digest is edge names and
        # numbers only, never conversation text.
        self._learning_narrator = IntegratorCell(
            name="learning_narrator",
            cluster="sleep",
            model="runpod-general",
            system_prompt=LEARNING_NARRATOR_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="low",
        )
        self._learning_narrator.set_router(router)

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

        # 1c. Relationship tier update — deterministic score+count gating.
        if settings.get("enable_relationship_stage_progression"):
            await self._update_familiarity_tiers(session_traces)

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

        # 2b. Cross-learning: reflect privately over this session's material,
        # de-id gate the conclusion, and fold an admitted principle into the
        # shared hypothesis store (provisional → established on distinct-source
        # corroboration). Fail-open for consolidation: a gate refusal or LLM
        # hiccup here must never block the rest of sleep.
        if settings.get("cross_learning", 0):
            try:
                from brain import cross_learning
                from brain.deid_gate import DeidGate
                from brain.private_rumination import PrivateRuminator

                source_id = os.environ.get("BRAIN_USER_ID", "").strip() or "primary"
                store = cross_learning.load_store()
                ruminator = PrivateRuminator(self._router, DeidGate(self._router))
                outcome = await cross_learning.learn_from_private(
                    ruminator, store, batch_text, source_id
                )
                if outcome.admitted:
                    cross_learning.save_store(store)
                    logger.info(
                        "[Cross-learning] principle %s (%s): %s",
                        outcome.hypothesis_id,
                        outcome.status,
                        (outcome.principle or "")[:100],
                    )
                else:
                    logger.info("[Cross-learning] nothing admitted (stage=%s)", outcome.stage)
            except Exception as e:
                logger.warning("[Cross-learning] pass failed: %s", e)

        # 3. REM-style thought consolidation — process the session's inner life.
        if session_thoughts:
            await self.consolidate_thoughts(
                session_id=session_id,
                session_thoughts=session_thoughts,
                topic_clusters=all_topic_clusters,
            )

        # 4. Angle synonym pass — infrequent; gates on history size + time since last run.
        await self.angle_synonym_pass(session_id)

        # 5. Chunk mining — consolidate recurring tool sub-sequences into motor chunks.
        await self.chunk_mining_pass(session_id)

        # 6. Learning stories — narrate what this session's Hebbian pass changed.
        # Must run AFTER the Hebbian pass (reads its ledger records) and inside the
        # same persona binding (consolidate_now binds; a detached task would lose it).
        if settings.get("learning_narrator", 1):
            await self.learning_story_pass(session_id)

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
        user_register_counter: Counter[str] = Counter()
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
            user_register_counter[t.get("user_register") or "unknown"] += 1
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
            "user_register_mix": dict(user_register_counter),
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

    # ── Relationship tier update ──────────────────────────────────────────────

    async def _update_familiarity_tiers(self, session_traces: list[dict]) -> None:
        """Stamp `Last seen` = now for every speaker seen this session.

        This is the second half of the bond model's two-touchpoint design:
        consolidation records WHEN we last interacted; the next session's BOOT
        reads that timestamp, computes the absence gap, and applies the decay
        (see hippocampus.apply_relationship_decay_at_boot). Decay must NOT run
        here — doing so would decay the affection this very session just earned.
        """
        if not session_traces or not settings.get("enable_bond_model"):
            return

        now = time.time()
        speakers = {t.get("speaker_name") or "" for t in session_traces}

        for speaker in speakers:
            try:
                schema_file = self._schema.ensure_speaker_schema(speaker) if speaker else "user.md"
                content = self._schema.read(schema_file)
                if not content:
                    continue
                seen_line = f"- Last seen: {now:.0f}"
                if re.search(r"- Last seen:[^\n]*", content):
                    content = re.sub(r"- Last seen:[^\n]*", seen_line, content, count=1)
                else:
                    content += f"\n{seen_line}"
                # Clean up any legacy low-score-sessions counter from the old model
                content = re.sub(r"\n?- Low score sessions:[^\n]*", "", content)
                # Honor the storage backend (Supabase on hosted) — a raw
                # _atomic_write would persist to local disk that nothing reads back.
                await self._schema.awrite(schema_file, content)
            except Exception as exc:
                logger.warning(
                    "[Relationship] Last-seen stamp failed for speaker=%s: %s",
                    speaker or "primary",
                    exc,
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
        """Route thought-consolidation output to the unified stores.

        Open questions → the single active ledger (open_questions.md ## Open
        threads), NOT a separate self.md section (de-fragmentation, B3).
        Insights → episodic memory as conclusions (B2 sleep feed), instead of
        being logged-and-discarded. The inner-life digest still lands in self.md.
        """
        preoccupations = result.get("preoccupations") or []
        cross_connections = result.get("cross_connections") or []
        insights = result.get("insights") or []
        open_questions = result.get("open_questions") or []
        digest = (result.get("preoccupations_digest") or "").strip()

        if not any([preoccupations, cross_connections, insights, open_questions, digest]):
            return

        # Open questions kept circling but unresolved → open them as threads in the
        # one active ledger so the DMN can pick them up and make progress.
        if open_questions:
            await self._append_questions_to_ledger(open_questions[:3])

        # Insights are the closest thing to settled knowledge — persist them to
        # memory (was previously logged to decisions and lost).
        for ins in insights[:3]:
            await self._encode_conclusion(str(ins), source="sleep")

        # Inner-life digest still lands in self.md (identity-adjacent, not knowledge).
        if digest:
            fact = sanitize_fact(f"Session inner-life digest: {digest}")
            if fact:
                await self._schema.aappend_fact("self.md", fact)
                logger.info("[Memory consolidation] Thought digest: %s", digest[:120])

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

    async def _append_questions_to_ledger(self, questions: list[str]) -> None:
        """Open sleep's unresolved questions as threads in open_questions.md,
        skipping near-duplicates of threads already there."""
        from brain import open_threads as ot

        try:
            text = self._schema.read(ot.LEDGER_FILE)
            threads = ot.parse_threads(ot.extract_section(text))
            existing_lc = [t.summary.lower() for t in threads]
            added = False
            for q in questions:
                q = str(q).strip()
                if not q:
                    continue
                ql = q.lower()
                if any(ql in s or s in ql for s in existing_lc):
                    continue  # already represented
                threads, _ = ot.open_thread(threads, q, bearing="sleep-surfaced")
                existing_lc.append(ql)
                added = True
            if added:
                await self._schema.upsert_section(
                    ot.LEDGER_FILE, ot.SECTION, ot.render_section_body(threads)
                )
                logger.info("[Memory consolidation] Opened sleep questions into the ledger")
        except Exception as e:
            logger.warning("[Memory consolidation] Could not append questions to ledger: %s", e)

    async def _encode_conclusion(self, text: str, source: str = "sleep") -> None:
        """Encode a settled insight into episodic memory as a [CONCLUDED] episode
        (mirrors Hippocampus.encode_conclusion; sleep holds _episodic directly)."""
        import uuid

        from brain.second_brain.store import Episode

        if not text.strip():
            return
        try:
            vec = None
            try:
                vec = await self._router.embed(text)
            except Exception:
                vec = None
            ep = Episode(
                session_id="sleep",
                turn_id=f"concl_{int(time.time())}_{uuid.uuid4().hex[:6]}",
                ts=time.time(),
                user_input="(sleep — concluded)",
                entity_response=f"[CONCLUDED] {text}",
                topic_tags=["conclusion", "knowledge", source],
                emotion_state="satisfied",
                user_emotion="unknown",
                entities=[],
                neuromod_snapshot={"DA": 0.6, "GABA": 0.1, "ACh": 0.4, "Glu": 0.3},
                surprise_score=0.7,
                vector=vec,
            )
            self._episodic.encode(ep)
            logger.info("[Memory consolidation] Insight encoded as conclusion: %r", text[:80])
        except Exception as e:
            logger.warning("[Memory consolidation] Conclusion encoding failed: %s", e)

    # ── Angle synonym pass ───────────────────────────────────────────────────

    _SYNONYM_MIN_HISTORY = 50  # angles recorded before first pass
    _SYNONYM_MIN_INTERVAL_DAYS = 7  # minimum days between passes

    async def angle_synonym_pass(self, session_id: str) -> None:
        """Cluster semantically similar angle labels and write angle_synonyms.json.

        Only runs when the angle history is large enough to be meaningful and
        enough time has passed since the last run. Reads/writes
        second_brain/sequence_weights.json for history + timestamp bookkeeping,
        and second_brain/angle_synonyms.json for the output mapping.
        """
        import os

        from brain.sequence_predictor import _SYNONYMS_PATH, _WEIGHTS_PATH, _normalize

        weights_path = os.path.abspath(_WEIGHTS_PATH)
        synonyms_path = os.path.abspath(_SYNONYMS_PATH)

        # Load weights file — need history + last-run timestamp.
        try:
            if not os.path.exists(weights_path):
                logger.debug("[AngleSynonyms] No sequence_weights.json yet — skipping")
                return
            with open(weights_path) as f:
                weights_data: dict = json.load(f)
        except Exception as e:
            logger.warning("[AngleSynonyms] Could not read sequence_weights.json: %s", e)
            return

        history: list[str] = weights_data.get("history", [])
        last_ts: float = float(weights_data.get("last_synonym_pass_ts", 0))
        now = time.time()

        if len(history) < self._SYNONYM_MIN_HISTORY:
            logger.debug(
                "[AngleSynonyms] Not enough history (%d/%d) — skipping",
                len(history),
                self._SYNONYM_MIN_HISTORY,
            )
            return

        if now - last_ts < self._SYNONYM_MIN_INTERVAL_DAYS * 86400:
            logger.debug(
                "[AngleSynonyms] Last pass was %.1f days ago — skipping", (now - last_ts) / 86400
            )
            return

        # Build angle frequency table from bigrams (each appearance as src or dst counts).
        sep = "\x1f"
        bigrams: dict = weights_data.get("bigrams", {})
        freq: Counter = Counter()
        for key, count in bigrams.items():
            parts = key.split(sep, 1)
            for p in parts:
                if p:
                    freq[p] += count
        # Also count raw history entries.
        for a in history:
            if a:
                freq[a] += 1

        # Build sorted list of (angle, count) for the LLM — most frequent first.
        angle_list = sorted(freq.items(), key=lambda x: -x[1])

        lines = ["Observed angles (label: frequency):"]
        for angle, count in angle_list[:120]:  # cap to keep prompt manageable
            lines.append(f"  {angle}: {count}")
        prompt = "\n".join(lines)

        logger.info(
            "[AngleSynonyms] Running synonym pass: %d unique angles from %d history entries",
            len(angle_list),
            len(history),
        )

        self._angle_synonym_cell.reset_turn(f"sleep_{session_id}_synonyms")
        raw = await self._angle_synonym_cell.call([{"role": "user", "content": prompt}])
        result: dict = safe_json_parse(raw) or {}

        mappings: list[dict] = result.get("mappings") or []
        if not mappings:
            logger.info("[AngleSynonyms] No synonym groups found — vocabulary may be consistent")
        else:
            # Build flat {variant: canonical} dict; load existing to merge.
            existing: dict = {}
            try:
                if os.path.exists(synonyms_path):
                    with open(synonyms_path) as f:
                        existing = json.load(f)
            except Exception:
                pass

            for group in mappings:
                canonical = str(group.get("canonical", "")).strip().lower()
                variants = [str(v).strip().lower() for v in (group.get("variants") or [])]
                if not canonical or not variants:
                    continue
                for v in variants:
                    norm = _normalize(v)
                    if norm and norm != canonical:
                        existing[norm] = canonical

            tmp = synonyms_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp, synonyms_path)
            logger.info(
                "[AngleSynonyms] Wrote %d synonym mappings (%d groups) to angle_synonyms.json",
                len(existing),
                len(mappings),
            )

        # Stamp the run time back into sequence_weights.json. Re-read first: the
        # LLM call above took seconds, and writing back our stale pre-call
        # snapshot would clobber any predictor.save() that landed meanwhile.
        try:
            with open(weights_path) as f:
                current: dict = json.load(f)
            current["last_synonym_pass_ts"] = now
            tmp = weights_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(current, f, indent=2)
            os.replace(tmp, weights_path)
        except Exception as e:
            logger.warning(
                "[AngleSynonyms] Could not update sequence_weights.json timestamp: %s", e
            )

    # ── Chunk mining pass ────────────────────────────────────────────────────

    _CHUNK_MIN_JOBS = 8  # job records before the first mining pass
    _CHUNK_MIN_INTERVAL_HOURS = 12  # minimum time between passes

    async def chunk_mining_pass(self, session_id: str) -> None:
        """Mine recurring tool sub-sequences from second_brain/jobs/*.json into
        second_brain/chunks.json (consumed at runtime by ChunkMemorySubsystem).

        Pure n-gram counting — no LLM call. Gated on having enough jobs and on
        time since the last pass; recomputed from scratch over the current jobs
        window so chunks that stop recurring naturally demote.
        """
        import os

        from brain.clusters.chunk_memory import _CHUNKS_PATH, mine_chunks
        from brain.clusters.job_store import JOBS_DIR

        chunks_path = str(_CHUNKS_PATH)

        # Interval gate.
        last_ts = 0.0
        try:
            if os.path.exists(chunks_path):
                with open(chunks_path) as f:
                    prev = json.load(f)
                last_ts = float(prev.get("ts_epoch", 0))
        except Exception:
            last_ts = 0.0
        now = time.time()
        if last_ts and now - last_ts < self._CHUNK_MIN_INTERVAL_HOURS * 3600:
            logger.debug(
                "[ChunkMining] Last pass was %.1f h ago — skipping", (now - last_ts) / 3600
            )
            return

        # Load job records.
        jobs: list[dict] = []
        try:
            for path in JOBS_DIR.glob("*.json"):
                with contextlib.suppress(Exception), open(path) as f:
                    jobs.append(json.load(f))
        except Exception as e:
            logger.warning("[ChunkMining] Could not read jobs dir: %s", e)
            return

        if len(jobs) < self._CHUNK_MIN_JOBS:
            logger.debug(
                "[ChunkMining] Not enough jobs (%d/%d) — skipping",
                len(jobs),
                self._CHUNK_MIN_JOBS,
            )
            return

        data = mine_chunks(jobs)
        data["ts_epoch"] = now
        n_active = sum(1 for c in data["chunks"].values() if c.get("state") == "active")
        try:
            tmp = chunks_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, chunks_path)
            logger.info(
                "[ChunkMining] Wrote %d chunks (%d active) from %d jobs",
                len(data["chunks"]),
                n_active,
                len(jobs),
            )
        except Exception as e:
            logger.warning("[ChunkMining] Could not write chunks.json: %s", e)

    # ── Learning narration pass ──────────────────────────────────────────────

    _STORIES_KEEP = 500  # rolling cap on learning_stories.jsonl

    def _learning_evidence(self, session_id: str) -> list[dict]:
        """Deterministic numeric digest of this session's learning events, grouped
        per edge/switch/pathway. Numbers and route names only — no user text (the
        narrator cell runs sensitivity=low on the strength of that)."""
        from brain.observability import learning_ledger

        evidence: list[dict] = []
        recs = learning_ledger.read(limit=2000, session_id=session_id)

        by_edge: dict[str, list[dict]] = defaultdict(list)
        for r in recs:
            if r.get("decision") == "hebbian_update_applied" and r.get("src") and r.get("tgt"):
                by_edge[f"{r['src']}→{r['tgt']}"].append(r)
        edge_groups = sorted(
            by_edge.items(),
            key=lambda kv: abs(sum(float(x.get("delta") or 0) for x in kv[1])),
            reverse=True,
        )[:10]
        for edge, rows in edge_groups:
            net = sum(float(r.get("delta") or 0) for r in rows)
            if abs(net) < 0.005:
                continue
            first_w = rows[0].get("from_weight")
            last_w = rows[-1].get("to_weight")
            outcomes = [float(r.get("outcome") or 0) for r in rows]
            evidence.append(
                {
                    "text": (
                        f"route {edge}: {len(rows)} updates, weight {first_w}→{last_w} "
                        f"(net {net:+.3f}), mean outcome {sum(outcomes) / len(outcomes):+.2f}"
                    ),
                    "subsystem": "routing",
                    "edges": [{"edge": edge, "from_w": first_w, "to_w": last_w, "delta": round(net, 4)}],
                    "decision_types": ["hebbian_update_applied"],
                    "turn_ids": [r.get("turn_id", "") for r in rows if r.get("turn_id")][:8],
                    "metrics": {"n_updates": len(rows), "mean_outcome": round(sum(outcomes) / len(outcomes), 3)},
                }
            )

        for kind, subsystem, keyfield in (
            ("switch_routing_credit_applied", "switches", "switch"),
            ("recall_routing_credit_applied", "recall", "strategy"),
            ("drafter_competition_applied", "drafters", "drafter"),
        ):
            by_key: dict[str, list[dict]] = defaultdict(list)
            for r in recs:
                if r.get("decision") == kind:
                    by_key[str(r.get(keyfield) or r.get("tgt") or "?")].append(r)
            for key, rows in sorted(by_key.items(), key=lambda kv: -len(kv[1]))[:4]:
                net = sum(float(r.get("delta") or 0) for r in rows)
                if abs(net) < 0.005:
                    continue
                evidence.append(
                    {
                        "text": f"{subsystem} credit for {key}: {len(rows)} events, net delta {net:+.3f}",
                        "subsystem": subsystem,
                        "edges": [{"edge": key, "delta": round(net, 4)}],
                        "decision_types": [kind],
                        "turn_ids": [r.get("turn_id", "") for r in rows if r.get("turn_id")][:8],
                        "metrics": {"n_events": len(rows)},
                    }
                )

        summaries = [r for r in recs if r.get("decision") == "session_plasticity_summary"]
        if summaries:
            s = summaries[-1]
            evidence.append(
                {
                    "text": (
                        f"session plasticity {s.get('plasticity_modulator')}, "
                        f"{s.get('edges_updated')} edges updated, signal quality {s.get('signal_quality')}"
                    ),
                    "subsystem": "reward",
                    "edges": [],
                    "decision_types": ["session_plasticity_summary"],
                    "turn_ids": [],
                    "metrics": {
                        "plasticity_modulator": s.get("plasticity_modulator"),
                        "edges_updated": s.get("edges_updated"),
                    },
                }
            )

        emissions = [r for r in recs if r.get("decision") == "reward_emission"]
        if emissions:
            by_type = Counter(r.get("signal_type") or "self_graded" for r in emissions)
            total = sum(by_type.values())
            evidence.append(
                {
                    "text": (
                        f"reward mix this session: {dict(by_type)} "
                        f"({100 * by_type.get('self_graded', 0) // max(total, 1)}% self-graded)"
                    ),
                    "subsystem": "reward",
                    "edges": [],
                    "decision_types": ["reward_emission"],
                    "turn_ids": [],
                    "metrics": {"by_signal_type": dict(by_type)},
                }
            )
        return evidence

    def _stories_path(self) -> str:
        root = os.environ.get(
            "SECOND_BRAIN_PATH",
            os.path.join(os.path.dirname(__file__), "..", "second_brain"),
        )
        return os.path.join(root, "learning_stories.jsonl")

    def _persist_stories(self, stories: list[dict]) -> None:
        path = self._stories_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            existing: list[str] = []
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    existing = [ln for ln in f.read().splitlines() if ln.strip()]
            lines = existing + [json.dumps(s, default=str) for s in stories]
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-self._STORIES_KEEP:]) + "\n")
            os.replace(path + ".tmp", path)
        except Exception as e:
            logger.warning("[LearningNarrator] could not persist stories: %s", e)

    async def learning_story_pass(self, session_id: str) -> None:
        """Narrate this session's learning as first-person stories with citations.

        Evidence is assembled deterministically from the ledger; the LLM only
        PHRASES claims and cites evidence by index (joined back structurally, so
        hallucinated citations are impossible). LLM failure or an off flag falls
        back to template phrasing — the surface is never empty. Fail-open: this
        pass must never block the rest of sleep."""
        try:
            from brain.persona_key import active_or_home_persona, persona_slug

            evidence = self._learning_evidence(session_id)
            if not evidence:
                logger.debug("[LearningNarrator] no learning evidence for %s — skipping", session_id)
                return
            slug = persona_slug(active_or_home_persona())
            now = time.time()
            stories: list[dict] = []

            def _mk(claim: str, subsystem: str, refs: list[int], generator: str, confidence: float = 0.0) -> dict:
                cited = [evidence[i] for i in refs]
                return {
                    "id": f"st_{int(now)}_{len(stories)}",
                    "session_id": session_id,
                    "persona": slug,
                    "ts": now,
                    "claim": claim,
                    "subsystem": subsystem,
                    "evidence": {
                        "edges": [e for c in cited for e in c["edges"]],
                        "decision_types": sorted({d for c in cited for d in c["decision_types"]}),
                        "turn_ids": [t for c in cited for t in c["turn_ids"]][:10],
                        "metrics": cited[0]["metrics"] if cited else {},
                    },
                    "confidence": confidence,
                    "generator": generator,
                }

            try:
                digest = "\n".join(f"[{i}] {e['text']}" for i, e in enumerate(evidence))
                self._learning_narrator.reset_turn(f"sleep_{session_id}_learning")
                raw = await self._learning_narrator.call([{"role": "user", "content": digest}])
                parsed: dict = safe_json_parse(raw) or {}
                for s in (parsed.get("stories") or [])[:6]:
                    refs = [
                        i
                        for i in (s.get("evidence_refs") or [])
                        if isinstance(i, int) and 0 <= i < len(evidence)
                    ]
                    claim = str(s.get("claim") or "").strip()
                    if not claim or not refs:
                        continue
                    subsystem = str(s.get("subsystem") or evidence[refs[0]]["subsystem"])
                    stories.append(_mk(claim, subsystem, refs, "llm", float(s.get("confidence") or 0)))
            except Exception as e:
                logger.debug("[LearningNarrator] LLM pass failed (%s) — template fallback", e)

            if not stories:
                for i, ev in enumerate(evidence[:4]):
                    if not ev["edges"]:
                        continue
                    e0 = ev["edges"][0]
                    delta = float(e0.get("delta") or 0)
                    verb = "strengthened" if delta >= 0 else "weakened"
                    claim = (
                        f"The route {e0['edge']} {verb} by {delta:+.3f} across "
                        f"{ev['metrics'].get('n_updates', ev['metrics'].get('n_events', '?'))} events this session."
                    )
                    stories.append(_mk(claim, ev["subsystem"], [i], "template"))

            if not stories:
                return
            self._persist_stories(stories)
            for s in stories:
                decisions.log(
                    "learning_story",
                    turn_id="",
                    cluster="sleep",
                    session_id=session_id,
                    story_id=s["id"],
                    claim=s["claim"][:300],
                    subsystem=s["subsystem"],
                    generator=s["generator"],
                    persona=s["persona"],
                )
            logger.info(
                "[LearningNarrator] %d stories (%s) for session %s",
                len(stories),
                stories[0]["generator"],
                session_id,
            )
        except Exception as e:
            logger.warning("[LearningNarrator] pass failed: %s", e)

    # ── Hebbian pass ─────────────────────────────────────────────────────────
