"""
Mandate catalog — the partner's assignment roster, sourced from the database.

A mandate is DATA, not prompt text: a row in the `mandates` table keyed
(org_id, persona, id) carrying the role text plus conduct rules and reward
weights (see 007_org_schema_reset.sql). The prompt layer (persona_context)
renders the catalog once into the cached context block and names the active id
per turn; this module is the bridge that loads {id: role_text} for it.

Loaded once per process (the catalog is small and static by design — what
varies per customer is WHICH mandate applies, not the roster). refresh() exists
for the engine admin path after a partner edits their catalog.

Companion mode / local dev: no Supabase or no rows → empty catalog → the
mandate blocks render to "" and nothing changes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_catalog: dict[str, str] | None = None


def catalog() -> dict[str, str]:
    """Return {mandate_id: role_text} for the active (org, persona), cached."""
    global _catalog
    if _catalog is None:
        _catalog = _load()
    return _catalog


def refresh() -> dict[str, str]:
    """Drop the cache and reload — for the admin path after catalog edits."""
    global _catalog
    _catalog = None
    return catalog()


def _load() -> dict[str, str]:
    try:
        from brain.second_brain import supabase_client
        from brain.second_brain.store import _persona_key, _resolve_persona

        if not supabase_client.is_enabled():
            return {}
        sb = supabase_client.get_client()
        org = supabase_client.get_org_id()
        persona = _persona_key(_resolve_persona(""))
        res = (
            sb.table("mandates")
            .select("id, role_text")
            .eq("org_id", org)
            .eq("persona", persona)
            .eq("active", True)
            .execute()
        )
        rows = res.data or []
        cat = {str(r["id"]): str(r.get("role_text") or "") for r in rows if r.get("id")}
        if cat:
            logger.info("[Mandates] Loaded %d mandate(s) for persona %s", len(cat), persona)
        return cat
    except Exception as e:
        logger.debug("[Mandates] Catalog load skipped: %s", e)
        return {}
