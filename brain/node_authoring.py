"""
node_authoring — Tier 2 structural plasticity: the brain AUTHORS a new specialization skill.

A gated sleep pass drafts a candidate skill grounded in a persona's PROVEN fragment clusters,
then admits it through the SAME screener that governs untrusted partner skills
(`brain/skills_screener.SkillScreener`) — auto-live only when the screener passes CLEAN
(judge=approve AND static-clean), otherwise it lands in the owner's existing flagged-skills
review queue and does NOT go live. This mirrors `cross_learning.learn_from_private`
(draft → gate → store), swapping the de-id gate for the skill screener and the hypothesis store
for `skills_registry`. The admitted skill enters the curated pool; Tier 1 then attaches it and
Tier 2 recruitment can crystallize it into a node — so authored cognition still has to EARN its
node through reward. Two independent gates: the screener admits authored content; reward promotes.

SAFETY: the screener is the admission boundary (static security scan reusing `security.screen_input`
+ an LLM judge, fail-safe to `flagged`); an admitted body is still injected behind the
untrusted-content fence downstream (role, not authority); evidence is drawn ONLY from the
descriptions of already-screened skills (never raw conversation), so authoring can't leak user
data; org-scoped (skills are keyed by org_id). Cloud-only — skills live in Supabase, so this is a
no-op in companion/local mode.
"""

from __future__ import annotations

import json
import logging
import re
import time

from brain.settings import settings

logger = logging.getLogger(__name__)

# System prompt for the local "architect" cell. It authors ONLY when there is a clear recurring
# specialization; otherwise it returns an empty skill_id. The body is operational guidance only —
# any tool-call syntax / URLs / injection markers force the screener to FLAG it.
NODE_ARCHITECT_SYSTEM = """You are the architect of a persistent AI brain's own capabilities.
You are shown OPERATIONAL APPROACHES that have repeatedly worked well for this user — each is
already a vetted skill. If, and ONLY IF, together they point at a clear recurring OPERATIONAL
specialization worth capturing as ONE new focused skill, author it. Otherwise author nothing.

Return STRICT JSON and nothing else:
{"skill_id": "<lowercase-hyphenated, <=48 chars>", "display_name": "<short name>",
 "description": "<one line: what this skill is for>",
 "body": "<the operational guide in markdown, <=3000 chars>"}

The body must be OPERATIONAL GUIDANCE ONLY — steps, heuristics, when to do what. Do NOT include
tool-call syntax, URLs, executable code, secrets, or instructions to ignore rules or lift safety
gates (such content is automatically rejected). If there is no clear recurring specialization,
return exactly {"skill_id": ""}."""

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SELF_PREFIX = "self-"
_MAX_BODY = 3000
_MAX_DESC = 500


# ── cadence marker (per-persona, best-effort) ────────────────────────────────


def _marker_path(persona: str):
    from brain.persona_key import persona_state_root

    return persona_state_root(persona) / "node_authoring.json"


def _load_marker(persona: str) -> dict:
    try:
        p = _marker_path(persona)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_marker(persona: str, ts: float, count: int) -> None:
    try:
        p = _marker_path(persona)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_ts": ts, "count": count}), encoding="utf-8")
    except Exception:
        pass


# ── evidence + proposal parsing ──────────────────────────────────────────────


def _proven_cluster_evidence(wiring, skills_by_id: dict) -> list[str]:
    """Deterministic evidence: proven fragment skills across the fixed drafters (weight ≥ the
    promote threshold), as 'id: description' lines. Descriptions come from already-screened
    skills only — no conversation text — so authoring cannot leak raw user data."""
    from brain.fragment_pool import is_admissible

    promote = float(settings.get("node_promote_threshold", 2.2))
    seen: dict[str, str] = {}
    for i in range(5):
        host = f"frontal.drafter_{chr(65 + i)}"
        for sid, w in wiring.attached_fragments(host):
            if w >= promote and is_admissible(sid, host) and sid not in seen:
                seen[sid] = (skills_by_id.get(sid) or "").strip()
    return [f"- {sid}: {desc}" if desc else f"- {sid}" for sid, desc in seen.items()]


def _normalize_id(raw_id: str) -> str:
    """Slugify the architect's id and force the `self-` provenance prefix; '' if unusable."""
    slug = _SLUG_RE.sub("-", str(raw_id or "").lower()).strip("-")
    if not slug:
        return ""
    if not slug.startswith(_SELF_PREFIX):
        slug = _SELF_PREFIX + slug
    slug = slug[:64]
    return slug if _ID_RE.match(slug) else ""


def _parse_proposal(raw: str) -> dict | None:
    """Parse + validate the architect's JSON. Returns a clean proposal or None."""
    from brain.utils import safe_json_parse

    data = safe_json_parse(raw) or {}
    if not isinstance(data, dict):
        return None
    sid = _normalize_id(data.get("skill_id", ""))
    body = str(data.get("body") or "").strip()
    desc = str(data.get("description") or "").strip()[:_MAX_DESC]
    if not sid or not body or len(body) > _MAX_BODY:
        return None
    return {
        "skill_id": sid,
        "display_name": str(data.get("display_name") or sid).strip()[:120],
        "description": desc,
        "body": body,
    }


# ── the pass ─────────────────────────────────────────────────────────────────


async def author_and_admit(
    persona: str,
    *,
    session_id: str,
    wiring,
    architect_cell,
    screener,
    trace_count: int,
) -> dict | None:
    """Author one candidate specialization for `persona` and admit it through the screener.

    Returns {skill_id, status} when a proposal was submitted (status ∈ enabled|flagged|rejected),
    or None when gated out / nothing authored. Fail-open: never raises. Auto-live only on the
    screener's clean `enabled`; anything else lands in the owner review queue."""
    if not settings.get("node_self_authoring", 1):
        return None
    try:
        from brain import skills_registry as sr
    except Exception:
        return None
    # Supabase-only. Companion/local → live_skills raises → no-op.
    try:
        skills = sr.live_skills()
    except Exception:
        return None

    # Cadence gates: enough new traces, min hours since the last authored proposal, per-persona cap.
    if trace_count < int(settings.get("node_author_min_traces", 20)):
        return None
    marker = _load_marker(persona)
    now = time.time()
    min_hours = float(settings.get("node_author_min_hours", 24.0))
    if marker.get("last_ts") and (now - float(marker["last_ts"])) < min_hours * 3600.0:
        return None
    if int(marker.get("count", 0)) >= int(settings.get("node_author_max_per_persona", 5)):
        return None

    skills_by_id = {s.get("id"): s.get("description", "") for s in skills}
    evidence = _proven_cluster_evidence(wiring, skills_by_id)
    if len(evidence) < max(1, int(settings.get("node_promote_min_cluster", 2))):
        return None

    # Draft (local model → zero cloud cost).
    architect_cell.reset_turn(f"sleep_{session_id}_author")
    raw = await architect_cell.call(
        [{"role": "user", "content": "Proven approaches for this user:\n" + "\n".join(evidence)}]
    )
    proposal = _parse_proposal(raw)
    if not proposal:
        return None
    sid = proposal["skill_id"]
    if sid in skills_by_id:  # already exists → don't re-author
        return None

    # Admit through the screener — the SAME untrusted-submission sequence as the engine API
    # (stage → screen → set_status). Auto-live ONLY on the clean `enabled` verdict.
    from brain.observability.decisions import decisions

    try:
        sr.stage_skill(
            sid,
            proposal["body"],
            proposal["description"],
            display_name=proposal["display_name"],
            tier=2,
            submitted_by="brain",  # provenance: grouped as self-authored in the Skills UI
        )
        verdict = await screener.screen(sid, proposal["body"], proposal["description"])
        status = str(verdict.get("status") or "flagged")
        sr.set_status(sid, status, screen_notes=verdict.get("notes"))
        if status == "enabled":
            # Org-scoped and live on next warm. (Per-persona agent mapping is a later refinement;
            # org-wide is safe — skills are keyed by org_id, so no cross-tenant leak.)
            try:
                sr.set_skill_all_agents(sid, True)
            except Exception:
                pass
        decisions.log(
            "node_self_authored",
            session_id=session_id,
            persona=str(persona),
            skill_id=sid,
            status=status,
            evidence_count=len(evidence),
        )
    except Exception as e:
        logger.warning("[NodeAuthoring] admit failed for %s: %s", sid, e)
        return None

    _save_marker(persona, now, int(marker.get("count", 0)) + 1)
    return {"skill_id": sid, "status": status}
