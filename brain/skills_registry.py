"""
App-provided skills library — the org's registry of partner-supplied capability
docs, sourced from the database (see 019_skills.sql).

A skill is DATA, like a mandate: a row in the `skills` table keyed (org_id, id)
carrying a description (what the SkillSelector matches/embeds on) and a markdown
body (instructions injected, fenced, when the skill is selected). Skills are
ORG-LEVEL — authored once by a B2B admin over the engine API, screened, then
selected per turn by the SkillSelector (or pinned per session).

Two halves, deliberately split:
  • This module is pure CRUD + the lifecycle state machine. It NEVER runs the
    screener or the LLM — it just records the verdict a caller hands it. That keeps
    it dependency-free (no router) and import-safe, exactly like brain/mandates.py.
  • brain/skills_screener.py owns the admission decision (static + LLM judge).
  • brain/clusters/skill_selector.warm_partner_skills() consumes live_skills() to
    inject the cleared bodies into the live index.

Injection invariant: only `approved_body` is ever served, and only while active.
`body` is the latest submission (maybe still being screened); `approved_body` is
the last body a human/LLM cleared. On edit the prior approved_body stays live until
the new one clears (the TOCTOU guard). live_skills() returns the approved bodies.

Companion / local dev: no Supabase → empty library → nothing injects. CRUD raises
SkillError in that mode (the API maps it to 503).
"""

from __future__ import annotations

import datetime as _dt
import logging

from brain import ids as _ids

logger = logging.getLogger(__name__)

# A skill id is a partner-/admin-chosen slug, also the per-turn selector / pin value
# and the index name — keep it URL/log safe and collision-resistant.
# Shape shared with mandate ids; the message and exception type stay local.
SKILL_ID_RE = _ids.ID_RE

# The cleared body is fenced into the cached context on every turn the skill is
# active, so its size is a recurring prompt cost; the drafter injection truncates at
# ~6k. Cap the body at the deterministic screener's input ceiling so a legitimate
# skill never trips the length check (security.MAX_INPUT_LENGTH == 8000).
MAX_BODY_CHARS = 8_000
MAX_DESCRIPTION_CHARS = 1_000
MAX_KEYWORDS = 24
MAX_ALLOWED_TOOLS = 32

VALID_STATUSES = ("pending", "enabled", "flagged", "rejected")


class SkillError(Exception):
    """Validation / availability failure — the API maps this to HTTP 400/503."""


# ── live library (read by the SkillSelector at warm time) ──────────────────────


def live_skills() -> list[dict]:
    """The org's injectable skills: active, with a cleared (approved) body. Returns
    [{id, display_name, description, body, keywords, allowed_tools, tier, all_agents,
    agents}] where ``body`` is the APPROVED body (never the unscreened latest
    submission), ``all_agents`` is True for org-wide skills, and ``agents`` is the list
    of mapped agent_ids ('persona.mandate_id') used when all_agents is False."""
    sb, org = _sb()
    res = (
        sb.table("skills")
        .select(
            "id, display_name, description, approved_body, keywords, allowed_tools, tier, all_agents"
        )
        .eq("org_id", org)
        .eq("active", True)
        .not_.is_("approved_body", "null")
        .order("id")
        .execute()
    )
    mapping = _agent_skill_map(sb, org)
    out: list[dict] = []
    for r in res.data or []:
        body = str(r.get("approved_body") or "").strip()
        if not body:
            continue
        sid = str(r["id"])
        out.append(
            {
                "id": sid,
                "display_name": r.get("display_name"),
                "description": str(r.get("description") or ""),
                "body": body,
                "keywords": list(r.get("keywords") or []),
                "allowed_tools": list(r.get("allowed_tools") or []),
                "tier": int(r.get("tier") or 2),
                "all_agents": bool(r.get("all_agents", True)),
                "agents": mapping.get(sid, []),
            }
        )
    if out:
        logger.info("[Skills] %d live partner skill(s) for org", len(out))
    return out


# ── library CRUD (org-level) ───────────────────────────────────────────────────


def list_skills(include_inactive: bool = False, status: str | None = None) -> list[dict]:
    """Full library rows for the org (admin view), ordered by id. Active-only unless
    asked. Never returns the in-flight `body`/`approved_body` text — just metadata +
    status + screen_notes, which is what a management UI needs."""
    sb, org = _sb()
    q = (
        sb.table("skills")
        .select(
            "id, display_name, description, keywords, allowed_tools, tier, status, all_agents, "
            "screen_notes, submitted_by, reviewed_by, reviewed_at, version, active, updated_at"
        )
        .eq("org_id", org)
    )
    if not include_inactive:
        q = q.eq("active", True)
    if status is not None:
        q = q.eq("status", _valid_status(status))
    rows = list(q.order("id").execute().data or [])
    mapping = _agent_skill_map(sb, org)
    for r in rows:
        r["agents"] = mapping.get(str(r.get("id")), [])
    return rows


def get_skill(skill_id: str) -> dict | None:
    """One full row including the body text (for review). None if not found."""
    sb, org = _sb()
    sid = _valid_id(skill_id)
    res = sb.table("skills").select("*").eq("org_id", org).eq("id", sid).execute()
    rows = res.data or []
    return rows[0] if rows else None


def list_flagged() -> list[dict]:
    """The superadmin review queue: skills the screener couldn't auto-clear. Includes
    the submitted `body` and `screen_notes` so a human can judge."""
    sb, org = _sb()
    res = (
        sb.table("skills")
        .select(
            "id, display_name, description, body, keywords, allowed_tools, tier, "
            "screen_notes, submitted_by, version, updated_at"
        )
        .eq("org_id", org)
        .eq("status", "flagged")
        .eq("active", True)
        .order("updated_at")
        .execute()
    )
    return list(res.data or [])


def stage_skill(
    skill_id: str,
    body: str,
    description: str = "",
    *,
    display_name: str | None = None,
    keywords: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    tier: int = 2,
    submitted_by: str | None = None,
) -> dict:
    """Create or re-submit a skill. Writes the new body with status='pending', bumps
    version, and PRESERVES the prior approved_body (so an already-approved skill keeps
    serving its cleared body while this submission is screened — the TOCTOU guard).
    Returns the staged row. The caller then runs the screener and calls set_status."""
    sb, org = _sb()
    sid = _valid_id(skill_id)
    text = str(body or "")
    if len(text) > MAX_BODY_CHARS:
        raise SkillError(f"body exceeds {MAX_BODY_CHARS} chars ({len(text)})")
    desc = str(description or "")
    if len(desc) > MAX_DESCRIPTION_CHARS:
        raise SkillError(f"description exceeds {MAX_DESCRIPTION_CHARS} chars ({len(desc)})")
    kw = _valid_str_list("keywords", keywords, MAX_KEYWORDS)
    tools = _valid_str_list("allowed_tools", allowed_tools, MAX_ALLOWED_TOOLS)

    existing = (
        sb.table("skills")
        .select("version, approved_body")
        .eq("org_id", org)
        .eq("id", sid)
        .execute()
    )
    prior = existing.data or []
    row = {
        "org_id": org,
        "id": sid,
        "display_name": (display_name or None),
        "description": desc,
        "body": text,
        # Keep the prior cleared body live; a fresh skill has none yet.
        "approved_body": (prior[0].get("approved_body") if prior else None),
        "keywords": kw,
        "allowed_tools": tools,
        "tier": int(tier or 2),
        "status": "pending",
        "submitted_by": (submitted_by or None),
        "active": True,
        "version": (int(prior[0].get("version") or 0) + 1) if prior else 1,
        "updated_at": _now(),
    }
    sb.table("skills").upsert(row, on_conflict="org_id,id").execute()
    return row


def set_status(
    skill_id: str,
    status: str,
    *,
    screen_notes: dict | None = None,
    reviewed_by: str | None = None,
) -> dict:
    """Record an admission verdict. 'enabled' promotes the submitted body to
    approved_body (it goes live on the next warm); 'flagged'/'rejected' leave the
    prior approved_body untouched (a previously-cleared version keeps serving). Returns
    the updated row. Raises if the skill doesn't exist."""
    sb, org = _sb()
    sid = _valid_id(skill_id)
    st = _valid_status(status)
    cur = sb.table("skills").select("body").eq("org_id", org).eq("id", sid).execute()
    rows = cur.data or []
    if not rows:
        raise SkillError(f"unknown skill id '{sid}'")
    patch: dict = {"status": st, "updated_at": _now()}
    if screen_notes is not None:
        patch["screen_notes"] = screen_notes
    if st == "enabled":
        # Promote the just-screened body to the live (approved) one.
        patch["approved_body"] = str(rows[0].get("body") or "")
    if reviewed_by is not None or st in ("enabled", "rejected"):
        patch["reviewed_by"] = reviewed_by
        patch["reviewed_at"] = _now()
    sb.table("skills").update(patch).eq("org_id", org).eq("id", sid).execute()
    return {"id": sid, **patch}


def delete_skill(skill_id: str) -> bool:
    """Soft-delete a skill (active=false → drops out of live_skills on next warm).
    Never hard-delete: episodes/logs may reference the id. False if not found."""
    sb, org = _sb()
    sid = _valid_id(skill_id)
    existing = sb.table("skills").select("id").eq("org_id", org).eq("id", sid).execute()
    if not (existing.data or []):
        return False
    sb.table("skills").update({"active": False, "updated_at": _now()}).eq("org_id", org).eq(
        "id", sid
    ).execute()
    return True


# ── skill ↔ agent mapping ───────────────────────────────────────────────────────


def set_skill_all_agents(skill_id: str, all_agents: bool) -> dict:
    """Set whether a skill applies to every agent (True) or only its mapped agents
    (False). Returns {id, all_agents}."""
    sb, org = _sb()
    sid = _valid_id(skill_id)
    existing = sb.table("skills").select("id").eq("org_id", org).eq("id", sid).execute()
    if not (existing.data or []):
        raise SkillError(f"unknown skill id '{sid}'")
    sb.table("skills").update({"all_agents": bool(all_agents), "updated_at": _now()}).eq(
        "org_id", org
    ).eq("id", sid).execute()
    return {"id": sid, "all_agents": bool(all_agents)}


def set_skill_agents(skill_id: str, agent_ids: list[str]) -> dict:
    """Replace the set of agents a skill is mapped to (skill-centric editing). Each
    agent_id is 'persona.mandate_id'. Does NOT change all_agents — pair with
    set_skill_all_agents(False) to make the mapping take effect."""
    sb, org = _sb()
    sid = _valid_id(skill_id)
    pairs = [_split_agent(a) for a in (agent_ids or [])]
    sb.table("agent_skills").delete().eq("org_id", org).eq("skill_id", sid).execute()
    if pairs:
        rows = [{"org_id": org, "persona": p, "mandate_id": m, "skill_id": sid} for (p, m) in pairs]
        sb.table("agent_skills").insert(rows).execute()
    return {"id": sid, "agents": [f"{p}.{m}" for (p, m) in pairs]}


def agent_skill_ids(persona: str, mandate_id: str) -> list[str]:
    """The skill ids explicitly mapped to one agent (excludes all_agents skills)."""
    from brain.mandates import _persona

    sb, org = _sb()
    p = _persona(persona)
    mid = _valid_id(mandate_id)
    res = (
        sb.table("agent_skills")
        .select("skill_id")
        .eq("org_id", org)
        .eq("persona", p)
        .eq("mandate_id", mid)
        .execute()
    )
    return [str(r["skill_id"]) for r in (res.data or [])]


def set_agent_skills(persona: str, mandate_id: str, skill_ids: list[str]) -> dict:
    """Replace the set of (specific-scope) skills mapped to one agent (agent-centric
    editing — used by the new-agent flow). all_agents skills are not listed here."""
    from brain.mandates import _persona

    sb, org = _sb()
    p = _persona(persona)
    mid = _valid_id(mandate_id)
    ids = [_valid_id(s) for s in (skill_ids or [])]
    sb.table("agent_skills").delete().eq("org_id", org).eq("persona", p).eq(
        "mandate_id", mid
    ).execute()
    if ids:
        rows = [{"org_id": org, "persona": p, "mandate_id": mid, "skill_id": s} for s in ids]
        sb.table("agent_skills").insert(rows).execute()
    return {"agent_id": f"{p}.{mid}", "skills": ids}


def _agent_skill_map(sb, org) -> dict[str, list[str]]:
    """{skill_id: ['persona.mandate_id', ...]} for the org (best-effort)."""
    try:
        res = (
            sb.table("agent_skills")
            .select("persona, mandate_id, skill_id")
            .eq("org_id", org)
            .execute()
        )
    except Exception as e:
        logger.debug("[Skills] agent_skills load skipped: %s", e)
        return {}
    out: dict[str, list[str]] = {}
    for r in res.data or []:
        skid = r.get("skill_id")
        if not skid:
            continue
        out.setdefault(str(skid), []).append(f"{r.get('persona')}.{r.get('mandate_id')}")
    return out


def _split_agent(agent_id: str) -> tuple[str, str]:
    s = str(agent_id or "").strip()
    if "." not in s:
        raise SkillError("agent_id must be '<persona>.<mandate_id>'")
    persona, mandate_id = s.split(".", 1)
    if not persona or not mandate_id:
        raise SkillError(f"malformed agent_id '{agent_id}'")
    return persona, mandate_id


# ── internals ──────────────────────────────────────────────────────────────────


def _sb():
    """(client, org_id) or raise SkillError when the Supabase backend is off."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        raise SkillError("skills require the Supabase storage backend")
    return supabase_client.get_client(), supabase_client.get_org_id()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _valid_id(skill_id: str) -> str:
    sid = str(skill_id or "").strip()
    if not SKILL_ID_RE.match(sid):
        raise SkillError(
            "skill id must be 1-64 chars of lowercase letters, digits, '_' or '-' "
            "and start with a letter or digit"
        )
    return sid


def _valid_status(status: str) -> str:
    st = str(status or "").strip().lower()
    if st not in VALID_STATUSES:
        raise SkillError(f"status must be one of {VALID_STATUSES}")
    return st


def _valid_str_list(field: str, value: list[str] | None, cap: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise SkillError(f"{field} must be a list of strings")
    cleaned = [v.strip() for v in value if v.strip()]
    if len(cleaned) > cap:
        raise SkillError(f"{field} exceeds {cap} entries")
    return cleaned
