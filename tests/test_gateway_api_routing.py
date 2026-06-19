"""Gateway engine-API (/v1) routing tests.

Guards that partner API traffic is bearer-authed, routes to the right tenant's API
port, and — crucially — spawns the brain + kicks the pod on demand (the mechanism
that spins a RunPod up from the API path, mirroring the UI path).
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx

import brain.api.auth as api_auth
from brain.gateway import server as gw
from brain.ui import auth as ui_auth


class _FakeProv:
    def __init__(self, status=None):
        self._status = status
        self.ensured: list[str] = []

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    def status(self, t):
        return self._status

    async def ensure(self, t):
        self.ensured.append(t)

    def touch(self, t):
        pass

    def live_count(self):
        return 1 if self._status else 0


class _FakeRunpod:
    def __init__(self):
        self.ensured = False

    async def ensure_running(self):
        self.ensured = True
        return True

    def published_host(self):
        return None


def test_v1_is_public_path():
    # /v1 must bypass the cookie gate (it's bearer-authed by the API layer).
    assert ui_auth.is_public_path("/v1/sessions")
    assert ui_auth.is_public_path("/v1/sessions/abc/turns")
    assert not ui_auth.is_public_path("/settings")


def _patch(monkeypatch, org):
    monkeypatch.setattr(api_auth, "resolve_partner_org", lambda _auth: org)


async def _post_v1(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/v1/sessions", headers=headers or {}, json={"end_user_id": "u"})


def test_v1_rejects_unknown_token(monkeypatch):
    _patch(monkeypatch, None)  # token resolves to no org
    app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])
    r = asyncio.run(_post_v1(app, {"authorization": "Bearer nope"}))
    assert r.status_code == 401


def test_v1_spawns_brain_and_kicks_pod_when_cold(monkeypatch):
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status=None)  # brain not up
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
        # spawn + pod warm are fire-and-forget tasks — let the loop drain them.
        for _ in range(5):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 503  # partner retries while the brain boots
    assert "org-1" in prov.ensured, "the brain must be spawned on demand"
    assert runpod.ensured is True, "the pod must be kicked on the API path"


def test_v1_routes_to_tenant_api_port_when_up(monkeypatch):
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    seen = {}

    async def fake_stream(request, port):
        seen["port"] = port
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True})

    monkeypatch.setattr(gw, "_proxy_http_stream", fake_stream)
    r = asyncio.run(_post_v1(app, {"authorization": "Bearer good"}))
    assert r.status_code == 200
    assert seen.get("port") == 9777, "must proxy to the tenant's API port, not the UI port"


# ── auth helpers (mocked Supabase) ──────────────────────────────────────────


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeTable(self._rows)


@contextlib.contextmanager
def _supabase(rows, org="o"):
    import brain.second_brain.supabase_client as sc

    o_enabled, o_get, o_org = sc.is_enabled, sc.get_client, sc.get_org_id
    sc.is_enabled = lambda: True
    sc.get_client = lambda: _FakeClient(rows)
    sc.get_org_id = lambda: org
    try:
        yield
    finally:
        sc.is_enabled, sc.get_client, sc.get_org_id = o_enabled, o_get, o_org


def test_resolve_partner_org_maps_token_to_org():
    with _supabase([{"org_id": "org-42"}]):
        assert api_auth.resolve_partner_org("Bearer sk_x") == "org-42"


def test_resolve_partner_org_unknown_is_none():
    with _supabase([]):
        assert api_auth.resolve_partner_org("Bearer sk_x") is None
    assert api_auth.resolve_partner_org(None) is None


def test_has_any_api_keys_true_when_rows_exist():
    with _supabase([{"id": "k1"}]):
        assert api_auth.has_any_api_keys() is True
    with _supabase([]):
        assert api_auth.has_any_api_keys() is False
