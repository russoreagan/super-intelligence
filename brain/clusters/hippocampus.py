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
from brain.neuron import SwitchNeuron
from brain.observability.decisions import decisions
from brain.predictor import should_bypass_gating
from brain.second_brain.store import Episode, EpisodicStore, SchemaStore
from brain.security import sanitize_fact
from brain.settings import settings
from brain.utils import safe_json_parse
from brain.wiring import Wiring

logger = logging.getLogger(__name__)

CLUSTER = "hippocampus"

# ── Bond field access (one reader, one writer) ────────────────────────────────
# `- Bond:` postdates the bond model, so a schema written before it shipped has
# no such line. Every reader of that field used to guess its absence differently
# (0.0 here, `max(0, affection)` there), and the 0.0 guess reported a 44-session
# close relationship as "new". These two helpers are the single place the field
# is parsed and written: absence means "predates the field", and is healed by
# reconstructing the bond the file's own history implies.

_BOND_RE = r"- Bond:\s*-?\d+(?:\.\d+)?"


def _parse_bond(content: str) -> tuple[float, bool]:
    """Read the bond for a speaker schema. Returns `(bond, seeded)`, where
    `seeded` marks a pre-bond-model file whose bond was reconstructed from its
    legacy signals and so MUST be persisted by the caller — leave it unwritten
    and the reconstruction re-runs every turn, letting the interaction count
    inflate the bond forever instead of the bond model governing it."""
    import re

    from brain.relationship import seed_bond_from_legacy

    m = re.search(r"- Bond:\s*(-?\d+(?:\.\d+)?)", content)
    if m:
        return float(m.group(1)), False

    m_aff = re.search(r"- Score:\s*(-?\d+)", content)
    m_count = re.search(r"- Interactions:\s*(\d+)", content)
    m_fam = re.search(r"- Familiarity:\s*(\w+)", content)
    bond = seed_bond_from_legacy(
        float(m_aff.group(1)) if m_aff else 0.0,
        int(m_count.group(1)) if m_count else 0,
        m_fam.group(1).lower() if m_fam else "",
        close_bond=float(settings.get("familiarity_close_bond")),
        acquainted_bond=float(settings.get("familiarity_acquainted_bond")),
    )
    return bond, True


def _write_bond(content: str, bond: float) -> str:
    """Replace or append the `- Bond:` line."""
    import re

    line = f"- Bond: {bond:.1f}"
    if re.search(_BOND_RE, content):
        return re.sub(_BOND_RE, line, content, count=1)
    return content + f"\n{line}"


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
  "relationship_note": string,// optional: if this turn reveals something about the relationship depth or
                              // the user's personality/humour/warmth, note it briefly (else empty string)
  "strategy_tags": [string]   // 0-2 labels for HOW the problem was handled (the problem-solving SHAPE,
                              // NOT the topic). Choose ONLY from this fixed vocabulary, or return []:
                              //   "decomposed-into-steps"          broke a big task into smaller parts
                              //   "sought-clarification-first"     asked a question before committing
                              //   "prioritized-under-time-pressure" triaged what mattered under constraint
                              //   "verified-before-acting"         checked/confirmed before doing
                              //   "analogized-from-prior"          reused an approach from something else
                              //   "explored-by-trial-and-error"    probed iteratively without a known path
                              // These must be domain-independent — the same tag should fit problems in
                              // completely unrelated subjects. Omit rather than inventing new tags.
}
Return ONLY JSON."""

# Canonical problem-solving "approach" vocabulary. Kept deliberately small and
# tightly scoped so the system settles into stable patterns; this is the main
# steering point for what the brain learns to name as a strategy. Stored on
# episodes namespaced as "approach:<tag>" so they ride the existing tag-scoped
# recall without polluting topic matching.
APPROACH_TAGS: tuple[str, ...] = (
    "decomposed-into-steps",
    "sought-clarification-first",
    "prioritized-under-time-pressure",
    "verified-before-acting",
    "analogized-from-prior",
    "explored-by-trial-and-error",
)

# Ordering shared by encode-time and query-time cognitive signatures. Chemistry
# channels + graded structural signals + binary problem-STRUCTURE flags. NO topic
# or entity content — that exclusion is what lets a memory transfer across domains.
SIGNATURE_KEYS: tuple[str, ...] = (
    # chemistry / activation profile
    "DA",
    "ACh",
    "GABA",
    "NE",
    "Glu",
    # graded structural signals
    "surprise",
    "salience",
    "inhibition",  # normalized suppression_pressure (inhibitory load)
    # binary problem-structure flags (0.0 / 1.0)
    "requires_decomposition",
    "requires_verification",
    "high_stakes",
    "time_pressure",
    "open_ended",
)

# Structural recall tuning. A candidate must clear MIN_SIM to be surfaced as a
# real cross-domain match; if even the closest candidate sits below ANOMALY_FLOOR
# the current cognitive STATE itself is without precedent (not just the topic).
STRUCTURAL_MIN_SIM = 0.80
STRUCTURAL_ANOMALY_FLOOR = 0.55


class HippocampusCluster:
    def __init__(self, bus: Bus, router: ModelRouter, wiring: Wiring | None = None) -> None:
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
            model="local",  # local-only cell — routes directly to Ollama, never cloud
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
            model="local",  # local-only cell — routes directly to Ollama, never cloud
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
            "encoder_gate",
            CLUSTER,
            polarity="inhibitory",
            threshold=0.5,
            # DA+NE: engaged moments encode thoroughly (skip harder).
            # CORT: chronic stress also encodes thoroughly — threat memories
            # are exactly what the hippocampus is designed to preserve.
            modulators={"DA": +0.10, "NE": +0.10, "CORT": +0.10},
        )
        self._recall_cache_reuse = SwitchNeuron(
            "recall_cache_reuse",
            CLUSTER,
            polarity="excitatory",
            threshold=0.5,
            modulators={"DA": -0.10},
        )
        # Plain comparator: only SwitchNeuron methods (.fire/.should_fire/
        # .modulation_delta) are ever used here — the StatefulSwitch accumulator/decay
        # was never touched, so the `decay=` was dead. Collapsed to SwitchNeuron.
        self._recall_fanout = SwitchNeuron(
            "recall_fanout",
            CLUSTER,
            polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": -0.10, "Glu": -0.05},
        )
        self._entity_grep = SwitchNeuron(
            "entity_grep_depth",
            CLUSTER,
            polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": -0.10},
        )
        # Structural (cross-domain) recall gate. Fires only on novelty — high
        # surprise / weak topic match / emotion-aware bypass. ACh (curiosity) and
        # surprise lower its threshold so a genuinely new situation casts the net
        # wider for analogous past problem-shapes.
        self._structural_recall = SwitchNeuron(
            "structural_recall",
            CLUSTER,
            polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": -0.10, "NE": -0.05},
        )

    async def boot(self, session_id: str) -> tuple[dict[str, str], list[dict]]:
        """Load core schema and recent episodes into working memory at session start."""
        self._session_id = session_id
        self._schema.ensure_self_schema()
        self._schema.ensure_user_schema()
        self._core_context = self._schema.load_core_context()
        logger.info(
            "[Memory] Loaded: self-model=%d chars, user-model=%d chars",
            len(self._core_context.get("self", "")),
            len(self._core_context.get("user", "")),
        )
        recent = self._episodic.recall_recent(limit=6)
        if recent:
            logger.info(
                "[Memory] Session bridge: seeding parietal with %d recent episodes", len(recent)
            )
        return self._core_context, recent

    def _active_core_context(self) -> dict[str, str]:
        """The self-model/core context for the persona bound on this turn (multi-
        persona Path B), cached per persona so the prompt prefix stays byte-stable
        for that persona. No persona bound → the boot-loaded process default,
        unchanged. load_core_context() reads self.md via _resolve_persona, so calling
        it under bind_persona() picks up the bound persona's files."""
        from brain.second_brain.store import active_persona

        p = active_persona()
        if not p:
            return self._core_context
        cache = getattr(self, "_persona_core", None)
        if cache is None:
            cache = self._persona_core = {}
        cc = cache.get(p)
        if cc is None:
            cc = cache[p] = self._schema.load_core_context()
        return cc

    async def recall(
        self,
        query: str,
        entities: list[str],
        turn_id: str,
        embedding_fn=None,
        novelty: bool = False,
        features: dict | None = None,
    ) -> dict:
        """
        Recall from episodic + schema stores.
        Returns combined context for the frontal lobe.

        When ``novelty`` is set, also run the structural (cross-domain) pass:
        match the current cognitive signature against stored signatures to surface
        problem-shapes the brain has solved before even when the topic is new.
        """
        chem = self._chem_snapshot()

        # Personal/sensitive CONVERSATION memory is partitioned by end-user. An agent
        # (partner) session recalls only its own end_user_id's episodes, so one user's
        # conversations are never carried into another session — nor is the owner's
        # general conversation. The owner/companion lane stays unscoped (its own brain,
        # and avoids dropping legacy NULL-end_user_id episodes).
        #
        # Abstracted cross-user learning is deliberately PRESERVED: it rides structural
        # (cog-signature) recall + procedure/motor memory + sleep consolidation, which
        # stay persona-wide so the agent keeps consolidating across users. Do NOT scope
        # those by end_user_id — only the conversation-content paths below.
        from brain.turn_ctx import current_turn

        _lane = current_turn()
        scope_eu = (
            _lane.get("end_user_id")
            if _lane.get("channel") == "agent" and _lane.get("end_user_id")
            else None
        )

        # ── Global-workspace spotlight (locked contract) ─────────────────────
        # The thalamus writes a "spotlight" verdict into features before recall
        # runs. When a coalition has IGNITED, the workspace is sustainedly focused
        # on one topic — so bias recall toward it: seed the cue with the focus's
        # hot entities (sustained focus the terse current turn may not name) and,
        # further down, widen the fan-out budget a notch. Absent key (older
        # callers) or not-ignited → strict no-op: `entities` and the budget below
        # stay byte-identical to the un-ignited path.
        spotlight = (features or {}).get("spotlight") or {}
        if spotlight.get("ignited"):
            seen = {e.lower().strip() for e in entities if e}
            hot_extra: list[str] = []
            for e in spotlight.get("hot_entities", []) or []:
                key = str(e).lower().strip()
                if key and key not in seen:
                    seen.add(key)
                    hot_extra.append(e)
            if hot_extra:
                entities = [*entities, *hot_extra]

        # ── Coordinator gate: reuse recent recall if query is near-identical ──
        # The recall_cache_reuse switch encodes "trust the cache" as a fire.
        # High DA lowers its threshold (more reuse under reward); low DA raises
        # it (force fresh recall when nothing's working). Keyed by lane so one
        # end-user's recall is never served from another's cache.
        cache_key = f"{scope_eu or ''}\x00{self._normalize_recall_key(query, entities)}"
        cached = self._recent_recall.get(cache_key)
        if cached is not None and self._recall_cache_reuse.should_fire(0.55, chem, turn_id):
            self._recall_cache_reuse.fire(0.55, "cache_hit", {"key": cache_key[:60]}, snapshot=chem)
            decisions.log(
                "reuse_recent_recall",
                turn_id=turn_id,
                cluster=CLUSTER,
                reason=f"normalized query key '{cache_key[:60]}' matches recent",
                cost_saved_est=0.0,
            )
            trace = self._record_trace()
            if trace is not None:
                trace.predictor_outcomes.append(
                    {
                        "cluster": CLUSTER,
                        "stage": "recall_coordinator",
                        "integrator_woken": False,
                        "reused": True,
                    }
                )
            return cached

        # ── Weighted recall fan-out (Hebbian × chemistry) ────────────────────
        strategy_weights = self._recall_strategy_weights()
        # The fanout switch's effective threshold biases the total budget:
        # under high ACh+Glu (lower threshold), the brain casts a wider net.
        # When the workspace spotlight is ignited, widen it a further notch.
        total_budget = self._fanout_total_budget(chem, spotlight)
        schema_k, episode_k = self._allocate_recall_budget(strategy_weights, total_budget)
        self._recall_fanout.fire(
            min(1.0, total_budget / 8.0), "fanout_budget", {"total": total_budget}, snapshot=chem
        )

        # Schema grep (free, fast). Entity-grep depth is shifted by ACh: under
        # high curiosity the brain scans entities more broadly.
        grep_depth = self._entity_grep_depth(chem, schema_k)
        schema_hits = []
        for entity in entities[:grep_depth]:
            hits = self._schema.grep(entity)
            schema_hits.extend(hits[:2])

        schema_context = "\n".join(f"[{f}] {line}" for f, line in schema_hits[:6])

        # Episodic recall — two parallel searches so deferred questions don't
        # compete with conversation memories for top-k slots:
        #   1. Main search: conversation memories only (excludes deferred_question)
        #   2. Deferred search: deferred_question episodes only, own budget of 2
        episodes = []
        deferred_episodes = []
        if embedding_fn and query:
            try:
                vec = await embedding_fn(query)
                episodes = self._episodic.recall(
                    vec,
                    limit=max(2, episode_k),
                    exclude_tags=["deferred_question"],
                    end_user_id=scope_eu,
                )
                deferred_episodes = self._episodic.recall_by_tag(
                    vec,
                    tag="deferred_question",
                    limit=2,
                    end_user_id=scope_eu,
                )
            except Exception as e:
                logger.warning(
                    "[Memory] Episode search failed — response won't include relevant past memories: %s",
                    e,
                )

        episode_text = ""
        if episodes:
            episode_text = "\n".join(
                f"[{ep.get('ts', 0):.0f}] {ep.get('user_input', '')} → "
                f"{ep.get('entity_response', '')[:200]}"
                for ep in episodes
            )
        if deferred_episodes:
            parts = []
            for ep in deferred_episodes:
                response = ep.get("entity_response", "")
                question_text = response.removeprefix("[PENDING QUESTION] ").strip()
                if question_text:
                    parts.append(f"- {question_text}")
            if parts:
                if episode_text:
                    episode_text += "\n"
                episode_text += (
                    "\nPENDING QUESTIONS (from idle reflection — now relevant):\n"
                    + "\n".join(parts)
                )

        # Compute emotional weight of recalled episodes. Strong positive valence
        # → ACh spike (recognition/warmth); strong negative → GABA spike (threat).
        # Only fires when at least one episode clears the significance threshold.
        recall_affect: dict[str, float] = {}
        if episodes:
            from brain.emotion_hierarchy import valence_of

            _THRESHOLD = 0.4  # |valence| must exceed this to register
            pos_peak = max(
                (valence_of(ep.get("emotion_state")) for ep in episodes),
                default=0.0,
            )
            neg_peak = min(
                (valence_of(ep.get("emotion_state")) for ep in episodes),
                default=0.0,
            )
            if pos_peak > _THRESHOLD:
                recall_affect["ACh"] = round((pos_peak - _THRESHOLD) * 0.25, 3)
            if neg_peak < -_THRESHOLD:
                recall_affect["GABA"] = round((-neg_peak - _THRESHOLD) * 0.20, 3)

        # ── Structural (cross-domain) recall — novelty-gated ─────────────────
        # Fires only when the situation looks novel, i.e. exactly when topic
        # match is least likely to help. Matches on cognitive signature, not
        # content, so it can bridge unrelated domains by problem-shape.
        structural_text = ""
        structural_hits: list[dict] = []
        structural_stance: dict = {}
        structural_summary: dict = {}
        gate_fired = self._structural_gate(novelty, chem, turn_id)
        trace = self._record_trace()
        if trace is not None:
            trace.structural_gate_fired = gate_fired
        if gate_fired:
            self._structural_recall.fire(0.5, "structural_pass", {"novelty": True}, snapshot=chem)
            cur_sig = self._build_cog_signature(
                features or {},
                self._bus.neuromod.snapshot() if hasattr(self._bus, "neuromod") else chem,
                float((features or {}).get("surprise_score", 0.0) or 0.0),
                inhibition=self._current_inhibition(),
            )
            approach_now = [f"approach:{t}" for t in self._infer_current_approach(features or {})]
            candidates = self._episodic.recall_structural(
                cur_sig,
                approach_tags=approach_now,
                limit=self._structural_limit(),
                exclude_session=getattr(self, "_session_id", None),
            )
            structural_hits = [c for c in candidates if c.get("cog_sim", 0.0) >= STRUCTURAL_MIN_SIM]
            best_sim = max((c.get("cog_sim", 0.0) for c in candidates), default=0.0)
            if structural_hits:
                lines = []
                for ep in structural_hits:
                    tags = ", ".join(
                        t.removeprefix("approach:")
                        for t in ep.get("topic_tags", [])
                        if t.startswith("approach:")
                    )
                    topic = ", ".join(
                        t for t in ep.get("topic_tags", []) if not t.startswith("approach:")
                    )[:60]
                    lines.append(
                        f"[approach: {tags or 'unlabeled'}] (was about: {topic or '?'}) "
                        f"{ep.get('user_input', '')[:80]} → {ep.get('entity_response', '')[:160]}"
                    )
                structural_text = "\n".join(lines)
            else:
                # No usable match — derive an honest fallback stance from live
                # state instead of leaning on the most-recent memory.
                anomalous = best_sim < STRUCTURAL_ANOMALY_FLOOR
                structural_stance = self._fallback_stance(chem, anomalous)
            structural_summary = {
                "gate_fired": True,
                "matched": bool(structural_hits),
                "hits": len(structural_hits),
                "best_sim": round(best_sim, 4),
                "approach_overlap": sorted(
                    {
                        t.removeprefix("approach:")
                        for ep in structural_hits
                        for t in ep.get("topic_tags", [])
                        if t.startswith("approach:")
                    }
                ),
                "fallback_stance": structural_stance.get("stance", ""),
            }
            if trace is not None:
                trace.structural_recall = structural_summary
            decisions.log(
                "structural_recall",
                turn_id=turn_id,
                cluster=CLUSTER,
                matched=bool(structural_hits),
                hits=len(structural_hits),
                best_sim=round(best_sim, 4),
                fallback_stance=structural_stance.get("stance", ""),
                approach_overlap=structural_summary["approach_overlap"],
            )

        result = {
            "schema": schema_context,
            "episodes": episode_text,
            "core": self._active_core_context(),
            # Side-granular contribution for the recall fan-out credit pass: how many
            # hits each pathway returned. schema={schema_grep,entity_tracker},
            # episode={cosine_recall,time_filter}. Drives Hebbian credit toward the
            # productive split. Excludes deferred_episodes (own fixed budget, not
            # part of the learned schema/episode allocation).
            "recall_contrib": {
                "schema": len(schema_hits),
                "episode": len(episodes),
                # Structural pathway hit count — feeds the recall fan-out Hebbian
                # credit so the brain learns whether analogical recall helped.
                "structural": len(structural_hits),
                # Budget allocation (pure function of the learned weights, independent
                # of memory content) — lets the recall surface be measured even when
                # the store is empty (the schema-vs-episode split is the learned signal).
                "schema_k": schema_k,
                "episode_k": episode_k,
            },
            **({"structural_episodes": structural_text} if structural_text else {}),
            **({"structural_stance": structural_stance} if structural_stance else {}),
            **({"recall_affect": recall_affect} if recall_affect else {}),
        }

        # Cache for potential reuse next turn
        self._cache_recall(cache_key, result)

        if self._wiring is not None and not self._wiring_frozen:
            decisions.log(
                "weighted_recall_fanout",
                turn_id=turn_id,
                cluster=CLUSTER,
                schema_k=schema_k,
                episode_k=episode_k,
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
            return {
                "cosine_recall": 1.0,
                "schema_grep": 1.0,
                "entity_tracker": 1.0,
                "time_filter": 1.0,
            }
        return {
            "cosine_recall": self._wiring.get_edge_weight(
                "mem.recall", "hippocampus.cosine_recall"
            ),
            "schema_grep": self._wiring.get_edge_weight("mem.recall", "hippocampus.schema_grep"),
            "entity_tracker": self._wiring.get_edge_weight(
                "mem.recall", "hippocampus.entity_tracker"
            ),
            "time_filter": self._wiring.get_edge_weight("mem.recall", "hippocampus.time_filter"),
        }

    def _allocate_recall_budget(
        self, weights: dict[str, float], total_budget: int = 8
    ) -> tuple[int, int]:
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

    def _fanout_total_budget(self, chem: dict[str, float], spotlight: dict | None = None) -> int:
        """Total recall lookups, biased by the recall_fanout switch's modulation
        delta. Base is 8; bounded to [4, 12]. Under high ACh+Glu (lower
        effective threshold), the brain casts a wider net."""
        # modulation_delta is negative when chemistry lowers the threshold;
        # we invert the sign so "lower threshold" corresponds to "more lookups".
        delta = -self._recall_fanout.modulation_delta(chem)
        # delta range under conservative coefficients (≤0.15) is approximately
        # ±0.075. Scale to integer shifts in {-3, …, +3}.
        shift = int(round(delta * 20))
        # N2 (colony-features-ii): when the hippocampus is recruited (a memory-heavy
        # turn won recruitment budget), widen the net further — up to +4 lookups.
        if settings.get("colony_features", 0):
            shift += int(round(self._bus.recruitment_level("hippocampus") * 4))
        # Global-workspace spotlight (locked contract): when a coalition has
        # ignited, the workspace is concentrated on one focus — cast a slightly
        # wider net. Small bounded nudge (0..+2, scaled by integrated
        # concentration 0..10), folded into the SAME [4, 12] clamp. Not ignited /
        # absent (falsy spotlight) → skipped, so the budget is unchanged.
        if spotlight and spotlight.get("ignited"):
            salience = float(spotlight.get("salience", 0.0) or 0.0)
            shift += int(round(min(1.0, max(0.0, salience) / 10.0) * 2))
        return max(4, min(12, 8 + shift))

    def _entity_grep_depth(self, chem: dict[str, float], schema_k: int) -> int:
        """Number of entities to grep against the schema store. Base is
        max(2, schema_k); ACh modulation widens or narrows it by ±1."""
        base = max(2, schema_k)
        delta = -self._entity_grep.modulation_delta(
            chem
        )  # negative coeff lowers thr → more entities
        shift = int(round(delta * 20))
        return max(1, min(8, base + shift))

    def _record_trace(self):
        try:
            from brain.observability.firing_path import current_turn_trace

            return current_turn_trace.get()
        except Exception:
            return None

    # ── Cognitive signature (cross-domain transfer) ──────────────────────────

    @staticmethod
    def _extract_approach_tags(encoded: dict) -> list[str]:
        """Namespaced ``approach:*`` tags from the encoder's strategy_tags. Only
        the canonical vocabulary is accepted, slugged and deduped, so the
        structural surface stays steerable and the tag space doesn't drift."""
        out: list[str] = []
        seen: set[str] = set()
        for raw_tag in encoded.get("strategy_tags", []) or []:
            slug = str(raw_tag).strip().lower().replace(" ", "-").replace("_", "-")
            if slug in APPROACH_TAGS and slug not in seen:
                seen.add(slug)
                out.append(f"approach:{slug}")
        return out

    def _structural_gate(self, novelty: bool, chem: dict[str, float], turn_id: str) -> bool:
        """Whether the structural pass runs: only on novelty, and only if the
        chemistry-modulated switch agrees. Gate firing is reported separately from
        match success so threshold calibration is observable."""
        return bool(novelty) and self._structural_recall.should_fire(0.5, chem, turn_id)

    @staticmethod
    def _structure_flags(features: dict) -> dict[str, float]:
        """Problem-STRUCTURE flags derived only from domain-free signals.
        Never read topic/entity strings here — that would let domain leak into
        the signature and break transfer."""
        intent = features.get("intent", "other")
        requires_action = bool(features.get("requires_action"))
        bool(features.get("requires_memory"))
        epistemic = bool(features.get("epistemic_action")) or intent == "epistemic_action"
        salience = float(features.get("salience", 0.5) or 0.0)
        tone = features.get("user_tone_toward_ai", "neutral")
        return {
            "requires_decomposition": 1.0 if (intent == "task" or requires_action) else 0.0,
            "requires_verification": 1.0
            if (epistemic or intent in ("question", "memory_recall"))
            else 0.0,
            "high_stakes": 1.0 if salience >= 0.7 else 0.0,
            "time_pressure": 1.0 if tone in ("impatient", "hostile") else 0.0,
            "open_ended": 1.0
            if (intent in ("chitchat", "question", "other") and not requires_action)
            else 0.0,
        }

    def _build_cog_signature(
        self,
        features: dict,
        neuromod_snap: dict | None,
        surprise_score: float,
        inhibition: float = 0.0,
    ) -> dict[str, float]:
        """Build the content-free activation signature used for structural recall.
        Same helper at encode-time and query-time so the vectors are comparable."""
        nm = neuromod_snap or {}
        sig: dict[str, float] = {
            "DA": round(float(nm.get("DA", 0.0)), 4),
            "ACh": round(float(nm.get("ACh", 0.0)), 4),
            "GABA": round(float(nm.get("GABA", 0.0)), 4),
            "NE": round(float(nm.get("NE", 0.0)), 4),
            "Glu": round(float(nm.get("Glu", 0.0)), 4),
            "surprise": round(float(surprise_score or 0.0), 4),
            "salience": round(float(features.get("salience", 0.5) or 0.0), 4),
            # suppression_pressure can exceed 1.0; squash into [0, 1].
            "inhibition": round(min(1.0, max(0.0, float(inhibition))), 4),
        }
        sig.update(self._structure_flags(features))
        return sig

    @staticmethod
    def _infer_current_approach(features: dict) -> list[str]:
        """Cheap query-time guess at the approach the current situation calls for,
        from the same domain-free structure flags. Used only to BOOST candidates
        whose stored approach tags overlap — never to filter."""
        flags = HippocampusCluster._structure_flags(features)
        out: list[str] = []
        if flags["requires_decomposition"]:
            out.append("decomposed-into-steps")
        if flags["requires_verification"]:
            out.append("verified-before-acting")
        if flags["time_pressure"]:
            out.append("prioritized-under-time-pressure")
        if flags["open_ended"]:
            out.append("explored-by-trial-and-error")
        return out

    def _current_inhibition(self) -> float:
        trace = self._record_trace()
        if trace is None:
            return 0.0
        return float(getattr(trace, "suppression_pressure", 0.0) or 0.0)

    def _structural_limit(self) -> int:
        """How many structural candidates to surface, scaled by the learned
        mem.recall→hippocampus.structural_recall edge weight (base 3, [1, 5])."""
        if self._wiring is None or self._wiring_frozen:
            return 3
        try:
            w = self._wiring.get_edge_weight("mem.recall", "hippocampus.structural_recall")
        except Exception:
            w = 1.0
        return max(1, min(5, round(3 * w)))

    @staticmethod
    def _fallback_stance(chem: dict[str, float], anomalous: bool) -> dict:
        """When no structural match exists, derive a problem-solving STANCE from
        live channel state rather than defaulting to the most-recent memory.
        Honest about working without prior experience."""
        if anomalous:
            return {
                "stance": "anomalous",
                "note": "No close prior experience, and this cognitive state itself "
                "has no precedent — proceed with care, low-stakes probing, and "
                "minimal assumptions.",
            }
        threat = float(chem.get("GABA", 0.0)) + float(chem.get("CORT", 0.0))
        engage = float(chem.get("DA", 0.0)) + float(chem.get("ACh", 0.0))
        if threat >= engage:
            return {
                "stance": "caution",
                "note": "No close prior experience — fall back to a careful, "
                "tried-and-true approach and verify as you go.",
            }
        return {
            "stance": "exploration",
            "note": "No close prior experience — fall back to exploration: try a "
            "promising approach, iterate by trial and error, keep stakes low.",
        }

    async def encode(
        self,
        session_id: str,
        turn_id: str,
        user_input: str,
        entity_response: str,
        features: dict,
        affect: dict,
        neuromod_snap: dict,
        surprise_score: float,
        embedding_fn=None,
    ) -> None:
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
            self._encoder_gate.fire(
                0.55,
                "encoder_skipped",
                {
                    "surprise": round(surprise_score, 3),
                    "salience": round(salience, 3),
                    "DA": round(da_now, 3),
                },
                snapshot=chem,
            )
            decisions.log(
                "skip_encoder",
                turn_id=turn_id,
                cluster=CLUSTER,
                reason=f"surprise={surprise_score:.2f} salience={salience:.2f} DA={da_now:.2f}",
                cost_saved_est=0.0005,
            )
            trace = self._record_trace()
            if trace is not None:
                trace.llm_calls_saved += 1
            encoded = {
                "topic_tags": [features.get("topic_summary", "low_salience")],
                "entities": [],
                "key_facts": [],
                "relationship_note": "",
                "summary": user_input[:120],
            }
            # Skip the LLM call but proceed to embed + store raw episode
            return await self._store_episode(
                session_id,
                turn_id,
                user_input,
                entity_response,
                features,
                affect,
                neuromod_snap,
                surprise_score,
                encoded,
                embedding_fn,
            )

        self._encoder.reset_turn(turn_id)
        messages = [
            {
                "role": "user",
                "content": f"User: {user_input}\nBrain: {entity_response}\n"
                f"Context: intent={intent}, emotion={affect.get('emotion', 'neutral')}",
            }
        ]
        raw = await self._encoder.call(messages)

        encoded = safe_json_parse(raw) or {}

        await self._store_episode(
            session_id,
            turn_id,
            user_input,
            entity_response,
            features,
            affect,
            neuromod_snap,
            surprise_score,
            encoded,
            embedding_fn,
        )

    async def encode_idle_thought(
        self,
        session_id: str,
        thought: str,
        overlap_with_user_input: float,
        user_input: str = "",
        embedding_fn=None,
    ) -> None:
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
            logger.info(
                "[Memory] Encoded idle thought as episode (overlap %.2f with user): %r",
                overlap_with_user_input,
                thought[:80],
            )
        except Exception as e:
            logger.warning("[Memory] Idle-thought encoding failed: %s", e)

    async def encode_deferred_question(
        self,
        session_id: str,
        text: str,
        urgency: str = "high",
        tags: list[str] | None = None,
        embedding_fn=None,
    ) -> None:
        """Encode a deferred question or idle thought into episodic memory so
        it can surface naturally when a relevant topic arises later.

        All urgency levels are stored here. Immediate/high urgency entries are
        ALSO written to deferred_thoughts.md (handled by the DMN caller) for
        explicit surfacing on user return. Normal/low urgency entries rely
        entirely on vector search to resurface — the brain "remembers" the
        question when the topic comes up again, the way a person might.

        Stored as an episode with topic_tags=["deferred_question", urgency, ...tags]
        and entity_response prefixed with "[PENDING QUESTION]" so the recall
        pipeline can present them distinctly from conversation memories.
        """
        if not text.strip():
            return
        # Map urgency to surprise_score so high-priority questions get stronger
        # memory signal and surface more readily in vector recall.
        _urgency_surprise = {"immediate": 0.8, "high": 0.6, "normal": 0.4, "low": 0.2}
        surprise = _urgency_surprise.get(urgency, 0.5)
        topic_tags = ["deferred_question", urgency] + [t for t in (tags or []) if t]
        try:
            vec = None
            if embedding_fn:
                try:
                    vec = await embedding_fn(text)
                except Exception as e:
                    logger.debug("[Memory] Deferred-question embed failed: %s", e)
            turn_id = f"defer_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            episode = Episode(
                session_id=session_id,
                turn_id=turn_id,
                ts=time.time(),
                user_input="(idle — deferred question)",
                entity_response=f"[PENDING QUESTION] {text}",
                topic_tags=topic_tags,
                emotion_state="curious",
                user_emotion="unknown",
                entities=[],
                neuromod_snapshot={"DA": 0.5, "GABA": 0.1, "ACh": 0.4, "Glu": 0.3},
                surprise_score=surprise,
                vector=vec,
            )
            self._episodic.encode(episode)
            logger.info(
                "[Memory] Deferred question encoded (urgency=%s, tags=%s): %r",
                urgency,
                tags,
                text[:80],
            )
        except Exception as e:
            logger.warning("[Memory] Deferred-question encoding failed: %s", e)

    async def encode_conclusion(
        self,
        session_id: str,
        text: str,
        source: str = "dmn",
        tags: list[str] | None = None,
        embedding_fn=None,
    ) -> None:
        """Encode a settled conclusion ("something I now know / have figured out")
        into episodic memory so it resurfaces through normal vector recall instead
        of being re-derived. Fed brain-wide: DMN-concluded threads (source="dmn"),
        sleep insights ("sleep"), successful jobs ("job"), notable turn learnings
        ("turn"), and user-confirmed conclusions ("confirmed").

        Stored as an episode tagged ["conclusion","knowledge",source,*tags] with
        entity_response prefixed "[CONCLUDED]" and a high surprise_score so the
        recall pipeline surfaces it readily and can present it distinctly.
        """
        if not text.strip():
            return
        topic_tags = ["conclusion", "knowledge", source] + [t for t in (tags or []) if t]
        try:
            vec = None
            if embedding_fn:
                try:
                    vec = await embedding_fn(text)
                except Exception as e:
                    logger.debug("[Memory] Conclusion embed failed: %s", e)
            turn_id = f"concl_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            episode = Episode(
                session_id=session_id,
                turn_id=turn_id,
                ts=time.time(),
                user_input="(idle — concluded)",
                entity_response=f"[CONCLUDED] {text}",
                topic_tags=topic_tags,
                emotion_state="satisfied",
                user_emotion="unknown",
                entities=[],
                neuromod_snapshot={"DA": 0.6, "GABA": 0.1, "ACh": 0.4, "Glu": 0.3},
                surprise_score=0.7,
                vector=vec,
            )
            self._episodic.encode(episode)
            logger.info(
                "[Memory] Conclusion encoded (source=%s, tags=%s): %r",
                source,
                tags,
                text[:80],
            )
        except Exception as e:
            logger.warning("[Memory] Conclusion encoding failed: %s", e)

    async def _store_episode(
        self,
        session_id: str,
        turn_id: str,
        user_input: str,
        entity_response: str,
        features: dict,
        affect: dict,
        neuromod_snap: dict,
        surprise_score: float,
        encoded: dict,
        embedding_fn,
    ) -> None:
        topic_tags = (
            encoded.get("topic_tags")
            or features.get("entities", [])
            or [features.get("topic_summary", "misc")]
        )
        # Append problem-solving "approach" tags (namespaced) from the encoder.
        topic_tags.extend(self._extract_approach_tags(encoded))
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
        await self._maybe_promote_familiarity(schema_file)

        # Build embedding vector
        vec = None
        if embedding_fn:
            try:
                combined = f"{user_input} {entity_response}"
                vec = await embedding_fn(combined)
            except Exception as e:
                logger.warning(
                    "[Memory] Could not generate embedding vector — episode will be stored without search index (won't appear in future recall): %s",
                    e,
                )

        cog_signature = self._build_cog_signature(
            features,
            neuromod_snap,
            surprise_score,
            inhibition=self._current_inhibition(),
        )

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
            cog_signature=cog_signature,
            end_user_id=str(features.get("end_user_id") or ""),
            mandate_id=str(features.get("mandate_id") or ""),
        )
        self._episodic.encode(episode)
        logger.debug("[Memory] Episode saved: turn=%s intent=%s", turn_id, intent)

    # Affection score deltas per tone (clamped to -50..+100)
    _AFFECTION_DELTAS: dict[str, int] = {
        "praising": +3,
        "warm": +2,
        "joking": +2,
        "polite": +1,
        "neutral": 0,
        "testing": -1,
        "impatient": -2,
        "dismissive": -3,
        "insulting": -5,
    }

    _AFFECTION_TIERS = [
        (40, "close friends — tease freely, very warm, in-jokes welcome"),
        (20, "warm friends — relaxed, light teasing okay, personal"),
        (5, "friendly — warm and engaged, hold the teasing"),
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
            bond, _bond_seeded = _parse_bond(content)

            # ── Reunion recovery boost (bond model) ───────────────────────────
            # A positive delta from a former-close friend whose affection decayed
            # during an absence recovers fast: scale the delta by how far below
            # the latent bond the live affection sits. Negative deltas unaffected.
            boost = 1.0
            if settings.get("enable_bond_model") and delta > 0 and bond > current:
                from brain.relationship import reunion_boost

                boost = reunion_boost(
                    float(current), bond, float(settings.get("bond_reunion_gain"))
                )
            effective_delta = delta * boost
            new_score = max(-50, min(100, round(current + effective_delta)))

            # ── Bond high-water mark ──────────────────────────────────────────
            new_bond = max(bond, float(new_score)) if settings.get("enable_bond_model") else bond

            tier_label = self._AFFECTION_TIERS[-1][1]
            for threshold, label in self._AFFECTION_TIERS:
                if new_score >= threshold:
                    tier_label = label
                    break
            if m:
                content = content[: m.start()] + f"- Score: {new_score}" + content[m.end() :]
            else:
                content += f"\n- Score: {new_score}"
            # Persist bond (replace or append)
            if settings.get("enable_bond_model"):
                content = _write_bond(content, new_bond)
            hist_line = f"- History: {tier_label} (last tone: {tone}, delta: {delta:+d})"
            content = re.sub(r"- History:.*", hist_line, content)
            if "- History:" not in content:
                content += f"\n{hist_line}"

            self._schema.write(
                schema_file, content
            )  # lock-free: caller already holds self._lock (awrite would deadlock)
            # Record the boost on the trace for instrumentation (P5)
            if boost != 1.0:
                try:
                    from brain.observability.firing_path import get_current_trace

                    _tr = get_current_trace()
                    if _tr is not None:
                        _tr.reunion_boost_applied = round(boost, 3)
                        _tr.bond = round(new_bond, 1)
                except Exception:
                    pass
            logger.debug(
                "[Memory] Affection [%s]: %d→%d (%s, tone=%s, bond=%.1f, boost=%.2f)",
                schema_file,
                current,
                new_score,
                tier_label,
                tone,
                new_bond,
                boost,
            )

    # Legacy interaction-count tiers — retained as a FALLBACK only when the bond
    # model is disabled. With the bond model on, familiarity is a pure function
    # of bond (see brain/relationship.familiarity_from_bond).
    _FAMILIARITY_TIERS = [
        (30, "close"),
        (8, "acquainted"),
        (0, "new"),
    ]

    async def _maybe_promote_familiarity(self, schema_file: str) -> None:
        """Increment the per-speaker interaction count and set the familiarity
        tier. With the bond model on (default), the tier is derived from the
        latent bond (history depth), so a fight that drops affection does not
        erase familiarity — only a long absence that decays the bond does.

        Fixes the accretion bug (F9): the rewrite now matches the ENTIRE
        `- Familiarity:` line (including any trailing `(interactions: N)` suffix)
        and replaces it, instead of prepending a fresh suffix each session."""
        import re

        from brain.relationship import TIER_ORDER

        async with self._schema._lock:
            content = self._schema.read(schema_file)
            # Read or initialise interaction count
            m_count = re.search(r"- Interactions:\s*(\d+)", content)
            count = int(m_count.group(1)) + 1 if m_count else 1

            if settings.get("enable_bond_model"):
                from brain.relationship import familiarity_from_bond

                # A pre-bond-model file has no `- Bond:` line; _parse_bond
                # reconstructs it from the file's own history. Persist the
                # reconstruction here so it happens exactly once — this runs on
                # EVERY turn, whereas _update_affection_score returns early on a
                # neutral tone (delta 0) and would leave the field absent.
                bond, bond_seeded = _parse_bond(content)
                if bond_seeded:
                    content = _write_bond(content, bond)
                new_tier = familiarity_from_bond(
                    bond,
                    float(settings.get("familiarity_close_bond")),
                    float(settings.get("familiarity_acquainted_bond")),
                )
            else:
                # Legacy count-based tiers
                new_tier = "new"
                for threshold, label in self._FAMILIARITY_TIERS:
                    if count >= threshold:
                        new_tier = label
                        break

            # ── Never downgrade on a turn the user is present for ─────────────
            # Familiarity is history depth: talking to someone cannot make you
            # know them LESS. Only measured absence may lower a tier, and that is
            # applied exclusively by apply_relationship_decay_at_boot, which
            # writes the tier itself and is deliberately NOT guarded — so this
            # ratchet cannot block genuine decay, it only stops a turn from
            # silently erasing a relationship (e.g. a bond that reads low because
            # the field is missing or has not been seeded yet).
            m_fam0 = re.search(r"- Familiarity:\s*(\w+)", content)
            current_tier = m_fam0.group(1).lower() if m_fam0 else "new"
            if TIER_ORDER.get(new_tier, 0) < TIER_ORDER.get(current_tier, 0):
                new_tier = current_tier

            # Write interaction count (whole-line replace)
            count_line = f"- Interactions: {count}"
            if m_count:
                content = re.sub(r"- Interactions:[^\n]*", count_line, content, count=1)
            else:
                content += f"\n{count_line}"
            # Write familiarity tier — match the ENTIRE line to stop suffix accretion
            fam_line = f"- Familiarity: {new_tier} (interactions: {count})"
            if re.search(r"- Familiarity:[^\n]*", content):
                content = re.sub(r"- Familiarity:[^\n]*", fam_line, content, count=1)
            else:
                content += f"\n{fam_line}"

            self._schema.write(
                schema_file, content
            )  # lock-free: caller already holds self._lock (awrite would deadlock)

    async def apply_relationship_decay_at_boot(self) -> None:
        """Apply the bond model's absence decay once at session boot.

        Reads each speaker schema's `Last seen`, computes the elapsed gap, and
        decays affection (fast, bond-protected) and bond (slow), then sets
        familiarity from the decayed bond. Does NOT refresh `Last seen` — that
        is stamped at consolidation (session end), so the gap measured here is
        the true inter-session absence. Talking during the session then recovers
        affection fast via the reunion boost.
        """
        if not settings.get("enable_bond_model"):
            return
        import re

        from brain.relationship import apply_absence, familiarity_from_bond

        now = time.time()
        aff_base = float(settings.get("bond_aff_halflife_base_days"))
        bond_base = float(settings.get("bond_bond_halflife_base_days"))
        scale = float(settings.get("bond_halflife_scale"))
        close_bond = float(settings.get("familiarity_close_bond"))
        acq_bond = float(settings.get("familiarity_acquainted_bond"))

        # Speaker schemas are user.md plus user_*.md (per-speaker profiles)
        try:
            files = [
                f for f in self._schema.list_files() if f == "user.md" or f.startswith("user_")
            ]
        except Exception:
            files = ["user.md"]

        for schema_file in files:
            try:
                async with self._schema._lock:
                    content = self._schema.read(schema_file)
                    if not content or "- Score:" not in content:
                        continue
                    m_seen = re.search(r"- Last seen:\s*(\d+(?:\.\d+)?)", content)
                    if not m_seen:
                        # First boot with the bond model: stamp now, nothing to
                        # decay yet (no record of when we last spoke, so no gap
                        # can be computed — the absence is forgiven, not guessed).
                        # Seed Bond in the same pass: this branch `continue`d
                        # before the Bond write below, so a pre-bond-model file
                        # left boot still missing the field.
                        content += f"\n- Last seen: {now:.0f}"
                        seed, seeded = _parse_bond(content)
                        if seeded:
                            content = _write_bond(content, seed)
                            logger.info(
                                "[Relationship] Migrated %s to the bond model: "
                                "seeded bond %.1f from legacy history (familiarity=%s)",
                                schema_file,
                                seed,
                                familiarity_from_bond(seed, close_bond, acq_bond),
                            )
                        self._schema.write(
                            schema_file, content
                        )  # lock-free: caller already holds self._lock (awrite would deadlock)
                        continue
                    last_seen = float(m_seen.group(1))
                    elapsed_days = max(0.0, (now - last_seen) / 86400.0)
                    if elapsed_days < 0.04:  # < ~1 hour — same-session reboot, skip
                        continue

                    m_aff = re.search(r"- Score:\s*(-?\d+)", content)
                    affection = float(m_aff.group(1)) if m_aff else 0.0
                    bond, _seeded = _parse_bond(content)

                    new_aff, new_bond = apply_absence(
                        affection,
                        bond,
                        elapsed_days,
                        aff_base=aff_base,
                        bond_base=bond_base,
                        scale=scale,
                    )
                    new_tier = familiarity_from_bond(new_bond, close_bond, acq_bond)

                    content = re.sub(
                        r"- Score:\s*-?\d+", f"- Score: {round(new_aff)}", content, count=1
                    )
                    content = _write_bond(content, new_bond)
                    count_m = re.search(r"- Interactions:\s*(\d+)", content)
                    count = int(count_m.group(1)) if count_m else 0
                    fam_line = f"- Familiarity: {new_tier} (interactions: {count})"
                    if re.search(r"- Familiarity:[^\n]*", content):
                        content = re.sub(r"- Familiarity:[^\n]*", fam_line, content, count=1)
                    else:
                        content += f"\n{fam_line}"

                    self._schema.write(
                        schema_file, content
                    )  # lock-free: caller already holds self._lock (awrite would deadlock)
                    logger.info(
                        "[Relationship] Boot decay %s: %.1f d → affection %.0f→%.0f, "
                        "bond %.1f→%.1f, familiarity=%s",
                        schema_file,
                        elapsed_days,
                        affection,
                        new_aff,
                        bond,
                        new_bond,
                        new_tier,
                    )
            except Exception as exc:
                logger.warning("[Relationship] Boot decay failed for %s: %s", schema_file, exc)

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
