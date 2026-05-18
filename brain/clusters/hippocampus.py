"""
Hippocampus — sole gatekeeper to the second brain.
Encodes ALL substantive turns (perfect, non-degrading memory).
Retrieval intelligence determines relevance; storage does not gate memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.second_brain.store import EpisodicStore, SchemaStore, Episode

logger = logging.getLogger(__name__)

CLUSTER = "hippocampus"

RECALL_REFORMULATION_SYSTEM = """You are the hippocampus of an AI brain.
Given a user query and conversation context, produce a search reformulation
optimized for semantic similarity search over episodic memory.
Return JSON: {"search_query": string, "topic_tags": [string], "time_filter": string|null}
Return ONLY JSON."""

ENCODER_SYSTEM = """You are the hippocampus encoding a conversation turn.
Summarize this turn into a compact episodic record.
Return JSON: {
  "summary": string,       // 1-2 sentences
  "topic_tags": [string],  // 2-4 topic tags
  "entities": [string],    // named entities mentioned
  "key_facts": [string]    // any facts stated that should be remembered long-term
}
Return ONLY JSON."""


class HippocampusCluster:
    def __init__(self, bus: Bus, router: ModelRouter) -> None:
        self._bus = bus
        self._router = router
        self._episodic = EpisodicStore()
        self._schema = SchemaStore()

        self._encoder = IntegratorCell(
            name="encoder",
            cluster=CLUSTER,
            model="flash-lite",
            system_prompt=ENCODER_SYSTEM,
            topics=["mem.encode"],
            max_calls_per_turn=1,
        )
        self._encoder.set_router(router)

        self._coordinator = IntegratorCell(
            name="coordinator",
            cluster=CLUSTER,
            model="flash-lite",
            system_prompt=RECALL_REFORMULATION_SYSTEM,
            topics=["mem.recall"],
            max_calls_per_turn=2,
        )
        self._coordinator.set_router(router)

        self._recall_inbox = bus.subscribe("mem.recall")

        # Pre-load core schema at boot (extended mind — reliably needed every session)
        self._core_context: dict[str, str] = {}

    async def boot(self, session_id: str) -> dict[str, str]:
        """Load core schema into working memory at session start."""
        self._session_id = session_id
        self._schema.ensure_self_schema()
        self._schema.ensure_user_schema()
        self._core_context = self._schema.load_core_context()
        logger.info("Hippocampus: core schema pre-loaded (self=%d chars, user=%d chars)",
                    len(self._core_context.get("self", "")),
                    len(self._core_context.get("user", "")))
        return self._core_context

    async def recall(self, query: str, entities: list[str],
                     turn_id: str, embedding_fn=None) -> dict:
        """
        Recall from episodic + schema stores.
        Returns combined context for the frontal lobe.
        """
        # Schema grep (free, fast)
        schema_hits = []
        for entity in entities[:4]:
            hits = self._schema.grep(entity)
            schema_hits.extend(hits[:2])

        schema_context = "\n".join(f"[{f}] {line}" for f, line in schema_hits[:6])

        # Episodic recall (vector search if embedding available)
        episodes = []
        if embedding_fn and query:
            try:
                vec = await embedding_fn(query)
                episodes = self._episodic.recall(vec, limit=4)
            except Exception as e:
                logger.warning("Episodic recall failed: %s", e)

        episode_text = ""
        if episodes:
            parts = []
            for ep in episodes:
                parts.append(f"[{ep.get('ts', 0):.0f}] {ep.get('user_input', '')} → "
                              f"{ep.get('entity_response', '')[:200]}")
            episode_text = "\n".join(parts)

        return {
            "schema": schema_context,
            "episodes": episode_text,
            "core": self._core_context,
        }

    async def encode(self, session_id: str, turn_id: str, user_input: str,
                     entity_response: str, features: dict, affect: dict,
                     neuromod_snap: dict, surprise_score: float,
                     embedding_fn=None) -> None:
        """
        Encode every substantive turn. Storage doesn't gate memory — retrieval does.
        Trivial turns (intent=greeting/ack, salience=0) are skipped.
        """
        intent = features.get("intent", "other")
        salience = features.get("salience", 0.5)

        # Only skip truly trivial turns
        if intent in ("greeting", "farewell", "ack") and salience < 0.1:
            return

        self._encoder.reset_turn(turn_id)
        messages = [{"role": "user", "content":
                     f"User: {user_input}\nBrain: {entity_response}\n"
                     f"Context: intent={intent}, emotion={affect.get('emotion', 'neutral')}"}]
        raw = await self._encoder.call(messages)

        encoded = {}
        try:
            encoded = json.loads(raw)
        except Exception:
            import re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    encoded = json.loads(m.group(0))
                except Exception:
                    pass

        topic_tags = (encoded.get("topic_tags") or features.get("entities", []) or
                      [features.get("topic_summary", "misc")])
        entities = encoded.get("entities") or features.get("entities", [])

        # Update schema with any new key facts
        for fact in encoded.get("key_facts", []):
            self._schema.append_fact("user.md", fact)

        # Build embedding vector
        vec = None
        if embedding_fn:
            try:
                combined = f"{user_input} {entity_response}"
                vec = await embedding_fn(combined)
            except Exception as e:
                logger.warning("Embedding failed: %s", e)

        episode = Episode(
            session_id=session_id,
            turn_id=turn_id,
            ts=time.time(),
            user_input=user_input,
            entity_response=entity_response,
            topic_tags=topic_tags,
            emotion_state=affect.get("emotion", "neutral"),
            user_emotion=features.get("user_emotion", "unknown"),
            entities=entities,
            neuromod_snapshot=neuromod_snap,
            surprise_score=surprise_score,
            vector=vec,
        )
        self._episodic.encode(episode)
        logger.debug("Hippocampus: encoded turn %s (intent=%s)", turn_id, intent)

    def update_self_schema(self, updates: dict) -> None:
        """Write updates to self.md (called at sleep consolidation)."""
        existing = self._schema.read("self.md")
        for section, content in updates.items():
            if section in existing:
                # Simple replace of section content — good enough for v0.1
                import re
                pattern = rf"(## {re.escape(section)}\n)(.*?)(\n## |\Z)"
                replacement = f"\\1{content}\n\\3"
                existing = re.sub(pattern, replacement, existing, flags=re.DOTALL)
        self._schema.write("self.md", existing)
