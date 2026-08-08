"""
Webhook delivery + retry — the gateway half of the webhook subsystem (migration 032).

The brain writes a `webhook_deliveries` row when a job finishes and then may go to
sleep, so it cannot own a retry schedule that spans hours. The gateway is always up and
holds cross-org service-role Supabase, so delivery lives here: a periodic sweep claims
due rows across every org, signs them, and POSTs them, backing off on failure and
dead-lettering after exhaustion.

Signing and the SSRF guard are shared with the rest of the system (webhook_sign,
net_guard). The URL is re-validated on *every* attempt, not just at registration — a
hostname that was public when registered can be repointed at an internal address later,
so a stale check is worthless.

The sweep is written as pure-ish functions over an injected Supabase client, an HTTP
poster, and a clock, so it is testable without a live gateway, database, or network.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging

from brain.api import webhook_sign
from brain.net_guard import UnsafeUrlError, validate_url

logger = logging.getLogger(__name__)

# Attempt N (1-based) waits this many seconds before the next try. Length caps retries.
_BACKOFF_S = [10, 60, 300, 1800, 7200, 21600]  # ~9h total horizon
# Consecutive dead-letters before a webhook is auto-disabled.
_AUTO_DISABLE_AFTER = 20
_SWEEP_LIMIT = 50


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).isoformat()


async def deliver_one(client, delivery: dict, *, now: float, http_post) -> str:
    """Deliver one claimed row. Returns the new state ('delivered' | 'failed' |
    'dead_letter'). Never raises — a delivery failure must not stop the sweep."""
    org = delivery["org_id"]
    wid = delivery["webhook_id"]
    attempts = int(delivery.get("attempts") or 1)

    # Look up the target + secret fresh each attempt (URL may have changed; the secret
    # may have rotated).
    try:
        hook = (
            client.table("partner_webhooks")
            .select("url, active, consecutive_failures")
            .eq("org_id", org)
            .eq("id", wid)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as e:
        return _reschedule(client, delivery, now, attempts, f"lookup failed: {e}")
    if not hook or not hook[0].get("active"):
        return _finalize(client, delivery, "dead_letter", None, "webhook gone or disabled")
    url = hook[0]["url"]

    try:
        secret = (
            client.rpc("get_partner_webhook_secret", {"p_id": wid, "p_org_id": org}).execute().data
        )
    except Exception as e:
        return _reschedule(client, delivery, now, attempts, f"secret read failed: {e}")
    if not secret:
        return _finalize(client, delivery, "dead_letter", None, "no signing secret")

    body = json.dumps(delivery.get("payload") or {}, separators=(",", ":")).encode()
    ts = int(now)
    headers = {
        "Content-Type": "application/json",
        webhook_sign.HEADER: webhook_sign.sign(secret, body, ts),
        "Elyceum-Event-Id": delivery.get("event_id", ""),
        "Elyceum-Delivery": delivery.get("id", ""),
        "Elyceum-Attempt": str(attempts),
        "User-Agent": "Elyceum-Webhooks/1",
    }

    # Re-validate the URL against the SSRF guard on THIS attempt.
    try:
        validate_url(url)
    except UnsafeUrlError as e:
        return _finalize(client, delivery, "dead_letter", None, f"unsafe url: {e}")

    try:
        status = await http_post(url, body, headers)
    except Exception as e:
        return _reschedule(client, delivery, now, attempts, f"post error: {e}")

    if 200 <= status < 300:
        return _finalize(client, delivery, "delivered", status, "")
    if status == 410:
        # Gone — the receiver is telling us to stop.
        return _finalize(client, delivery, "dead_letter", status, "410 gone")
    # A persistent client error (not 408/429) is a receiver bug, not a blip — give up
    # quickly rather than retrying a doomed request for nine hours.
    if 400 <= status < 500 and status not in (408, 429) and attempts >= 2:
        return _finalize(client, delivery, "dead_letter", status, f"http {status}")
    return _reschedule(client, delivery, now, attempts, f"http {status}", last_status=status)


def _reschedule(client, delivery, now, attempts, err, last_status=None) -> str:
    """Mark the row failed with a backoff, or dead-letter it when attempts run out."""
    if attempts >= len(_BACKOFF_S):
        return _finalize(client, delivery, "dead_letter", last_status, err)
    delay = _BACKOFF_S[attempts]  # attempts already incremented at claim
    _update(
        client,
        delivery,
        {
            "state": "failed",
            "next_attempt_ts": _iso(now + delay),
            "last_error": err[:500],
            **({"last_status": last_status} if last_status is not None else {}),
        },
    )
    return "failed"


def _finalize(client, delivery, state, last_status, err) -> str:
    _update(
        client,
        delivery,
        {
            "state": state,
            "last_error": err[:500],
            **({"last_status": last_status} if last_status is not None else {}),
        },
    )
    if state == "dead_letter":
        _bump_webhook_failure(client, delivery)
    return state


def _update(client, delivery, patch: dict) -> None:
    patch = {**patch, "updated_ts": _iso_now()}
    try:
        client.table("webhook_deliveries").update(patch).eq("org_id", delivery["org_id"]).eq(
            "id", delivery["id"]
        ).execute()
    except Exception as e:
        logger.warning("[webhooks] delivery update failed: %s", e)


def _bump_webhook_failure(client, delivery) -> None:
    """Count a dead-letter against the webhook and auto-disable a chronically broken
    one, so a dead endpoint stops generating doomed deliveries forever."""
    org, wid = delivery["org_id"], delivery["webhook_id"]
    try:
        rows = (
            client.table("partner_webhooks")
            .select("consecutive_failures")
            .eq("org_id", org)
            .eq("id", wid)
            .limit(1)
            .execute()
            .data
            or []
        )
        n = int((rows[0].get("consecutive_failures") if rows else 0) or 0) + 1
        patch = {"consecutive_failures": n}
        if n >= _AUTO_DISABLE_AFTER:
            patch.update(active=False, disabled_reason="repeated_delivery_failure")
        client.table("partner_webhooks").update(patch).eq("org_id", org).eq("id", wid).execute()
    except Exception as e:
        logger.debug("[webhooks] failure bump skipped: %s", e)


def _iso_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def claim_due(client, now: float, limit: int = _SWEEP_LIMIT) -> list[dict]:
    """Claim up to `limit` due deliveries: flip each from pending/failed to 'sending'
    and increment attempts. The conditional update (…and state=<observed>) makes the
    claim safe if a second sweeper ever runs — only one flips the row."""
    try:
        due = (
            client.table("webhook_deliveries")
            .select("*")
            .in_("state", ["pending", "failed"])
            .lte("next_attempt_ts", _iso(now))
            .order("next_attempt_ts")
            .limit(limit)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.debug("[webhooks] claim scan failed: %s", e)
        return []
    claimed = []
    for d in due:
        try:
            res = (
                client.table("webhook_deliveries")
                .update({"state": "sending", "attempts": int(d.get("attempts") or 0) + 1})
                .eq("org_id", d["org_id"])
                .eq("id", d["id"])
                .eq("state", d["state"])  # lost to a racing sweeper → no-op
                .execute()
            )
            if res.data:
                row = dict(d)
                row["attempts"] = int(d.get("attempts") or 0) + 1
                claimed.append(row)
        except Exception as e:
            logger.debug("[webhooks] claim update failed: %s", e)
    return claimed


async def sweep_once(client, *, now: float, http_post) -> int:
    """One sweep pass. Returns how many deliveries were attempted."""
    claimed = claim_due(client, now)
    for d in claimed:
        await deliver_one(client, d, now=now, http_post=http_post)
    return len(claimed)


async def _default_post(url: str, body: bytes, headers: dict) -> int:
    import httpx

    from brain.net_guard import pin_request, validate_url

    # Connect to a freshly-vetted pinned IP with the hostname preserved (Host + SNI).
    # deliver_one already validated `url`, but that only proves it was safe a moment
    # ago; DNS could rebind to an internal address before this connect. Pinning to the
    # IP we resolve *here* closes that window. validate_url may raise UnsafeUrlError
    # (rebind caught in the act); deliver_one treats a raised post as a transient error
    # and reschedules. follow_redirects stays False.
    ips = validate_url(url)
    pinned = pin_request(url, ips[0])
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as c:
        r = await c.post(
            pinned.url,
            content=body,
            headers={**headers, **pinned.headers},
            follow_redirects=False,
            extensions=pinned.extensions,
        )
        return r.status_code


async def sweeper_loop(interval_s: float = 15.0) -> None:
    """Run sweeps forever. Started on gateway boot; cancelled on shutdown."""
    import time

    from brain.second_brain import supabase_client

    while True:
        try:
            if supabase_client.is_enabled():
                await sweep_once(
                    supabase_client.get_client(), now=time.time(), http_post=_default_post
                )
        except Exception as e:  # a sweep must never kill its own loop
            logger.warning("[webhooks] sweep error: %s", e)
        await asyncio.sleep(interval_s)
