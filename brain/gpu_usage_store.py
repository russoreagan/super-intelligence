"""Wall-clock metering for standalone / org GPU pods (migration 038, `gpu_usage`).

The platform pool's share is already metered per persona as `agent_usage.pod_s`
(016) — inference seconds on a shared card. A standalone or org pod bills for its
UPTIME whether or not anyone is inferring, so it needs its own ledger: the gateway's
placement controller writes one additive row per tick per persona (`pod_kind`
`standalone`, or `org` with the tick split evenly across the org's dedicated
instances). Summing rows over [since, until) is correct across restarts, exactly
like agent_usage.

Same best-effort shape as brain/agent_usage_store.py: writes never fail the
caller; reads return [] before the migration is applied (one debug line) so
GET /v1/usage simply shows no dedicated hours.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

TABLE = "gpu_usage"
KINDS = ("standalone", "org", "pool")


def _sb():
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def record(org_id: str, rows: list[dict], client=None) -> bool:
    """Append additive uptime rows for ONE org (the gateway passes the org
    explicitly — it serves many). Each row: {persona, pod_kind, pod_id, seconds,
    usd}. Best-effort; True when written."""
    if not rows or not org_id:
        return False
    if client is None:
        sb = _sb()
        if sb is None:
            return False
        client = sb[0]
    payload = [
        {
            "org_id": org_id,
            "persona": str(r.get("persona") or ""),
            "pod_kind": str(r.get("pod_kind") or "standalone")
            if str(r.get("pod_kind") or "standalone") in KINDS
            else "standalone",
            "pod_id": str(r.get("pod_id") or ""),
            "seconds": float(r.get("seconds") or 0.0),
            "usd": float(r.get("usd") or 0.0),
        }
        for r in rows
        if float(r.get("seconds") or 0.0) > 0
    ]
    if not payload:
        return False
    try:
        client.table("gpu_usage").insert(payload).execute()
        return True
    except Exception as e:
        logger.debug("[gpu_usage] record skipped: %s", e)
        return False


def by_day(since_iso: str | None = None, until_iso: str | None = None) -> list[dict]:
    """Per-day (UTC), per-persona, per-pod_kind sums over [since, until) for THIS
    org: [{day, persona, pod_kind, seconds, usd}]. [] on any error or local mode
    (RPC not yet applied → graceful empty)."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    try:
        res = client.rpc(
            "gpu_usage_by_day", {"p_org_id": org, "p_since": since_iso, "p_until": until_iso}
        ).execute()
        rows = res.data or []
    except Exception as e:
        logger.debug("[gpu_usage] by_day skipped: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "day": str(r.get("day") or ""),
                "persona": str(r.get("persona") or ""),
                "pod_kind": str(r.get("pod_kind") or ""),
                "seconds": float(r.get("seconds") or 0.0),
                "usd": float(r.get("usd") or 0.0),
            }
        )
    return out


def usd_today() -> float:
    """What this org has spent on standalone / org pods so far today (UTC) — the
    figure GET /v1/usage reports against gpu_daily_usd_budget."""
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = by_day(start.isoformat(), (start + timedelta(days=1)).isoformat())
    return round(sum(r["usd"] for r in rows), 4)


def rate_per_hr() -> float:
    """$/hr the pool bills at, for reporting pool time in dollars (plan §10.6 #8:
    basic-tier usage is pod_s × rate, pricing wording only). The pool file's pool
    pods when the gateway publishes one, else the pod ledger's live/env/fallback
    rate."""
    try:
        from brain.placement_client import pool_file_path

        path = pool_file_path()
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            rates = [
                float(p.get("cost_per_hr") or 0.0)
                for p in (data.get("pods") or [])
                if isinstance(p, dict) and p.get("kind") == "pool" and p.get("cost_per_hr")
            ]
            if rates:
                return round(max(rates), 4)
    except Exception as e:
        logger.debug("[gpu_usage] pool rate unavailable: %s", e)
    try:
        from brain import pod_budget

        return float(pod_budget.rate_per_hr())
    except Exception:
        return 0.5
