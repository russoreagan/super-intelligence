"""
SkillSelector — picks the right thinking/EI framework from the 171 skills
imported by brain/skills/_import_humanity.py.

Two modes:

  select_conversational(...) — Haiku path, gated by turn type / user emotion.
    Embedding shortlist + LLM pick. Sticky across turns via ActiveSkillContext.
    Emits a "needs_guided_question" flag when the picked router has multiple
    leaves scoring closely (genuine ambiguity).

  select_autonomous(...) — Ollama path, used by DMN cells that aren't bound
    by user response pressure. More aggressive candidate pool; biased toward
    picking something.

  ruminate(...) — Open-ended reflection loop. Each step a meta-cell picks
    the next move (skill + which prior thought + mode: transform/branch/
    reframe/stop). Used by the DMN planner and monologue.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brain.cell import IntegratorCell
from brain.model_router import ModelRouter

logger = logging.getLogger(__name__)

INDEX_PATH = Path(__file__).resolve().parents[1] / "skills" / "_humanity_index.json"
TIERS_PATH = Path(__file__).resolve().parents[1] / "skills" / "_humanity_tiers.json"

# Thresholds — tunable per session-log analysis.
TIER_3_MIN_SCORE = 0.55  # Tier-3 must clear this cosine to enter the pool
CLEAR_WINNER_MARGIN = 0.15  # Top-1 vs top-2; if exceeded, skip LLM
GUIDED_QUESTION_AMBIGUITY = 0.08  # Margin within active router for "ambiguous" question
TOPIC_DRIFT_THRESHOLD = 0.4  # Cosine below this clears active context
STICKY_TURN_BUDGET = 8  # Turns before soft-decay of active context

CONVERSATIONAL_TOP_K = 5
AUTONOMOUS_TOP_K = 10


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass
class ActiveSkillContext:
    category: str
    current_leaf: str | None = None
    secondary_categories: list[str] = field(default_factory=list)
    anchor_topic_embedding: list[float] = field(default_factory=list)
    turns_active: int = 0
    awaiting_user_direction: bool = False
    background_candidates: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class SkillBundle:
    tier1: list[str]
    chosen: list[str]
    needs_guided_question: bool = False
    pick_source: str = ""  # "active_reuse" | "embed_winner" | "llm_pick" | "llm_null"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class _Index:
    """In-memory wrapper around _humanity_index.json with cosine search."""

    def __init__(self, path: Path = INDEX_PATH, tiers_path: Path = TIERS_PATH):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing — run `python -m brain.skills._import_humanity` first."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.skills: list[dict] = data["skills"]
        self._by_name: dict[str, dict] = {s["name"]: s for s in self.skills}

        tiers = json.loads(tiers_path.read_text(encoding="utf-8")) if tiers_path.exists() else {}
        self.tier1_names: list[str] = tiers.get("tier_1", [])

    def get(self, name: str) -> dict | None:
        return self._by_name.get(name)

    def leaves_in_category(self, cat: str) -> list[dict]:
        return [s for s in self.skills if s["category"] == cat and not s["is_router"]]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        num = sum(x * y for x, y in zip(a, b, strict=False))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return num / (na * nb)

    def rank(
        self,
        query_vec: list[float],
        *,
        include_tier_1: bool = False,
        tier_3_floor: float = TIER_3_MIN_SCORE,
        only_category: str | None = None,
        only_leaves: bool = False,
    ) -> list[tuple[dict, float]]:
        """Cosine-rank skills. Returns (skill_entry, score) pairs sorted descending."""
        scored: list[tuple[dict, float]] = []
        for s in self.skills:
            if not include_tier_1 and s["tier"] == 1:
                continue
            if only_leaves and s["is_router"]:
                continue
            if only_category and s["category"] != only_category:
                continue
            score = self.cosine(query_vec, s["embedding"])
            if s["tier"] == 3 and score < tier_3_floor:
                continue
            scored.append((s, score))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

_CONVERSATIONAL_SYSTEM = """You are a skill router for a conversational AI brain. Given a candidate list of reasoning/emotional frameworks and the current turn's classification, pick the single best fit OR decline.

Output ONLY valid JSON: {"skill": "<name>" | null, "why": "<≤15 words>"}

Strong bias toward null. Pick a skill only when applying it would meaningfully improve the response (not just sound clever). For casual or simple turns, return null."""

_AUTONOMOUS_SYSTEM = """You are a skill router for an AI thinking on its own. Given a list of reasoning frameworks and the thought to explore, pick 1 (optionally 2) frameworks that would produce the most insight.

Output ONLY valid JSON: {"primary": "<name>", "secondary": "<name>" | null, "why": "<≤20 words>"}

Default to picking something — this is autonomous deliberation, not user response. Only return primary=null if no framework is remotely applicable."""

_META_RUMINATE_SYSTEM = """You are guiding internal reflection. The agent is mulling a thought, applying different frameworks. Each iteration, decide the next move:

- "transform": apply a new skill to the latest thought (refine it).
- "branch": apply a new skill to an EARLIER thought in the chain.
- "reframe": pick a skill specifically designed to challenge or invert the current take.
- "stop": the thought has stabilized OR exploration is complete.

Output ONLY valid JSON: {"mode": "...", "skill": "<name>" | null, "base_idx": <int>, "why": "<≤15 words>"}

base_idx must be a valid index into the existing chain (0 = seed). On "stop", skill may be null."""


class SkillSelector:
    def __init__(self, router: ModelRouter):
        self._router = router
        self._index = _Index()

        # Lightweight Haiku selector for the conversational path
        self._conversational_cell = IntegratorCell(
            name="skill_selector_conversational",
            cluster="frontal",
            model="haiku",
            system_prompt=_CONVERSATIONAL_SYSTEM,
            topics=[],
            max_tokens=80,
            timeout_seconds=8.0,
            sensitivity="normal",
        )
        self._conversational_cell.set_router(router)

        # Ollama selector for autonomous one-shot picks
        self._autonomous_cell = IntegratorCell(
            name="skill_selector_autonomous",
            cluster="dmn",
            model="local",
            system_prompt=_AUTONOMOUS_SYSTEM,
            topics=[],
            max_tokens=120,
            timeout_seconds=15.0,
            locality="local",
        )
        self._autonomous_cell.set_router(router)

        # Ollama meta-decider for rumination
        self._meta_cell = IntegratorCell(
            name="skill_ruminate_meta",
            cluster="dmn",
            model="local",
            system_prompt=_META_RUMINATE_SYSTEM,
            topics=[],
            max_tokens=120,
            timeout_seconds=15.0,
            locality="local",
        )
        self._meta_cell.set_router(router)

        # Reset per-call counters every use; we use the cells in non-turn-bound mode.
        for c in (self._conversational_cell, self._autonomous_cell, self._meta_cell):
            c.max_calls_per_turn = 999

    # ----- public API --------------------------------------------------

    @property
    def tier1_names(self) -> list[str]:
        return list(self._index.tier1_names)

    def get_skill(self, name: str) -> dict | None:
        return self._index.get(name)

    def gate_conversational(
        self,
        *,
        response_type: str = "",
        user_emotion: str = "",
    ) -> bool:
        """Return True if this turn qualifies for skill injection."""
        if response_type in {"informative", "task", "defuse", "introspective", "recall"}:
            return True
        return user_emotion in {
            "distressed",
            "sad",
            "frustrated",
            "anxious",
            "angry",
            "overwhelmed",
            "upset",
            "hostile",
        }

    async def select_conversational(
        self,
        user_input: str,
        executive_out: dict,
        user_emotion: str,
        recent_turns: list[str],
        active: ActiveSkillContext | None,
        *,
        turn_id: str = "",
    ) -> tuple[SkillBundle | None, ActiveSkillContext | None, dict]:
        """
        Returns (bundle, updated_active_context, log_extras).
        bundle is None when the turn is gated out (no skill block injected at all).
        """
        log_extras: dict[str, Any] = {}
        response_type = executive_out.get("response_type", "")
        key_points = executive_out.get("key_points", [])

        if not self.gate_conversational(
            response_type=response_type,
            user_emotion=user_emotion,
        ):
            log_extras["gated"] = True
            return None, active, log_extras

        # 1. Embed the current query
        query_text = user_input
        if key_points:
            query_text += "  " + " ".join(str(k) for k in key_points)
        query_vec = await self._router.embed(query_text)
        if query_vec is None:
            logger.debug("Skill selector: embedding unavailable, skipping")
            log_extras["embed_failed"] = True
            return None, active, log_extras

        # 2. Sticky-context check: reuse or drift?
        if active is not None:
            drift = self._index.cosine(query_vec, active.anchor_topic_embedding)
            log_extras["sticky_drift_score"] = drift
            if drift < TOPIC_DRIFT_THRESHOLD or active.turns_active >= STICKY_TURN_BUDGET:
                log_extras["sticky_action"] = "drifted_or_expired"
                active = None
            else:
                # Reuse — return the locked leaf (or category overview if leaf not yet picked)
                active.turns_active += 1
                chosen = [active.current_leaf] if active.current_leaf else [active.category]
                bundle = SkillBundle(
                    tier1=self.tier1_names,
                    chosen=chosen,
                    needs_guided_question=False,
                    pick_source="active_reuse",
                )
                log_extras["sticky_action"] = "reused"
                log_extras["active_category"] = active.category
                log_extras["active_leaf"] = active.current_leaf
                return bundle, active, log_extras

        # 3. Rank candidates (Tier 2 + qualifying Tier 3, leaves and routers)
        ranked = self._index.rank(query_vec, include_tier_1=False)
        top = ranked[:CONVERSATIONAL_TOP_K]
        if not top:
            log_extras["no_candidates"] = True
            return (
                SkillBundle(tier1=self.tier1_names, chosen=[], pick_source="no_candidates"),
                None,
                log_extras,
            )

        log_extras["candidates"] = [(s["name"], round(score, 3)) for s, score in top]

        # 4. Clear winner skip
        top1_score = top[0][1]
        top2_score = top[1][1] if len(top) > 1 else 0.0
        if top1_score - top2_score >= CLEAR_WINNER_MARGIN:
            picked = top[0][0]
            log_extras["pick_path"] = "embed_winner"
            return (
                self._build_bundle_from_pick(
                    picked,
                    query_vec,
                    active=None,
                    top_results=top,
                ),
                None,
                log_extras,
            )

        # 5. LLM pick
        prompt_candidates = "\n".join(f"- {s['name']}: {s['description'][:200]}" for s, _ in top)
        context_block = (
            f"Turn type: {response_type}\n"
            f"Tone: {executive_out.get('tone', '')}\n"
            f"User emotion: {user_emotion or 'none'}\n"
            f"User input: {user_input[:400]}\n"
            f"Recent turns:\n" + "\n".join(recent_turns[-2:])
        )
        msg = [{"role": "user", "content": f"{context_block}\n\nCandidates:\n{prompt_candidates}"}]
        self._conversational_cell.reset_turn(turn_id)
        raw = await self._conversational_cell.call(msg)
        log_extras["llm_raw"] = raw[:200]

        pick = self._parse_json_field(raw, "skill")
        if not pick or pick not in self._index._by_name:
            log_extras["pick_path"] = "llm_null"
            return (
                SkillBundle(tier1=self.tier1_names, chosen=[], pick_source="llm_null"),
                None,
                log_extras,
            )

        picked = self._index.get(pick)
        log_extras["pick_path"] = "llm_pick"
        return (
            self._build_bundle_from_pick(
                picked,
                query_vec,
                active=None,
                top_results=top,
            ),
            None,
            log_extras,
        )

    def _build_bundle_from_pick(
        self,
        picked: dict,
        query_vec: list[float],
        *,
        active: ActiveSkillContext | None,
        top_results: list[tuple[dict, float]],
    ) -> SkillBundle:
        """Construct a SkillBundle from a chosen skill, deciding if guided-question is needed."""
        needs_question = False
        chosen_names = [picked["name"]]

        if picked["is_router"]:
            # Check ambiguity among leaves under this router
            leaves = self._index.leaves_in_category(picked["category"])
            if leaves:
                leaf_scores = sorted(
                    (
                        (leaf["name"], self._index.cosine(query_vec, leaf["embedding"]))
                        for leaf in leaves
                    ),
                    key=lambda p: p[1],
                    reverse=True,
                )
                if len(leaf_scores) >= 2:
                    top1, top2 = leaf_scores[0][1], leaf_scores[1][1]
                    if top1 - top2 < GUIDED_QUESTION_AMBIGUITY:
                        needs_question = True
                    else:
                        # Clear leaf winner — lock it silently
                        chosen_names = [leaf_scores[0][0]]

        return SkillBundle(
            tier1=self.tier1_names,
            chosen=chosen_names,
            needs_guided_question=needs_question,
            pick_source="embed_winner" if not picked["is_router"] else "llm_pick",
        )

    def build_active_context(
        self,
        bundle: SkillBundle,
        query_vec: list[float],
    ) -> ActiveSkillContext | None:
        """Construct the ActiveSkillContext to store on parietal after a pick."""
        if not bundle.chosen:
            return None
        primary = self._index.get(bundle.chosen[0])
        if primary is None:
            return None
        leaf = bundle.chosen[0] if not primary["is_router"] else None
        return ActiveSkillContext(
            category=primary["category"],
            current_leaf=leaf,
            anchor_topic_embedding=list(query_vec),
            turns_active=1,
            awaiting_user_direction=bundle.needs_guided_question,
        )

    async def lock_leaf_from_reply(
        self,
        user_reply: str,
        active: ActiveSkillContext,
    ) -> ActiveSkillContext:
        """After a guided question, lock the leaf the user's reply most closely matches
        under the active router's category. Embedding-only (no LLM)."""
        if not active.awaiting_user_direction:
            return active
        query_vec = await self._router.embed(user_reply)
        if query_vec is None:
            return active
        ranked = self._index.rank(
            query_vec,
            include_tier_1=True,
            tier_3_floor=0.0,
            only_category=active.category,
            only_leaves=True,
        )
        if ranked:
            active.current_leaf = ranked[0][0]["name"]
            active.awaiting_user_direction = False
            active.turns_active += 1
        return active

    async def background_explore(
        self,
        active: ActiveSkillContext,
        recent_turn_text: str,
    ) -> list[tuple[str, float]]:
        """Free Ollama-side rescan over ALL leaves to surface alternatives to the active leaf.
        Result written to active.background_candidates and consulted on next turn."""
        query_vec = await self._router.embed(recent_turn_text)
        if query_vec is None:
            return []
        ranked = self._index.rank(
            query_vec, include_tier_1=False, tier_3_floor=0.0, only_leaves=True
        )
        active.background_candidates = [(s["name"], round(score, 3)) for s, score in ranked[:5]]
        return active.background_candidates

    async def select_autonomous(
        self,
        prompt: str,
        recent_thoughts: list[str] | None = None,
        active: ActiveSkillContext | None = None,
        *,
        turn_id: str = "",
        top_k: int = AUTONOMOUS_TOP_K,
    ) -> SkillBundle | None:
        """One-shot autonomous pick for DMN cells without rumination."""
        query_vec = await self._router.embed(prompt)
        if query_vec is None:
            return SkillBundle(tier1=self.tier1_names, chosen=[], pick_source="embed_failed")

        ranked = self._index.rank(query_vec, include_tier_1=False)
        top = ranked[:top_k]
        if not top:
            return SkillBundle(tier1=self.tier1_names, chosen=[], pick_source="no_candidates")

        prompt_candidates = "\n".join(f"- {s['name']}: {s['description'][:200]}" for s, _ in top)
        msg_text = f"Thought:\n{prompt[:500]}\n\nCandidates:\n{prompt_candidates}"
        self._autonomous_cell.reset_turn(turn_id)
        raw = await self._autonomous_cell.call([{"role": "user", "content": msg_text}])
        primary = self._parse_json_field(raw, "primary")
        secondary = self._parse_json_field(raw, "secondary")
        chosen = [p for p in (primary, secondary) if p and p in self._index._by_name]
        if not chosen:
            # Fall back to embedding winner
            chosen = [top[0][0]["name"]]
        return SkillBundle(tier1=self.tier1_names, chosen=chosen, pick_source="autonomous")

    async def ruminate(
        self,
        seed_thought: str,
        *,
        max_iters: int = 6,
        time_budget_s: int = 30,
        turn_id: str = "",
        flavor: str = "engaged",
    ) -> tuple[str, list[dict]]:
        """Open-ended reflection loop.

        Returns (final_take, chain).
        chain[i] = {"thought", "skill", "parent", "mode"} where parent is the index
        of the thought operated on, or None for the seed.

        flavor biases skill/mode selection: "anxious" leans on resolution/closure
        frameworks + reframe (brooding that tries to settle a worry); "engaged"
        leans on generative frameworks + branch/transform (curious deepening).
        """
        chain: list[dict] = [
            {
                "thought": seed_thought,
                "skill": None,
                "parent": None,
                "mode": "seed",
            }
        ]
        started = time.time()

        for _step in range(max_iters):
            if time.time() - started > time_budget_s:
                break
            decision = await self._meta_decide(chain, turn_id=turn_id, flavor=flavor)
            if decision.get("mode") == "stop":
                break
            skill = decision.get("skill")
            base_idx = max(0, min(int(decision.get("base_idx", len(chain) - 1)), len(chain) - 1))
            if not skill or skill not in self._index._by_name:
                # Fall back: pick a flavor-appropriate skill so the loop progresses
                skill = self._fallback_skill(flavor)
            if not skill:
                break

            base_thought = chain[base_idx]["thought"]
            new_thought = await self._apply_skill(
                skill, base_thought, decision.get("mode", "transform"), turn_id
            )
            chain.append(
                {
                    "thought": new_thought,
                    "skill": skill,
                    "parent": base_idx,
                    "mode": decision.get("mode", "transform"),
                }
            )

        final = await self._synthesize_chain(chain, turn_id=turn_id)
        return final, chain

    # ----- internals --------------------------------------------------

    # Flavor → preferred skill pools / modes for rumination.
    _ANXIOUS_SKILLS = (
        "decision-premortem-analysis",
        "constraint-scope-reduction",
        "logic-consistency-check",
        "emotional-resistance-diagnosis",
        "decision-reversibility-analysis",
        "logic-check",
    )
    _ENGAGED_SKILLS = (
        "creativity-lateral-thinking",
        "analogy-domain-transfer",
        "systems-feedback-mapping",
        "creativity-concept-fan",
        "analogy-perspective-shifting",
        "systems-leverage-analysis",
    )

    def _fallback_skill(self, flavor: str) -> str | None:
        """Pick a flavor-appropriate skill when the meta-cell names none, so the
        rumination loop still progresses in the right register."""
        pool = self._ANXIOUS_SKILLS if flavor == "anxious" else self._ENGAGED_SKILLS
        available = [n for n in pool if n in self._index._by_name]
        if available:
            return random.choice(available)
        tier2 = [s for s in self._index.skills if s["tier"] == 2 and not s["is_router"]]
        return random.choice(tier2)["name"] if tier2 else None

    async def _meta_decide(
        self, chain: list[dict], *, turn_id: str = "", flavor: str = "engaged"
    ) -> dict:
        """Ask the meta-cell for the next move."""
        recent = chain[-5:]
        chain_summary = "\n".join(
            f"[{i}] (skill={c['skill']}, mode={c['mode']}): {c['thought'][:160]}"
            for i, c in enumerate(recent)
        )
        # Compact skill catalog (names + 1-line descriptions). Cap at all-non-routers.
        skill_catalog = "\n".join(
            f"- {s['name']}: {s['description'][:120]}"
            for s in self._index.skills
            if not s["is_router"]
        )[:8000]  # rough token cap
        flavor_hint = (
            "This reflection is ANXIOUS (worried/brooding): lean toward 'reframe' and "
            "resolution/closure frameworks (decision, constraint, logic-consistency, "
            "emotional-resistance) that help SETTLE the worry."
            if flavor == "anxious"
            else "This reflection is ENGAGED (curious/interested): lean toward 'branch' and "
            "'transform' with generative frameworks (creativity, analogy, systems) that "
            "DEEPEN and expand the idea."
        )
        user = (
            f"Chain so far (most recent {len(recent)} entries):\n{chain_summary}\n\n"
            f"Available skills:\n{skill_catalog}\n\n"
            f"{flavor_hint}\n\n"
            f"Decide next move."
        )
        self._meta_cell.reset_turn(turn_id)
        raw = await self._meta_cell.call([{"role": "user", "content": user}])
        try:
            return json.loads(self._strip_to_json(raw))
        except Exception:
            return {
                "mode": "stop",
                "skill": None,
                "base_idx": len(chain) - 1,
                "why": "parse-failed",
            }

    async def _apply_skill(self, skill_name: str, thought: str, mode: str, turn_id: str) -> str:
        """Apply a skill to a thought, returning a refined/branched/reframed take."""
        skill_entry = self._index.get(skill_name)
        descr = skill_entry["description"] if skill_entry else ""
        mode_hint = {
            "transform": "Refine and deepen this thought through the framework.",
            "branch": "Take this in a fresh direction using the framework.",
            "reframe": "Challenge or invert the thought through the framework.",
        }.get(mode, "Apply the framework to this thought.")

        # Use a transient local cell — skill text is auto-injected by SkillLoader via cell.skills
        worker = IntegratorCell(
            name="skill_ruminate_worker",
            cluster="dmn",
            model="local",
            system_prompt=(
                f"You are reflecting on a thought using a specific cognitive framework "
                f"({skill_name}: {descr[:150]}). {mode_hint} Produce a single concise "
                f"paragraph (≤120 words) — the new take."
            ),
            topics=[],
            max_tokens=300,
            timeout_seconds=18.0,
            locality="local",
            skills=[skill_name],
            max_calls_per_turn=999,
        )
        worker.set_router(self._router)
        worker.reset_turn(turn_id)
        return await worker.call([{"role": "user", "content": thought}])

    async def _synthesize_chain(self, chain: list[dict], *, turn_id: str = "") -> str:
        """Pick the strongest skill-refined take from the rumination chain.

        Rather than blindly returning the last entry, score each skill-produced
        take by a cheap blend of novelty-vs-seed (it should have moved the thought
        somewhere) and adequate substance (length). This makes the multi-skill
        comparison actually influence the output — the point of trying a thought
        against several analytical processes.
        """
        if len(chain) <= 1:
            return chain[0]["thought"]
        seed = chain[0]["thought"]
        best = chain[-1]
        best_score = -1.0
        for c in chain[1:]:
            t = (c.get("thought") or "").strip()
            if not t:
                continue
            novelty = 1.0 - self._token_overlap(t, seed)
            substance = min(len(t) / 240.0, 1.0)
            score = 0.7 * novelty + 0.3 * substance
            if score > best_score:
                best_score, best = score, c
        return best["thought"]

    @staticmethod
    def _token_overlap(a: str, b: str) -> float:
        wa = {w for w in a.lower().split() if len(w) >= 3}
        wb = {w for w in b.lower().split() if len(w) >= 3}
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    @staticmethod
    def _strip_to_json(text: str) -> str:
        """Best-effort: extract the first {...} block from a model response."""
        if not text:
            return "{}"
        s = text.find("{")
        e = text.rfind("}")
        if s == -1 or e == -1 or e <= s:
            return "{}"
        return text[s : e + 1]

    @classmethod
    def _parse_json_field(cls, raw: str, field_name: str) -> Any:
        try:
            d = json.loads(cls._strip_to_json(raw))
            return d.get(field_name)
        except Exception:
            return None
