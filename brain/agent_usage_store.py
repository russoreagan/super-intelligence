"""
Durable per-agent model-usage ledger (migration 016_agent_usage).

The live ModelRouter meters each agent's model usage in memory (tokens, pod
compute-seconds, cloud $). That resets every time the brain process restarts, so
it can't answer "what did this agent cost over the last 7 days across all the
times it ran." The router periodically flushes its DELTA-since-last-flush here;
summing the rows in a [since, until] window gives the cumulative total, correct
across any number of boot/shutdown cycles.

Best-effort and Supabase-backed, exactly like brain/agent_log.py: a write never
fails the caller, and companion/local mode (no Supabase) is a silent no-op (the
in-memory meter still powers the live "This session" view).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_FIELDS = ("calls", "cloud_calls", "in_tok", "out_tok", "cloud_usd", "pod_s")


def _sb():
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def record_deltas(rows: list[dict]) -> bool:
    """Append one additive usage-delta row per agent. Best-effort; returns True if
    written. Each row carries the usage accumulated since the previous flush."""
    if not rows:
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    payload = [
        {
            "org_id": org,
            "agent_id": str(r.get("agent_id") or ""),
            "persona": str(r.get("persona") or ""),
            "calls": int(r.get("calls") or 0),
            "cloud_calls": int(r.get("cloud_calls") or 0),
            "in_tok": int(r.get("in_tok") or 0),
            "out_tok": int(r.get("out_tok") or 0),
            "cloud_usd": float(r.get("cloud_usd") or 0.0),
            "pod_s": float(r.get("pod_s") or 0.0),
        }
        for r in rows
    ]
    try:
        client.table("agent_usage").insert(payload).execute()
        return True
    except Exception as e:
        logger.debug("[agent_usage] record skipped: %s", e)
        return False


def aggregate(since_iso: str | None = None, until_iso: str | None = None) -> dict:
    """Per-agent cumulative totals over [since, until] (ISO-8601 strings, either may
    be None). Returns { agent_id: {calls, cloud_calls, in_tok, out_tok, cloud_usd,
    pod_s, last_ts} }. Empty on any error or local mode (table/RPC not yet applied
    → graceful empty, so the dashboard simply shows no range data)."""
    sb = _sb()
    if sb is None:
        return {}
    client, org = sb
    try:
        res = client.rpc(
            "agent_usage_totals",
            {"p_org_id": org, "p_since": since_iso, "p_until": until_iso},
        ).execute()
        rows = res.data or []
    except Exception as e:
        logger.debug("[agent_usage] aggregate skipped: %s", e)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        aid = r.get("agent_id") or ""
        if not aid or aid == "owner":
            continue
        out[aid] = {
            "calls": int(r.get("calls") or 0),
            "cloud_calls": int(r.get("cloud_calls") or 0),
            "in_tok": int(r.get("in_tok") or 0),
            "out_tok": int(r.get("out_tok") or 0),
            "cloud_usd": float(r.get("cloud_usd") or 0.0),
            "pod_s": float(r.get("pod_s") or 0.0),
            "last_ts": r.get("last_ts") or "",
        }
    return out


def aggregate_all(since_iso: str | None = None, until_iso: str | None = None) -> list[dict]:
    """Cross-org rollup for the platform super-admin's "All orgs" view: one row per
    (org, agent) over [since, until]. Returns [] on any error or local mode. Uses a
    service-role-only RPC, so only a platform process can read it — the caller (the
    /agents/usage endpoint) is still responsible for gating this to is_admin."""
    sb = _sb()
    if sb is None:
        return []
    client, _org = sb
    try:
        res = client.rpc(
            "agent_usage_totals_all", {"p_since": since_iso, "p_until": until_iso}
        ).execute()
        rows = res.data or []
    except Exception as e:
        logger.debug("[agent_usage] aggregate_all skipped: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        aid = r.get("agent_id") or ""
        if not aid or aid == "owner":
            continue
        out.append({
            "org_id": str(r.get("org_id") or ""),
            "org_name": r.get("org_name") or "",
            "agent_id": aid,
            "calls": int(r.get("calls") or 0),
            "cloud_calls": int(r.get("cloud_calls") or 0),
            "in_tok": int(r.get("in_tok") or 0),
            "out_tok": int(r.get("out_tok") or 0),
            "cloud_usd": float(r.get("cloud_usd") or 0.0),
            "pod_s": float(r.get("pod_s") or 0.0),
            "last_ts": r.get("last_ts") or "",
        })
    return out
