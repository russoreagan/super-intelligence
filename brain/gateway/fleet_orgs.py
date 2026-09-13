"""
Superadmin cross-org fleet view (plan §2.5) — the data behind the gateway's
`GET /__fleet/orgs` and `GET /__fleet/deploy`.

The gateway is the only process that sees every org at once: it owns the
provisioner (which brains are live, their RSS and tier), the sleep status, the
pod, and a service-role Supabase client that is not pinned to an org. Everything
here is a count, a state, a cost or a timestamp — no persona rows, no content —
so the view can be shown to the platform admin without consulting the read
policy. Per org:

  org_id, org_name, learning_mode, instance_seed   organizations (one query)
  persona_count, clone_count, templates,           personas index (039): head
  active_roster_size                                 counts, null when absent
  live_brains [{key, persona, tier, rss_mb,        provisioner.tenant_stats()
               uptime_s, booting}]
  sleep_state                                      the gateway's sleep status
  cost_24h, cost_7d, pod_s                         agent_usage_totals_all_daily
                                                     (falls back to the raw RPC)
  breaker, dormant, idle_s, roster_size            each LIVE brain's /health

The per-brain /health fetch is bounded: at most BRAIN_MAX_TENANTS calls per
build, 2 s timeout each, cached 30 s per process key. The Supabase snapshot is
cached for the same 30 s so the console's refresh timer never becomes a query
storm against the platform database. If Supabase cannot be reached the view
still returns what the provisioner knows, with nulls for the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
import os
import time

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = 2.0
HEALTH_CACHE_S = 30.0
DB_CACHE_S = 30.0
ACTIVE_DAYS = 7

_STARTED_AT = time.time()

# process key → (fetched_at, health dict | None)
_health_cache: dict[str, tuple[float, dict | None]] = {}
# (fetched_at, snapshot) of the Supabase-derived part
_db_cache: tuple[float, dict] | None = None


def _reset_for_tests() -> None:
    global _db_cache
    _health_cache.clear()
    _db_cache = None


def _client():
    """The service-role Supabase client, or None when storage is local / the
    client cannot be built. The gateway is not pinned to an org, so this never
    touches get_org_id()."""
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client()
    except Exception as e:
        logger.debug("[fleet_orgs] no supabase client: %s", e)
        return None


def max_tenants() -> int:
    try:
        from brain import provisioner as _p

        return int(os.environ.get("BRAIN_MAX_TENANTS", str(_p.MAX_TENANTS)) or 0)
    except Exception:
        return int(os.environ.get("BRAIN_MAX_TENANTS", "25") or 0)


# ── Supabase side ───────────────────────────────────────────────────────────────


def _org_rows(client) -> list[dict] | None:
    """[{org_id, org_name, learning_mode, instance_seed}] for every org, or None
    when the table cannot be read."""
    try:
        res = client.table("organizations").select("id,name,learning_mode,instance_seed").execute()
    except Exception as e:
        logger.debug("[fleet_orgs] organizations read failed: %s", e)
        return None
    out = []
    for r in res.data or []:
        oid = str(r.get("id") or "")
        if not oid:
            continue
        out.append(
            {
                "org_id": oid,
                "org_name": str(r.get("name") or ""),
                "learning_mode": r.get("learning_mode") or None,
                "instance_seed": r.get("instance_seed") or None,
            }
        )
    return out


def _head_count(client, org_id: str, **filters) -> int | None:
    """One `count=exact, head=True` query on the personas index for an org."""
    try:
        q = (
            client.table("personas")
            .select("persona", count="exact", head=True)
            .eq("org_id", org_id)
            .is_("deleted_at", "null")
        )
        for name, args in filters.items():
            q = getattr(q, name)(*args)
        res = q.execute()
        n = getattr(res, "count", None)
        if n is None:
            n = len(getattr(res, "data", None) or [])
        return int(n)
    except Exception as e:
        logger.debug("[fleet_orgs] persona count failed for %s: %s", org_id[:8], e)
        return None


def _persona_counts(client, org_id: str, now: float) -> dict:
    """persona_count (custom, live), clone_count (template <> ''), templates
    (custom minus clones) and active_roster_size (a human turn within
    ACTIVE_DAYS) — all null when the index table is absent."""
    customs = _head_count(client, org_id, eq=("builtin", False))
    if customs is None:
        return {
            "persona_count": None,
            "clone_count": None,
            "templates": None,
            "active_roster_size": None,
        }
    clones = _head_count(client, org_id, eq=("builtin", False), neq=("template", ""))
    cutoff = _dt.datetime.fromtimestamp(now - ACTIVE_DAYS * 86400, _dt.UTC).isoformat()
    active = _head_count(client, org_id, gte=("last_human_turn_ts", cutoff))
    return {
        "persona_count": customs,
        "clone_count": clones,
        "templates": (customs - clones) if clones is not None else None,
        "active_roster_size": active,
    }


def db_snapshot(now: float | None = None) -> dict:
    """The Supabase-derived part of the view, cached DB_CACHE_S:
    {orgs: [...] | None, counts: {org_id: {...}}, usage_24h, usage_7d, usage_source}."""
    global _db_cache
    now = time.time() if now is None else now
    if _db_cache is not None and now - _db_cache[0] < DB_CACHE_S:
        return _db_cache[1]
    snap: dict = {
        "orgs": None,
        "counts": {},
        "usage_24h": {},
        "usage_7d": {},
        "usage_source": "none",
    }
    client = _client()
    if client is not None:
        from brain import agent_usage_store

        orgs = _org_rows(client)
        snap["orgs"] = orgs
        for o in orgs or []:
            snap["counts"][o["org_id"]] = _persona_counts(client, o["org_id"], now)

        def _since(seconds: float) -> tuple[str, str]:
            t = _dt.datetime.fromtimestamp(now - seconds, _dt.UTC)
            return t.isoformat(), t.date().isoformat()

        iso24, date24 = _since(86400)
        iso7, date7 = _since(7 * 86400)
        snap["usage_24h"], src = agent_usage_store.totals_all_by_org(client, iso24, date24)
        snap["usage_7d"], _ = agent_usage_store.totals_all_by_org(client, iso7, date7)
        snap["usage_source"] = src
    _db_cache = (now, snap)
    return snap


# ── live brains ─────────────────────────────────────────────────────────────────


def _split_key(key: str) -> tuple[str, str | None]:
    org, _, persona = str(key).partition("::")
    return org, (persona or None)


async def fetch_health(port: int, timeout_s: float = HEALTH_TIMEOUT_S) -> dict | None:
    """One brain's /health body, or None (down, slow, unparsable)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.get(f"http://127.0.0.1:{int(port)}/health")
            if r.status_code != 200:
                return None
            body = r.json()
            return body if isinstance(body, dict) else None
    except Exception:
        return None


async def brain_healths(
    provisioner, stats: list[dict], now: float | None = None, fetch=None
) -> dict[str, dict | None]:
    """{process key: /health body | None} for the live, non-booting brains in
    `stats`, at most max_tenants() fetches per call, each cached HEALTH_CACHE_S.
    A brain that is booting or whose port is unknown is skipped (None). `fetch`
    defaults to fetch_health, resolved at call time so a test can swap it."""
    fetch = fetch or fetch_health
    now = time.time() if now is None else now
    out: dict[str, dict | None] = {}
    todo: list[tuple[str, int]] = []
    budget = max(0, max_tenants())
    for s in stats:
        key = str(s.get("key") or "")
        if not key:
            continue
        hit = _health_cache.get(key)
        if hit is not None and now - hit[0] < HEALTH_CACHE_S:
            out[key] = hit[1]
            continue
        if s.get("booting"):
            out[key] = None
            continue
        org, persona = _split_key(key)
        st = None
        with contextlib.suppress(Exception):
            st = provisioner.status(org, persona)
        port = (st or {}).get("port")
        if not port or len(todo) >= budget:
            out[key] = None
            continue
        todo.append((key, int(port)))
    if todo:
        results = await asyncio.gather(*(fetch(port) for _k, port in todo), return_exceptions=True)
        for (key, _port), body in zip(todo, results, strict=True):
            body = body if isinstance(body, dict) else None
            _health_cache[key] = (now, body)
            out[key] = body
    return out


def _health_fields(body: dict | None) -> dict:
    if not body:
        return {"breaker": None, "dormant": None, "idle_s": None, "roster_size": None}
    outages = body.get("provider_outages")
    dmn = body.get("dmn") if isinstance(body.get("dmn"), dict) else {}
    return {
        "breaker": outages if outages else None,
        "dormant": (bool(dmn.get("dormant")) if dmn else None),
        "idle_s": dmn.get("idle_s") if dmn else None,
        "roster_size": dmn.get("roster_size") if dmn else None,
    }


def _brain_row(s: dict) -> dict:
    _org, persona = _split_key(str(s.get("key") or ""))
    return {
        "key": s.get("key"),
        "persona": persona,
        "tier": s.get("tier"),
        "rss_mb": s.get("rss_mb"),
        "uptime_s": s.get("uptime_s"),
        "booting": bool(s.get("booting")),
    }


# ── the view ────────────────────────────────────────────────────────────────────


async def build_orgs_view(
    provisioner, sleep_status: dict | None, now: float | None = None, fetch=None
) -> dict:
    """{orgs: [row...], live: n, full: n, max_tenants: n, usage_source, generated_at}.
    One row per org known to Supabase, plus a row for any org with a live brain
    that Supabase did not list (or when Supabase is unreachable) — the live
    process is the ground truth for "is it running", the table for "what is it"."""
    now = time.time() if now is None else now
    stats: list[dict] = []
    with contextlib.suppress(Exception):
        stats = list(provisioner.tenant_stats() or [])
    snap = await asyncio.to_thread(db_snapshot, now)
    healths = await brain_healths(provisioner, stats, now, fetch=fetch)

    by_org: dict[str, list[dict]] = {}
    for s in stats:
        org, _p = _split_key(str(s.get("key") or ""))
        by_org.setdefault(org, []).append(s)

    rows: dict[str, dict] = {}
    for o in snap.get("orgs") or []:
        rows[o["org_id"]] = dict(o)
    for org in by_org:
        rows.setdefault(
            org,
            {"org_id": org, "org_name": "", "learning_mode": None, "instance_seed": None},
        )

    out_rows = []
    for org_id, row in rows.items():
        counts = snap["counts"].get(org_id) or {
            "persona_count": None,
            "clone_count": None,
            "templates": None,
            "active_roster_size": None,
        }
        live = by_org.get(org_id, [])
        # The default (shared) instance answers for the org's DMN; a dedicated
        # persona instance only when there is no default. Breaker = any instance.
        default = next((s for s in live if "::" not in str(s.get("key"))), None)
        primary = default or (live[0] if live else None)
        hf = _health_fields(healths.get(str(primary.get("key"))) if primary else None)
        breakers = [(healths.get(str(s.get("key"))) or {}).get("provider_outages") for s in live]
        breakers = [b for b in breakers if b]
        if breakers and not hf["breaker"]:
            hf["breaker"] = breakers[0]
        u24 = (snap.get("usage_24h") or {}).get(org_id) or {}
        u7 = (snap.get("usage_7d") or {}).get(org_id) or {}
        if not row.get("org_name"):
            row["org_name"] = u7.get("org_name") or u24.get("org_name") or ""
        sleep = (sleep_status or {}).get(org_id) or {}
        out_rows.append(
            {
                **row,
                **counts,
                "live_brains": [_brain_row(s) for s in live],
                "sleep_state": sleep.get("state") or ("awake" if live else None),
                "cost_24h": round(float(u24.get("cloud_usd") or 0.0), 4) if u24 else None,
                "cost_7d": round(float(u7.get("cloud_usd") or 0.0), 4) if u7 else None,
                "pod_s": round(float(u7.get("pod_s") or 0.0)) if u7 else None,
                **hf,
            }
        )
    out_rows.sort(key=lambda r: (-(len(r["live_brains"])), -(r["cost_7d"] or 0), r["org_id"]))
    live_n = sum(1 for s in stats)
    full_n = sum(1 for s in stats if s.get("tier") != "lite")
    return {
        "orgs": out_rows,
        "live": live_n,
        "full": full_n,
        "max_tenants": max_tenants(),
        "usage_source": snap.get("usage_source", "none"),
        "generated_at": _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(),
    }


def deploy_view(provisioner) -> dict:
    """{sha, started_at, live, full, max_tenants} — what code this gateway runs
    and how full the host is. `sha` is RAILWAY_GIT_COMMIT_SHA (the same value the
    unauthenticated /health reports as `commit`)."""
    live = full = 0
    with contextlib.suppress(Exception):
        live = int(provisioner.live_count())
    with contextlib.suppress(Exception):
        full = int(provisioner.full_count())
    return {
        "sha": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:12],
        "started_at": _dt.datetime.fromtimestamp(_STARTED_AT, _dt.UTC).isoformat(),
        "uptime_s": round(time.time() - _STARTED_AT),
        "live": live,
        "full": full,
        "max_tenants": max_tenants(),
    }
