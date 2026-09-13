"""Superadmin cross-org fleet view (plan §2.5): gateway GET /__fleet/orgs and
GET /__fleet/deploy.

Platform admin only (ui_auth.is_admin) — an org admin is refused like any other
member. Rows are content-free by construction; the per-org cost comes from the
daily rollup RPC regrouped by org (raw-row RPC as fallback); the per-brain
/health fetch is capped at BRAIN_MAX_TENANTS calls and cached 30 s.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from brain import agent_usage_store
from brain.gateway import fleet_orgs as fo
from brain.gateway import server as gw
from brain.ui import auth as ui_auth

CONTENT_KEYS = {
    "prompt",
    "response",
    "goal",
    "summary",
    "thought",
    "tool_input",
    "end_user_id",
    "text",
    "user_input",
}


def _walk(o, found: set):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in CONTENT_KEYS:
                found.add(k)
            _walk(v, found)
    elif isinstance(o, list):
        for v in o:
            _walk(v, found)


# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeProv:
    def __init__(self, stats=None):
        self._stats = stats or []

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    def tenant_stats(self):
        return [dict(s) for s in self._stats]

    def status(self, org, persona=None):
        key = org if not persona else f"{org}::{persona}"
        for s in self._stats:
            if s["key"] == key:
                return {"port": s["port"], "api_port": None, "booting": s["booting"], "pid": 1}
        return None

    def live_count(self):
        return len(self._stats)

    def full_count(self):
        return sum(1 for s in self._stats if s["tier"] != "lite")

    def keys_for(self, org):  # pragma: no cover
        return [s["key"] for s in self._stats if s["key"].split("::")[0] == org]

    def touch(self, *a):  # pragma: no cover
        pass


class _Res:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _Query:
    def __init__(self, sb, table):
        self.sb, self.table, self.filters, self.kwargs = sb, table, [], {}

    def select(self, *a, **k):
        self.kwargs = k
        return self

    def __getattr__(self, name):
        if name in ("eq", "is_", "neq", "gte"):

            def _f(*a):
                self.filters.append((name, *a))
                return self

            return _f
        raise AttributeError(name)

    def execute(self):
        self.sb.calls.append((self.table, list(self.filters), dict(self.kwargs)))
        if self.table in self.sb.fail:
            raise RuntimeError(self.sb.fail[self.table])
        if self.table == "organizations":
            return _Res(self.sb.orgs)
        if self.table == "personas":
            org = next((f[2] for f in self.filters if f[0] == "eq" and f[1] == "org_id"), None)
            kinds = {f[0] for f in self.filters}
            c = self.sb.counts.get(org, {})
            if "neq" in kinds:
                return _Res(count=c.get("clones"))
            if "gte" in kinds:
                return _Res(count=c.get("active"))
            return _Res(count=c.get("customs"))
        return _Res()


class _FakeSb:
    def __init__(self):
        self.calls = []
        self.rpcs = []
        self.fail = {}
        self.fail_rpc = set()
        self.orgs = []
        self.counts = {}
        self.rpc_rows = {}

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        sb = self

        class _Rpc:
            def execute(self_inner):
                sb.rpcs.append((name, dict(params)))
                if name in sb.fail_rpc:
                    raise RuntimeError(f"function {name} does not exist")
                return _Res(sb.rpc_rows.get(name, []))

        return _Rpc()


@pytest.fixture
def sb(monkeypatch):
    from brain.second_brain import supabase_client

    fake = _FakeSb()
    fake.orgs = [
        {"id": "org-a", "name": "Acme", "learning_mode": "isolated", "instance_seed": "default"},
        {
            "id": "org-b",
            "name": "Bolt",
            "learning_mode": "consolidated",
            "instance_seed": "current",
        },
    ]
    fake.counts = {"org-a": {"customs": 120, "clones": 117, "active": 40}}
    fake.rpc_rows["agent_usage_totals_all_daily"] = [
        {
            "org_id": "org-a",
            "org_name": "Acme",
            "agent_id": "t_x.sales",
            "cloud_usd": 1.5,
            "pod_s": 30,
            "calls": 3,
        },
        {
            "org_id": "org-a",
            "org_name": "Acme",
            "agent_id": "",
            "cloud_usd": 0.25,
            "pod_s": 600,
            "calls": 9,
        },
        {
            "org_id": "org-b",
            "org_name": "Bolt",
            "agent_id": "owner",
            "cloud_usd": 2.0,
            "pod_s": 0,
            "calls": 1,
        },
    ]
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    fo._reset_for_tests()
    yield fake
    fo._reset_for_tests()


@contextlib.contextmanager
def _auth_patched(claims: dict, org_admin: bool = False):
    orig = (
        ui_auth.is_disabled,
        ui_auth.is_configured,
        ui_auth.authenticate,
        ui_auth.set_session_cookies,
        ui_auth.is_org_admin,
    )
    ui_auth.is_disabled = lambda: False
    ui_auth.is_configured = lambda: True

    async def _fake_auth(_request):
        return dict(claims), None

    ui_auth.authenticate = _fake_auth
    ui_auth.set_session_cookies = lambda *a, **k: None
    if org_admin:
        ui_auth.is_org_admin = lambda c: True
    try:
        yield
    finally:
        (
            ui_auth.is_disabled,
            ui_auth.is_configured,
            ui_auth.authenticate,
            ui_auth.set_session_cookies,
            ui_auth.is_org_admin,
        ) = orig


ADMIN = {"sub": "u1", "email": "a@x", "app_metadata": {"is_admin": True}}
MEMBER = {"sub": "u2", "email": "m@x", "app_metadata": {}}


async def _get(prov, path):
    app = gw.build_gateway_app(prov, [None])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path, headers={"accept": "application/json"})


# ── gating ──────────────────────────────────────────────────────────────────


def test_fleet_routes_refuse_non_admins(sb, monkeypatch):
    monkeypatch.delenv("BRAIN_ADMIN_EMAILS", raising=False)
    prov = _FakeProv()
    with _auth_patched(MEMBER):
        for path in ("/__fleet/orgs", "/__fleet/deploy"):
            r = asyncio.run(_get(prov, path))
            assert r.status_code == 403, path
    # An ORG admin is still not a platform admin.
    with _auth_patched(MEMBER, org_admin=True):
        assert ui_auth.is_org_admin(MEMBER) is True
        assert asyncio.run(_get(prov, "/__fleet/orgs")).status_code == 403
        assert asyncio.run(_get(prov, "/__fleet/deploy")).status_code == 403
    assert sb.calls == [] and sb.rpcs == []  # refused before any database read


# ── the rows ────────────────────────────────────────────────────────────────


def _stats():
    return [
        {
            "key": "org-a",
            "pid": 1,
            "rss_mb": 900.0,
            "uptime_s": 120,
            "tier": "full",
            "booting": False,
            "port": 9001,
        },
        {
            "key": "org-a::t_x",
            "pid": 2,
            "rss_mb": 400.0,
            "uptime_s": 60,
            "tier": "lite",
            "booting": False,
            "port": 9002,
        },
        {
            "key": "org-c",
            "pid": 3,
            "rss_mb": None,
            "uptime_s": 5,
            "tier": "full",
            "booting": True,
            "port": 9003,
        },
    ]


async def _fake_fetch(port, timeout_s=2.0):
    return {
        9001: {
            "status": "ok",
            "tier": "full",
            "provider_outages": {"anthropic": {"until": 1}},
            "dmn": {"dormant": True, "idle_s": 4000, "roster_size": 12},
        },
        9002: {"status": "ok", "tier": "lite"},
    }.get(port)


def test_admin_gets_content_free_org_rows(sb, monkeypatch):
    monkeypatch.setattr(fo, "fetch_health", _fake_fetch)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234567890")
    prov = _FakeProv(_stats())
    with _auth_patched(ADMIN):
        r = asyncio.run(_get(prov, "/__fleet/orgs"))
        d = asyncio.run(_get(prov, "/__fleet/deploy")).json()
    assert r.status_code == 200
    body = r.json()
    found: set = set()
    _walk(body, found)
    assert found == set(), f"content keys leaked: {found}"
    rows = {o["org_id"]: o for o in body["orgs"]}
    assert set(rows) == {"org-a", "org-b", "org-c"}
    a = rows["org-a"]
    assert a["org_name"] == "Acme" and a["learning_mode"] == "isolated"
    assert a["instance_seed"] == "default"
    assert (a["persona_count"], a["clone_count"], a["templates"], a["active_roster_size"]) == (
        120,
        117,
        3,
        40,
    )
    assert [b["persona"] for b in a["live_brains"]] == [None, "t_x"]
    assert a["live_brains"][0]["rss_mb"] == 900.0 and a["live_brains"][1]["tier"] == "lite"
    assert set(a["live_brains"][0]) == {"key", "persona", "tier", "rss_mb", "uptime_s", "booting"}
    # Cost: the daily RPC regrouped by org — owner lane (agent_id '') included.
    assert a["cost_24h"] == 1.75 and a["cost_7d"] == 1.75 and a["pod_s"] == 630
    # Health of the DEFAULT instance answers for the org.
    assert a["breaker"] == {"anthropic": {"until": 1}}
    assert a["dormant"] is True and a["idle_s"] == 4000 and a["roster_size"] == 12
    assert a["sleep_state"] == "awake"
    # No live brain, no index rows for it: zero personas (the table answered),
    # health nulls (nothing to ask), cost from the RPC alone.
    b = rows["org-b"]
    assert b["live_brains"] == [] and b["persona_count"] == 0 and b["active_roster_size"] == 0
    assert b["cost_7d"] == 2.0 and b["breaker"] is None and b["dormant"] is None
    assert b["sleep_state"] is None
    # A live brain Supabase does not know still gets a row (booting → no health).
    c = rows["org-c"]
    assert c["learning_mode"] is None and c["live_brains"][0]["booting"] is True
    assert c["dormant"] is None and c["cost_7d"] is None
    assert body["live"] == 3 and body["full"] == 2 and body["usage_source"] == "daily"
    # Deploy line.
    assert d["sha"] == "abcdef123456" and d["live"] == 3 and d["full"] == 2
    assert d["max_tenants"] == fo.max_tenants() and d["started_at"]


def test_persona_counts_are_null_when_the_index_table_is_missing(sb, monkeypatch):
    sb.fail["personas"] = 'relation "personas" does not exist'
    monkeypatch.setattr(fo, "fetch_health", _fake_fetch)
    with _auth_patched(ADMIN):
        body = asyncio.run(_get(_FakeProv(), "/__fleet/orgs")).json()
    for o in body["orgs"]:
        assert o["persona_count"] is None and o["clone_count"] is None
        assert o["templates"] is None and o["active_roster_size"] is None
    assert body["orgs"][0]["cost_7d"] is not None  # the rest of the row still fills in


def test_orgs_view_without_supabase_returns_live_brains_with_nulls(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    monkeypatch.setattr(fo, "fetch_health", _fake_fetch)
    fo._reset_for_tests()
    prov = _FakeProv(_stats())
    with _auth_patched(ADMIN):
        body = asyncio.run(_get(prov, "/__fleet/orgs")).json()
    fo._reset_for_tests()
    rows = {o["org_id"]: o for o in body["orgs"]}
    assert set(rows) == {"org-a", "org-c"}
    assert rows["org-a"]["persona_count"] is None and rows["org-a"]["cost_7d"] is None
    assert rows["org-a"]["dormant"] is True  # the live brain still answers
    assert body["usage_source"] == "none"


# ── usage regroup + fallback ────────────────────────────────────────────────


def test_regroup_by_org_keeps_the_owner_lane():
    rows = [
        {
            "org_id": "o1",
            "org_name": "One",
            "agent_id": "a.b",
            "cloud_usd": "1.5",
            "pod_s": 10,
            "calls": 2,
        },
        {"org_id": "o1", "org_name": "", "agent_id": "", "cloud_usd": 0.5, "pod_s": 20, "calls": 1},
        {"org_id": "o1", "agent_id": "owner", "cloud_usd": 0.25, "pod_s": 0},
        {
            "org_id": "o2",
            "org_name": "Two",
            "agent_id": "x.y",
            "cloud_usd": 3,
            "pod_s": 0,
            "calls": 1,
        },
        {"org_id": "", "agent_id": "ghost", "cloud_usd": 99},
    ]
    out = agent_usage_store.regroup_by_org(rows)
    assert out == {
        "o1": {"org_name": "One", "cloud_usd": 2.25, "pod_s": 30.0, "calls": 3},
        "o2": {"org_name": "Two", "cloud_usd": 3.0, "pod_s": 0.0, "calls": 1},
    }


def test_totals_all_by_org_falls_back_to_the_raw_rpc(sb):
    sb.fail_rpc.add("agent_usage_totals_all_daily")
    sb.rpc_rows["agent_usage_totals_all"] = [
        {"org_id": "org-b", "org_name": "Bolt", "agent_id": "x.y", "cloud_usd": 4.0, "pod_s": 7}
    ]
    out, src = agent_usage_store.totals_all_by_org(sb, "2026-09-11T00:00:00+00:00", "2026-09-11")
    assert src == "raw" and out == {
        "org-b": {"org_name": "Bolt", "cloud_usd": 4.0, "pod_s": 7.0, "calls": 0}
    }
    # The daily RPC got DATE params, the raw one the timestamp.
    assert sb.rpcs[0] == (
        "agent_usage_totals_all_daily",
        {"p_since": "2026-09-11", "p_until": None},
    )
    assert sb.rpcs[1] == (
        "agent_usage_totals_all",
        {"p_since": "2026-09-11T00:00:00+00:00", "p_until": None},
    )
    sb.fail_rpc.add("agent_usage_totals_all")
    assert agent_usage_store.totals_all_by_org(sb, "x", "y") == ({}, "none")
    assert agent_usage_store.totals_all_by_org(None, "x", "y") == ({}, "none")


def test_orgs_view_uses_raw_fallback_when_daily_rpc_missing(sb, monkeypatch):
    sb.fail_rpc.add("agent_usage_totals_all_daily")
    sb.rpc_rows["agent_usage_totals_all"] = [
        {"org_id": "org-a", "org_name": "Acme", "agent_id": "x.y", "cloud_usd": 9.0, "pod_s": 1}
    ]
    monkeypatch.setattr(fo, "fetch_health", _fake_fetch)
    with _auth_patched(ADMIN):
        body = asyncio.run(_get(_FakeProv(), "/__fleet/orgs")).json()
    rows = {o["org_id"]: o for o in body["orgs"]}
    assert body["usage_source"] == "raw" and rows["org-a"]["cost_7d"] == 9.0
    assert rows["org-b"]["cost_7d"] is None


# ── per-brain /health: capped + cached ──────────────────────────────────────


def test_brain_health_fetch_is_capped_and_cached(monkeypatch):
    fo._reset_for_tests()
    calls: list[int] = []

    async def _fetch(port, timeout_s=2.0):
        calls.append(port)
        return {"status": "ok", "dmn": {"dormant": False, "idle_s": 1, "roster_size": 2}}

    stats = [
        {
            "key": f"org-{i}",
            "pid": i,
            "rss_mb": 1.0,
            "uptime_s": 1,
            "tier": "full",
            "booting": False,
            "port": 9100 + i,
        }
        for i in range(4)
    ]
    prov = _FakeProv(stats)
    monkeypatch.setenv("BRAIN_MAX_TENANTS", "2")
    out = asyncio.run(fo.brain_healths(prov, prov.tenant_stats(), now=1000.0, fetch=_fetch))
    assert sorted(calls) == [9100, 9101]  # capped at BRAIN_MAX_TENANTS
    assert out["org-0"]["dmn"]["roster_size"] == 2 and out["org-2"] is None
    # Within the cache window nothing is refetched; the uncached ones get their turn.
    calls.clear()
    out = asyncio.run(fo.brain_healths(prov, prov.tenant_stats(), now=1010.0, fetch=_fetch))
    assert sorted(calls) == [9102, 9103] and out["org-0"] is not None
    calls.clear()
    asyncio.run(fo.brain_healths(prov, prov.tenant_stats(), now=1020.0, fetch=_fetch))
    assert calls == []
    # After the window the first two are stale again.
    asyncio.run(
        fo.brain_healths(
            prov, prov.tenant_stats(), now=1000.0 + fo.HEALTH_CACHE_S + 1, fetch=_fetch
        )
    )
    assert sorted(calls) == [9100, 9101]
    fo._reset_for_tests()


def test_brain_health_skips_booting_and_tolerates_failures(monkeypatch):
    fo._reset_for_tests()
    calls: list[int] = []

    async def _fetch(port, timeout_s=2.0):
        calls.append(port)
        if port == 9201:
            raise RuntimeError("boom")
        return None

    stats = [
        {
            "key": "org-x",
            "pid": 1,
            "rss_mb": 1.0,
            "uptime_s": 1,
            "tier": "full",
            "booting": True,
            "port": 9200,
        },
        {
            "key": "org-y",
            "pid": 2,
            "rss_mb": 1.0,
            "uptime_s": 1,
            "tier": "full",
            "booting": False,
            "port": 9201,
        },
    ]
    prov = _FakeProv(stats)
    monkeypatch.delenv("BRAIN_MAX_TENANTS", raising=False)
    out = asyncio.run(fo.brain_healths(prov, prov.tenant_stats(), now=1.0, fetch=_fetch))
    assert calls == [9201] and out == {"org-x": None, "org-y": None}
    assert fo._health_fields(None) == {
        "breaker": None,
        "dormant": None,
        "idle_s": None,
        "roster_size": None,
    }
    fo._reset_for_tests()
