"""
Partner ownership of end users — the join point for per-customer scoping.

An `end_user_id` is partner-chosen free text ("your customer"). Sessions recorded
which partner opened them, but nothing recorded who OWNS a customer, so every other
per-end-user surface was scoped only to the org. Within one org that let any partner
key read, overwrite or delete another partner's customers' connector tokens.

This module is the single answer to "whose customer is this". Migration 029 holds the
table; `require()` is the predicate every per-end-user route calls, mirroring the
shape of `_skill_owned` in brain/api/server.py.

Two rules worth keeping in mind when extending this:

  • Claiming is FIRST-WRITER-WINS, never an upsert. An upsert would let a second
    partner overwrite the ownership row and take the customer.
  • Local mode (no Supabase) is a NO-OP, not an isolation guarantee. Multi-partner
    only exists in the hosted shape; a companion/local brain has exactly one caller,
    so there is nothing to isolate and nowhere to record it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _sb():
    """(client, org_id) or None when there is no usable Supabase backend.

    Tolerant of a backend that reports enabled but cannot hand out a client (an
    unset org, a half-configured deployment): ownership is an isolation refinement
    on top of org scoping, so it degrades to "no registry" rather than taking the
    whole API down with it."""
    from brain.second_brain import supabase_client

    try:
        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception as e:  # pragma: no cover - deployment shape
        logger.warning("[end_users] backend unavailable: %s", e)
        return None


def claim(end_user_id: str, partner_id: str | None) -> str | None:
    """Record `partner_id` as the owner of `end_user_id` if nobody owns it yet, and
    return the owner that is now on record (which may be someone else).

    Insert-if-absent then read back, so two partners racing on the same id resolve to
    whichever landed first rather than the last writer."""
    sb = _sb()
    if sb is None:
        return partner_id
    client, org = sb
    row = {"org_id": org, "end_user_id": end_user_id, "partner_id": partner_id}
    try:
        client.table("end_users").upsert(
            row, on_conflict="org_id,end_user_id", ignore_duplicates=True
        ).execute()
    except Exception as e:  # pragma: no cover - network/backend shape
        logger.warning("[end_users] claim failed for %s: %s", end_user_id[:32], e)
    exists, owner = owner_of(end_user_id)
    return owner if exists else partner_id


def owner_of(end_user_id: str) -> tuple[bool, str | None]:
    """(is_registered, owning_partner_id). An unregistered id is (False, None) —
    distinct from a registered but owner-owned id, which is (True, None)."""
    sb = _sb()
    if sb is None:
        return (False, None)
    client, org = sb
    try:
        res = (
            client.table("end_users")
            .select("partner_id")
            .eq("org_id", org)
            .eq("end_user_id", end_user_id)
            .limit(1)
            .execute()
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] lookup failed for %s: %s", end_user_id[:32], e)
        return (False, None)
    rows = res.data or []
    if not rows:
        return (False, None)
    return (True, rows[0].get("partner_id"))


def is_allowed(ctx: dict, end_user_id: str, *, unregistered_ok: bool) -> bool:
    """Whether `ctx` may act on this end user.

    The owner always may. A partner may when it owns the row. `unregistered_ok`
    separates the two shapes of call site: a WRITE may claim an id nobody owns yet,
    while a READ of an unknown id must not succeed (and its caller should 404 rather
    than 403, so the response does not confirm whether the id exists)."""
    if ctx.get("owner"):
        return True
    exists, owner = owner_of(end_user_id)
    if not exists:
        return unregistered_ok
    return owner is not None and owner == ctx.get("partner_id")


def forget(end_user_id: str) -> None:
    """Drop the ownership row. Called LAST in a purge: while it exists the customer is
    still owned and therefore still re-purgeable, so a failure mid-way leaves work
    that can be retried rather than an orphaned set of rows nobody can reach."""
    sb = _sb()
    if sb is None:
        return
    client, org = sb
    try:
        client.table("end_users").delete().eq("org_id", org).eq(
            "end_user_id", end_user_id
        ).execute()
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] forget failed for %s: %s", end_user_id[:32], e)


def list_for_partner(partner_id: str | None) -> list[str]:
    """Every end_user_id owned by a partner. Used by partner-scoped erasure."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    try:
        q = client.table("end_users").select("end_user_id").eq("org_id", org)
        q = q.is_("partner_id", "null") if partner_id is None else q.eq("partner_id", partner_id)
        return [r["end_user_id"] for r in (q.execute().data or [])]
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] list failed: %s", e)
        return []
