"""
Hippocampus — sole gatekeeper to the second brain.
Encodes ALL substantive turns (perfect, non-degrading memory).
Retrieval intelligence determines relevance; storage does not gate memory.
"""
from __future__ import annotations

import logging
import os
import time
import uuid

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import StatefulSwitch, SwitchNeuron
from brain.observability.decisions import decisions
from brain.predictor import should_bypass_gating
from brain.second_brain.store import Episode, EpisodicStore, SchemaStore
from brain.security import sanitize_fact
from brain.utils import safe_json_parse
from brain.wiring import Wiring

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
    def __init__(self, bus: Bus, router: ModelRouter,
                 wiring: Wiring | None = None) -> None:
        self._bus = bus
        self._router = router
        self._episodic = EpisodicStore()
        self._schema = SchemaStore()
        self._wiring = wiring
        self._wiring_frozen = os.environ.get("BRAIN_WIRING_FROZEN", "false").lower() == "true"
        # Recent-recall reuse cache (query → last result)
        self._recent_recall: dict[str, dict] = {}
        self._recent_recall_order: list[str] = []  # MRU order, capped at 8

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

        # Switch neurons promoted from inline threshold constants. Profiles
        # mirror Temporal/Motor — modulators encode each gate's biological
        # identity; the effective threshold is shifted by chemistry at runtime.
        # See plan /Users/russ/.claude/plans/and-what-affects-these-memoized-parnas.md.
        self._encoder_gate = SwitchNeuron(
            "encoder_gate", CLUSTER, polarity="inhibitory",
            threshold=0.5,
            # DA+NE: engaged moments encode thoroughly (skip harder).
            # CORT: chronic stress also encodes thoroughly — threat memories
            # are exactly what the hippocampus is designed to preserve.
            modulators={"DA": +0.10, "NE": +0.10, "CORT": +0.10},
        )
        self._recall_cache_reuse = SwitchNeuron(
            "recall_cache_reuse", CLUSTER, polarity="excitatory",
            threshold=0.5,
            modulators={"DA": -0.10},
        )
        self._recall_fanout = StatefulSwitch(
            "recall_fanout", CLUSTER, decay=0.95,
            polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": -0.10, "Glu": -0.05},
        )
        self._entity_grep = SwitchNeuron(
            "entity_grep_depth", CLUSTER, polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": -0.10},
        )

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
        chem = self._chem_snapshot()

        # ── Coordinator gate: reuse recent recall if query is near-identical ──
        # The recall_cache_reuse switch encodes "trust the cache" as a fire.
        # High DA lowers its threshold (more reuse under reward); low DA raises
        # it (force fresh recall when nothing's working).
        cache_key = self._normalize_recall_key(query, entities)
        cached = self._recent_recall.get(cache_key)
        if cached is not None and self._recall_cache_reuse.should_fire(0.55, chem, turn_id):
            self._recall_cache_reuse.fire(0.55, "cache_hit",
                                          {"key": cache_key[:60]}, snapshot=chem)
            decisions.log(
                "reuse_recent_recall", turn_id=turn_id, cluster=CLUSTER,
                reason=f"normalized query key '{cache_key[:60]}' matches recent",
                cost_saved_est=0.0,
            )
            trace = self._record_trace()
            if trace is not None:
                trace.predictor_outcomes.append({
                    "cluster": CLUSTER, "stage": "recall_coordinator",
                    "integrator_woken": False, "reused": True,
                })
            return cached

        # ── Weighted recall fan-out (Hebbian × chemistry) ────────────────────
        strategy_weights = self._recall_strategy_weights()
        # The fanout switch's effective threshold biases the total budget:
        # under high ACh+Glu (lower threshold), the brain casts a wider net.
        total_budget = self._fanout_total_budget(chem)
        schema_k, episode_k = self._allocate_recall_budget(strategy_weights, total_budget)
        self._recall_fanout.fire(min(1.0, total_budget / 8.0), "fanout_budget",
                                  {"total": total_budget}, snapshot=chem)

        # Schema grep (free, fast). Entity-grep depth is shifted by ACh: under
        # high curiosity the brain scans entities more broadly.
        grep_depth = self._entity_grep_depth(chem, schema_k)
        schema_hits = []
        for entity in entities[:grep_depth]:
            hits = self._schema.grep(entity)
            schema_hits.extend(hits[:2])

        schema_context = "\n".join(f"[{f}] {line}" for f, line in schema_hits[:6])

        # Episodic recall (vector search if embedding available)
        episodes = []
        if embedding_fn and query:
            try:
                vec = await embedding_fn(query)
                episodes = self._episodic.recall(vec, limit=max(2, episode_k))
            except Exception as e:
                logger.warning("[Memory] Episode search failed — response won't include relevant past memories: %s", e)

        episode_text = ""
        if episodes:
            parts = []
            for ep in episodes:
                parts.append(f"[{ep.get('ts', 0):.0f}] {ep.get('user_input', '')} → "
                              f"{ep.get('entity_response', '')[:200]}")
            episode_text = "\n".join(parts)

        result = {
            "schema": schema_context,
            "episodes": episode_text,
            "core": self._core_context,
        }

        # Cache for potential reuse next turn
        self._cache_recall(cache_key, result)

        if self._wiring is not None and not self._wiring_frozen:
            decisions.log(
                "weighted_recall_fanout", turn_id=turn_id, cluster=CLUSTER,
                schema_k=schema_k, episode_k=episode_k,
                weights={k: round(v, 3) for k, v in strategy_weights.items()},
            )
        return result

    def _normalize_recall_key(self, query: str, entities: list[str]) -> str:
        """Cheap normalization for cache key — lowercase, dedupe whitespace, sort entities."""
        q = " ".join((query or "").lower().split())
        ents = ",".join(sorted([e.lower().strip() for e in entities if e]))
        return f"{q}|{ents}"

    def _cache_recall(self, key: str, result: dict) -> None:
        self._recent_recall[key] = result
        self._recent_recall_order.append(key)
        while len(self._recent_recall_order) > 8:
            old = self._recent_recall_order.pop(0)
            self._recent_recall.pop(old, None)

    def _recall_strategy_weights(self) -> dict[str, float]:
        """Edge weights into each recall strategy. Uniform when no wiring."""
        if self._wiring is None or self._wiring_frozen:
            return {"cosine_recall": 1.0, "schema_grep": 1.0,
                    "entity_tracker": 1.0, "time_filter": 1.0}
        return {
            "cosine_recall": self._wiring.get_edge_weight("mem.recall", "hippocampus.cosine_recall"),
            "schema_grep": self._wiring.get_edge_weight("mem.recall", "hippocampus.schema_grep"),
            "entity_tracker": self._wiring.get_edge_weight("mem.recall", "hippocampus.entity_tracker"),
            "time_filter": self._wiring.get_edge_weight("mem.recall", "hippocampus.time_filter"),
        }

    def _allocate_recall_budget(self, weights: dict[str, float],
                                  total_budget: int = 8) -> tuple[int, int]:
        """Divide a fixed total fan-out across schema vs episodes by weight ratio."""
        schema_w = weights["schema_grep"] + weights["entity_tracker"]
        episode_w = weights["cosine_recall"] + weights["time_filter"]
        total = schema_w + episode_w
        if total <= 0:
            half = total_budget // 2
            return max(1, half), max(1, total_budget - half)
        schema_share = schema_w / total
        schema_k = max(1, round(schema_share * total_budget))
        episode_k = max(1, total_budget - schema_k)
        return schema_k, episode_k

    def _chem_snapshot(self) -> dict[str, float]:
        """Merged neuromod + hormonal snapshot for switch modulation."""
        try:
            nm = self._bus.neuromod.snapshot()
        except Exception:
            nm = {}
        try:
            hs = self._bus.hormonal.snapshot()
        except Exception:
            hs = {}
        return {**nm, **hs}

    def _fanout_total_budget(self, chem: dict[str, float]) -> int:
        """Total recall lookups, biased by the recall_fanout switch's modulation
        delta. Base is 8; bounded to [4, 12]. Under high ACh+Glu (lower
        effective threshold), the brain casts a wider net."""
        # modulation_delta is negative when chemistry lowers the threshold;
        # we invert the sign so "lower threshold" corresponds to "more lookups".
        delta = -self._recall_fanout.modulation_delta(chem)
        # delta range under conservative coefficients (≤0.15) is approximately
        # ±0.075. Scale to integer shifts in {-3, …, +3}.
        shift = int(round(delta * 20))
        return max(4, min(12, 8 + shift))

    def _entity_grep_depth(self, chem: dict[str, float], schema_k: int) -> int:
        """Number of entities to grep against the schema store. Base is
        max(2, schema_k); ACh modulation widens or narrows it by ±1."""
        base = max(2, schema_k)
        delta = -self._entity_grep.modulation_delta(chem)  # negative coeff lowers thr → more entities
        shift = int(round(delta * 20))
        return max(1, min(8, base + shift))

    def _record_trace(self):
        try:
            from brain.observability.firing_path import current_turn_trace
            return current_turn_trace.get()
        except Exception:
            return None

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

        # Encoder gate: skip the LLM encoder when surprise + DA delta + facts
        # are all low. The episode still gets stored as raw text below, just
        # without an LLM-generated summary. The encoder_gate switch's chemistry
        # modulation can suppress the skip under high DA+NE (engaged moments
        # encode more thoroughly).
        bypass, bypass_reason = should_bypass_gating(affect, features)
        da_now = neuromod_snap.get("DA", 0.5) if neuromod_snap else 0.5
        chem = self._chem_snapshot()
        baseline_skip = (
            not bypass
            and surprise_score < 0.25
            and salience < 0.4
            and da_now < 0.6
            and not features.get("entities")
        )
        skip_encoder = baseline_skip and self._encoder_gate.should_fire(0.55, chem, turn_id)

        if skip_encoder:
            self._encoder_gate.fire(0.55, "encoder_skipped",
                                     {"surprise": round(surprise_score, 3),
                                      "salience": round(salience, 3),
                                      "DA": round(da_now, 3)},
                                     snapshot=chem)
            decisions.log(
                "skip_encoder", turn_id=turn_id, cluster=CLUSTER,
                reason=f"surprise={surprise_score:.2f} salience={salience:.2f} DA={da_now:.2f}",
                cost_saved_est=0.0005,
            )
            trace = self._record_trace()
            if trace is not None:
                trace.llm_calls_saved += 1
            encoded = {"topic_tags": [features.get("topic_summary", "low_salience")],
                       "entities": [], "key_facts": [], "relationship_note": "",
                       "summary": user_input[:120]}
            # Skip the LLM call but proceed to embed + store raw episode
            return await self._store_episode(
                session_id, turn_id, user_input, entity_response,
                features, affect, neuromod_snap, surprise_score,
                encoded, embedding_fn,
            )

        self._encoder.reset_turn(turn_id)
        messages = [{"role": "user", "content":
                     f"User: {user_input}\nBrain: {entity_response}\n"
                     f"Context: intent={intent}, emotion={affect.get('emotion', 'neutral')}"}]
        raw = await self._encoder.call(messages)

        encoded = safe_json_parse(raw) or {}

        await self._store_episode(
            session_id, turn_id, user_input, entity_response,
            features, affect, neuromod_snap, surprise_score,
            encoded, embedding_fn,
        )

    async def encode_idle_thought(self, session_id: str, thought: str,
                                   overlap_with_user_input: float,
                                   user_input: str = "",
                                   embedding_fn=None) -> None:
        """Encode a DMN idle thought as a low-priority episode.

        Fires when the user's actual input has high word-overlap with a
        recent idle thought — the brain was right to think about it, so
        the thought becomes part of the entity's autobiography (it
        "remembers" what it was musing about when the user spoke to that
        topic).
        """
        if not thought.strip():
            return
        try:
            vec = None
            if embedding_fn:
                try:
                    vec = await embedding_fn(thought)
                except Exception as e:
                    logger.debug("[Memory] Idle-thought embed failed: %s", e)
            turn_id = f"idle_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            episode = Episode(
                session_id=session_id,
                turn_id=turn_id,
                ts=time.time(),
                user_input=user_input[:200] if user_input else "(no user input — idle thought)",
                entity_response=thought,
                topic_tags=["idle_thought", "reinforced"],
                emotion_state="reflective",
                user_emotion="unknown",
                entities=[],
                neuromod_snapshot={"DA": 0.5, "GABA": 0.1, "ACh": 0.4, "Glu": 0.3},
                surprise_score=max(0.0, 1.0 - overlap_with_user_input),
                vector=vec,
            )
            self._episodic.encode(episode)
            logger.info("[Memory] Encoded idle thought as episode "
                        "(overlap %.2f with user): %r",
                        overlap_with_user_input, thought[:80])
        except Exception as e:
            logger.warning("[Memory] Idle-thought encoding failed: %s", e)

    async def _store_episode(self, session_id: str, turn_id: str,
                              user_input: str, entity_response: str,
                              features: dict, affect: dict,
                              neuromod_snap: dict, surprise_score: float,
                              encoded: dict, embedding_fn) -> None:
        topic_tags = (encoded.get("topic_tags") or features.get("entities", []) or
                      [features.get("topic_summary", "misc")])
        entities = encoded.get("entities") or features.get("entities", [])
        intent = features.get("intent", "other")

        # Route facts to the current speaker's schema file (or primary user.md)
        speaker_name = features.get("speaker_name", "")
        if speaker_name:
            schema_file = self._schema.ensure_speaker_schema(speaker_name)
        else:
            schema_file = "user.md"

        # Update schema with any new key facts
        for raw_fact in encoded.get("key_facts", []):
            fact = sanitize_fact(raw_fact)
            if fact:
                await self._schema.aappend_fact(schema_file, fact)

        # Record relationship observations so familiarity accumulates over time
        rel_note = encoded.get("relationship_note", "").strip()
        if rel_note:
            rel_fact = sanitize_fact(rel_note)
            if rel_fact:
                await self._schema.aappend_fact(schema_file, f"[relationship] {rel_fact}")

        # Update running affection score based on how the user treated the AI this turn
        tone = features.get("user_tone_toward_ai", "neutral")
        await self._update_affection_score(tone, speaker_name=speaker_name)

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

    async def _update_affection_score(self, tone: str, speaker_name: str = "") -> None:
        """Read current affection score for speaker, apply delta, write back."""
        import re
        delta = self._AFFECTION_DELTAS.get(tone, 0)
        if delta == 0:
            return
        if speaker_name:
            schema_file = self._schema.ensure_speaker_schema(speaker_name)
        else:
            schema_file = "user.md"
        async with self._schema._lock:
            content = self._schema.read(schema_file)
            m = re.search(r"- Score:\s*(-?\d+)", content)
            current = int(m.group(1)) if m else 0
            new_score = max(-50, min(100, current + delta))
            tier_label = self._AFFECTION_TIERS[-1][1]
            for threshold, label in self._AFFECTION_TIERS:
                if new_score >= threshold:
                    tier_label = label
                    break
            if m:
                content = content[:m.start()] + f"- Score: {new_score}" + content[m.end():]
            else:
                content += f"\n- Score: {new_score}"
            hist_line = f"- History: {tier_label} (last tone: {tone}, delta: {delta:+d})"
            content = re.sub(r"- History:.*", hist_line, content)
            if "- History:" not in content:
                content += f"\n{hist_line}"
            from brain.second_brain.store import SCHEMA_DIR
            self._schema._atomic_write(SCHEMA_DIR / schema_file, content)
            logger.debug("[Memory] Affection score [%s]: %d→%d (%s, tone=%s)",
                         schema_file, current, new_score, tier_label, tone)

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
