"""
Runtime API-key auth for the engine surface.

A partner's backend authenticates with a bearer key — the *runtime* credential
(distinct from the admin console login). It can open sessions and run turns, but
nothing else. Fail-closed: if no keys are configured, every request is denied, so
an accidentally-exposed server is not open by default.

Two key kinds:
  • The ORG OWNER key — BRAIN_API_KEYS / BRAIN_API_KEY env or the ``api_keys``
    setting. Plaintext compare. Full access (partner_id = None, owner = True).
  • PER-PARTNER keys — rows in the ``api_keys`` table (011), each mapped to a
    partner_id. Only the SHA-256 hash is stored; the token is shown once at mint.
    Scoped: a partner can only drive sessions it opened.

``resolve_partner`` returns the partner context for a bearer token; ``check_bearer``
is the boolean gate built on it (so both key kinds authenticate).
"""

from __future__ import annotations

import hashlib
import os
import secrets


def configured_keys() -> set[str]:
    raw = os.environ.get("BRAIN_API_KEYS") or os.environ.get("BRAIN_API_KEY") or ""
    if not raw:
        try:
            from brain.settings import settings

            raw = str(settings.get("api_keys", "") or "")
        except Exception:
            raw = ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    auth = authorization.strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return auth or None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_partner(authorization: str | None) -> dict | None:
    """Return the caller's partner context, or None if the token is invalid.

    {"partner_id": str | None, "owner": bool}. The org owner's key → owner=True,
    partner_id=None (full access). A per-partner table key → owner=False with its
    partner_id. Fail-closed: unknown token → None."""
    token = _extract_token(authorization)
    if not token:
        return None
    owner_keys = configured_keys()
    if owner_keys and token in owner_keys:
        return {"partner_id": None, "owner": True}
    row = _lookup_partner_key(token)
    if row:
        return {"partner_id": row.get("partner_id"), "owner": False}
    return None


def _lookup_partner_key(token: str) -> dict | None:
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        client = supabase_client.get_client()
        org = supabase_client.get_org_id()
        res = (
            client.table("api_keys")
            .select("partner_id, active")
            .eq("org_id", org)
            .eq("key_hash", _hash(token))
            .eq("active", True)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def resolve_partner_org(authorization: str | None) -> str | None:
    """GATEWAY-side: map a bearer token to the org (tenant) that owns it, ACROSS all
    orgs — so the multi-tenant gateway can route a partner request to the right
    brain. Requires service-role Supabase (the gateway has it). Returns the org_id,
    or None for an unknown/inactive token. Fail-closed.

    Per-partner table keys only — owner keys (env BRAIN_API_KEYS) are per-tenant and
    not resolvable here; they're for single-tenant/standalone deployments."""
    token = _extract_token(authorization)
    if not token:
        return None
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        client = supabase_client.get_client()
        res = (
            client.table("api_keys")
            .select("org_id")
            .eq("key_hash", _hash(token))
            .eq("active", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0]["org_id"] if rows else None
    except Exception:
        return None


def has_any_api_keys() -> bool:
    """BRAIN-side (org-scoped): does THIS org have any active per-partner key? Lets a
    tenant brain decide to start its engine API server even when no owner env key is
    set (the multi-tenant B2B path keys live in the api_keys table, not env)."""
    if configured_keys():
        return True
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return False
        client = supabase_client.get_client()
        org = supabase_client.get_org_id()
        res = (
            client.table("api_keys")
            .select("id")
            .eq("org_id", org)
            .eq("active", True)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        return False


def check_bearer(authorization: str | None) -> bool:
    """True iff the Authorization header carries a valid owner or partner key.
    Fail-closed: no match → False."""
    return resolve_partner(authorization) is not None


# ── key management (owner-only; used by the engine API mint/revoke routes) ─────


def mint_partner_key(partner_id: str, label: str | None = None) -> dict:
    """Create a per-partner key. Returns {id, partner_id, token} — the plaintext
    ``token`` is shown ONCE and never stored (only its hash is). Requires Supabase."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        raise RuntimeError("per-partner keys require the Supabase storage backend")
    pid = str(partner_id or "").strip()
    if not pid:
        raise ValueError("partner_id required")
    token = "sk_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(8)
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    client.table("api_keys").insert(
        {
            "org_id": org,
            "id": key_id,
            "key_hash": _hash(token),
            "partner_id": pid,
            "label": label,
            "active": True,
        }
    ).execute()
    return {"id": key_id, "partner_id": pid, "label": label, "token": token}


def list_partner_keys() -> list[dict]:
    """Key metadata (never the token/hash) for the org."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return []
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    res = (
        client.table("api_keys")
        .select("id, partner_id, label, active, created_ts")
        .eq("org_id", org)
        .order("created_ts")
        .execute()
    )
    return list(res.data or [])


def revoke_partner_key(key_id: str) -> bool:
    """Deactivate a key by its public id. Returns False if not found."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return False
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    existing = client.table("api_keys").select("id").eq("org_id", org).eq("id", key_id).execute()
    if not (existing.data or []):
        return False
    client.table("api_keys").update({"active": False}).eq("org_id", org).eq("id", key_id).execute()
    return True
