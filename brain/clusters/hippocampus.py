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
from brain.security import sanitize_fact

logger = logging.getLogger(__name__)

CLUSTER = "hippocampus"

RECALL_REFORMULATION_SYSTEM = """You are the hippocampus of an AI brain.
Given a user query and conversation context, produce a search reformulation
optimized for semantic similarity search over episodic memory.
Return JSON: {"search_query": string, "topic_tags": [string], "time_filter": string|null}
Return ONLY JSON."""

ENCODER_SYSTEM = """You are the hippocampus encoding a conversation turn into long-term memory.
Summarize this turn into a compact episodic record.
Return JSON: {
  "summary": string,          // 1-2 sentences describing what happened this turn
  "topic_tags": [string],     // 2-4 topic tags
  "entities": [string],       // named entities mentioned
  "key_facts": [string],      // facts about the user worth remembering long-term (preferences, life details, opinions)
  "relationship_note": string // optional: if this turn reveals something about the relationship depth or
                              // the user's personality/humour/warmth, note it briefly (else empty string)
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
            model="local",   # local-only cell — routes directly to Ollama, never cloud
            system_prompt=ENCODER_SYSTEM,
            topics=["mem.encode"],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._encoder.set_router(router)

        self._coordinator = IntegratorCell(
            name="coordinator",
            cluster=CLUSTER,
            model="local",   # local-only cell — routes directly to Ollama, never cloud
            system_prompt=RECALL_REFORMULATION_SYSTEM,
            topics=["mem.recall"],
            max_calls_per_turn=2,
            locality="local",
            sensitivity="sensitive",
        )
        self._coordinator.set_router(router)

        self._recall_inbox = bus.subscribe("mem.recall")

        # Pre-load core schema at boot (extended mind — reliably needed every session)
        self._core_context: dict[str, str] = {}

    async def boot(self, session_id: str) -> tuple[dict[str, str], list[dict]]:
        """Load core schema and recent episodes into working memory at session start."""
        self._session_id = session_id
        self._schema.ensure_self_schema()
        self._schema.ensure_user_schema()
        self._core_context = self._schema.load_core_context()
        logger.info("[Memory] Loaded: self-model=%d chars, user-model=%d chars",
                    len(self._core_context.get("self", "")),
                    len(self._core_context.get("user", "")))
        recent = self._episodic.recall_recent(limit=6)
        if recent:
            logger.info("[Memory] Session bridge: seeding parietal with %d recent episodes", len(recent))
        return self._core_context, recent

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
                logger.warning("[Memory] Episode search failed — response won't include relevant past memories: %s", e)

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
        for raw_fact in encoded.get("key_facts", []):
            fact = sanitize_fact(raw_fact)
            if fact:
                await self._schema.aappend_fact("user.md", fact)

        # Record relationship observations so familiarity accumulates over time
        rel_note = encoded.get("relationship_note", "").strip()
        if rel_note:
            rel_fact = sanitize_fact(rel_note)
            if rel_fact:
                await self._schema.aappend_fact("user.md", f"[relationship] {rel_fact}")

        # Update running affection score based on how the user treated the AI this turn
        tone = features.get("user_tone_toward_ai", "neutral")
        await self._update_affection_score(tone)

        # Build embedding vector
        vec = None
        if embedding_fn:
            try:
                combined = f"{user_input} {entity_response}"
                vec = await embedding_fn(combined)
            except Exception as e:
                logger.warning("[Memory] Could not generate embedding vector — episode will be stored without search index (won't appear in future recall): %s", e)

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
        logger.debug("[Memory] Episode saved: turn=%s intent=%s", turn_id, intent)

    # Affection score deltas per tone (clamped to -50..+100)
    _AFFECTION_DELTAS: dict[str, int] = {
        "praising":   +3,
        "warm":       +2,
        "joking":     +2,
        "polite":     +1,
        "neutral":     0,
        "testing":    -1,
        "impatient":  -2,
        "dismissive": -3,
        "insulting":  -5,
    }

    _AFFECTION_TIERS = [
        (40,  "close friends — tease freely, very warm, in-jokes welcome"),
        (20,  "warm friends — relaxed, light teasing okay, personal"),
        (5,   "friendly — warm and engaged, hold the teasing"),
        (-10, "neutral — polite and helpful, professional warmth"),
        (-25, "cool — measured, less personal, minimal humour"),
        (-50, "guarded — formal, no warmth, brief answers"),
    ]

    async def _update_affection_score(self, tone: str) -> None:
        """Read current affection score from user.md, apply delta, write back."""
        import re
        delta = self._AFFECTION_DELTAS.get(tone, 0)
        if delta == 0:
            return
        async with self._schema._lock:
            content = self._schema.read("user.md")
            # Find current score
            m = re.search(r"- Score:\s*(-?\d+)", content)
            current = int(m.group(1)) if m else 0
            new_score = max(-50, min(100, current + delta))
            # Find tier label
            tier_label = self._AFFECTION_TIERS[-1][1]
            for threshold, label in self._AFFECTION_TIERS:
                if new_score >= threshold:
                    tier_label = label
                    break
            # Replace score line
            if m:
                content = content[:m.start()] + f"- Score: {new_score}" + content[m.end():]
            else:
                content += f"\n- Score: {new_score}"
            # Replace or insert history line
            hist_line = f"- History: {tier_label} (last tone: {tone}, delta: {delta:+d})"
            content = re.sub(r"- History:.*", hist_line, content)
            if "- History:" not in content:
                content += f"\n{hist_line}"
            from brain.second_brain.store import SCHEMA_DIR
            self._schema._atomic_write(SCHEMA_DIR / "user.md", content)
            logger.debug("[Memory] Affection score: %d→%d (%s, tone=%s)", current, new_score, tier_label, tone)

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
