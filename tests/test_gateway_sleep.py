"""Gateway Sleep-flow tests.

Guards the shutdown sequencing the UI relies on to show progress:
  consolidating → stopping → pausing_pod → asleep   (or error, naming the phase)

And the cost-critical rule: the shared pod is paused ONLY when this was the last
live brain; with other sessions still up it is kept.

And the consolidation hop itself: the gateway's POST /shutdown to a tenant must
carry the internal token (the tenant keeps its cookie gate on), and a refusal is
logged at WARNING rather than swallowed — silently eating the 401 is exactly how
every Sleep came to burn SLEEP_CONSOLIDATE_WAIT_S and then cut consolidation short.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

import brain.provisioner as pv
from brain.gateway import server as gw
from brain.ui import auth as ui_auth

TOKEN = "s" * 40


class _FakeProv:
    def __init__(self, live_count=0, org_keys=None, live_port=0):
        self.stopped: list[str] = []
        self._live = live_count
        # Keys the sleep sweep iterates: default just the org itself; tests for
        # the multi-instance sweep inject ["org::persona", ..., "org"].
        self._org_keys = org_keys
        self.live_port = live_port

    async def start(self):  # pragma: no cover - not exercised
        pass

    async def stop(self):  # pragma: no cover
        pass

    def status(self, t, persona=None):
        # booting=True makes _sleep skip the brain HTTP call + graceful wait,
        # keeping the test free of real sockets/timing. The consolidation-hop
        # tests set `live_port` so the gateway actually POSTs /shutdown.
        if self.live_port:
            return {"port": self.live_port, "booting": False, "pid": 1}
        return {"port": 0, "booting": True, "pid": 1}

    def is_running(self, t, persona=None):
        return False

    async def stop_user(self, t, persona=None):
        self.stopped.append(f"{t}::{persona}" if persona else t)

    def keys_for(self, t):
        return list(self._org_keys) if self._org_keys is not None else [t]

    def live_count(self):
        return self._live

    def full_count(self):
        # The sleep path gates the pod on full-tier brains; in these tests every
        # session is full, so it mirrors live_count.
        return self._live

    def touch(self, t):  # pragma: no cover
        pass


class _FakeRunpod:
    def __init__(self):
        self.paused = False
        self._consumer = False
        self._pod_id = "pod1"

    async def pause(self):
        self.paused = True
        self._pod_id = None

    def status(self):  # pragma: no cover - not asserted here
        return {"state": "off", "detail": "", "elapsed_s": 0}


@contextlib.contextmanager
def _auth_patched():
    """Force the gateway auth gate to admit a fixed user without a real Supabase."""
    orig_disabled = ui_auth.is_disabled
    orig_configured = ui_auth.is_configured
    orig_auth = ui_auth.authenticate
    orig_set = ui_auth.set_session_cookies
    import brain.org as org

    orig_org = org.org_id_for_user

    ui_auth.is_disabled = lambda: False
    ui_auth.is_configured = lambda: True

    async def _fake_auth(_request):
        return {"sub": "u1"}, None

    ui_auth.authenticate = _fake_auth
    ui_auth.set_session_cookies = lambda *a, **k: None
    org.org_id_for_user = lambda uid: uid  # personal org == uid
    gw._org_cache.clear()
    try:
        yield
    finally:
        ui_auth.is_disabled = orig_disabled
        ui_auth.is_configured = orig_configured
        ui_auth.authenticate = orig_auth
        ui_auth.set_session_cookies = orig_set
        org.org_id_for_user = orig_org
        gw._org_cache.clear()


async def _run_sleep(prov, runpod):
    app = gw.build_gateway_app(prov, [runpod])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/shutdown")
        assert r.status_code == 200 and r.json().get("ok") is True
        # Let the background _sleep task progress; poll until terminal.
        final = None
        for _ in range(100):
            s = (await client.get("/__sleep_status")).json()
            final = s
            if s["state"] in ("asleep", "error"):
                break
            await asyncio.sleep(0.01)
        return final


def test_sleep_pauses_pod_when_last_brain():
    with _auth_patched():
        prov = _FakeProv(live_count=0)
        runpod = _FakeRunpod()
        final = asyncio.run(_run_sleep(prov, runpod))
    assert final["state"] == "asleep"
    assert final["pod"] == "paused"
    assert runpod.paused is True
    assert "u1" in prov.stopped


def test_sleep_keeps_pod_when_other_brains_live():
    with _auth_patched():
        prov = _FakeProv(live_count=1)  # another session still using the pod
        runpod = _FakeRunpod()
        final = asyncio.run(_run_sleep(prov, runpod))
    assert final["state"] == "asleep"
    assert final["pod"] == "kept"
    assert runpod.paused is False
    assert "u1" in prov.stopped


def test_sleep_sweeps_all_org_instances_default_last():
    """An org with dedicated persona instances gets a full sweep: every instance
    consolidated + stopped, the default (fallback) instance last — this is the
    'all persona learning per org consolidates' guarantee under elastic placement."""
    with _auth_patched():
        prov = _FakeProv(
            live_count=0,
            org_keys=["u1::the_analyst", "u1::the_poet", "u1"],
        )
        runpod = _FakeRunpod()
        final = asyncio.run(_run_sleep(prov, runpod))
    assert final["state"] == "asleep"
    assert prov.stopped == ["u1::the_analyst", "u1::the_poet", "u1"]  # default LAST


def test_sleep_status_awake_by_default():
    with _auth_patched():
        app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])

        async def _check():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return (await c.get("/__sleep_status")).json()

        d = asyncio.run(_check())
    assert d["state"] == "awake"


# ── the consolidation hop: gateway → tenant POST /shutdown ─────────────────


@contextlib.contextmanager
def _tenant_shutdown(monkeypatch, status: int):
    """Stand in for the tenant's /shutdown: capture what the gateway sends and
    answer `status`. The gateway's OWN test client (ASGITransport) is left alone."""
    seen: list[dict] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request):
        seen.append(
            {
                "url": str(request.url),
                "token": request.headers.get("x-brain-internal-token"),
            }
        )
        if status == 200:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status, json={"error": "unauthorized"})

    def _client(**kw):
        if "transport" not in kw:
            kw["transport"] = httpx.MockTransport(handler)
        return real(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(pv, "_INTERNAL_TOKEN", TOKEN)
    yield seen


def test_sleep_shutdown_hop_presents_the_internal_token(monkeypatch, caplog):
    """The tenant keeps cookie auth ON, so the gateway's /shutdown must carry the
    per-boot internal token — otherwise the tenant answers 401 and never SIGTERMs."""
    with _auth_patched(), _tenant_shutdown(monkeypatch, 200) as seen:
        prov = _FakeProv(live_count=0, live_port=9101)
        runpod = _FakeRunpod()
        with caplog.at_level(logging.WARNING, logger="brain.gateway.server"):
            final = asyncio.run(_run_sleep(prov, runpod))
    assert final["state"] == "asleep"
    assert prov.stopped == ["u1"]
    assert seen == [{"url": "http://127.0.0.1:9101/shutdown", "token": TOKEN}]
    assert not [r for r in caplog.records if "/shutdown" in r.getMessage()]


def test_sleep_shutdown_hop_sweeps_every_instance_with_the_token(monkeypatch):
    with _auth_patched(), _tenant_shutdown(monkeypatch, 200) as seen:
        prov = _FakeProv(live_count=0, org_keys=["u1::the_poet", "u1"], live_port=9102)
        final = asyncio.run(_run_sleep(prov, _FakeRunpod()))
    assert final["state"] == "asleep"
    assert [c["token"] for c in seen] == [TOKEN, TOKEN]
    assert prov.stopped == ["u1::the_poet", "u1"]


def test_sleep_shutdown_hop_refusal_is_logged_not_swallowed(monkeypatch, caplog):
    """A non-200 from the tenant means consolidation is about to be cut short by
    the reaper's SIGTERM: it must surface at WARNING, and Sleep still completes."""
    with _auth_patched(), _tenant_shutdown(monkeypatch, 401) as seen:
        prov = _FakeProv(live_count=0, live_port=9103)
        with caplog.at_level(logging.WARNING, logger="brain.gateway.server"):
            final = asyncio.run(_run_sleep(prov, _FakeRunpod()))
    assert final["state"] == "asleep" and prov.stopped == ["u1"]
    assert len(seen) == 1
    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warn and "refused /shutdown" in warn[0].getMessage() and "401" in warn[0].getMessage()


def test_sleep_shutdown_hop_transport_failure_is_logged(monkeypatch, caplog):
    real = httpx.AsyncClient

    def boom(request):
        raise httpx.ConnectError("down")

    def _client(**kw):
        if "transport" not in kw:
            kw["transport"] = httpx.MockTransport(boom)
        return real(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(pv, "_INTERNAL_TOKEN", TOKEN)
    with _auth_patched():
        prov = _FakeProv(live_count=0, live_port=9104)
        with caplog.at_level(logging.WARNING, logger="brain.gateway.server"):
            final = asyncio.run(_run_sleep(prov, _FakeRunpod()))
    assert final["state"] == "asleep" and prov.stopped == ["u1"]
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("/shutdown to tenant" in m and "down" in m for m in msgs)
