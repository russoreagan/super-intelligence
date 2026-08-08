"""
Partner webhook registration and the job-completion outbox (migration 032).

Two responsibilities, both org-scoped (this runs in the tenant brain):

  • Registration CRUD backing POST/GET/DELETE /v1/webhooks. The signing secret is
    minted here, handed back once, and stored only in Vault via the set RPC.
  • enqueue(): when a job reaches a terminal state, write a webhook_deliveries row for
    every webhook that should receive it. Delivery itself is the gateway's job — the
    brain sleeps and cannot own a retry schedule — so this only writes the outbox.

Routing rule: a partner-registered webhook (partner_id set) receives only that
partner's jobs; an owner-registered webhook (partner_id '') receives everything,
including the brain's own self-directed work. Without this, one partner's job goals
would be delivered to another partner's endpoint.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger(__name__)

_WH_PREFIX = "wh_"
_SECRET_PREFIX = "whsec_"
_DLV_PREFIX = "dlv_"
_EVT_PREFIX = "evt_"


def _sb():
    from brain.second_brain import supabase_client

    if not supabase_client.is_enabled():
        return None
    return supabase_client.get_client(), supabase_client.get_org_id()


def _owns(ctx: dict, row: dict | None) -> bool:
    """The owner sees every webhook; a partner only its own."""
    if row is None:
        return False
    return bool(ctx.get("owner")) or row.get("partner_id") == ctx.get("partner_id")


def register(ctx: dict, url: str, events: list[str] | None = None) -> dict:
    """Create a webhook and return {id, url, events, secret}. The secret is shown ONCE
    and never retrievable — only its Vault-encrypted form is stored. Raises RuntimeError
    without Supabase (the route maps it to 503)."""
    sb = _sb()
    if sb is None:
        raise RuntimeError("webhooks require the Supabase storage backend")
    client, org = sb
    wid = _WH_PREFIX + secrets.token_hex(8)
    secret = _SECRET_PREFIX + secrets.token_urlsafe(32)
    partner_id = "" if ctx.get("owner") else str(ctx.get("partner_id") or "")
    ev = list(events) if events else ["job"]
    client.rpc(
        "set_partner_webhook",
        {
            "p_id": wid,
            "p_partner_id": partner_id,
            "p_url": url,
            "p_events": ev,
            "p_secret": secret,
            "p_org_id": org,
        },
    ).execute()
    return {"id": wid, "url": url, "events": ev, "partner_id": partner_id, "secret": secret}


def rotate(ctx: dict, webhook_id: str) -> dict | None:
    """Mint a new secret for an existing webhook (invalidating the old one). Returns
    {id, secret} or None if the caller doesn't own it / it doesn't exist."""
    row = _get(webhook_id)
    if not _owns(ctx, row):
        return None
    sb = _sb()
    client, org = sb
    secret = _SECRET_PREFIX + secrets.token_urlsafe(32)
    client.rpc(
        "set_partner_webhook",
        {
            "p_id": webhook_id,
            "p_partner_id": row.get("partner_id") or "",
            "p_url": row.get("url"),
            "p_events": row.get("events") or ["job"],
            "p_secret": secret,
            "p_org_id": org,
        },
    ).execute()
    return {"id": webhook_id, "secret": secret}


def list_for(ctx: dict) -> list[dict]:
    """Webhook metadata (never the secret), filtered to the caller's own unless owner."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    q = (
        client.table("partner_webhooks")
        .select(
            "id, partner_id, url, events, active, disabled_reason, consecutive_failures, created_ts"
        )
        .eq("org_id", org)
    )
    rows = list(q.execute().data or [])
    if not ctx.get("owner"):
        pid = ctx.get("partner_id")
        rows = [r for r in rows if r.get("partner_id") == pid]
    return rows


def delete(ctx: dict, webhook_id: str) -> bool:
    """Remove a webhook and its Vault secret. False if not owned / not found."""
    row = _get(webhook_id)
    if not _owns(ctx, row):
        return False
    sb = _sb()
    client, org = sb
    res = client.rpc("delete_partner_webhook", {"p_id": webhook_id, "p_org_id": org}).execute()
    return bool(res.data)


def list_deliveries(ctx: dict, webhook_id: str, limit: int = 50) -> list[dict] | None:
    """Recent delivery attempts for one webhook (for debugging). None if not owned."""
    row = _get(webhook_id)
    if not _owns(ctx, row):
        return None
    sb = _sb()
    client, org = sb
    res = (
        client.table("webhook_deliveries")
        .select(
            "id, event_id, event_type, state, attempts, last_status, last_error, next_attempt_ts, created_ts"
        )
        .eq("org_id", org)
        .eq("webhook_id", webhook_id)
        .order("created_ts", desc=True)
        .limit(max(1, min(int(limit or 50), 200)))
        .execute()
    )
    return list(res.data or [])


def _get(webhook_id: str) -> dict | None:
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        rows = (
            client.table("partner_webhooks")
            .select("id, partner_id, url, events, active")
            .eq("org_id", org)
            .eq("id", webhook_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None
    except Exception as e:
        logger.debug("[webhooks] get failed: %s", e)
        return None


def enqueue(event_type: str, payload: dict, partner_id: str) -> int:
    """Write a delivery row for every webhook that should receive this event. Returns
    how many were enqueued. Best-effort — a webhook failure must never affect the job.

    `partner_id` is the job's owning partner ("" for owner-lane/self-directed work).
    An owner-registered webhook (partner_id '') always matches; a partner-registered
    one matches only its own jobs."""
    sb = _sb()
    if sb is None:
        return 0
    client, org = sb
    try:
        hooks = (
            client.table("partner_webhooks")
            .select("id, partner_id")
            .eq("org_id", org)
            .eq("active", True)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.debug("[webhooks] enqueue lookup failed: %s", e)
        return 0

    # One event id across the whole fan-out and every retry, so a receiver subscribed
    # to several webhooks (or getting a retry) can dedupe on it.
    event_id = _EVT_PREFIX + secrets.token_hex(10)
    n = 0
    for h in hooks:
        hp = h.get("partner_id") or ""
        # Owner-registered ('') gets everything; partner-registered only its own.
        if hp and hp != (partner_id or ""):
            continue
        try:
            # org_id inline (not via a variable) so the tenant-isolation guard can see
            # the row is stamped for this org — tests/security/test_org_scoping.py.
            client.table("webhook_deliveries").insert(
                {
                    "org_id": org,
                    "id": _DLV_PREFIX + secrets.token_hex(10),
                    "webhook_id": h["id"],
                    "event_id": event_id,  # stable across the fan-out AND across retries
                    "event_type": event_type,
                    "payload": payload,
                    "state": "pending",
                }
            ).execute()
            n += 1
        except Exception as e:
            logger.warning("[webhooks] enqueue insert failed for %s: %s", h.get("id"), e)
    return n
