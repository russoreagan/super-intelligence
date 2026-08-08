"""
Runtime API-key auth for the engine surface.

A partner's backend authenticates with a bearer key — the *runtime* credential
(distinct from the admin console login). It can open sessions and run turns, but
nothing else. Fail-closed: if no keys are configured, every request is denied, so
an accidentally-exposed server is not open by default.

Two key kinds:
  • The ORG OWNER key — BRAIN_API_KEYS / BRAIN_API_KEY env or the ``api_keys``
    setting. Constant-time compare. Full access (partner_id = None, owner = True).
    Per-tenant, so it is NOT resolvable at the multi-tenant gateway.
  • TABLE keys — rows in the ``api_keys`` table (011), each mapped to a partner_id
    and a role. Only the SHA-256 hash is stored; the token is shown once at mint.
    role='partner' is scoped (a partner only drives what it created); role='owner'
    is an owner-grade credential that, unlike the env key, works through the gateway.

``resolve_partner`` returns the partner context for a bearer token; ``check_bearer``
is the boolean gate built on it. ``resolve_key_context`` is the gateway-side variant
that resolves across orgs.

Three outcomes, never conflated: a context (known caller), None (no such key), and
AuthBackendError (the store is down, so identity is unknown → 503).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets


class AuthBackendError(RuntimeError):
    """The key store could not be reached, so the caller's identity is UNKNOWN.

    Distinct from "no such key" (None). Callers must map this to a 503 and never to
    a decision about who the caller is — treating a backend blip as "not a partner"
    is how a fail-closed design turns fail-open."""


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
    """The token from an ``Authorization: Bearer <token>`` header, or None.

    Bearer only. A bare token with no scheme (and anything under another scheme) is
    rejected: accepting arbitrary header values as credentials widens what counts as
    a credential-bearing request and encourages clients to put secrets in
    oddly-shaped headers."""
    if not authorization:
        return None
    auth = authorization.strip()
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _matches_an_owner_key(token: str, owner_keys: set[str]) -> bool:
    """Constant-time membership. ``token in owner_keys`` compares with ``==``, whose
    early exit leaks a prefix-length timing signal; and iterating with a plain ``or``
    would short-circuit on the first hit, leaking position. Compare against every key
    and OR the results without branching."""
    hit = False
    for k in owner_keys:
        hit |= hmac.compare_digest(token, k)
    return hit


def resolve_partner(authorization: str | None) -> dict | None:
    """Return the caller's partner context, or None if the token is invalid.

    {"partner_id": str | None, "owner": bool}. The org owner's key → owner=True,
    partner_id=None (full access). A per-partner table key → owner=False with its
    partner_id, unless the row carries role='owner' (an owner-grade table key, which
    unlike the env key IS resolvable through the hosted gateway).

    Fail-closed: unknown token → None. Raises AuthBackendError if the key store is
    unreachable — an unknown identity is not the same as a known non-owner."""
    token = _extract_token(authorization)
    if not token:
        return None
    owner_keys = configured_keys()
    if owner_keys and _matches_an_owner_key(token, owner_keys):
        return {"partner_id": None, "owner": True}
    row = _lookup_partner_key(token)
    if row:
        is_owner = str(row.get("role") or "partner") == "owner"
        return {"partner_id": row.get("partner_id"), "owner": is_owner}
    return None


def _lookup_partner_key(token: str) -> dict | None:
    """The api_keys row for a token, or None when there is no such active key.

    Raises AuthBackendError when the lookup itself fails. Swallowing that and
    returning None told callers "not a partner" during a Supabase blip, which
    _require then upgraded to full org owner."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return None
    try:
        client = supabase_client.get_client()
        org = supabase_client.get_org_id()
        res = (
            client.table("api_keys")
            # "*" rather than naming `role`: PostgREST errors on an unknown column, so
            # naming it would turn EVERY auth lookup into a 503 on any deployment that
            # ships this code before migration 028 lands. With "*" a pre-migration row
            # simply has no role and reads as 'partner' — the safe direction.
            .select("*")
            .eq("org_id", org)
            .eq("key_hash", _hash(token))
            .eq("active", True)
            .execute()
        )
    except Exception as e:
        raise AuthBackendError(str(e)) from e
    rows = res.data or []
    return rows[0] if rows else None


def resolve_key_context(authorization: str | None) -> dict | None:
    """GATEWAY-side: map a bearer token to its org, partner and role, ACROSS all orgs
    — so the multi-tenant gateway can route a request to the right brain AND decide
    whether the caller may drive org-wide controls like /v1/sleep.

    Returns {"org_id": str, "partner_id": str | None, "role": "owner"|"partner"}, or
    None for an unknown/inactive token. Requires service-role Supabase (the gateway
    has it). Raises AuthBackendError if the lookup fails.

    Per-partner table keys only — env owner keys (BRAIN_API_KEYS) are per-tenant and
    invisible here, which is why an org that needs an API-reachable owner credential
    mints a table key with role='owner'."""
    token = _extract_token(authorization)
    if not token:
        return None
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return None
    try:
        client = supabase_client.get_client()
        res = (
            client.table("api_keys")
            # See _lookup_partner_key: "*" keeps this working before migration 028.
            .select("*")
            .eq("key_hash", _hash(token))
            .eq("active", True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise AuthBackendError(str(e)) from e
    rows = res.data or []
    if not rows:
        return None
    row = rows[0]
    return {
        "org_id": row["org_id"],
        "partner_id": row.get("partner_id"),
        "role": str(row.get("role") or "partner"),
    }


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
    Fail-closed: no match → False. Propagates AuthBackendError rather than reporting
    False on a backend blip, so callers can tell "denied" from "cannot tell"."""
    return resolve_partner(authorization) is not None


# ── key management (owner-only; used by the engine API mint/revoke routes) ─────


KEY_ROLES = ("partner", "owner")


def mint_partner_key(partner_id: str, label: str | None = None, role: str = "partner") -> dict:
    """Create a per-partner key. Returns {id, partner_id, role, token} — the plaintext
    ``token`` is shown ONCE and never stored (only its hash is). Requires Supabase.

    ``role='owner'`` mints an owner-grade key. Unlike the env owner key it lives in
    api_keys, so it resolves through the hosted gateway — this is how an org gets a
    credential that can call owner-gated routes on api.elyceum.app at all. Minting
    one is itself owner-gated at every call site."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        raise RuntimeError("per-partner keys require the Supabase storage backend")
    pid = str(partner_id or "").strip()
    if not pid:
        raise ValueError("partner_id required")
    role = str(role or "partner").strip().lower()
    if role not in KEY_ROLES:
        raise ValueError(f"role must be one of {', '.join(KEY_ROLES)}")
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
            "role": role,
        }
    ).execute()
    return {"id": key_id, "partner_id": pid, "label": label, "role": role, "token": token}


def list_partner_keys() -> list[dict]:
    """Key metadata (never the token/hash) for the org."""
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return []
    client = supabase_client.get_client()
    org = supabase_client.get_org_id()
    res = (
        client.table("api_keys")
        .select("id, partner_id, label, active, role, created_ts")
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
