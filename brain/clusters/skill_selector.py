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

import contextlib
import functools
import json
import logging
import math
import random
import re
import time
from contextvars import ContextVar
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


# The agent a conversational turn speaks as, bound by the frontal cluster around
# skill selection (features["agent_id"] = persona.mandate). A turn binds nothing in
# brain.agent_ctx — that is bound only for motor dispatch and jobs — so without this
# the agent-scope gate saw no agent on every conversational turn.
_turn_agent: ContextVar[str] = ContextVar("skill_turn_agent", default="")


@contextlib.contextmanager
def bind_turn_agent(agent_id: str | None):
    """Bind the turn's agent id for skill selection. Falsy = no-op."""
    if not agent_id:
        yield
        return
    token = _turn_agent.set(str(agent_id))
    try:
        yield
    finally:
        _turn_agent.reset(token)


def turn_agent_id() -> str:
    """The agent this skill decision is for: an explicit agent_ctx binding (motor,
    jobs), else the conversational turn's agent, else the engine session's agent
    id from turn_ctx. "" = no agent (the owner's own chat, the idle mind)."""
    try:
        from brain.agent_ctx import current_agent

        a = current_agent()
        if a and a.get("agent_id"):
            return str(a["agent_id"])
    except Exception:
        pass
    bound = _turn_agent.get()
    if bound:
        return bound
    try:
        from brain.turn_ctx import current_turn

        return str(current_turn().get("agent_id") or "")
    except Exception:
        return ""


def _disabled_for(agent_id: str) -> frozenset[str]:
    if not agent_id:
        return frozenset()
    try:
        from brain import agents

        return agents.disabled_skills(agent_id)
    except Exception:
        return frozenset()


def _org_only_for(agent_id: str) -> bool:
    if not agent_id:
        return False
    try:
        from brain import agents

        return agents.org_skills_only(agent_id)
    except Exception:
        return False


# What the person is asking for on a turn, coarsely: "seek" = asking for something
# (a question, help, a next step), "share" = telling, venting, reflecting. "" = no
# read (a follow-up or clarification continues whatever came before). Read from the
# parser's intent and the executive's response_type, never from the words.
_SEEK_INTENTS = frozenset({"question", "task"})
_SHARE_INTENTS = frozenset({"chitchat", "epistemic_action", "other", "greeting"})
_SEEK_RESPONSE_TYPES = frozenset({"informative", "task"})
_SHARE_RESPONSE_TYPES = frozenset({"chitchat", "introspective", "defuse"})


def ask_mode(response_type: str | None = "", intent: str | None = "") -> str:
    """ "seek" | "share" | "" for this turn. The parser's intent is the person's own
    move and wins; the executive's response_type is the fallback read."""
    intent = (intent or "").strip().lower()
    response_type = (response_type or "").strip().lower()
    if intent in _SEEK_INTENTS:
        return "seek"
    if intent in _SHARE_INTENTS:
        return "share"
    if intent:  # follow_up, clarification_request, memory_recall: a continuation
        return ""
    if response_type in _SEEK_RESPONSE_TYPES:
        return "seek"
    if response_type in _SHARE_RESPONSE_TYPES:
        return "share"
    return ""


def native_skill_catalog() -> list[dict]:
    """The built-in operational skills (brain/skills/*.md with frontmatter, stances
    excluded) as {id, name, description, tools} — what an agent's Skills card lists
    next to the org's own skills. `tools` is the frontmatter's declared tool list
    (same vocabulary as an org skill's allowed_tools). Read from disk; no index or
    embedding needed."""
    try:
        import yaml
    except ImportError:
        return []
    out: list[dict] = []
    for md_path in sorted(INDEX_PATH.parent.glob("*.md")):
        try:
            raw = md_path.read_text(encoding="utf-8")
            if not raw.startswith("---"):
                continue
            fm = yaml.safe_load(raw[3 : raw.index("---", 3)])
        except Exception:
            continue
        if not isinstance(fm, dict) or not fm.get("name") or fm.get("kind") == "stance":
            continue
        out.append(
            {
                "id": str(fm["name"]),
                "name": str(fm.get("display_name") or fm["name"]),
                "description": str(fm.get("description") or ""),
                "tools": [str(t) for t in (fm.get("tools") or []) if str(t).strip()],
            }
        )
    return out


@functools.lru_cache(maxsize=1024)
def _phrase_pattern(phrase: str) -> re.Pattern | None:
    """Whole-word, case-insensitive matcher for a skill keyword or a full skill id.
    Words may be separated by spaces, hyphens or underscores in either the phrase
    or the user's text; a trailing plural ``s``/``es`` on the last word counts.
    Never a substring: "act" does not match "actually", "self" not "myself"."""
    words = [w for w in re.split(r"[\s_\-]+", (phrase or "").strip().lower()) if w]
    if not words:
        return None
    body = r"[\s_\-]+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?:e?s)?(?!\w)")


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
    # ask_mode() of the turn the skill locked on. When a later turn's mode differs
    # (venting → "so what can I do?"), the person is asking for something else and
    # selection re-opens even though the topic hasn't drifted.
    anchor_ask: str = ""


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

    def inject_native(self, entry: dict) -> None:
        """Add a native (non-humanity) skill to the in-memory index."""
        if entry["name"] not in self._by_name:
            self.skills.append(entry)
            self._by_name[entry["name"]] = entry

    def inject_partner(self, entry: dict) -> None:
        """Add or replace an app-provided (partner) skill. Won't shadow a built-in or
        native skill of the same name — a partner can't override the operator's own
        skills by reusing an id (logged + skipped)."""
        name = entry["name"]
        existing = self._by_name.get(name)
        if existing is not None and not existing.get("_partner"):
            logger.warning("partner skill id %r collides with a built-in skill — skipped", name)
            return
        if existing is not None:
            self.skills = [s for s in self.skills if s["name"] != name]
        self.skills.append(entry)
        self._by_name[name] = entry

    def remove_partner(self) -> list[str]:
        """Drop every partner skill from the index (called before each re-warm so
        deleted/edited skills don't linger). Returns the removed names."""
        names = [s["name"] for s in self.skills if s.get("_partner")]
        if names:
            self.skills = [s for s in self.skills if not s.get("_partner")]
            for n in names:
                self._by_name.pop(n, None)
        return names

    def keyword_match(self, user_input: str, allowed=None) -> dict | None:
        """Return the native/partner skill the user named directly, or None.

        A skill is named when the user says one of its declared ``keywords`` or its
        full id as a phrase ("trading analyst", "self compassion break") — whole
        words only, case-insensitive (see ``_phrase_pattern``). The id is never split
        into single words: the old per-token substring test loaded a partner's
        ``haven-self-compassion`` on "myself" and ``haven-act`` on "actually", and
        would load ``supabase-postgres-best-practices`` on "best". Keywords are the
        author's explicit "these words mean this skill", so they keep the short-
        circuit; everything else goes through the gated embedding/LLM pick. When
        several skills match, the longest phrase wins (most specific). ``allowed``
        (entry → bool) filters candidates first — the agent-scope gate — so an
        out-of-scope partner skill can't shadow an in-scope match.

        Stances (kind == "stance") are excluded: they are drawn by the stance machinery,
        never picked as the conversational active skill — without this gate a native
        stance file would be keyword-matchable and its body would be injected under the
        operational "tools are REAL" framing."""
        lowered = (user_input or "").lower()
        if not lowered:
            return None
        best: tuple[int, dict] | None = None
        for s in self.skills:
            if not s.get("_native") or s.get("kind") == "stance":
                continue
            if allowed is not None and not allowed(s):
                continue
            phrases = [s["name"], *(str(k) for k in s.get("keywords") or [])]
            for phrase in phrases:
                pat = _phrase_pattern(phrase.lower())
                if pat is None or not pat.search(lowered):
                    continue
                if best is None or len(phrase) > best[0]:
                    best = (len(phrase), s)
        return best[1] if best else None

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
            if s.get("kind") == "stance":
                # Stances never enter conversational/autonomous skill selection —
                # they are drawn by the stance machinery (info_pool/method_pool).
                continue
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
        self._native_body_cache: dict[str, str] = {}

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

        # Ollama selector for autonomous one-shot picks. "runpod" (not "local"): on a
        # hosted tenant there is no Ollama in the container, so model="local" made this
        # cell error on EVERY idle tick ("All connection attempts failed", found
        # 2026-08-22) — the same wrong-first-hop bug ResultReporter documents. The
        # runpod key resolves to the pod when there is one and degrades to local Ollama
        # when there isn't, so it is right in both deployments; locality stays "local"
        # because RunPod Ollama is still the local-provider tier, never cloud.
        self._autonomous_cell = IntegratorCell(
            name="skill_selector_autonomous",
            cluster="dmn",
            model="runpod",
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
            model="runpod",
            system_prompt=_META_RUMINATE_SYSTEM,
            topics=[],
            max_tokens=120,
            timeout_seconds=15.0,
        )
        self._meta_cell.set_router(router)

        # Reset per-call counters every use; we use the cells in non-turn-bound mode.
        for c in (self._conversational_cell, self._autonomous_cell, self._meta_cell):
            c.max_calls_per_turn = 999

    # ----- public API --------------------------------------------------

    async def warm_native_skills(self) -> None:
        """Scan brain/skills/*.md for native operational skill files and inject into the index.

        Also checks whether any new skills have appeared in .claude/skills/ that aren't
        yet in the humanity index and runs _import_humanity if so, keeping the index
        current without requiring a manual re-import step.

        Any brain/skills/*.md file with YAML frontmatter and a 'name:' field that still
        isn't in the index after the sync is treated as a native skill and injected
        in-memory only.
        """
        try:
            import yaml
        except ImportError:
            logger.warning("warm_native_skills: PyYAML not installed, skipping")
            return

        # --- Humanity index freshness check -----------------------------------
        # If .claude/skills/ has skill directories not yet in the index, trigger
        # a re-import so the index stays current automatically.
        try:
            from brain.skills._import_humanity import SOURCE_DIR
            from brain.skills._import_humanity import main as _import_main

            if SOURCE_DIR.exists():
                source_names = {
                    sd.name
                    for sd in SOURCE_DIR.iterdir()
                    if sd.is_dir() and (sd / "SKILL.md").exists()
                }
                new_skills = source_names - set(self._index._by_name.keys())
                if new_skills:
                    logger.info(
                        "warm_native_skills: %d new skill(s) detected — syncing index: %s",
                        len(new_skills),
                        sorted(new_skills),
                    )
                    await _import_main()
                    self._index = _Index()  # reload from updated JSON
        except Exception as _sync_err:
            logger.warning("warm_native_skills: index sync failed: %s", _sync_err)
        # ----------------------------------------------------------------------

        skills_dir = INDEX_PATH.parent
        for md_path in sorted(skills_dir.glob("*.md")):
            raw = md_path.read_text(encoding="utf-8")
            if not raw.startswith("---"):
                continue
            try:
                end = raw.index("---", 3)
            except ValueError:
                continue
            fm_text = raw[3:end]
            try:
                fm = yaml.safe_load(fm_text)
            except Exception:
                continue
            if not isinstance(fm, dict):
                continue
            name = fm.get("name")
            if not name or name in self._index._by_name:
                continue
            desc = fm.get("description", "")
            keywords = fm.get("keywords", [])
            embed_text = f"{name} {desc} {' '.join(str(k) for k in keywords)}"
            vec = await self._router.embed(embed_text)
            if vec is None:
                logger.warning("warm_native_skills: embed failed for %s", name)
                vec = []
            entry = {
                "name": name,
                "description": desc,
                "category": fm.get("category", "native"),
                "tier": fm.get("tier", 2),
                "is_router": fm.get("is_router", False),
                "keywords": keywords,
                "embedding": vec,
                "_native": True,
            }
            # Stance files (kind: stance) carry their draw metadata in frontmatter —
            # the affinity map and complexity ride with the stance, not a table in code.
            # The kind field is also the containment discriminator: everything reading
            # the index for CONVERSATIONAL selection (rank, keyword_match, manifest,
            # native_skill_body) excludes kind == "stance".
            if fm.get("kind"):
                entry["kind"] = str(fm["kind"])
            if fm.get("complexity") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    entry["complexity"] = max(0.0, min(1.0, float(fm["complexity"])))
            if isinstance(fm.get("affinity"), dict):
                entry["affinity"] = {str(k): v for k, v in fm["affinity"].items()}
            self._index.inject_native(entry)
            logger.debug("warm_native_skills: injected %s", name)

    async def warm_partner_skills(self) -> None:
        """Load the org's APPROVED app-provided skills into the live index.

        Idempotent: drops the current partner entries, then re-injects the live set
        from brain.skills_registry. Call at boot (after warm_native_skills) and again
        after any admin change (submit-approved / superadmin-approve / delete) so the
        next turn selects against the current set. Only skills with a cleared
        (approved) body are loaded — pending/flagged/rejected never inject.

        Treated like native operational skills for body injection (native_skill_body
        returns the cached body → the frontal active-skill path fences it into context,
        reaching cloud + local), but tagged _partner so that injection uses the
        untrusted-content precedence framing instead of the trusted-native one."""
        try:
            from brain import skills_registry

            live = skills_registry.live_skills()
        except Exception as e:  # SkillError when Supabase is off, or a load failure
            logger.debug("warm_partner_skills: skipped (%s)", e)
            self._drop_partner_skills()  # registry gone → don't keep stale entries
            return

        self._drop_partner_skills()
        from brain.skill_loader import SkillLoader

        for sk in live:
            name = sk["id"]
            desc = sk.get("description") or ""
            keywords = sk.get("keywords") or []
            body = sk.get("body") or ""
            embed_text = f"{name} {desc} {' '.join(str(k) for k in keywords)}"
            vec = await self._router.embed(embed_text)
            if vec is None:
                logger.warning("warm_partner_skills: embed failed for %s", name)
                vec = []
            entry = {
                "name": name,
                "description": desc,
                "category": "partner",
                "tier": int(sk.get("tier") or 2),
                "is_router": False,
                "keywords": keywords,
                "allowed_tools": sk.get("allowed_tools") or [],
                "embedding": vec,
                "_native": True,  # body-injection + capability-manifest treat it like native
                "_partner": True,  # untrusted → fenced precedence framing on injection
                # Agent scoping: available to every agent, or only the mapped ones.
                "_all_agents": bool(sk.get("all_agents", True)),
                "_agents": set(sk.get("agents") or []),
            }
            self._index.inject_partner(entry)
            self._native_body_cache[name] = body
            SkillLoader.register_partner(name, body)
        if live:
            logger.info("warm_partner_skills: injected %d partner skill(s)", len(live))

    def _drop_partner_skills(self) -> None:
        from brain.skill_loader import SkillLoader

        for n in self._index.remove_partner():
            self._native_body_cache.pop(n, None)
        SkillLoader.clear_partner()

    def is_partner_skill(self, name: str) -> bool:
        """True iff this skill is app-provided (untrusted) rather than built-in/native."""
        entry = self._index.get(name)
        return bool(entry and entry.get("_partner"))

    def _agent_allowed(self, entry: dict) -> bool:
        """Agent gate for a candidate skill, for the agent this turn speaks as
        (turn_agent_id). Two layers:
          1. the agent's own off-switches (agents.disabled_skills — the Skills card
             on the agent page): any skill, built-in or org, can be turned off;
          2. an org skill's audience: all agents, or the specific agents it's
             mapped to. With no agent (the owner's own chat, the idle mind), only
             all-agents org skills apply.
        Humanity frameworks and built-in skills pass layer 2 unconditionally."""
        aid = turn_agent_id()
        if aid and entry.get("name") in _disabled_for(aid):
            return False
        if not entry.get("_partner"):
            # skills_org_only: built-ins and generic frameworks are off for this agent.
            return not (aid and _org_only_for(aid))
        if entry.get("_all_agents", True):
            return True
        return bool(aid) and aid in (entry.get("_agents") or set())

    def allowed_for_current_agent(self, name: str) -> bool:
        """Public form of _agent_allowed by skill name (pin + executive-pick paths)."""
        entry = self._index.get(name)
        return bool(entry) and self._agent_allowed(entry)

    def attachable_fragment_ids(self) -> list[str]:
        """The curated pool a fragment attachment (Tier 1 structural plasticity) may draw
        from: app-provided (partner) skills currently warmed with an injectable body AND
        allowed for the agent bound this turn. Native humanity frameworks are excluded —
        Claude has them and their body is empty. Agent-scope gating here keeps a partner
        org's fragments from leaking across tenants, same as skill selection."""
        out: list[str] = []
        for name, body in self._native_body_cache.items():
            if not body:
                continue
            entry = self._index.get(name)
            if entry and entry.get("_partner") and self._agent_allowed(entry):
                out.append(name)
        return out

    # ----- stance library (approach-competition Phase B) ----------------
    #
    # Two per-turn stance axes ride the skills index without entering skill SELECTION:
    #   info axis   — brain/skills/stance-*.md files (kind: stance): information posture.
    #   method axis — humanity reasoning leaves in the strategy-shaped categories below:
    #                 how to attack the problem.
    # Selection containment lives in rank()/keyword_match()/capability_manifest()/
    # native_skill_body(); these pools are the stance machinery's own entry points.

    METHOD_CATEGORIES: frozenset = frozenset(
        {
            "investigation",
            "constraint",
            "analogy",
            "creativity",
            "decision",
            "logic",
            "probability",
            "epistemology",
            "play",
            "systems",
            "aesthetic",
        }
    )

    def info_pool(self) -> list[dict]:
        """The information-posture stances (kind == "stance") with a usable embedding."""
        return [s for s in self._index.skills if s.get("kind") == "stance" and s.get("embedding")]

    def method_pool(self) -> list[dict]:
        """Humanity leaves usable as method stances: strategy-shaped categories only,
        no routers, no tier-1 always-on checks, no stances, embedding present. Each
        entry gets a derived `complexity` (cached) for the cognitive-economy term."""
        out: list[dict] = []
        for s in self._index.skills:
            if (
                s.get("kind") == "stance"
                or s.get("is_router")
                or s.get("tier") == 1
                or s.get("category") not in self.METHOD_CATEGORIES
                or not s.get("embedding")
            ):
                continue
            if "complexity" not in s:
                s["complexity"] = self._derived_complexity(s)
            out.append(s)
        return out

    def _derived_complexity(self, entry: dict) -> float:
        """Deterministic complexity in [0.1, 0.95] for a method skill — how heavy the
        method is to actually run. Derived, never hand-listed (project rule): tier sets
        the base (tier 3 = specialized depth), and the on-disk body's structure count
        (headings, numbered steps, bullets) adds the rest. Stable across boots."""
        base = 0.6 if entry.get("tier") == 3 else 0.45
        body = self._read_disk_body(str(entry.get("name", "")))
        steps = sum(
            1
            for line in body.splitlines()
            if line.startswith(("#", "- ", "* ")) or line[:2].rstrip(".").isdigit()
        )
        return round(max(0.1, min(0.95, base + min(0.3, steps * 0.02))), 3)

    def stance_kind(self, name: str) -> str | None:
        """ "info" | "method" | None — which stance axis a skill id belongs to."""
        entry = self._index.get(name)
        if not entry:
            return None
        if entry.get("kind") == "stance":
            return "info"
        if (
            not entry.get("is_router")
            and entry.get("tier") != 1
            and entry.get("category") in self.METHOD_CATEGORIES
        ):
            return "method"
        return None

    def stance_directive(self, name: str) -> str:
        """Competition-tier form of a stance: `name — first sentence of description`.
        ~15 tokens; enough to make candidates diverge without the body's weight."""
        entry = self._index.get(name)
        if not entry:
            return ""
        desc = str(entry.get("description", ""))
        first = desc.split(". ")[0].strip().rstrip(".")
        return f"{name} — {first}." if first else name

    def stance_body(self, name: str) -> str:
        """Winner-tier form: the full on-disk body, bypassing the humanity short-circuit
        in native_skill_body (which stays correct for the selector's own uniform pick —
        this method is the stance machinery's deliberate read)."""
        if self.stance_kind(name) is None:
            return ""
        return self._read_disk_body(name)

    def rank_fragments_by_relevance(
        self, skill_ids: list[str], query_vec: list[float]
    ) -> list[tuple[str, float]]:
        """Cosine-rank the given skill ids against a query embedding, descending.

        Used by relevance-ranked fragment exploration (frontal._explore_candidate):
        instead of a blind roll over the whole pool, the exploring drafter draws
        from the fragments most similar to the current input. Ids with no index
        entry or no embedding are omitted. Input is name-sorted before the stable
        score sort so equal-score ties break by name — keeps the per-turn roll
        deterministic in tests."""
        scored: list[tuple[str, float]] = []
        for sid in sorted(skill_ids):
            entry = self.get_skill(sid)
            emb = (entry or {}).get("embedding") or []
            if not emb:
                continue
            scored.append((sid, _Index.cosine(query_vec, emb)))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored

    MANIFEST_MAX_NATIVE = 16
    MANIFEST_DESC_CHARS = 240

    def capability_manifest(self, *, include_frameworks: bool = True) -> str:
        """Compact skill manifest for the executive context, for the agent bound
        this turn (turn_agent_id): only skills that agent may use are listed, so the
        executive can't name one the pick would then refuse.

        Returns a plain-text listing of native operational skills (full detail)
        and category-level humanity routers (brief) so the executive LLM can
        pick a relevant skill inline on each turn. The org's own skills come first:
        an app installed them for this agent, and their descriptions are written as
        "when to use" conditions, so they are never the ones the cap cuts.

        `include_frameworks=False` returns the natives only. The DMN calls it that way:
        it asks for this block to learn which tools it can invoke directly, and the
        routers half is ~3.1KB of "Entry point for the X toolkit. Routes to the right X
        skill…" boilerplate restating category names the DMN already has from
        FRAMEWORKS_CATEGORIES. On an 8192-token window that mattered.

        Both halves iterate the whole index, and every approved partner skill lands in
        the natives half — so the natives are capped rather than left to grow with the
        registry.
        """
        lines: list[str] = []

        native = [
            s
            for s in self._index.skills
            if s.get("_native") and s.get("kind") != "stance" and self._agent_allowed(s)
        ]
        native.sort(key=lambda s: not s.get("_partner"))  # stable: org skills first
        if native:
            lines.append("Operational capabilities:")
            for s in native[: self.MANIFEST_MAX_NATIVE]:
                lines.append(f"  {s['name']}: {s['description'][: self.MANIFEST_DESC_CHARS]}")
            if len(native) > self.MANIFEST_MAX_NATIVE:
                lines.append(f"  (+{len(native) - self.MANIFEST_MAX_NATIVE} more)")

        if include_frameworks:
            routers = sorted(
                [
                    s
                    for s in self._index.skills
                    if s.get("is_router") and not s.get("_native") and self._agent_allowed(s)
                ],
                key=lambda x: x["name"],
            )
            if routers:
                lines.append("Reasoning frameworks (use category name as skill):")
                for s in routers:
                    lines.append(f"  {s['name']}: {s['description'][:100]}")

        return "\n".join(lines)

    @property
    def tier1_names(self) -> list[str]:
        return list(self._index.tier1_names)

    def get_skill(self, name: str) -> dict | None:
        return self._index.get(name)

    def native_skill_body(self, name: str) -> str:
        """Operational guide (markdown body, frontmatter stripped) of a NATIVE skill.

        Returns "" for humanity reasoning frameworks (Claude knows those natively,
        so injecting them is bloat) and for unknown skills. Only native operational
        skills — brain/skills/<name>.md with a 'name:' frontmatter not in the
        humanity index, e.g. trading-analyst — return a body, because the model has
        no other way to learn the app-specific tools/data files they describe. Cached."""
        entry = self._index.get(name)
        if not entry or not entry.get("_native"):
            return ""
        # Containment: a stance can never reach the conversational injection path.
        # Even if one were somehow chosen as the active skill, this returns "" so the
        # "tools are REAL" operational framing can't wrap a thinking stance. Stance
        # consumers use stance_body(), which reads the disk deliberately.
        if entry.get("kind") == "stance":
            return ""
        if name in self._native_body_cache:
            return self._native_body_cache[name]
        # Partner skills have no disk file — their body is always pre-cached at warm
        # time (an empty body here means it isn't live), so never fall through to disk.
        if entry.get("_partner"):
            return ""
        body = self._read_disk_body(name)
        self._native_body_cache[name] = body
        return body

    @staticmethod
    def _read_disk_body(name: str) -> str:
        """Markdown body of brain/skills/<name>.md with frontmatter stripped, or ""."""
        try:
            path = INDEX_PATH.parent / f"{name}.md"
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                if raw.startswith("---"):
                    with contextlib.suppress(ValueError):
                        raw = raw[raw.index("---", 3) + 3 :]
                return raw.strip()
        except Exception:
            return ""
        return ""

    def gate_conversational(
        self,
        *,
        response_type: str = "",
        user_emotion: str = "",
        sentiment: float | None = None,
    ) -> bool:
        """Return True if this turn qualifies for skill injection: a substantive
        turn type, or the person seems to be having a hard time — any negative-
        valence emotion label, or negative message sentiment when the label reads
        neutral (brain/user_emotion.py). By valence, not a list of labels: the
        parser says "struggling", "tired", "disappointed", and off-list "worried" or
        "stuck", and every one of them is a turn a framework can help with."""
        if response_type in {"informative", "task", "defuse", "introspective", "recall"}:
            return True
        from brain.user_emotion import is_negative_state

        return is_negative_state(user_emotion, sentiment)

    def _org_skills_in_scope(self) -> bool:
        """True when the agent bound this turn has at least one of its org's own
        skills available."""
        return any(s.get("_partner") and self._agent_allowed(s) for s in self._index.skills)

    async def select_conversational(
        self,
        user_input: str,
        executive_out: dict,
        user_emotion: str,
        recent_turns: list[str],
        active: ActiveSkillContext | None,
        *,
        turn_id: str = "",
        intent: str = "",
        sentiment: float | None = None,
    ) -> tuple[SkillBundle | None, ActiveSkillContext | None, dict]:
        """
        Returns (bundle, updated_active_context, log_extras).
        bundle is None when the turn is gated out (no skill block injected at all).

        `intent` is the parser's read of the person's move and `sentiment` its
        -1..1 message valence; both feed the gate and the re-open check.
        """
        log_extras: dict[str, Any] = {}
        # The sticky budget counts conversation turns, not only the turns that
        # reused the skill: a gated or direct-match turn still ages it.
        if active is not None:
            active.turns_active += 1

        # Direct-name pre-pass: if the user explicitly references a native skill by
        # keyword (e.g. "trading tool", "check my watchlist"), short-circuit straight
        # to that skill without going through the embedding/LLM path.
        # Skills this turn's agent can't use (switched off, or an org skill not
        # scoped to it) are filtered before matching.
        _direct = self._index.keyword_match(user_input, allowed=self._agent_allowed)
        if _direct is not None:
            log_extras["pick_path"] = "direct_name_match"
            log_extras["direct_match"] = _direct["name"]
            return (
                SkillBundle(
                    tier1=self.tier1_names,
                    chosen=[_direct["name"]],
                    pick_source="direct_name_match",
                ),
                active,
                log_extras,
            )

        response_type = executive_out.get("response_type", "")
        key_points = executive_out.get("key_points", [])
        ask = ask_mode(response_type, intent)

        # A turn the gate would drop still goes to selection when the agent has its
        # org's own skills — over THOSE skills only. The gate exists to keep the
        # generic framework library off casual turns; an app's skills were installed
        # for this agent's purpose, their descriptions say when they apply, and the
        # picker still declines (strong null bias) when none does.
        org_pool_only = False
        if not self.gate_conversational(
            response_type=response_type,
            user_emotion=user_emotion,
            sentiment=sentiment,
        ):
            if not self._org_skills_in_scope():
                log_extras["gated"] = True
                return None, active, log_extras
            org_pool_only = True
            log_extras["gate_bypass"] = "org_skills"

        # 1. Embed the current query
        query_text = user_input
        if key_points:
            query_text += "  " + " ".join(str(k) for k in key_points)
        query_vec = await self._router.embed(query_text)
        if query_vec is None:
            logger.debug("Skill selector: embedding unavailable, skipping")
            log_extras["embed_failed"] = True
            return None, active, log_extras

        # 2. Sticky-context check: reuse, or re-open selection?
        carry = active is not None
        if carry and org_pool_only:
            # On an org-pool turn only an org skill may carry over; a generic
            # framework stays locked for the next substantive turn, unused on this one.
            leaf = self._index.get(active.current_leaf or "")
            carry = bool(leaf and leaf.get("_partner"))
        if carry:
            drift = self._index.cosine(query_vec, active.anchor_topic_embedding)
            log_extras["sticky_drift_score"] = drift
            if drift < TOPIC_DRIFT_THRESHOLD or active.turns_active > STICKY_TURN_BUDGET:
                log_extras["sticky_action"] = "drifted_or_expired"
                active = None
            elif ask and active.anchor_ask and ask != active.anchor_ask:
                # Same topic, different ask: the person went from telling to asking
                # (or back). What fits may have changed, so pick again.
                log_extras["sticky_action"] = "ask_shifted"
                log_extras["ask"] = f"{active.anchor_ask}->{ask}"
                active = None
            elif active.current_leaf and not self.allowed_for_current_agent(active.current_leaf):
                # The sticky skill was switched off for this agent since it locked.
                log_extras["sticky_action"] = "disallowed"
                active = None
            else:
                # Reuse — return the locked leaf (or category overview if leaf not yet picked)
                if ask and not active.anchor_ask:
                    active.anchor_ask = ask  # locked on a continuation turn: adopt this read
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
        # Drop skills this turn's agent can't use before shortlisting.
        ranked = [
            (s, sc)
            for (s, sc) in ranked
            if self._agent_allowed(s) and (s.get("_partner") or not org_pool_only)
        ]
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
            f"User intent: {intent or 'unknown'}\n"
            f"User input: {user_input[:400]}\n"
            f"Recent turns:\n" + "\n".join(recent_turns[-2:])
        )
        msg = [{"role": "user", "content": f"{context_block}\n\nCandidates:\n{prompt_candidates}"}]
        self._conversational_cell.reset_turn(turn_id)
        raw = await self._conversational_cell.call(msg)
        log_extras["llm_raw"] = raw[:200]

        pick = self._parse_json_field(raw, "skill")
        if not pick or pick not in self._index._by_name or not self.allowed_for_current_agent(pick):
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
        *,
        ask: str = "",
    ) -> ActiveSkillContext | None:
        """Construct the ActiveSkillContext to store on parietal after a pick.
        `ask` is the locking turn's ask_mode()."""
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
            anchor_ask=ask,
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
                skill = self._fallback_skill(flavor, self._blocked_category(chain))
            elif self._skill_category(skill) == self._blocked_category(chain):
                # Meta-cell ignored the category cap (it can't see the blocked category in its
                # prompt, but a stale pick can still land here) — redirect to a fresh lens.
                skill = self._fallback_skill(flavor, self._blocked_category(chain)) or skill
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

    # Allow a few skills from one category in a row, then force a move to a different lens.
    _MAX_CONSEC_CATEGORY = 3

    def _skill_category(self, name: str | None) -> str | None:
        entry = self._index._by_name.get(name) if name else None
        return entry.get("category") if entry else None

    def _blocked_category(self, chain: list[dict]) -> str | None:
        """If the last _MAX_CONSEC_CATEGORY applied skills are all the same category, return it
        so the next pick is forced into a DIFFERENT lens — variety across categories, while still
        allowing a short run within one category first."""
        cats = [self._skill_category(c.get("skill")) for c in chain if c.get("skill")]
        if len(cats) >= self._MAX_CONSEC_CATEGORY:
            tail = cats[-self._MAX_CONSEC_CATEGORY :]
            if tail[0] and all(c == tail[0] for c in tail):
                return tail[0]
        return None

    def _fallback_skill(self, flavor: str, blocked_cat: str | None = None) -> str | None:
        """Pick a flavor-appropriate skill when the meta-cell names none, so the
        rumination loop still progresses in the right register. Honors the category cap."""
        pool = self._ANXIOUS_SKILLS if flavor == "anxious" else self._ENGAGED_SKILLS
        available = [
            n for n in pool if n in self._index._by_name and self._skill_category(n) != blocked_cat
        ]
        if available:
            return random.choice(available)
        tier2 = [
            s
            for s in self._index.skills
            if s["tier"] == 2 and not s["is_router"] and s["category"] != blocked_cat
        ]
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
        # Compact skill catalog (names + 1-line descriptions). The list is alphabetical and the
        # 8k-char budget only fits ~1/3 of skills, so a fixed prefix would hide the back half of
        # the library (logic/systems/strategy/writing…) and bias rumination toward early-alphabet
        # categories like analogy. SHUFFLE so each step samples variety across categories, and
        # drop the over-used category (consecutive-category cap) to force a fresh lens.
        blocked_cat = self._blocked_category(chain)
        pool = [
            s for s in self._index.skills if not s["is_router"] and s["category"] != blocked_cat
        ]
        random.shuffle(pool)
        lines, budget = [], 0
        for s in pool:
            line = f"- {s['name']}: {s['description'][:120]}"
            if budget + len(line) + 1 > 8000:
                break
            lines.append(line)
            budget += len(line) + 1
        skill_catalog = "\n".join(lines)
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
            model="runpod",
            system_prompt=(
                f"You are reflecting on a thought using a specific cognitive framework "
                f"({skill_name}: {descr[:150]}). {mode_hint} Produce a single concise "
                f"paragraph (≤120 words) — the new take."
            ),
            topics=[],
            max_tokens=300,
            timeout_seconds=18.0,
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
