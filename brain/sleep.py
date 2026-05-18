"""
Sleep consolidation — runs at session end (or between sessions).
Re-indexes recent episodes, compresses for retrieval efficiency,
updates self.md autobiography, extracts facts to user.md.
Uses batch-friendly API calls (no real-time constraint).

v0.2 feature.
"""
from __future__ import annotations

import json
import logging
import time

from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.second_brain.store import SchemaStore, EpisodicStore

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
                 episodic: EpisodicStore) -> None:
        self._router = router
        self._schema = schema
        self._episodic = episodic

        self._self_updater = IntegratorCell(
            name="self_updater",
            cluster="sleep",
            model="flash-lite",
            system_prompt=SELF_UPDATE_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
        )
        self._self_updater.set_router(router)

        self._synthesizer = IntegratorCell(
            name="episode_synthesizer",
            cluster="sleep",
            model="flash-lite",
            system_prompt=EPISODE_SYNTHESIS_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
        )
        self._synthesizer.set_router(router)

    async def consolidate(self, session_id: str, session_traces: list[dict]) -> None:
        """
        Run full consolidation after a session ends.
        session_traces: list of {user_input, entity_response, emotion, topic_tags} dicts.
        """
        if not session_traces:
            return

        logger.info("Sleep: consolidating %d turns from session %s",
                    len(session_traces), session_id)
        start = time.time()

        # 1. Episode synthesis — extract facts from session
        turn_id = f"sleep_{session_id}"
        self._synthesizer.reset_turn(turn_id)

        batch_text = "\n".join(
            f"Turn {i+1}: User: {t.get('user_input', '')[:200]} | "
            f"Brain: {t.get('entity_response', '')[:200]}"
            for i, t in enumerate(session_traces[-20:])  # last 20 turns
        )
        raw = await self._synthesizer.call([{"role": "user", "content": batch_text}])

        synthesis: dict = {}
        try:
            synthesis = json.loads(raw)
        except Exception:
            import re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    synthesis = json.loads(m.group(0))
                except Exception:
                    pass

        # Update user.md with discovered facts
        for fact in synthesis.get("user_facts", []):
            self._schema.append_fact("user.md", fact)
            logger.debug("Sleep: user fact → %s", fact[:80])

        # 2. Self-model update
        self._self_updater.reset_turn(turn_id + "_self")
        current_self = self._schema.read("self.md")
        context = (
            f"Current self-model:\n{current_self}\n\n"
            f"Session summary:\n{batch_text[:1000]}\n\n"
            f"Topics: {', '.join(synthesis.get('topic_clusters', []))}\n"
            f"Patterns: {', '.join(synthesis.get('response_patterns', []))}"
        )
        raw_self = await self._self_updater.call([{"role": "user", "content": context}])

        updates: dict = {}
        try:
            updates = json.loads(raw_self)
        except Exception:
            import re
            m = re.search(r'\{.*\}', raw_self, re.DOTALL)
            if m:
                try:
                    updates = json.loads(m.group(0))
                except Exception:
                    pass

        if updates:
            self._apply_self_updates(updates)

        elapsed = time.time() - start
        logger.info("Sleep: consolidation complete in %.2fs", elapsed)

    def _apply_self_updates(self, updates: dict) -> None:
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

        self._schema.write("self.md", existing)
        logger.debug("Sleep: self.md updated")
