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


def _row(r: dict, org: str | None = None) -> dict:
    out = {
        "agent_id": str(r.get("agent_id") or ""),
        "end_user_id": str(r.get("end_user_id") or ""),
        "persona": str(r.get("persona") or ""),
        "calls": int(r.get("calls") or 0),
        "cloud_calls": int(r.get("cloud_calls") or 0),
        "in_tok": int(r.get("in_tok") or 0),
        "out_tok": int(r.get("out_tok") or 0),
        "cloud_usd": float(r.get("cloud_usd") or 0.0),
        "pod_s": float(r.get("pod_s") or 0.0),
    }
    if org is not None:
        out = {"org_id": org, **out}
    return out


# Raw agent_usage gained end_user_id in migration 039. Until it is applied, an
# insert carrying the column fails; remember that for a while and strip it (the
# rows still land, per agent) rather than lose every raw delta pre-migration.
_raw_end_user_retry_ts: float = 0.0
_RAW_END_USER_RETRY_S = 600.0


def record_deltas(rows: list[dict]) -> bool:
    """Append one additive usage-delta row per (agent, end_user). Best-effort;
    returns True if written. Each row carries the usage accumulated since the
    previous flush."""
    global _raw_end_user_retry_ts
    if not rows:
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    import time as _time

    payload = [_row(r, org) for r in rows]
    strip = _raw_end_user_retry_ts > _time.time()
    if strip:
        for p in payload:
            p.pop("end_user_id", None)
    try:
        client.table("agent_usage").insert(payload).execute()
        return True
    except Exception as e:
        if not strip and "end_user_id" in str(e):
            _raw_end_user_retry_ts = _time.time() + _RAW_END_USER_RETRY_S
            logger.debug("[agent_usage] end_user_id column missing (039) — retrying without")
            for p in payload:
                p.pop("end_user_id", None)
            try:
                client.table("agent_usage").insert(payload).execute()
                return True
            except Exception as e2:
                logger.debug("[agent_usage] record skipped: %s", e2)
                return False
        logger.debug("[agent_usage] record skipped: %s", e)
        return False


def bump_daily(rows: list[dict]) -> bool:
    """Add the same delta rows into agent_usage_daily (migration 039) in ONE
    `bump_agent_usage_daily` RPC (on-conflict add, keyed org/date/agent/end_user).
    Best-effort; False when not written (no backend, RPC missing, error)."""
    if not rows:
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        client.rpc(
            "bump_agent_usage_daily", {"p_org_id": org, "p_rows": [_row(r) for r in rows]}
        ).execute()
        return True
    except Exception as e:
        logger.debug("[agent_usage] daily bump skipped: %s", e)
        return False


def prune_raw(days: int) -> int | None:
    """Delete this org's raw agent_usage rows older than `days` (the daily rollup
    keeps the history). Returns rows removed, or None when skipped/failed."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    import datetime as _dt

    cutoff = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).isoformat()
    try:
        res = client.table("agent_usage").delete().eq("org_id", org).lt("ts", cutoff).execute()
        return len(getattr(res, "data", None) or [])
    except Exception as e:
        logger.debug("[agent_usage] prune skipped: %s", e)
        return None


def _read_daily() -> bool:
    try:
        from brain.settings import settings

        return bool(int(settings.get("agent_usage_read_daily", 1) or 0))
    except Exception:
        return True


def _iso_date(iso: str | None) -> str | None:
    """ISO-8601 datetime (or date) → 'YYYY-MM-DD' for the daily RPCs' DATE params."""
    if not iso:
        return None
    s = str(iso)
    return s[:10] if len(s) >= 10 else s


def _rows_daily_or_raw(client, org, since_iso, until_iso) -> list[dict]:
    """Per-agent rows from agent_usage_totals_daily (migration 039) when enabled,
    else — or on any daily failure (RPC missing) — from the raw agent_usage_totals."""
    if _read_daily():
        try:
            res = client.rpc(
                "agent_usage_totals_daily",
                {"p_org_id": org, "p_since": _iso_date(since_iso), "p_until": _iso_date(until_iso)},
            ).execute()
            return res.data or []
        except Exception as e:
            logger.debug("[agent_usage] daily aggregate unavailable, raw fallback: %s", e)
    res = client.rpc(
        "agent_usage_totals",
        {"p_org_id": org, "p_since": since_iso, "p_until": until_iso},
    ).execute()
    return res.data or []


def _totals_row(r: dict) -> dict:
    return {
        "calls": int(r.get("calls") or 0),
        "cloud_calls": int(r.get("cloud_calls") or 0),
        "in_tok": int(r.get("in_tok") or 0),
        "out_tok": int(r.get("out_tok") or 0),
        "cloud_usd": float(r.get("cloud_usd") or 0.0),
        "pod_s": float(r.get("pod_s") or 0.0),
        "last_ts": r.get("last_ts") or "",
    }


def persona_totals(
    personas: list[str] | tuple[str, ...],
    since_iso: str | None = None,
    until_iso: str | None = None,
) -> dict[str, dict]:
    """Per-PERSONA totals for the given slugs over [since, until] from the daily
    rollup (RPC persona_usage_totals, O(page)). {} without the daily table."""
    slugs = sorted({str(p) for p in personas if p})
    sb = _sb()
    if sb is None or not slugs or not _read_daily():
        return {}
    client, org = sb
    try:
        res = client.rpc(
            "persona_usage_totals",
            {
                "p_org_id": org,
                "p_personas": slugs,
                "p_since": _iso_date(since_iso),
                "p_until": _iso_date(until_iso),
            },
        ).execute()
    except Exception as e:
        logger.debug("[agent_usage] persona totals skipped: %s", e)
        return {}
    return {str(r.get("persona")): _totals_row(r) for r in (res.data or []) if r.get("persona")}


def aggregate(since_iso: str | None = None, until_iso: str | None = None) -> dict:
    """Per-agent cumulative totals over [since, until] (ISO-8601 strings, either may
    be None). Returns { agent_id: {calls, cloud_calls, in_tok, out_tok, cloud_usd,
    pod_s, last_ts} }. Reads the daily rollup (agent_usage_read_daily) and falls
    back to the raw deltas. Empty on any error or local mode (table/RPC not yet
    applied → graceful empty, so the dashboard simply shows no range data)."""
    sb = _sb()
    if sb is None:
        return {}
    client, org = sb
    try:
        rows = _rows_daily_or_raw(client, org, since_iso, until_iso)
    except Exception as e:
        logger.debug("[agent_usage] aggregate skipped: %s", e)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        aid = r.get("agent_id") or ""
        if not aid or aid == "owner":
            continue
        out[aid] = _totals_row(r)
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
        out.append(
            {
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
            }
        )
    return out


def regroup_by_org(rows: list[dict]) -> dict[str, dict]:
    """Fold cross-org RPC rows (one per org × agent) into one total per org_id:
    {org_id: {org_name, cloud_usd, pod_s, calls}}. Unlike aggregate_all this KEEPS
    the owner lane — for a platform-wide cost view the home process's idle GPU
    seconds are spend like any other."""
    out: dict[str, dict] = {}
    for r in rows or []:
        oid = str(r.get("org_id") or "")
        if not oid:
            continue
        acc = out.setdefault(
            oid, {"org_name": r.get("org_name") or "", "cloud_usd": 0.0, "pod_s": 0.0, "calls": 0}
        )
        if not acc["org_name"] and r.get("org_name"):
            acc["org_name"] = r["org_name"]
        acc["cloud_usd"] += float(r.get("cloud_usd") or 0.0)
        acc["pod_s"] += float(r.get("pod_s") or 0.0)
        acc["calls"] += int(r.get("calls") or 0)
    return out


def totals_all_by_org(client, since_iso: str, since_date: str) -> tuple[dict[str, dict], str]:
    """Cross-org spend per org since a moment, for the platform super-admin's fleet
    view. Takes the Supabase client explicitly because the gateway (the caller) is
    not pinned to an org. Reads the daily rollup RPC (migration 039,
    `agent_usage_totals_all_daily`, DATE-grained: `since_date`) and falls back to
    the raw-row RPC (017, `agent_usage_totals_all`, timestamp-grained: `since_iso`)
    when the daily one is missing or fails. Returns (per-org totals, source) with
    source in {'daily', 'raw', 'none'}; empty on any error (never raises)."""
    if client is None:
        return {}, "none"
    try:
        res = client.rpc(
            "agent_usage_totals_all_daily", {"p_since": since_date, "p_until": None}
        ).execute()
        return regroup_by_org(res.data or []), "daily"
    except Exception as e:
        logger.debug("[agent_usage] totals_all_by_org daily skipped: %s", e)
    try:
        res = client.rpc(
            "agent_usage_totals_all", {"p_since": since_iso, "p_until": None}
        ).execute()
        return regroup_by_org(res.data or []), "raw"
    except Exception as e:
        logger.debug("[agent_usage] totals_all_by_org raw skipped: %s", e)
    return {}, "none"


def _by_day_rows(client, org, since_iso, until_iso) -> list[dict]:
    """Per-day rows from agent_usage_by_day_daily (migration 040, over the daily
    rollup) when the daily read is enabled, else — or on any daily failure (RPC
    not yet applied) — from the raw agent_usage_by_day (038). The raw ledger is
    pruned to agent_usage_raw_retention_days (7), so without the daily RPC a
    92-day window silently lost everything older than a week."""
    if _read_daily():
        try:
            res = client.rpc(
                "agent_usage_by_day_daily",
                {"p_org_id": org, "p_since": _iso_date(since_iso), "p_until": _iso_date(until_iso)},
            ).execute()
            return res.data or []
        except Exception as e:
            logger.debug("[agent_usage] daily by_day unavailable, raw fallback: %s", e)
    res = client.rpc(
        "agent_usage_by_day",
        {"p_org_id": org, "p_since": since_iso, "p_until": until_iso},
    ).execute()
    return res.data or []


def by_day(since_iso: str | None = None, until_iso: str | None = None) -> list[dict]:
    """Per-day (UTC), per-persona, per-agent sums over [since, until) behind
    GET /v1/usage — the daily rollup RPC agent_usage_by_day_daily (040), falling
    back to the raw agent_usage_by_day (038). Unlike aggregate() this KEEPS the
    owner lane: DMN idle thinking has no agent_id and lands there, and for a
    usage bill a persona's idle GPU seconds are exactly the point. Rows with an
    empty persona are the org's home process. [] on any error or local mode."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    try:
        rows = _by_day_rows(client, org, since_iso, until_iso)
    except Exception as e:
        logger.debug("[agent_usage] by_day skipped: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "day": str(r.get("day") or ""),
                "persona": str(r.get("persona") or ""),
                "agent_id": str(r.get("agent_id") or ""),
                "calls": int(r.get("calls") or 0),
                "cloud_calls": int(r.get("cloud_calls") or 0),
                "in_tok": int(r.get("in_tok") or 0),
                "out_tok": int(r.get("out_tok") or 0),
                "cloud_usd": float(r.get("cloud_usd") or 0.0),
                "pod_s": float(r.get("pod_s") or 0.0),
            }
        )
    return out
