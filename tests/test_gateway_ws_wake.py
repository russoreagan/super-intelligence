"""The UI /ws lane wakes a brain that is simply not running, but never undoes a Sleep.

Before this, a /ws connect that found no brain closed 1013 (the browser sees a 403)
and spawned nothing, so an open tab whose brain went away — a gateway redeploy, a
backstop reap — reconnected every 2 s forever until the user reloaded. The HTTP
catch-all and the partner stream both spawned in the same situation; /ws did not.

The Sleep contract is the constraint: a deliberately slept org stays asleep through
the page's reconnects, and only the page's explicit `?wake=1` (the user sent a
message) brings it back.
"""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brain.gateway import server as gw
from tests.test_gateway_sleep import _auth_patched

# ── the decision ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry, query, expected",
    [
        (None, {}, True),  # nothing running, nobody slept it → wake
        ({"state": "asleep"}, {}, False),  # deliberate Sleep holds
        ({"state": "consolidating"}, {}, False),
        ({"state": "pausing_pod"}, {}, False),
        ({"state": "error"}, {}, True),  # a failed sleep is not a hold
        (None, {"passive": "1"}, False),  # a sleeping page's probe never spawns
        ({"state": "asleep"}, {"wake": "1"}, True),  # the user asked
        ({"state": "error"}, {"wake": "1"}, True),
        ({"state": "stopping"}, {"wake": "1"}, False),  # never race a sweep
    ],
)
def test_ws_should_wake(entry, query, expected):
    assert gw._ws_should_wake(entry, query) is expected


# ── wired through the real /ws route ────────────────────────────────────────


class _NoBrainProv:
    """No brain is running for anyone; records nothing itself (ensure is patched)."""

    def __init__(self):
        self.stopped: list[str] = []

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    def status(self, t, persona=None):
        return None

    def is_running(self, t, persona=None):
        return False

    async def stop_user(self, t, persona=None):
        self.stopped.append(t)

    def keys_for(self, t):
        return [t]

    def live_count(self):
        return 0

    def full_count(self):
        return 0

    def touch(self, t, persona=None):  # pragma: no cover
        pass


@pytest.fixture
def ensured(monkeypatch):
    calls: list[str] = []

    async def _fake_ensure(_prov, uid, persona=None):
        calls.append(uid)

    async def _has_key(_org):
        return True

    monkeypatch.setattr(gw, "_safe_ensure", _fake_ensure)
    monkeypatch.setattr(gw, "_org_has_anthropic", _has_key)
    return calls


def _ws_refused(client: TestClient, path: str) -> None:
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(path):
        pass
    assert exc.value.code == 1013  # not ready — the page retries


def _settle():
    # The spawn is fire-and-forget (create_task); give the app loop a beat.
    time.sleep(0.05)


def test_reconnect_wakes_a_brain_that_is_not_running(ensured):
    with _auth_patched():
        app = gw.build_gateway_app(_NoBrainProv(), [None])
        with TestClient(app) as client:
            _ws_refused(client, "/ws")
            _settle()
    assert ensured == ["u1"]


def test_passive_probe_never_spawns(ensured):
    with _auth_patched():
        app = gw.build_gateway_app(_NoBrainProv(), [None])
        with TestClient(app) as client:
            _ws_refused(client, "/ws?passive=1")
            _settle()
    assert ensured == []


def test_no_anthropic_key_never_spawns(ensured, monkeypatch):
    async def _no_key(_org):
        return False

    monkeypatch.setattr(gw, "_org_has_anthropic", _no_key)
    with _auth_patched():
        app = gw.build_gateway_app(_NoBrainProv(), [None])
        with TestClient(app) as client:
            _ws_refused(client, "/ws")
            _settle()
    assert ensured == []


def test_sleep_holds_until_explicit_wake(ensured):
    with _auth_patched():
        app = gw.build_gateway_app(_NoBrainProv(), [None])
        with TestClient(app) as client:
            assert client.post("/shutdown").json().get("ok") is True
            for _ in range(200):
                if client.get("/__sleep_status").json()["state"] in ("asleep", "error"):
                    break
                time.sleep(0.01)
            assert client.get("/__sleep_status").json()["state"] == "asleep"

            # The page's reconnects while asleep must not undo the Sleep.
            _ws_refused(client, "/ws")
            _ws_refused(client, "/ws?passive=1")
            _settle()
            assert ensured == []

            # The user sends a message → the page connects with ?wake=1.
            _ws_refused(client, "/ws?wake=1")
            _settle()
            assert ensured == ["u1"]
            # Waking clears the sleep record, so ordinary reconnects spawn again.
            assert client.get("/__sleep_status").json()["state"] == "awake"
