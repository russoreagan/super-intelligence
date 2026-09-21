"""
Org-based tenancy resolution (organizations + memberships).

An organization is the tenant unit — it owns one brain process and all per-tenant
data. Users are members of an org with a role ('admin' | 'member'). The platform
super-user is the existing app_metadata.is_admin (see brain.ui.auth.is_admin), not
modeled here.

This module resolves a user (and, later, an API key) to their org so the gateway
can route, and lets the brain check membership. A user may belong to SEVERAL orgs
(memberships is many-to-many, migration 006) — orgs_for_user() returns all of
them for the console's org switcher, and org_id_for_user() names the default one
a login lands on.

Backed by the service-role Supabase client (bypasses RLS); every function
degrades gracefully to None/False/[] when Supabase is off or the query fails, so
dev/local single-user runs are unaffected.

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


def _membership_rank(row: dict) -> tuple:
    """Sort key for a user's memberships: admin first, then oldest, then by id.

    Deliberately NOT by org name — the DEFAULT org a login lands on must not move
    when somebody renames an org. And deliberately total: before this, two admin
    memberships tied and the winner was whatever PostgREST happened to emit first,
    which `_org_cache` then pinned for 30 minutes. A user's home org must be the
    same org on every request."""
    return (
        0 if row.get("role") == "admin" else 1,
        str(row.get("created_at") or ""),
        str(row.get("org_id") or ""),
    )


def orgs_for_user(user_id: str, client=None) -> list[dict]:
    """Every org this user belongs to, as [{"org_id", "name", "role"}], best
    default first (see _membership_rank). [] when they have none, or on any error
    — fail-closed like every other resolver here.

    One query, not two: the org NAME rides along as a PostgREST embedded resource
    (memberships.org_id references organizations, migration 006). That keeps this
    a `memberships` select, which matters because tests/security/test_org_scoping's
    AST guard allowlists exactly ("org.py", "memberships", "select") — an
    unscoped `organizations` select would be a new hole to justify. A missing or
    null embed degrades to the id as the display name rather than raising."""
    if not user_id:
        return []
    try:
        client = client or _client()
        rows = (
            client.table("memberships")
            .select("org_id, role, created_at, organizations(name)")
            .eq("user_id", user_id)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning("[org] orgs_for_user failed: %s", e)
        return []
    out: list[dict] = []
    for r in sorted(rows, key=_membership_rank):
        org_id = str(r.get("org_id") or "")
        if not org_id:
            continue
        embed = r.get("organizations") or {}
        if isinstance(embed, list):  # PostgREST returns a list for some rel shapes
            embed = embed[0] if embed else {}
        name = str((embed or {}).get("name") or "").strip() or org_id
        out.append({"org_id": org_id, "name": name, "role": str(r.get("role") or "member")})
    return out


def org_id_for_user(user_id: str, client=None) -> str | None:
    """The user's DEFAULT org — the one a login lands on when nothing else is
    selected. Admin membership first, else the oldest (see _membership_rank). For
    a personal org this returns their own user_id (org_id == user_id by seed).
    None when the user has no membership or Supabase is unavailable.

    Defined in terms of orgs_for_user so "the default" is always the head of the
    list the switcher shows, and the two can never disagree."""
    rows = orgs_for_user(user_id, client=client)
    return rows[0]["org_id"] if rows else None


def org_key_owner(org_id: str, client=None) -> str | None:
    """The user id of the org's admin member (oldest wins), or None.

    Lives here rather than in brain/vault.py on purpose: this is a `memberships`
    select, and org.py is the module the AST-guard allowlist already exempts for
    exactly that (a bootstrap lookup cannot filter by the org_id it is resolving
    FROM — here it filters by org_id, but keeping every membership query in one
    module is what makes that allowlist reviewable)."""
    if not org_id:
        return None
    try:
        client = client or _client()
        rows = (
            client.table("memberships")
            .select("user_id, role, created_at")
            .eq("org_id", org_id)
            .eq("role", "admin")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning("[org] org_key_owner failed: %s", e)
        return None
    if not rows:
        return None
    rows.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("user_id") or "")))
    return str(rows[0]["user_id"])


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
