"""Gateway engine-API (/v1) routing tests.

Guards that partner API traffic is bearer-authed, routes to the right tenant's API
port, and — crucially — spawns the brain + kicks the pod on demand (the mechanism
that spins a RunPod up from the API path, mirroring the UI path).
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import brain.api.auth as api_auth
from brain.gateway import server as gw
from brain.ui import auth as ui_auth


class _FakeProv:
    def __init__(self, status=None, live=None):
        self._status = status
        self._live = live
        self.ensured: list[str] = []
        self.ensured_personas: list[str | None] = []
        self.status_calls: list[tuple[str, str | None]] = []
        self.touched: list[tuple[str, str | None]] = []
        self.stopped: list[str] = []
        # Personas with a LIVE dedicated instance — the only ones X-Brain-Persona
        # may route to (gateway._routable_persona).
        self.promoted: list[str] = []

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    def status(self, t, persona=None):
        self.status_calls.append((t, persona))
        return self._status

    async def ensure(self, t, persona=None):
        self.ensured.append(t)
        self.ensured_personas.append(persona)

    async def stop_user(self, t, persona=None):
        self.stopped.append(t)

    def is_running(self, t, persona=None):
        return False

    def keys_for(self, t):
        # Sleep-sweep surface: these tests model a single default instance.
        return [t]

    def touch(self, t, persona=None):
        self.touched.append((t, persona))

    def promoted_personas(self, t):
        return list(self.promoted)

    def live_count(self):
        if self._live is not None:
            return self._live
        return 1 if self._status else 0

    def full_count(self):
        # These tests treat every live brain as full-tier, so it mirrors live_count.
        return self.live_count()


class _FakeRunpod:
    def __init__(self):
        self.ensured = False
        self.paused = False

    async def ensure_running(self):
        self.ensured = True
        return True

    async def pause(self):
        self.paused = True

    def status(self):
        return {"state": "ready", "detail": "", "elapsed_s": 1}

    def published_host(self):
        return None


def test_v1_is_public_path():
    # /v1 must bypass the cookie gate (it's bearer-authed by the API layer).
    assert ui_auth.is_public_path("/v1/sessions")
    assert ui_auth.is_public_path("/v1/sessions/abc/turns")
    assert not ui_auth.is_public_path("/settings")


def _patch(monkeypatch, org, role="owner"):
    """Resolve every bearer to `org` with `role`. Defaults to owner so the routing
    tests below exercise routing rather than authorisation; the owner-gating tests
    pass role="partner" explicitly."""
    ctx = None if org is None else {"org_id": org, "partner_id": "p", "role": role}
    monkeypatch.setattr(api_auth, "resolve_key_context", lambda _auth: ctx)

    # The /v1 lane now refuses to SPAWN for an org with no Anthropic vault key
    # (mirroring the UI catch-all). These tests model an org with a key on file;
    # tests/test_gateway_v1_anthropic_gate.py covers the refusal.
    async def _has_key(_org):
        return True

    monkeypatch.setattr(gw, "_org_has_anthropic", _has_key)


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
    # BRAIN_TIER=full is the authoritative override the hosted deploy runs under, so a
    # cold brain is known-full → the pod is warmed eagerly to overlap its boot.
    monkeypatch.setenv("BRAIN_TIER", "full")
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


def test_v1_cold_does_not_kick_pod_when_tier_unknown(monkeypatch):
    # With per-tenant tiers (BRAIN_TIER unset) and no full brain already alive, an
    # as-yet-unbooted brain's tier is unknown — it might be lite, which never uses the
    # pod. So the API path must NOT eagerly spin a GPU; the reconciler brings the pod up
    # only once the brain reports full on /health. The brain is still spawned on demand.
    _patch(monkeypatch, "org-1")
    monkeypatch.delenv("BRAIN_TIER", raising=False)
    prov = _FakeProv(status=None)  # brain not up → full_count() == 0
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
        for _ in range(5):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 503
    assert "org-1" in prov.ensured, "the brain must still be spawned on demand"
    assert runpod.ensured is False, "no GPU pod for an unknown-tier (possibly lite) brain"


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


# ── multi-persona routing (X-Brain-Persona) ─────────────────────────────────


def test_v1_ignores_persona_header_when_flag_off(monkeypatch):
    # Default deployment: the header is ignored, routing keys on the tenant only.
    monkeypatch.setattr(gw, "_MULTI_PERSONA", False)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    r = asyncio.run(
        _post_v1(app, {"authorization": "Bearer good", "x-brain-persona": "the_adversary"})
    )
    assert r.status_code == 200
    assert prov.status_calls[-1] == ("org-1", None)  # persona dropped


def test_v1_routes_by_persona_header_when_flag_on(monkeypatch):
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    prov.promoted = ["the_adversary"]  # a live dedicated instance exists for it
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    r = asyncio.run(
        _post_v1(app, {"authorization": "Bearer good", "x-brain-persona": "the_adversary"})
    )
    assert r.status_code == 200
    assert prov.status_calls[-1] == ("org-1", "the_adversary")
    assert prov.touched[-1] == ("org-1", "the_adversary")


def test_v1_unpromoted_persona_header_routes_to_the_shared_instance(monkeypatch):
    """A header naming a persona with no dedicated instance is ignored — the
    request goes where it would have gone without the header."""
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    prov.promoted = ["someone_else"]
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    r = asyncio.run(
        _post_v1(app, {"authorization": "Bearer good", "x-brain-persona": "the_adversary"})
    )
    assert r.status_code == 200
    assert prov.status_calls[-1] == ("org-1", None)
    assert prov.touched[-1] == ("org-1", None)


def test_v1_cold_header_never_spawns_a_persona(monkeypatch):
    """Audit 2026-09-13: with the flag on, a cold status for a header persona went
    straight to _safe_ensure(org, persona) — any partner key could spawn an
    arbitrary persona process by naming a slug. Now only the shared instance is
    spawned; a dedicated instance is the placement's job."""
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status=None)  # nothing up for this org
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/v1/sessions",
                headers={"authorization": "Bearer good", "x-brain-persona": "the_visionary"},
                json={},
            )
        for _ in range(5):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 503
    assert prov.status_calls[-1] == ("org-1", None)
    assert prov.ensured == ["org-1"]
    assert prov.ensured_personas == [None], "the header must never pick what gets spawned"


def test_v1_provisioner_without_promoted_set_ignores_the_header(monkeypatch):
    """Fail closed: no promoted-set surface → no header routing at all."""
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    prov.promoted_personas = None  # type: ignore[assignment]
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    asyncio.run(_post_v1(app, {"authorization": "Bearer good", "x-brain-persona": "the_adversary"}))
    assert prov.status_calls[-1] == ("org-1", None)


# ── the WebSocket lane follows the same rule ────────────────────────────────


def _ws(app, headers):
    """Open the engine WS through the gateway and return the close code."""
    with (
        pytest.raises(WebSocketDisconnect) as ei,
        TestClient(app).websocket_connect("/v1/sessions/s1/stream", headers=headers),
    ):
        pass
    return ei.value.code


def test_ws_unpromoted_header_routes_to_the_shared_instance(monkeypatch):
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status=None)
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    code = _ws(app, {"authorization": "Bearer good", "x-brain-persona": "the_visionary"})
    assert code == 1013  # not ready — partner retries
    assert prov.status_calls[-1] == ("org-1", None)


def test_ws_activity_touches_the_persona_instance(monkeypatch):
    """Audit 2026-09-13: on_activity touched (org) only, so a long stream on a
    dedicated persona instance kept the DEFAULT instance alive while the idle
    reaper took the one actually serving it."""
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    prov.promoted = ["ahab"]
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def _fake_proxy_ws(client_ws, port, **kw):
        kw["on_activity"]()  # one inbound client frame
        await client_ws.close(code=1000)

    monkeypatch.setattr(gw, "_proxy_ws", _fake_proxy_ws)
    _ws(app, {"authorization": "Bearer good", "x-brain-persona": "ahab"})
    assert prov.status_calls[-1] == ("org-1", "ahab")
    assert prov.touched == [("org-1", "ahab"), ("org-1", "ahab")]


def test_ws_zero_retry_after_is_still_a_refusal(monkeypatch):
    """limiter.check returns None when allowed; anything else is a Retry-After.
    A 0.0 must not be read as 'allowed'."""
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw._rl.limiter, "check", lambda bucket, key: 0.0)

    code = _ws(app, {"authorization": "Bearer good"})
    assert code == 1013
    assert prov.status_calls == []


# ── persona header is a filesystem path segment ─────────────────────────────
# It reaches TENANTS_DIR/<tenant>/personas/<persona>, which is then mkdir'd, and
# Path joins ".." literally — so an unsanitised header escaped the tenant directory.


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc",
        "..",
        "a/../../b",
        "/absolute",
        "with space",
        "x" * 200,
        "..%2f..%2fetc",
    ],
)
def test_v1_persona_header_cannot_carry_a_path(monkeypatch, hostile):
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    asyncio.run(_post_v1(app, {"authorization": "Bearer good", "x-brain-persona": hostile}))

    routed = prov.status_calls[-1][1]
    assert routed is None or ("/" not in routed and ".." not in routed and len(routed) <= 64)


def test_v1_persona_header_is_slugified_not_rejected(monkeypatch):
    """A display name still routes — normalise the common case rather than 400."""
    monkeypatch.setattr(gw, "_MULTI_PERSONA", True)
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    prov.promoted = ["the_visionary"]
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    asyncio.run(_post_v1(app, {"authorization": "Bearer good", "x-brain-persona": "The Visionary"}))
    assert prov.status_calls[-1] == ("org-1", "the_visionary")


# ── body size cap ───────────────────────────────────────────────────────────


def test_v1_rejects_an_oversized_body(monkeypatch):
    """The gateway buffers the whole body to forward it, so an unbounded body is an
    unbounded allocation in the one process every tenant depends on."""
    monkeypatch.setenv("BRAIN_MAX_BODY_BYTES", "1024")
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/v1/sessions",
                headers={"authorization": "Bearer good"},
                content=b"x" * 5000,
            )

    r = asyncio.run(run())
    assert r.status_code == 413
    # Rejected before the tenant was ever consulted.
    assert not prov.status_calls


# ── rate limiting ───────────────────────────────────────────────────────────


def test_v1_throttles_a_hot_key(monkeypatch):
    monkeypatch.setenv("BRAIN_RATE_LIMIT", "1")
    monkeypatch.setenv("BRAIN_RL_KEY_PER_MIN", "2")
    monkeypatch.setattr(gw._rl, "limiter", gw._rl.RateLimiter())
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            out = []
            for _ in range(4):
                r = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
                out.append(r)
            return out

    codes = [r.status_code for r in asyncio.run(run())]
    assert codes[:2] == [200, 200]
    assert 429 in codes


def test_unknown_key_is_cached_and_stops_hitting_the_database(monkeypatch):
    """The point of the negative cache: a flood of bad keys must not become a query
    per request against an uncached cross-org lookup."""
    monkeypatch.setattr(gw._rl, "limiter", gw._rl.RateLimiter())
    calls = {"n": 0}

    def _resolver(_auth):
        calls["n"] += 1
        return None

    monkeypatch.setattr(api_auth, "resolve_key_context", _resolver)
    app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            for _ in range(5):
                r = await c.post("/v1/sessions", headers={"authorization": "Bearer bad"}, json={})
                assert r.status_code in (401, 429)

    asyncio.run(run())
    assert calls["n"] == 1, "repeat bad keys must be served from the negative cache"


def test_at_capacity_is_distinguishable_from_booting(monkeypatch):
    """CapacityError happens in a fire-and-forget task, so it used to reach nobody and
    the caller retried 'booting' forever against a host that would never boot it."""
    from brain.provisioner import CapacityError

    _patch(monkeypatch, "org-1")
    monkeypatch.setattr(gw, "capacity_refusals", {})
    prov = _FakeProv(status=None)

    async def _boom(_uid, _persona=None):
        raise CapacityError("host at 25 brains")

    prov.ensure = _boom
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            first = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
            for _ in range(10):  # let the fire-and-forget ensure fail
                await asyncio.sleep(0)
            second = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
            return first, second

    first, second = asyncio.run(run())
    assert first.json()["status"] == "booting"
    assert second.status_code == 503
    assert second.json()["status"] == "at_capacity"
    assert second.headers.get("Retry-After")


async def _fake_stream(request, port):
    from fastapi.responses import JSONResponse

    return JSONResponse({"ok": True})


# ── cost control: /v1/sleep + /v1/status ────────────────────────────────────


def test_v1_sleep_unauthorized(monkeypatch):
    _patch(monkeypatch, None)
    app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/v1/sleep", headers={"authorization": "Bearer nope"})

    assert asyncio.run(run()).status_code == 401


def test_v1_sleep_stops_brain_and_pauses_pod(monkeypatch):
    _patch(monkeypatch, "org-1")
    # booting=True makes _do_sleep skip the brain HTTP call; live=0 → pod paused.
    prov = _FakeProv(status={"port": 0, "api_port": 1, "booting": True, "pid": 1}, live=0)
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sleep", headers={"authorization": "Bearer good"})
        for _ in range(10):  # let the background _do_sleep task finish
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 200 and r.json()["state"] == "sleeping"
    assert "org-1" in prov.stopped, "the org's brain must be stopped"
    assert runpod.paused is True, "the pod must be paused when no brain remains"


def test_v1_status_reports_cost_state(monkeypatch):
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 0, "api_port": 1, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/status", headers={"authorization": "Bearer good"})

    d = asyncio.run(run()).json()
    assert d["brain"] == "awake"
    assert d["pod"]["state"] == "ready"


# ── owner gating on the cost-control pair ───────────────────────────────────
# _do_sleep sweeps EVERY instance of the org and pauses the SHARED pod, so a partner
# key here is a denial-of-service against its own co-tenants: it kills their in-flight
# sessions and forces them into a cold start, with no cooldown and no way to tell who
# did it. Cost control is an owner concern.


def test_v1_sleep_rejects_a_partner_key(monkeypatch):
    _patch(monkeypatch, "org-1", role="partner")
    prov = _FakeProv(status={"port": 0, "api_port": 1, "booting": True, "pid": 1}, live=0)
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sleep", headers={"authorization": "Bearer partner"})
        for _ in range(10):  # give any (wrongly) spawned sleep task a chance to run
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 403
    # The sweep must not have started — a 403 that still slept the org is the bug.
    assert not prov.stopped
    assert not runpod.paused


def test_v1_status_hides_shared_pod_state_from_partners(monkeypatch):
    """The GPU pod is shared across orgs, so its state is not the partner's data —
    not even as a coarse ready/not-ready boolean, which is still a side channel."""
    _patch(monkeypatch, "org-1", role="partner")
    prov = _FakeProv(status={"port": 0, "api_port": 1, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/status", headers={"authorization": "Bearer partner"})

    d = asyncio.run(run()).json()
    assert "pod" not in d
    # Its own org-scoped state is still visible.
    assert d["brain"] == "awake"


def test_auth_backend_error_is_503_not_unauthorized(monkeypatch):
    """A Supabase blip must not read as 'no such key' at the gateway either."""

    def _boom(_auth):
        raise api_auth.AuthBackendError("connection reset")

    monkeypatch.setattr(api_auth, "resolve_key_context", _boom)
    app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/status", headers={"authorization": "Bearer x"})

    assert asyncio.run(run()).status_code == 503


# ── dedicated API host (BRAIN_API_HOST) ─────────────────────────────────────
# The gateway routes by PATH and never inspects Host, so attaching a second domain
# to the service would serve the login page and the cookie-authed UI proxy on it
# too. BRAIN_API_HOST narrows that one hostname to /v1 (+ /health). Unset must be
# byte-for-byte unchanged.


def _api_host_app(monkeypatch, prov=None, host="api.elyceum.app"):
    monkeypatch.setenv("BRAIN_API_HOST", host)
    return gw.build_gateway_app(prov or _FakeProv(), [_FakeRunpod()])


async def _get(app, path, *, host, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as c:
        return await c.get(path, headers=headers or {})


def test_api_host_unset_leaves_every_path_reachable(monkeypatch):
    """Default deployment: no host is special, nothing changes."""
    monkeypatch.delenv("BRAIN_API_HOST", raising=False)
    app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])
    r = asyncio.run(_get(app, "/login", host="api.elyceum.app"))
    assert r.status_code == 200, "with the gate off, the API hostname is just another host"


def test_api_host_serves_v1(monkeypatch):
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = _api_host_app(monkeypatch, prov)
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.elyceum.app") as c:
            return await c.post(
                "/v1/sessions", headers={"authorization": "Bearer good"}, json={"end_user_id": "u"}
            )

    assert asyncio.run(run()).status_code == 200


def test_api_host_404s_non_v1_paths(monkeypatch):
    """A partner who typos a path gets JSON, not an HTML login redirect."""
    app = _api_host_app(monkeypatch)
    for path in ("/login", "/settings", "/", "/v2/sessions"):
        r = asyncio.run(_get(app, path, host="api.elyceum.app"))
        assert r.status_code == 404, f"{path} must not be served on the API host"
        assert r.json()["detail"].startswith("not found"), f"{path} must return JSON"


def test_api_host_allows_health(monkeypatch):
    app = _api_host_app(monkeypatch)
    r = asyncio.run(_get(app, "/health", host="api.elyceum.app"))
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_host_gate_ignores_port_in_host_header(monkeypatch):
    app = _api_host_app(monkeypatch, host="localhost")
    r = asyncio.run(_get(app, "/login", host="localhost:8080"))
    assert r.status_code == 404, "the port must not defeat the host match"


def test_app_host_is_untouched_by_the_gate(monkeypatch):
    """The app hostname keeps serving everything, /v1 included — existing
    integrations pointed at elyceum.app must not break."""
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 9001, "api_port": 9777, "booting": False, "pid": 1})
    app = _api_host_app(monkeypatch, prov)
    monkeypatch.setattr(gw, "_proxy_http_stream", _fake_stream)

    assert asyncio.run(_get(app, "/login", host="elyceum.app")).status_code == 200

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://elyceum.app") as c:
            return await c.post(
                "/v1/sessions", headers={"authorization": "Bearer good"}, json={"end_user_id": "u"}
            )

    assert asyncio.run(run()).status_code == 200, "/v1 must keep working on the app host"


# ── public OpenAPI schema + Swagger UI ──────────────────────────────────────
# Swagger never worked through the gateway: the engine app's schema lives at
# /openapi.json on the origin root, which the cookie-authed catch-all bounces to
# login. The gateway now builds the document itself from the route table, with no
# tenant process involved.


def _openapi(app):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/openapi.json")

    return asyncio.run(run())


def test_openapi_served_without_a_key_or_a_tenant():
    """Swagger UI can't attach a bearer to its own schema fetch, so the document is
    public — and it must not spawn a brain or touch the pod to answer."""
    prov, runpod = _FakeProv(status=None), _FakeRunpod()
    r = _openapi(gw.build_gateway_app(prov, [runpod]))
    assert r.status_code == 200
    assert prov.ensured == [], "serving the schema must not spawn a tenant"
    assert runpod.ensured is False, "serving the schema must not kick the pod"


def _real_and_published():
    from starlette.routing import WebSocketRoute

    from brain.api.server import build_api_router

    async def _dummy(*a, **k):
        return {}

    real = {
        (m.lower(), r.path)
        for r in build_api_router(_dummy).routes
        if not isinstance(r, WebSocketRoute)
        for m in (r.methods or [])
        if m not in ("HEAD", "OPTIONS")
    }
    doc = _openapi(gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])).json()
    published = {(m, p) for p, ops in doc["paths"].items() for m in ops}
    return real, published


def test_openapi_covers_every_partner_route():
    """Drift guard: a new partner-callable route must show up in the published
    schema."""
    from brain.api.reference import is_owner_route

    real, published = _real_and_published()
    expected = {(m, p) for m, p in real if not is_owner_route(m.upper(), p)}
    assert not (expected - published), (
        f"routes missing from the OpenAPI schema: {sorted(expected - published)}"
    )


def test_public_openapi_omits_owner_routes():
    """The schema is unauthenticated. Owner-gated routes are unreachable without an
    owner credential anyway, but publishing the index hands out a map of the admin
    surface — key minting, the GDPR purge, the skill review queue, the DMN switch —
    with docstrings explaining each."""
    from brain.api.reference import is_owner_route

    _real, published = _real_and_published()
    leaked = sorted((m, p) for m, p in published if is_owner_route(m.upper(), p))
    assert not leaked, f"owner-gated routes published in the public schema: {leaked}"


def test_public_openapi_keeps_reads_whose_writes_are_owner_only():
    """Org config is partner-READABLE, so filtering must be per method, not per
    path — dropping the whole path would hide the roster a partner needs to resolve
    an agent_id."""
    _real, published = _real_and_published()
    assert ("get", "/v1/mandates") in published
    assert ("get", "/v1/personas") in published
    assert ("put", "/v1/mandates/{mandate_id}") not in published


def test_swagger_ui_points_at_the_gateway_schema_url():
    """The whole bug was Swagger fetching a schema URL the gateway wouldn't serve."""

    async def run():
        transport = httpx.ASGITransport(app=gw.build_gateway_app(_FakeProv(), [_FakeRunpod()]))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/docs")

    r = asyncio.run(run())
    assert r.status_code == 200
    assert "/v1/openapi.json" in r.text


def test_public_api_docs_kill_switch(monkeypatch):
    """BRAIN_PUBLIC_API_DOCS=0 removes the routes; they then fall through to the
    bearer-authed /v1 proxy, which 401s an unkeyed request."""
    monkeypatch.setenv("BRAIN_PUBLIC_API_DOCS", "0")
    monkeypatch.setattr(api_auth, "resolve_key_context", lambda _auth: None)
    r = _openapi(gw.build_gateway_app(_FakeProv(), [_FakeRunpod()]))
    assert r.status_code == 401


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


def test_resolve_key_context_maps_token_to_org():
    with _supabase([{"org_id": "org-42", "partner_id": "acme", "role": "partner"}]):
        ctx = api_auth.resolve_key_context("Bearer sk_x")
    assert ctx == {
        "org_id": "org-42",
        "partner_id": "acme",
        "role": "partner",
        # Pre-036 row: no allowlist column → unrestricted; no id column in this fixture.
        "allowed_agents": None,
        "key_id": None,
    }


def test_resolve_key_context_defaults_role_to_partner():
    """A row predating migration 028 (no role) must never read as owner."""
    with _supabase([{"org_id": "org-42", "partner_id": "acme"}]):
        assert api_auth.resolve_key_context("Bearer sk_x")["role"] == "partner"


def test_resolve_key_context_unknown_is_none():
    with _supabase([]):
        assert api_auth.resolve_key_context("Bearer sk_x") is None
    assert api_auth.resolve_key_context(None) is None


def test_has_any_api_keys_true_when_rows_exist():
    with _supabase([{"id": "k1"}]):
        assert api_auth.has_any_api_keys() is True
    with _supabase([]):
        assert api_auth.has_any_api_keys() is False


# ── first over-cap request, booting Retry-After, whoami, X-RateLimit-Reset ────


def test_first_over_cap_request_is_at_capacity(monkeypatch):
    """The synchronous check: a provisioner that KNOWS it is full answers the very
    first request with at_capacity — no spawn is scheduled, nothing is kicked."""
    _patch(monkeypatch, "org-1")
    monkeypatch.setattr(gw, "capacity_refusals", {})
    prov = _FakeProv(status=None)
    prov.capacity_refusal = lambda org, persona=None: "host at 25 brains"
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
        for _ in range(5):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 503
    assert r.json() == {"status": "at_capacity", "detail": "host at 25 brains"}
    assert r.headers.get("Retry-After") == "30"
    assert prov.ensured == [] and runpod.ensured is False


def test_booting_503_carries_retry_after(monkeypatch):
    _patch(monkeypatch, "org-1")
    monkeypatch.setattr(gw, "capacity_refusals", {})
    app = gw.build_gateway_app(_FakeProv(status=None), [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
        for _ in range(5):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 503 and r.json()["status"] == "booting"
    assert r.headers.get("Retry-After") == "2"


def test_v1_whoami_answers_cold_without_spawning(monkeypatch):
    """Mirrors test_v1_status_reports_cost_state: answered from the key row, never
    spawns, never kicks the pod — so it works during a cold start."""
    ctx = {
        "org_id": "org-1",
        "partner_id": "acme",
        "role": "partner",
        "key_id": "k1",
        "allowed_agents": ["p.m"],
    }
    monkeypatch.setattr(api_auth, "resolve_key_context", lambda _auth: ctx)
    monkeypatch.setattr(gw, "_org_learning_cache", {})
    from brain import org_settings

    reads: list[str] = []

    def _row(org, client=None):
        reads.append(org)
        return {"id": org, "learning_mode": "isolated", "instance_seed": "current"}

    monkeypatch.setattr(org_settings, "read_org_row", _row)
    prov = _FakeProv(status=None)  # brain not up
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            a = await c.get("/v1/whoami", headers={"authorization": "Bearer good"})
            b = await c.get("/v1/whoami", headers={"authorization": "Bearer good"})
            return a, b

    a, b = asyncio.run(run())
    assert a.status_code == 200
    # The engine twin's shape (api_guide "GET /v1/whoami"): learning_mode and
    # instance_seed ride along, read from the organizations row.
    assert a.json() == {**ctx, "learning_mode": "isolated", "instance_seed": "current"}
    assert b.json() == a.json() and reads == ["org-1"], "one cached row read per org"
    assert prov.ensured == [] and runpod.ensured is False


def test_v1_whoami_unknown_mode_when_the_org_row_is_unreadable(monkeypatch):
    """Same convention as the engine twin: unknown = not read, treat as isolated.
    A later successful read is served; a later failure keeps the last-known value."""
    _patch(monkeypatch, "org-1")
    monkeypatch.setattr(gw, "_org_learning_cache", {})
    monkeypatch.setattr(gw, "_ORG_LEARNING_TTL_S", 0.0)  # every call re-reads
    from brain import org_settings

    rows = iter([None, {"learning_mode": "consolidated", "instance_seed": "default"}, None])
    monkeypatch.setattr(org_settings, "read_org_row", lambda org, client=None: next(rows))
    app = gw.build_gateway_app(_FakeProv(status=None), [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            out = []
            for _ in range(3):
                r = await c.get("/v1/whoami", headers={"authorization": "Bearer good"})
                out.append((r.json()["learning_mode"], r.json()["instance_seed"]))
            return out

    assert asyncio.run(run()) == [
        ("unknown", None),
        ("consolidated", "default"),
        ("consolidated", "default"),
    ]


def test_v1_whoami_unauthorized(monkeypatch):
    _patch(monkeypatch, None)
    app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/whoami", headers={"authorization": "Bearer nope"})

    assert asyncio.run(run()).status_code == 401


def test_rate_limit_reset_header_on_success_and_429(monkeypatch):
    from brain.api import rate_limit as rl

    monkeypatch.setenv("BRAIN_RATE_LIMIT", "1")
    monkeypatch.setenv("BRAIN_RL_KEY_PER_MIN", "2")
    monkeypatch.setattr(rl, "limiter", rl.RateLimiter())
    _patch(monkeypatch, "org-1")
    prov = _FakeProv(status={"port": 0, "api_port": 1, "booting": False, "pid": 1})
    app = gw.build_gateway_app(prov, [_FakeRunpod()])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            h = {"authorization": "Bearer good"}
            a = await c.get("/v1/status", headers=h)
            b = await c.get("/v1/status", headers=h)
            over = await c.get("/v1/status", headers=h)
            return a, b, over

    a, b, over = asyncio.run(run())
    assert a.status_code == 200
    assert a.headers["X-RateLimit-Limit"] == "2" and a.headers["X-RateLimit-Remaining"] == "1"
    assert a.headers["X-RateLimit-Reset"].isdigit()
    assert b.headers["X-RateLimit-Reset"] == a.headers["X-RateLimit-Reset"]  # same window
    assert over.status_code == 429
    assert over.headers.get("Retry-After") and over.headers["X-RateLimit-Reset"].isdigit()
    assert abs(int(over.headers["X-RateLimit-Reset"]) - int(a.headers["X-RateLimit-Reset"])) <= 1
