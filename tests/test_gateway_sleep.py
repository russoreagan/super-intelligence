"""Gateway Sleep-flow tests.

Guards the shutdown sequencing the UI relies on to show progress:
  consolidating → stopping → pausing_pod → asleep   (or error, naming the phase)

And the cost-critical rule: the shared pod is paused ONLY when this was the last
live brain; with other sessions still up it is kept.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from brain.gateway import server as gw
from brain.ui import auth as ui_auth


class _FakeProv:
    def __init__(self, live_count=0):
        self.stopped: list[str] = []
        self._live = live_count

    async def start(self):  # pragma: no cover - not exercised
        pass

    async def stop(self):  # pragma: no cover
        pass

    def status(self, t):
        # booting=True makes _sleep skip the brain HTTP call + graceful wait,
        # keeping the test free of real sockets/timing.
        return {"port": 0, "booting": True, "pid": 1}

    def is_running(self, t):
        return False

    async def stop_user(self, t):
        self.stopped.append(t)

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


def test_sleep_status_awake_by_default():
    with _auth_patched():
        app = gw.build_gateway_app(_FakeProv(), [_FakeRunpod()])

        async def _check():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return (await c.get("/__sleep_status")).json()

        d = asyncio.run(_check())
    assert d["state"] == "awake"
