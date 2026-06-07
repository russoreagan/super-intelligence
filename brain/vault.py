"""
Per-user API-key vault — thin client over the Supabase Vault RPCs.

There is NO cryptography in this module. Keys live in Supabase Vault
(AEAD-encrypted at rest with the key held in Supabase's backend); this file only
calls the SECURITY DEFINER RPCs defined in migrations/003_key_vault.sql:

  Gateway (as the authenticated user, via their JWT):
    - set_key(access_token, provider, value)   → set_user_api_key       (write-only)
    - delete_key(access_token, provider)        → delete_user_api_key    (write-only)
    - get_status(access_token)                  → get_my_api_key_status  (booleans only)

  Pod boot (operator tier, via the service role):
    - fetch_user_keys(uid)                      → get_user_api_keys      (decrypt)
    - apply_user_keys_to_env(uid)               → fetch + export to os.environ

The gateway never decrypts (no read-back path); only the user's own pod does, at
boot, for its own BRAIN_USER_ID.
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


def set_key(access_token: str, provider: str, value: str) -> None:
    """Store/replace one provider key for the authenticated user (write-only)."""
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    value = (value or "").strip()
    if not value:
        # Blank means "leave unchanged" — caller should not reach here, but never
        # let a blank wipe a stored key.
        return
    _user_client(access_token).rpc(
        "set_user_api_key", {"p_provider": provider, "p_value": value}
    ).execute()


def delete_key(access_token: str, provider: str) -> None:
    """Remove one provider key for the authenticated user."""
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    _user_client(access_token).rpc("delete_user_api_key", {"p_provider": provider}).execute()


def get_status(access_token: str) -> dict:
    """Return {provider: bool, ..., updated_at} for the authenticated user —
    booleans only, never values. Drives the UI's masked 'key on file' state."""
    resp = _user_client(access_token).rpc("get_my_api_key_status", {}).execute()
    return resp.data or {}


# ── service-role client (pod boot) ──────────────────────────────────────────
def fetch_user_keys(uid: str) -> dict:
    """Return {provider: decrypted_value} for one user. Service-role only — call
    this on the user's own pod with its BRAIN_USER_ID."""
    from brain.second_brain import supabase_client

    resp = supabase_client.get_client().rpc("get_user_api_keys", {"p_uid": uid}).execute()
    return resp.data or {}


def apply_user_keys_to_env(uid: str | None = None) -> list[str]:
    """Pod-boot helper: fetch the user's keys and export them to os.environ so the
    clients (which all read os.environ) pick them up. Returns the list of env vars
    set. Never logs values. uid defaults to BRAIN_USER_ID."""
    uid = (uid or os.environ.get("BRAIN_USER_ID", "")).strip()
    if not uid:
        logger.error("[vault] apply_user_keys_to_env: no uid / BRAIN_USER_ID set")
        return []
    keys = fetch_user_keys(uid)
    applied: list[str] = []
    for provider, value in keys.items():
        env_name = PROVIDER_ENV.get(provider)
        if env_name and value:
            os.environ[env_name] = value
            applied.append(env_name)
    logger.info("[vault] applied %d key(s) from vault: %s", len(applied), ", ".join(applied) or "—")
    return applied
