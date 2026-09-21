"""Fleet → Reindex: the persona index (migration 039) rebuilt from the console.

Gateway POST /__fleet/orgs/{org_id}/reindex and POST /__fleet/reindex_all
(platform admin only) ask an org's live tenant to run its gateway-only
POST /__reindex over the per-boot internal token the provisioner hands every
child. A dormant org is booted for the single-org route and skipped (reported)
by the sweep unless ?spawn=1. Every answer is counts and seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import brain.provisioner as pv  # noqa: E402
from brain import persona_index as pi  # noqa: E402
from brain.gateway import fleet_orgs as fo  # noqa: E402
from brain.gateway import server as gw  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402

TOKEN = "t" * 40

# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeProv:
    """Live keys → ports. ensure() makes the org's default instance live."""

    def __init__(self, live: dict[str, int] | None = None, booting: set[str] | None = None):
        self.live = dict(live or {})
        self.booting = set(booting or ())
        self.ensured: list[str] = []
        self.refuse_spawn = False

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    def tenant_stats(self):
        return [
            {"key": k, "port": p, "tier": "full", "booting": k in self.booting}
            for k, p in self.live.items()
        ]

    def status(self, org, persona=None):
        key = org if not persona else f"{org}::{persona}"
        if key not in self.live:
            return None
        return {"port": self.live[key], "api_port": None, "booting": key in self.booting, "pid": 1}

    async def ensure(self, org, persona=None):
        self.ensured.append(org)
        if self.refuse_spawn:
            raise pv.CapacityError("host full")
        self.live.setdefault(org, 9000 + len(self.live))
        return self.live[org]

    def keys_for(self, org):
        return [k for k in self.live if k.split("::")[0] == org]

    def live_count(self):
        return len(self.live)

    def full_count(self):
        return len(self.live)

    def touch(self, *a):  # pragma: no cover
        pass


def _post_ok(calls: list, body=None):
    async def _post(port, timeout_s=None):
        calls.append(port)
        return dict(
            body or {"ok": True, "indexed": 5, "learned": 2, "batches": 1, "elapsed_s": 0.4}
        )

    return _post


@contextlib.contextmanager
def _auth_patched(claims: dict):
    orig = (
        ui_auth.is_disabled,
        ui_auth.is_configured,
        ui_auth.authenticate,
        ui_auth.set_session_cookies,
    )
    ui_auth.is_disabled = lambda: False
    ui_auth.is_configured = lambda: True

    async def _fake_auth(_request):
        return dict(claims), None

    ui_auth.authenticate = _fake_auth
    ui_auth.set_session_cookies = lambda *a, **k: None
    try:
        yield
    finally:
        (
            ui_auth.is_disabled,
            ui_auth.is_configured,
            ui_auth.authenticate,
            ui_auth.set_session_cookies,
        ) = orig


ADMIN = {"sub": "u1", "email": "a@x", "app_metadata": {"is_admin": True}}
MEMBER = {"sub": "u2", "email": "m@x", "app_metadata": {}}


async def _post(prov, path):
    app = gw.build_gateway_app(prov, [None])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(path, headers={"accept": "application/json"})


@pytest.fixture
def no_db(monkeypatch):
    """The gateway has no Supabase: the sweep only knows live orgs."""
    fo._reset_for_tests()
    monkeypatch.setattr(fo, "_client", lambda: None)
    yield
    fo._reset_for_tests()


# ── the internal token ──────────────────────────────────────────────────────


def test_internal_token_is_stable_per_process_and_pinnable(monkeypatch):
    monkeypatch.setattr(pv, "_INTERNAL_TOKEN", None)
    monkeypatch.delenv("BRAIN_INTERNAL_TOKEN", raising=False)
    a = pv.internal_token()
    assert len(a) >= 32 and pv.internal_token() == a
    monkeypatch.setattr(pv, "_INTERNAL_TOKEN", None)
    monkeypatch.setenv("BRAIN_INTERNAL_TOKEN", TOKEN)
    assert pv.internal_token() == TOKEN


def test_provisioner_hands_every_tenant_the_token(tmp_path, monkeypatch):
    import brain.gateway.org_token as ot
    import brain.vault as vault

    monkeypatch.setattr(pv, "TENANTS_DIR", tmp_path)
    monkeypatch.setattr(pv, "_INTERNAL_TOKEN", None)
    monkeypatch.setenv("BRAIN_INTERNAL_TOKEN", TOKEN)
    uid = "tenant-token"
    root = tmp_path / uid
    (root / "second_brain").mkdir(parents=True)
    (root / "settings.json").write_text(json.dumps({"persona_name": "ahab"}), encoding="utf-8")
    monkeypatch.setattr(ot, "mint_org_token", lambda _uid: "")
    monkeypatch.setattr(vault, "fetch_org_keys", lambda _org: {})
    seen = {}

    def _cmd(_port, env):
        seen.update(env)
        return [sys.executable, "-c", "pass"]

    prov = pv.Provisioner(cmd_builder=_cmd)
    proc, _port, _api = prov._build_and_launch(uid)
    try:
        assert seen.get("BRAIN_INTERNAL_TOKEN") == TOKEN
    finally:
        proc.terminate()


class _Req:
    def __init__(self, path, headers=None):
        self.url = type("U", (), {"path": path})()
        self.headers = dict(headers or {})


def test_internal_request_admits_only_internal_paths_with_the_token(monkeypatch):
    monkeypatch.setenv("BRAIN_INTERNAL_TOKEN", TOKEN)
    ok = {"x-brain-internal-token": TOKEN}
    assert ui_auth.is_internal_request(_Req("/__reindex", ok))
    assert not ui_auth.is_internal_request(_Req("/__reindex", {"x-brain-internal-token": "x"}))
    assert not ui_auth.is_internal_request(_Req("/__reindex"))
    assert not ui_auth.is_internal_request(_Req("/fleet/health", ok))  # never a read path
    # Sleep's consolidation hop: the gateway SIGTERMs a tenant through /shutdown.
    assert ui_auth.is_internal_request(_Req("/shutdown", ok))
    assert not ui_auth.is_internal_request(_Req("/shutdown"))
    assert not ui_auth.is_internal_request(_Req("/restart", ok))  # the gateway never calls it
    monkeypatch.setenv("BRAIN_INTERNAL_TOKEN", "short")
    assert not ui_auth.is_internal_request(_Req("/__reindex", {"x-brain-internal-token": "short"}))
    monkeypatch.delenv("BRAIN_INTERNAL_TOKEN")
    assert not ui_auth.is_internal_request(_Req("/__reindex", {"x-brain-internal-token": ""}))


# ── the tenant route ────────────────────────────────────────────────────────


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    """A tenant app with the REAL auth gate (no session ever validates) and a
    pinned internal token."""
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setenv("BRAIN_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_configured", lambda: True)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    state = {"claims": None}

    async def _auth(_request):
        return state["claims"], None

    monkeypatch.setattr(ui_auth, "authenticate", _auth)
    from brain.ui.server import UIServer

    server = UIServer(emitter_queue=asyncio.Queue())
    return TestClient(server._build_app()), state


def test_tenant_reindex_needs_the_gateway_token(tenant, monkeypatch):
    client, state = tenant
    seen = []
    monkeypatch.setattr(
        pi,
        "reindex",
        lambda: seen.append(1) or {"indexed": 9, "learned": 3, "batches": 1, "elapsed_s": 0.2},
    )
    assert client.post("/__reindex").status_code == 401  # no session, no token
    assert client.post("/__reindex", headers={"x-brain-internal-token": "nope"}).status_code == 401
    # A valid SESSION is not the gateway either.
    state["claims"] = {"sub": "u1", "app_metadata": {"is_admin": True}}
    assert client.post("/__reindex").status_code == 403
    state["claims"] = None
    assert seen == []
    r = client.post("/__reindex", headers={"x-brain-internal-token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "indexed": 9, "learned": 3, "batches": 1, "elapsed_s": 0.2}
    assert seen == [1]


def test_tenant_shutdown_admits_the_gateway_token_and_a_session(tenant, monkeypatch):
    """The gateway's Sleep sweep POSTs /shutdown with the internal token (no
    session); the UI's Sleep button on a standalone tenant still gets in with a
    session. Nothing else does — a session-less, token-less call is the 401 that
    used to make every hosted Sleep burn its full consolidation wait."""
    import os

    client, state = tenant
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    assert client.post("/shutdown").status_code == 401
    assert client.post("/shutdown", headers={"x-brain-internal-token": "nope"}).status_code == 401
    r = client.post("/shutdown", headers={"x-brain-internal-token": TOKEN})
    assert r.status_code == 200 and r.json() == {"ok": True}
    state["claims"] = {"sub": "u1", "app_metadata": {}}
    r = client.post("/shutdown")
    assert r.status_code == 200 and r.json() == {"ok": True}
    # /restart is NOT an internal path: token-only callers stay out.
    state["claims"] = None
    assert client.post("/restart", headers={"x-brain-internal-token": TOKEN}).status_code == 401


def test_tenant_reindex_is_503_while_the_index_is_off(tenant, monkeypatch):
    client, _ = tenant

    def _boom():
        raise pi.IndexUnavailable("persona index unavailable")

    monkeypatch.setattr(pi, "reindex", _boom)
    r = client.post("/__reindex", headers={"x-brain-internal-token": TOKEN})
    assert r.status_code == 503 and r.json()["ok"] is False


def test_reindex_helper_wraps_reconcile_and_refuses_when_disabled(monkeypatch):
    monkeypatch.setattr(pi, "enabled", lambda: True)
    monkeypatch.setattr(
        pi, "reconcile", lambda learned=False: {"indexed": 4, "learned": 1, "batches": 1}
    )
    res = pi.reindex()
    assert res["indexed"] == 4 and res["learned"] == 1 and "elapsed_s" in res
    monkeypatch.setattr(pi, "enabled", lambda: False)
    with pytest.raises(pi.IndexUnavailable):
        pi.reindex()


# ── the gateway → tenant call ───────────────────────────────────────────────


def test_post_reindex_presents_the_token_and_maps_failures(monkeypatch):
    monkeypatch.setattr(pv, "_INTERNAL_TOKEN", TOKEN)
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("x-brain-internal-token")
        return httpx.Response(200, json={"ok": True, "indexed": 2, "learned": 0})

    real = httpx.AsyncClient

    def _client(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    body = asyncio.run(fo.post_reindex(9001))
    assert body["ok"] is True and body["status"] == 200 and body["indexed"] == 2
    assert seen["url"] == "http://127.0.0.1:9001/__reindex" and seen["token"] == TOKEN

    def handler503(request):
        return httpx.Response(503, json={"ok": False, "error": "index off"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler503), **kw)
    )
    body = asyncio.run(fo.post_reindex(9001))
    assert body["ok"] is False and body["status"] == 503 and body["error"] == "index off"

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw)
    )
    body = asyncio.run(fo.post_reindex(9001))
    assert body["ok"] is False and body["status"] == 0 and "down" in body["error"]


def test_reindex_org_prefers_the_default_instance(no_db):
    prov = _FakeProv(live={"org-a::ahab": 9100, "org-a": 9001})
    calls: list[int] = []
    row = asyncio.run(fo.reindex_org(prov, "org-a", spawn=False, post=_post_ok(calls)))
    assert row["state"] == "reindexed" and row["key"] == "org-a" and calls == [9001]
    assert row["indexed"] == 5 and row["learned"] == 2 and row["spawned"] is False


def test_reindex_org_uses_a_dedicated_instance_when_no_default(no_db):
    prov = _FakeProv(live={"org-a::ahab": 9100})
    calls: list[int] = []
    row = asyncio.run(fo.reindex_org(prov, "org-a", spawn=False, post=_post_ok(calls)))
    assert row["state"] == "reindexed" and row["key"] == "org-a::ahab" and calls == [9100]


def test_reindex_org_skips_or_boots_a_dormant_org(no_db):
    prov = _FakeProv()
    calls: list[int] = []
    row = asyncio.run(fo.reindex_org(prov, "org-z", spawn=False, post=_post_ok(calls)))
    assert row["state"] == "skipped" and row["error"] == "dormant" and calls == []
    assert prov.ensured == []
    row = asyncio.run(fo.reindex_org(prov, "org-z", spawn=True, post=_post_ok(calls)))
    assert row["state"] == "reindexed" and row["spawned"] is True
    assert prov.ensured == ["org-z"] and calls == [prov.live["org-z"]]


def test_reindex_org_reports_a_refused_spawn(no_db):
    prov = _FakeProv()
    prov.refuse_spawn = True
    row = asyncio.run(fo.reindex_org(prov, "org-z", spawn=True, post=_post_ok([])))
    assert row["state"] == "error" and "spawn failed" in row["error"]


def test_reindex_org_treats_a_booting_instance_as_not_live(no_db):
    prov = _FakeProv(live={"org-a": 9001}, booting={"org-a"})
    row = asyncio.run(fo.reindex_org(prov, "org-a", spawn=False, post=_post_ok([])))
    assert row["state"] == "skipped"


def test_reindex_all_sweeps_live_orgs_and_reports_dormant_ones(monkeypatch):
    fo._reset_for_tests()
    monkeypatch.setattr(fo, "_client", lambda: None)
    # Supabase knows org-a, org-b, org-c; only org-a and org-b are live.
    monkeypatch.setattr(
        fo,
        "db_snapshot",
        lambda now=None: {
            "orgs": [{"org_id": o} for o in ("org-a", "org-b", "org-c")],
            "counts": {},
            "usage_24h": {},
            "usage_7d": {},
            "usage_source": "none",
        },
    )
    prov = _FakeProv(live={"org-b": 9002, "org-a": 9001})
    calls: list[int] = []
    out = asyncio.run(fo.reindex_all(prov, spawn=False, post=_post_ok(calls)))
    assert [r["org_id"] for r in out["orgs"]] == ["org-b", "org-a", "org-c"]
    assert out["reindexed"] == 2 and out["skipped"] == 1 and out["errors"] == 0
    assert sorted(calls) == [9001, 9002] and prov.ensured == []
    assert out["orgs"][2]["state"] == "skipped"
    # ?spawn=1 boots the dormant one too.
    out = asyncio.run(fo.reindex_all(prov, spawn=True, post=_post_ok(calls)))
    assert out["reindexed"] == 3 and out["skipped"] == 0 and prov.ensured == ["org-c"]
    fo._reset_for_tests()


# ── the gateway routes ──────────────────────────────────────────────────────


def test_fleet_reindex_routes_refuse_non_admins(no_db, monkeypatch):
    monkeypatch.delenv("BRAIN_ADMIN_EMAILS", raising=False)
    prov = _FakeProv(live={"org-a": 9001})
    calls: list[int] = []
    monkeypatch.setattr(fo, "post_reindex", _post_ok(calls))
    with _auth_patched(MEMBER):
        assert asyncio.run(_post(prov, "/__fleet/orgs/org-a/reindex")).status_code == 403
        assert asyncio.run(_post(prov, "/__fleet/reindex_all")).status_code == 403
    assert calls == [] and prov.ensured == []


def test_fleet_reindex_org_route_returns_counts(no_db, monkeypatch):
    prov = _FakeProv(live={"org-a": 9001})
    calls: list[int] = []
    monkeypatch.setattr(fo, "post_reindex", _post_ok(calls))
    with _auth_patched(ADMIN):
        r = asyncio.run(_post(prov, "/__fleet/orgs/org-a/reindex"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "reindexed" and body["indexed"] == 5 and body["learned"] == 2
    assert calls == [9001]
    # Content-free by construction.
    assert set(body) <= {
        "org_id",
        "state",
        "spawned",
        "indexed",
        "learned",
        "batches",
        "elapsed_s",
        "error",
        "key",
    }


def test_fleet_reindex_org_route_boots_a_dormant_org_unless_spawn_0(no_db, monkeypatch):
    prov = _FakeProv()
    calls: list[int] = []
    monkeypatch.setattr(fo, "post_reindex", _post_ok(calls))
    with _auth_patched(ADMIN):
        r = asyncio.run(_post(prov, "/__fleet/orgs/org-z/reindex?spawn=0"))
        assert r.status_code == 409 and r.json()["state"] == "skipped"
        assert prov.ensured == [] and calls == []
        r = asyncio.run(_post(prov, "/__fleet/orgs/org-z/reindex"))
    assert r.status_code == 200 and r.json()["spawned"] is True
    assert prov.ensured == ["org-z"] and len(calls) == 1


def test_fleet_reindex_org_route_is_502_when_the_tenant_fails(no_db, monkeypatch):
    prov = _FakeProv(live={"org-a": 9001})
    monkeypatch.setattr(fo, "post_reindex", _post_ok([], {"ok": False, "error": "index off"}))
    with _auth_patched(ADMIN):
        r = asyncio.run(_post(prov, "/__fleet/orgs/org-a/reindex"))
    assert r.status_code == 502 and r.json()["error"] == "index off"
    with _auth_patched(ADMIN):
        assert asyncio.run(_post(prov, "/__fleet/orgs/org-a::ahab/reindex")).status_code == 400


def test_fleet_reindex_all_route(no_db, monkeypatch):
    prov = _FakeProv(live={"org-a": 9001, "org-b": 9002})
    calls: list[int] = []
    monkeypatch.setattr(fo, "post_reindex", _post_ok(calls))
    with _auth_patched(ADMIN):
        r = asyncio.run(_post(prov, "/__fleet/reindex_all"))
    assert r.status_code == 200
    body = r.json()
    assert body["reindexed"] == 2 and body["skipped"] == 0 and body["spawn"] is False
    assert sorted(calls) == [9001, 9002]
    assert {o["org_id"] for o in body["orgs"]} == {"org-a", "org-b"}
