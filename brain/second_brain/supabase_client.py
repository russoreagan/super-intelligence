"""
Supabase client singleton for the brain storage backend.

Activated by BRAIN_STORAGE_BACKEND=supabase.
Uses service-role key (server-side only — never exposed to browser).
The user_id is injected per-session; see set_user_id().
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_client = None
_user_id: str | None = None


def get_client():
    """Return the supabase-py client, initialising it on first call."""
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set when "
            "BRAIN_STORAGE_BACKEND=supabase"
        )

    try:
        from supabase import create_client
        _client = create_client(url, key)
        logger.info("[Supabase] Client initialised (url=%s...)", url[:30])
        return _client
    except ImportError:
        raise RuntimeError(
            "supabase package not installed. Run: uv add supabase"
        )


def set_user_id(uid: str) -> None:
    """Set the active user_id for all subsequent storage calls."""
    global _user_id
    _user_id = uid
    logger.debug("[Supabase] Active user_id set to %s", uid)


def get_user_id() -> str:
    if not _user_id:
        raise RuntimeError(
            "No user_id set. Call supabase_client.set_user_id(uid) at session start."
        )
    return _user_id


def is_enabled() -> bool:
    return os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase"
