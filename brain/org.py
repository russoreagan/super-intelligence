"""
Org-based tenancy resolution (organizations + memberships).

An organization is the tenant unit — it owns one brain process and all per-tenant
data. Users are members of an org with a role ('admin' | 'member'). The platform
super-user is the existing app_metadata.is_admin (see brain.ui.auth.is_admin), not
modeled here.

This module resolves a user (and, later, an API key) to their org so the gateway
can route, and lets the brain check membership. Backed by the service-role Supabase
client (bypasses RLS); every function degrades gracefully to None/False when
Supabase is off or the query fails, so dev/local single-user runs are unaffected.

The client is injectable so the resolution logic is unit-tested without a live DB.
Calls are synchronous (supabase-py is sync), matching the vault/store pattern — fine
at gateway traffic levels.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _client():
    from brain.second_brain.supabase_client import get_client

    return get_client()


def org_id_for_user(user_id: str, client=None) -> str | None:
    """The org this user belongs to. v1 assumes one org per user; if there are
    several, prefer an 'admin' membership, else the first. For the dev's personal
    org this returns their own user_id (org_id == user_id by seed). None when the
    user has no membership or Supabase is unavailable."""
    if not user_id:
        return None
    try:
        client = client or _client()
        rows = (
            client.table("memberships").select("org_id, role").eq("user_id", user_id).execute().data
            or []
        )
        if not rows:
            return None
        admin = [r for r in rows if r.get("role") == "admin"]
        return str((admin or rows)[0]["org_id"])
    except Exception as e:
        logger.warning("[org] org_id_for_user failed: %s", e)
        return None


def membership_role(user_id: str, org_id: str, client=None) -> str | None:
    """The caller's role in this org ('admin' | 'member'), or None when they're not
    a member / Supabase is unavailable / on any error. Lets the brain tell an
    org-admin (manages the org's agents, roles, connectors, keys — the per-agent
    narrowing within the account ceilings) apart from a plain member. Fail-closed:
    None on error, so a lookup failure denies rather than grants."""
    if not user_id or not org_id:
        return None
    try:
        client = client or _client()
        rows = (
            client.table("memberships")
            .select("role")
            .eq("user_id", user_id)
            .eq("org_id", org_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return str(rows[0]["role"]) if rows else None
    except Exception as e:
        logger.warning("[org] membership_role failed: %s", e)
        return None


def is_member(user_id: str, org_id: str, client=None) -> bool:
    """True iff the user is a member of the org. Used by the brain to gate access
    to its org's process (the membership-aware successor to the BRAIN_USER_ID == sub
    pin). Fail-closed: False on any error / missing data."""
    if not user_id or not org_id:
        return False
    try:
        client = client or _client()
        rows = (
            client.table("memberships")
            .select("user_id")
            .eq("user_id", user_id)
            .eq("org_id", org_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return bool(rows)
    except Exception as e:
        logger.warning("[org] is_member failed: %s", e)
        return False
