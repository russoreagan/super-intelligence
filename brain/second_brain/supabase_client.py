"""
Supabase client singleton for the brain storage backend.

Activated by BRAIN_STORAGE_BACKEND=supabase.

Credential resolution (in order):
  1. BRAIN_SUPABASE_JWT — a gateway-minted org token (sub = org_id). The client
     connects with the ANON key and attaches this JWT, so every query runs under
     RLS: `auth.uid() = org_id` is enforced by Postgres itself. This is the only
     mode tenant processes run in; they never see the service key.
  2. SUPABASE_SERVICE_KEY — RLS-bypassing. Gateway, provisioner, admin scripts,
     and single-user local dev (where the operator IS the platform).

The org_id is injected per-session; see set_org_id().
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_client = None
_org_id: str | None = None


def get_client():
    """Return the supabase-py client, initialising it on first call."""
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("SUPABASE_URL", "")
    org_jwt = os.environ.get("BRAIN_SUPABASE_JWT", "").strip()
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    try:
        from supabase import create_client
    except ImportError as e:
        raise RuntimeError("supabase package not installed. Run: uv add supabase") from e

    if org_jwt and anon_key:
        if not url:
            raise RuntimeError("SUPABASE_URL must be set when BRAIN_STORAGE_BACKEND=supabase")
        _client = create_client(url, anon_key)
        # Attach the org token so PostgREST evaluates RLS as this org.
        _client.postgrest.auth(org_jwt)
        logger.info("[Supabase] Client initialised with scoped org JWT (RLS enforced)")
        return _client

    if not url or not service_key:
        raise RuntimeError(
            "Supabase backend needs SUPABASE_URL plus either BRAIN_SUPABASE_JWT + "
            "SUPABASE_ANON_KEY (tenant) or SUPABASE_SERVICE_KEY (platform)."
        )
    _client = create_client(url, service_key)
    logger.info("[Supabase] Client initialised with service role (url=%s...)", url[:30])
    return _client


def set_org_id(org_id: str) -> None:
    """Set the active org (tenant) id for all subsequent storage calls."""
    global _org_id
    _org_id = org_id
    logger.debug("[Supabase] Active org_id set to %s", org_id)


def get_org_id() -> str:
    if not _org_id:
        raise RuntimeError("No org_id set. Call supabase_client.set_org_id(oid) at session start.")
    return _org_id


# Back-compat aliases (pre-007 naming) — same semantics, the "user id" always was
# the tenant key.
set_user_id = set_org_id
get_user_id = get_org_id


def is_enabled() -> bool:
    return os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase"
