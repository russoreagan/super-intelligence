"""Persona ownership binding — who a persona belongs to in an ISOLATED org.

In an isolated org (organizations.learning_mode = 'isolated', brain/org_settings.py)
"one persona per purchase" is meant to be structural, not partner discipline: the
FIRST end_user_id to open a session on a persona owns it, and a session for any
other end user on that persona is refused with the same 404 an unknown agent gets
(brain/api/server.py POST /v1/sessions). The isolation audit can then assert a
persona's state was only ever shaped by one person.

Same first-writer-wins shape as brain/api/end_users.py: insert-if-absent, then read
back. Never an upsert — an upsert would let a second end user take the persona.

Exempt: the org's home persona (it is the org's own agent, shared by design) and
owner keys (the org inspecting its own personas). Consolidated orgs never enforce;
their personas are shared by design. Rows are kept across a switch back to
consolidated (no longer enforced) and removed by the persona hard purge.

Pre-migration (table absent) every call degrades to "no registry": claim() returns
None, binding is not enforced, and the switch response reports it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TABLE = "persona_owners"


def _sb():
    from brain.second_brain import supabase_client

    try:
        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception as e:  # pragma: no cover - deployment shape
        logger.warning("[persona_owners] backend unavailable: %s", e)
        return None


def _slug(persona: str) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(persona)


def lookup(persona: str) -> dict | None:
    """The ownership row, or None when unowned / no backend / table missing."""
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        res = (
            client.table(TABLE)
            .select("*")
            .eq("org_id", org)
            .eq("persona", _slug(persona))
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.debug("[persona_owners] lookup failed for %s: %s", persona, e)
        return None
    rows = res.data or []
    return dict(rows[0]) if rows else None


def owner_of(persona: str) -> str | None:
    """The end_user_id that owns the persona, or None."""
    row = lookup(persona)
    return str(row.get("end_user_id")) if row and row.get("end_user_id") else None


def claim(persona: str, end_user_id: str) -> str | None:
    """Record end_user_id as the persona's owner if nobody owns it yet; return the
    owner now on record (which may be someone else). None when the registry is
    unavailable (no backend, or migration 037 not applied) — callers then do NOT
    enforce, and say so."""
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    row = {"org_id": org, "persona": _slug(persona), "end_user_id": end_user_id}
    try:
        client.table(TABLE).upsert(
            row, on_conflict="org_id,persona", ignore_duplicates=True
        ).execute()
    except Exception as e:
        logger.warning(
            "[persona_owners] claim failed for %s (%s) — binding not enforced; "
            "apply migration 037_org_learning_mode",
            persona,
            e,
        )
        return None
    return owner_of(persona)


def forget(persona: str) -> int:
    """Remove the ownership row (persona hard purge). Returns rows removed."""
    sb = _sb()
    if sb is None:
        return 0
    client, org = sb
    try:
        res = client.table(TABLE).delete().eq("org_id", org).eq("persona", _slug(persona)).execute()
        return len(res.data or [])
    except Exception as e:
        logger.debug("[persona_owners] forget failed for %s: %s", persona, e)
        return 0


def registry_available() -> bool:
    """True when the table answers (migration 037 applied). Used by the switch
    response to say whether binding is actually enforceable."""
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        client.table(TABLE).select("persona").eq("org_id", org).limit(1).execute()
        return True
    except Exception:
        return False


def multi_owner_personas() -> list[dict]:
    """Personas with MORE than one distinct end_user_id in api_sessions — they
    cannot be bound to a single owner and are reported by the switch as
    "multi-owner, cannot be bound". The home persona is excluded (shared by design)."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    try:
        res = (
            client.table("api_sessions").select("agent_id, end_user_id").eq("org_id", org).execute()
        )
    except Exception as e:
        logger.debug("[persona_owners] api_sessions scan failed: %s", e)
        return []
    from brain import org_settings

    users: dict[str, set[str]] = {}
    for r in res.data or []:
        aid = str(r.get("agent_id") or "")
        eu = str(r.get("end_user_id") or "")
        if "." not in aid or not eu:
            continue
        persona = _slug(aid.split(".", 1)[0])
        if not persona or org_settings.is_home(persona):
            continue
        users.setdefault(persona, set()).add(eu)
    return [
        {"persona": p, "end_users": len(u), "status": "multi-owner, cannot be bound"}
        for p, u in sorted(users.items())
        if len(u) > 1
    ]
