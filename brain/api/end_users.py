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


def claim(end_user_id: str, partner_id: str | None, *, revive_if_owned: bool = False) -> str | None:
    """Record `partner_id` as the owner of `end_user_id` if nobody owns it yet, and
    return the owner that is now on record (which may be someone else).

    Insert-if-absent then read back, so two partners racing on the same id resolve to
    whichever landed first rather than the last writer.

    `revive_if_owned` is the session-open shape: an ERASED customer whose owner (or
    the org owner, partner_id None) opens a new session starts afresh on the same
    handle, so the tombstone is cleared. Other call sites (storing a connector
    token) leave it in place and are refused with 410 instead."""
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
    existing = lookup(end_user_id)
    if existing is None:
        return partner_id
    owner = existing.get("partner_id")
    if (
        revive_if_owned
        and existing.get("erased_at")
        and (partner_id is None or owner == partner_id)
    ):
        revive(end_user_id)
    return owner


def lookup(end_user_id: str) -> dict | None:
    """The registry row for an end user, or None when unregistered / no backend.

    select("*") on purpose: the row gained `erased_at` in migration 035, and naming
    columns here would 503 every ownership check on a deployment that has not
    applied it yet. A missing column simply reads as "live"."""
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        res = (
            client.table("end_users")
            .select("*")
            .eq("org_id", org)
            .eq("end_user_id", end_user_id)
            .limit(1)
            .execute()
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] lookup failed for %s: %s", end_user_id[:32], e)
        return None
    rows = res.data or []
    return dict(rows[0]) if rows else None


def owner_of(end_user_id: str) -> tuple[bool, str | None]:
    """(is_registered, owning_partner_id). An unregistered id is (False, None) —
    distinct from a registered but owner-owned id, which is (True, None). An
    ERASED row still counts as registered: ownership outlives the data so the
    owning partner can be told 410 rather than 404 (see erased_at)."""
    row = lookup(end_user_id)
    if row is None:
        return (False, None)
    return (True, row.get("partner_id"))


def erased_at(end_user_id: str) -> str | None:
    """The erasure timestamp for a tombstoned end user, or None when live /
    unregistered. Callers check ownership FIRST (is_allowed) so a foreign id never
    learns whether the customer existed — 404 stays 404."""
    row = lookup(end_user_id)
    if not row:
        return None
    ts = row.get("erased_at")
    return str(ts) if ts else None


def revive(end_user_id: str) -> bool:
    """Clear a tombstone: the owning partner is starting the customer afresh on the
    same handle (a session open). Returns True when a row was updated. No-op on a
    deployment without the column (nothing to clear)."""
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        res = (
            client.table("end_users")
            .update({"erased_at": None})
            .eq("org_id", org)
            .eq("end_user_id", end_user_id)
            .execute()
        )
        return bool(res.data)
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] revive failed for %s: %s", end_user_id[:32], e)
        return False


def access(ctx: dict, end_user_id: str, *, unregistered_ok: bool) -> tuple[bool, str | None]:
    """(allowed, erased_at) for `ctx` acting on this end user — ONE registry read.

    The owner always may. A partner may when it owns the row. `unregistered_ok`
    separates the two shapes of call site: a WRITE may claim an id nobody owns yet,
    while a READ of an unknown id must not succeed (and its caller should 404 rather
    than 403, so the response does not confirm whether the id exists).

    `erased_at` is only ever returned alongside allowed=True: a caller that may not
    act on the id learns nothing about it (404 stays 404), while its owner is told
    the id is a tombstone so the route can answer 410."""
    row = lookup(end_user_id)
    stamp = str(row.get("erased_at")) if row and row.get("erased_at") else None
    if ctx.get("owner"):
        return (True, stamp)
    if row is None:
        return (unregistered_ok, None)
    owner = row.get("partner_id")
    if owner is not None and owner == ctx.get("partner_id"):
        return (True, stamp)
    return (False, None)


def is_allowed(ctx: dict, end_user_id: str, *, unregistered_ok: bool) -> bool:
    """Whether `ctx` may act on this end user (see access())."""
    return access(ctx, end_user_id, unregistered_ok=unregistered_ok)[0]


def forget(end_user_id: str) -> str | None:
    """Tombstone the ownership row (migration 035): stamp `erased_at` instead of
    deleting, so the owning partner's next request on this id can be answered 410
    rather than the 404 a foreign id gets. Called LAST in a purge: until it runs
    the customer is still live and therefore still re-purgeable, so a failure
    mid-way leaves work that can be retried rather than rows nobody can reach.

    Returns the stamp written. Pre-migration (column absent) the update is refused
    and this falls back to the old row delete — exactly today's behaviour."""
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    import datetime as _dt

    stamp = _dt.datetime.now(_dt.UTC).isoformat()
    try:
        client.table("end_users").update({"erased_at": stamp}).eq("org_id", org).eq(
            "end_user_id", end_user_id
        ).execute()
        return stamp
    except Exception as e:
        logger.warning(
            "[end_users] tombstone failed for %s (%s) — deleting the row instead; "
            "apply migration 035 for 410 semantics",
            end_user_id[:32],
            e,
        )
    try:
        client.table("end_users").delete().eq("org_id", org).eq(
            "end_user_id", end_user_id
        ).execute()
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] forget failed for %s: %s", end_user_id[:32], e)
    return None


def list_for_partner(partner_id: str | None) -> list[str]:
    """Every LIVE end_user_id owned by a partner. Used by partner-scoped erasure.
    Tombstoned rows are filtered client-side so the read stays select("*")-safe on
    a deployment that has not applied migration 035."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    try:
        q = client.table("end_users").select("*").eq("org_id", org)
        q = q.is_("partner_id", "null") if partner_id is None else q.eq("partner_id", partner_id)
        return [r["end_user_id"] for r in (q.execute().data or []) if not r.get("erased_at")]
    except Exception as e:  # pragma: no cover
        logger.warning("[end_users] list failed: %s", e)
        return []
