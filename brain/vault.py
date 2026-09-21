"""
Per-ORG API-key vault — thin client over the Supabase Vault RPCs.

There is NO cryptography in this module. Keys live in Supabase Vault
(AEAD-encrypted at rest with the key held in Supabase's backend); this file only
calls the SECURITY DEFINER RPCs defined in migrations 003 and 045:

  Gateway (as the authenticated user, via their JWT — authorized per-org in the
  RPC body against their memberships row):
    - set_key(org_id, access_token, provider, value) → set_org_api_key   (write-only)
    - delete_key(org_id, access_token, provider)     → delete_org_api_key (write-only)
    - get_status(org_id, access_token)               → get_org_api_key_status (booleans)

  Pod boot / spawn gate (operator tier, via the service role):
    - fetch_org_keys(org_id)                    → get_org_api_keys       (decrypt)
    - apply_org_keys_to_env(org_id)             → fetch + export to os.environ

The gateway never decrypts (no read-back path); only the org's own pod does, at
boot, for its own BRAIN_ORG_ID.

WHY ORG AND NOT USER. The tenant unit is the organization: it owns the brain
process, the volume and every per-tenant row, and every production read here has
always passed an ORG id. Until migration 045 the underlying table was keyed by
auth.users.id, which resolved only because every org alive was a "personal org"
seeded with id == its owner's user id. An org with a fresh uuid found no row, so
the spawn gate saw no Anthropic key and the gateway bounced the user to /keys
forever. 045 moves the store to org_api_keys_meta and keeps reading the legacy
user row in place when an org has no row of its own, so personal orgs are
unchanged. See that migration's header for the full argument.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# provider slug (used in the RPCs / vault secret names) → process env var the
# clients read. Derived from settings.API_KEY_ENV so the two never drift.
try:
    from brain.settings import API_KEY_ENV

    PROVIDER_ENV = {k.replace("api_key_", ""): v for k, v in API_KEY_ENV.items()}
except Exception:  # pragma: no cover - settings import should always succeed
    PROVIDER_ENV = {
        "anthropic": "ANTHROPIC_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "deepgram": "DEEPGRAM_API_KEY",
        "google": "GOOGLE_API_KEY",
    }

VALID_PROVIDERS = frozenset(PROVIDER_ENV)


# ── user-context client (gateway) ───────────────────────────────────────────
def _user_client(access_token: str):
    """A Supabase client that acts AS the end user (so auth.uid() resolves and the
    write-only RPCs scope to them). Uses the anon key + the user's access token —
    never the service role."""
    url = os.environ.get("SUPABASE_URL", "")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not anon:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
    if not access_token:
        raise RuntimeError("access_token required for user-context vault calls")
    from supabase import create_client

    client = create_client(url, anon)
    # Run subsequent PostgREST/RPC calls under the user's identity.
    client.postgrest.auth(access_token)
    return client


def set_key(org_id: str, access_token: str, provider: str, value: str) -> None:
    """Store/replace one provider key for `org_id` (write-only).

    `org_id` selects the target; the RPC AUTHORIZES it against the caller's own
    auth.uid() via an admin-membership check, so naming an org you don't belong to
    raises in Postgres rather than being taken on trust here."""
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if not org_id:
        raise ValueError("org_id required")
    value = (value or "").strip()
    if not value:
        # Blank means "leave unchanged" — caller should not reach here, but never
        # let a blank wipe a stored key.
        return
    _user_client(access_token).rpc(
        "set_org_api_key", {"p_org_id": org_id, "p_provider": provider, "p_value": value}
    ).execute()


def delete_key(org_id: str, access_token: str, provider: str) -> None:
    """Remove one provider key for `org_id`. Authorized in the RPC as set_key is."""
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if not org_id:
        raise ValueError("org_id required")
    _user_client(access_token).rpc(
        "delete_org_api_key", {"p_org_id": org_id, "p_provider": provider}
    ).execute()


def get_status(org_id: str, access_token: str) -> dict:
    """Return {provider: bool, ..., updated_at} for `org_id` — booleans only, never
    values. Drives the UI's masked 'key on file' state.

    Resolves through the same org row (and the same legacy fallback) that
    fetch_org_keys uses, which is what keeps this from disagreeing with the spawn
    gate. Before 045 it read the USER's row while the gate read the org's, so the
    page could report a key on file while the gate refused to spawn."""
    if not org_id:
        raise ValueError("org_id required")
    resp = _user_client(access_token).rpc("get_org_api_key_status", {"p_org_id": org_id}).execute()
    return resp.data or {}


# ── service-role client (pod boot / spawn gate) ─────────────────────────────
def fetch_org_keys(org_id: str) -> dict:
    """Return {provider: decrypted_value} for one ORG. Service-role only — called
    on the org's own pod with its BRAIN_ORG_ID, and by the gateway's pre-spawn key
    gate.

    Falls back to 003's get_user_api_keys when get_org_api_keys does not exist yet,
    so this module can deploy BEFORE migration 045 is pushed (the ordering 036's
    header asks for). The fallback is per-call and not cached: it is a
    rollout-window path, not a steady state, and the RPC resolves identically once
    045 lands because 045 redefines get_user_api_keys to delegate."""
    from brain.second_brain import supabase_client

    client = supabase_client.get_client()
    try:
        resp = client.rpc("get_org_api_keys", {"p_org_id": org_id}).execute()
    except Exception as e:
        if not _is_undefined_function(e):
            raise
        logger.info("[vault] get_org_api_keys absent (pre-045) — using get_user_api_keys")
        resp = client.rpc("get_user_api_keys", {"p_uid": org_id}).execute()
    return resp.data or {}


def _is_undefined_function(exc: Exception) -> bool:
    """True when Postgres rejected the call because the function does not exist.

    Matched on SQLSTATE 42883 where the client surfaces it, else on the message —
    PostgREST reports it as PGRST202 ('Could not find the function'). Deliberately
    narrow: any other failure (permission denied, a decrypt error, the backend
    being down) must propagate, because silently retrying the legacy RPC would
    mask a real fault as a missing migration."""
    text = f"{getattr(exc, 'code', '')} {exc}".lower()
    return "42883" in text or "pgrst202" in text or "could not find the function" in text


def apply_org_keys_to_env(org_id: str | None = None) -> list[str]:
    """Pod-boot helper: fetch the org's keys and export them to os.environ so the
    clients (which all read os.environ) pick them up. Returns the list of env vars
    set. Never logs values. org_id defaults to BRAIN_ORG_ID, then BRAIN_USER_ID —
    the provisioner sets both to the org id (brain/provisioner.py)."""
    org_id = (
        org_id or os.environ.get("BRAIN_ORG_ID", "") or os.environ.get("BRAIN_USER_ID", "")
    ).strip()
    if not org_id:
        logger.error("[vault] apply_org_keys_to_env: no org_id / BRAIN_ORG_ID set")
        return []
    keys = fetch_org_keys(org_id)
    applied: list[str] = []
    for provider, value in keys.items():
        env_name = PROVIDER_ENV.get(provider)
        if env_name and value:
            os.environ[env_name] = value
            applied.append(env_name)
    logger.info("[vault] applied %d key(s) from vault: %s", len(applied), ", ".join(applied) or "—")
    return applied
