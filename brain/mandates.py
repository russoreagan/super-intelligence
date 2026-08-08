"""
Mandate library + persona assignments — the org's role catalog, sourced from the
database.

A mandate is DATA, not prompt text: a row in the `mandates` table keyed
(org_id, id) carrying the role text plus conduct rules and reward weights (see
008_mandate_assignments.sql). Mandates are ORG-LEVEL — authored once, then
assigned to any persona via the `agents` table (the persona↔role pairing — each
row is an agent; see brain/agents.py and 009_agents.sql). The prompt
layer (persona_context) renders the active persona's assigned catalog once into
the cached context block and names the active id per turn; this module is the
bridge that loads {id: role_text} for it and the CRUD used by the engine API and
settings UI.

The catalog is loaded once per process (small and static by design — what varies
per customer is WHICH mandate applies, not the roster). refresh() drops the cache
after any edit/assignment change so the next turn rebuilds the cached context.

Companion mode / local dev: no Supabase → empty catalog → the mandate blocks
render to "" and nothing changes. CRUD raises MandateError in that mode.
"""

from __future__ import annotations

import json
import logging

from brain import ids as _ids

logger = logging.getLogger(__name__)

# A mandate id is a partner-/admin-chosen slug, also used as the per-turn
# selector value and stamped onto episodes/tasks — keep it URL/log safe.
# Shape shared with skill ids; the message and exception type stay local.
MANDATE_ID_RE = _ids.ID_RE

# The ASSIGNED catalog rides every cached context block, so its size is a
# recurring prompt-cache cost. Cap per-mandate text, per-persona count, and the
# combined assigned text.
MAX_ROLE_TEXT_CHARS = 16_000
MAX_ASSIGNED_PER_PERSONA = 16
MAX_CATALOG_CHARS = 24_000
_MAX_JSON_BYTES = 2_048  # conduct_rules / reward_weights are opaque-but-bounded

# Catalog cache keyed by persona_key (Path B): one process can serve many personas,
# each with its own byte-stable assigned-mandate catalog (so the cached prompt block
# stays cache-friendly per persona). Empty dict = nothing loaded yet.
_catalog: dict[str, dict[str, dict]] = {}


class MandateError(Exception):
    """Validation / availability failure — callers map this to HTTP 400/503."""


# ── live catalog (read by the prompt layer) ───────────────────────────────────


def catalog() -> dict[str, dict]:
    """Return {mandate_id: {"text": role_text, "conduct": conduct_rules, "weights":
    reward_weights}} for the active (org, persona), cached per persona (Path B)."""
    persona = _active_persona()
    cached = _catalog.get(persona)
    if cached is None:
        cached = _catalog[persona] = _load()
    return cached


def refresh() -> dict[str, dict]:
    """Drop the cache (every persona) and reload the active one — call after any
    catalog or assignment edit so the next turn rebuilds the cached context block."""
    _catalog.clear()
    return catalog()


def _load() -> dict[str, dict]:
    try:
        sb, org = _sb()
        persona = _active_persona()
        assigns = (
            sb.table("agents")
            .select("mandate_id, sort_order")
            .eq("org_id", org)
            .eq("persona", persona)
            .eq("enabled", True)
            .order("sort_order")
            .order("mandate_id")
            .execute()
        )
        rows = assigns.data or []
        ids = [str(r["mandate_id"]) for r in rows if r.get("mandate_id")]
        if not ids:
            return {}
        lib = (
            sb.table("mandates")
            .select("id, role_text, conduct_rules, reward_weights")
            .eq("org_id", org)
            .in_("id", ids)
            .eq("active", True)
            .execute()
        )
        entries = {
            str(r["id"]): {
                "text": str(r.get("role_text") or ""),
                "conduct": r.get("conduct_rules") or None,
                "weights": r.get("reward_weights") or {},
            }
            for r in (lib.data or [])
        }
        # Preserve assignment order; drop ids whose library row is missing/inactive.
        cat = {i: entries[i] for i in ids if i in entries}
        if cat:
            logger.info("[Mandates] Loaded %d mandate(s) for persona %s", len(cat), persona)
        return cat
    except MandateError:
        return {}
    except Exception as e:
        logger.debug("[Mandates] Catalog load skipped: %s", e)
        return {}


# ── library CRUD (org-level) ──────────────────────────────────────────────────


def list_mandates(include_inactive: bool = False) -> list[dict]:
    """Full library rows for the org, ordered by id. Active-only unless asked."""
    sb, org = _sb()
    q = (
        sb.table("mandates")
        .select("id, role_text, conduct_rules, reward_weights, version, active, updated_at")
        .eq("org_id", org)
    )
    if not include_inactive:
        q = q.eq("active", True)
    res = q.order("id").execute()
    return list(res.data or [])


def upsert_mandate(
    mandate_id: str,
    role_text: str,
    conduct_rules: dict | None = None,
    reward_weights: dict | None = None,
) -> dict:
    """Create or update a library mandate. Bumps version on every edit and revives
    a deactivated mandate (re-saving an id brings it back). reward_weights is a
    per-dimension multiplier consumed by neuron.reward_weight() via the ambient
    turn's mandate; conduct_rules is stored but not yet consumed."""
    sb, org = _sb()
    mid = _valid_id(mandate_id)
    text = str(role_text or "")
    if len(text) > MAX_ROLE_TEXT_CHARS:
        raise MandateError(f"role_text exceeds {MAX_ROLE_TEXT_CHARS} chars ({len(text)})")
    cr = _valid_json("conduct_rules", conduct_rules)
    rw = _valid_reward_weights(reward_weights)

    # Read-then-write version bump. One process per org and a single editor in
    # practice, so the race window is acceptable.
    existing = sb.table("mandates").select("version").eq("org_id", org).eq("id", mid).execute()
    prior = existing.data or []
    row = {
        "org_id": org,
        "id": mid,
        "role_text": text,
        "conduct_rules": cr,
        "reward_weights": rw,
        "active": True,
        "version": (int(prior[0].get("version") or 0) + 1) if prior else 1,
    }
    sb.table("mandates").upsert(row, on_conflict="org_id,id").execute()
    refresh()
    return row


def deactivate_mandate(mandate_id: str) -> bool:
    """Soft-delete a library mandate (never hard-delete — episodes/tasks reference
    the id). Returns False if the id wasn't found."""
    sb, org = _sb()
    mid = _valid_id(mandate_id)
    existing = sb.table("mandates").select("version").eq("org_id", org).eq("id", mid).execute()
    prior = existing.data or []
    if not prior:
        return False
    sb.table("mandates").update(
        {"active": False, "version": int(prior[0].get("version") or 0) + 1}
    ).eq("org_id", org).eq("id", mid).execute()
    refresh()
    return True


# ── assignment CRUD (the persona↔mandate pairing = an agent row) ──────────────


def list_assignments(persona: str | None = None) -> list[dict]:
    """Assignment rows for a persona (defaults to the active one)."""
    sb, org = _sb()
    p = _persona(persona)
    res = (
        sb.table("agents")
        .select("mandate_id, enabled, sort_order, overrides, updated_at")
        .eq("org_id", org)
        .eq("persona", p)
        .order("sort_order")
        .order("mandate_id")
        .execute()
    )
    return list(res.data or [])


def list_all_assignments() -> list[dict]:
    """Every pairing row for the org, across all personas — the full role↔persona
    map. Roles are org-level and many-to-many with personas, so the management UI
    needs the whole matrix, not one persona's slice."""
    sb, org = _sb()
    res = (
        sb.table("agents")
        .select("persona, mandate_id, enabled, sort_order")
        .eq("org_id", org)
        .order("mandate_id")
        .execute()
    )
    return list(res.data or [])


def assign(persona: str | None, mandate_id: str, sort_order: int = 0) -> dict:
    """Assign a library mandate to a persona (enabling an existing assignment if
    present). Validates the mandate exists + is active and enforces the per-persona
    count and combined-text caps."""
    sb, org = _sb()
    p = _persona(persona)
    mid = _valid_id(mandate_id)

    lib = sb.table("mandates").select("role_text, active").eq("org_id", org).eq("id", mid).execute()
    librow = lib.data or []
    if not librow or not librow[0].get("active"):
        raise MandateError(f"mandate '{mid}' does not exist or is inactive")

    # Current enabled assignments (excluding this id) → enforce caps.
    cur = (
        sb.table("agents")
        .select("mandate_id")
        .eq("org_id", org)
        .eq("persona", p)
        .eq("enabled", True)
        .execute()
    )
    other_ids = [str(r["mandate_id"]) for r in (cur.data or []) if str(r["mandate_id"]) != mid]
    if len(other_ids) + 1 > MAX_ASSIGNED_PER_PERSONA:
        raise MandateError(
            f"persona already has the maximum {MAX_ASSIGNED_PER_PERSONA} assigned mandates"
        )
    total = len(str(librow[0].get("role_text") or ""))
    if other_ids:
        others = (
            sb.table("mandates")
            .select("role_text")
            .eq("org_id", org)
            .in_("id", other_ids)
            .eq("active", True)
            .execute()
        )
        total += sum(len(str(r.get("role_text") or "")) for r in (others.data or []))
    if total > MAX_CATALOG_CHARS:
        raise MandateError(f"assigned catalog would exceed {MAX_CATALOG_CHARS} chars ({total})")

    row = {
        "org_id": org,
        "persona": p,
        "mandate_id": mid,
        "enabled": True,
        "sort_order": int(sort_order),
    }
    sb.table("agents").upsert(row, on_conflict="org_id,persona,mandate_id").execute()
    refresh()
    return row


def unassign(persona: str | None, mandate_id: str) -> bool:
    """Remove a persona's assignment (hard-delete the pairing row). Returns False if
    no such assignment existed."""
    sb, org = _sb()
    p = _persona(persona)
    mid = _valid_id(mandate_id)
    existing = (
        sb.table("agents")
        .select("mandate_id")
        .eq("org_id", org)
        .eq("persona", p)
        .eq("mandate_id", mid)
        .execute()
    )
    if not (existing.data or []):
        return False
    sb.table("agents").delete().eq("org_id", org).eq("persona", p).eq("mandate_id", mid).execute()
    refresh()
    return True


# ── internals ─────────────────────────────────────────────────────────────────


def _sb():
    """(client, org_id) or raise MandateError when the Supabase backend is off."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        raise MandateError("mandates require the Supabase storage backend")
    return supabase_client.get_client(), supabase_client.get_org_id()


def _active_persona() -> str:
    from brain.second_brain.store import _persona_key, _resolve_persona

    return _persona_key(_resolve_persona(""))


def _persona(persona: str | None) -> str:
    from brain.second_brain.store import _persona_key, _resolve_persona

    return _persona_key(_resolve_persona(persona or ""))


def _valid_id(mandate_id: str) -> str:
    mid = str(mandate_id or "").strip()
    if not MANDATE_ID_RE.match(mid):
        raise MandateError(
            "mandate id must be 1-64 chars of lowercase letters, digits, '_' or '-' "
            "and start with a letter or digit"
        )
    return mid


def _valid_json(field: str, value: dict | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MandateError(f"{field} must be a JSON object")
    if len(json.dumps(value)) > _MAX_JSON_BYTES:
        raise MandateError(f"{field} exceeds {_MAX_JSON_BYTES} bytes")
    return value


# The 7 reward dimensions a persona can be valued on — source of truth is the
# comment at neuron.py:326-329; kept duplicated here (not imported) to avoid a
# mandates<->neuron import cycle at module load.
_REWARD_SOURCES = {
    "correctness",
    "connection",
    "novelty",
    "aesthetic",
    "relief",
    "mastery",
    "levity",
}
_MANDATE_WEIGHT_MIN, _MANDATE_WEIGHT_MAX = 0.1, 3.0


def _valid_reward_weights(value: dict | None) -> dict:
    """Clip each dimension to [_MANDATE_WEIGHT_MIN, _MANDATE_WEIGHT_MAX]; silently
    drop unknown dimension keys and non-numeric values rather than raising, since
    this is a tunable dial, not a structural contract."""
    raw = _valid_json("reward_weights", value)
    out: dict[str, float] = {}
    for k, v in raw.items():
        if k not in _REWARD_SOURCES:
            continue
        try:
            out[k] = max(_MANDATE_WEIGHT_MIN, min(_MANDATE_WEIGHT_MAX, float(v)))
        except (TypeError, ValueError):
            continue
    return out
