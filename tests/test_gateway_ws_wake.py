"""The UI /ws lane wakes a brain that is simply not running, but never undoes a Sleep.

Before this, a /ws connect that found no brain closed 1013 (the browser sees a 403)
and spawned nothing, so an open tab whose brain went away — a gateway redeploy, a
backstop reap — reconnected every 2 s forever until the user reloaded. The HTTP
catch-all and the partner stream both spawned in the same situation; /ws did not.

The Sleep contract is the constraint: a deliberately slept org stays asleep through
any reconnect. A sleeping page does not poll; the brain comes back when the user
logs in, or on the page's explicit `?wake=1` (the user sent a message).
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
            _settle()
            assert ensured == []

            # The user sends a message → the page connects with ?wake=1.
            _ws_refused(client, "/ws?wake=1")
            _settle()
            assert ensured == ["u1"]
            # Waking clears the sleep record, so ordinary reconnects spawn again.
            assert client.get("/__sleep_status").json()["state"] == "awake"


def _sleep(client: TestClient) -> None:
    assert client.post("/shutdown").json().get("ok") is True
    for _ in range(200):
        if client.get("/__sleep_status").json()["state"] in ("asleep", "error"):
            break
        time.sleep(0.01)
    assert client.get("/__sleep_status").json()["state"] == "asleep"


@pytest.fixture
def login_ok(monkeypatch):
    from brain.ui import auth as ui_auth

    async def _login(email, password):
        return {"access_token": "at", "refresh_token": "rt", "user": {"id": "u1"}}

    monkeypatch.setattr(ui_auth, "password_login", _login)


def test_login_wakes_a_slept_brain(ensured, login_ok):
    with _auth_patched():
        app = gw.build_gateway_app(_NoBrainProv(), [None])
        with TestClient(app) as client:
            _sleep(client)
            r = client.post("/auth/login", json={"email": "a@b.c", "password": "pw"})
            assert r.json()["ok"] is True
            _settle()
            assert ensured == ["u1"]
            assert client.get("/__sleep_status").json()["state"] == "awake"


def test_failed_login_wakes_nothing(ensured, monkeypatch):
    from brain.ui import auth as ui_auth

    async def _bad(email, password):
        return None

    monkeypatch.setattr(ui_auth, "password_login", _bad)
    with _auth_patched():
        app = gw.build_gateway_app(_NoBrainProv(), [None])
        with TestClient(app) as client:
            _sleep(client)
            r = client.post("/auth/login", json={"email": "a@b.c", "password": "no"})
            assert r.status_code == 401
            _settle()
    assert ensured == []


# ── every door, not just /ws ────────────────────────────────────────────────
# _ws_should_wake hardened ONE of five paths that spawn a brain. The other four
# popped sleep_status unconditionally, so a readiness poll or a page load from a
# tab left open quietly undid a deliberate Sleep, and a login during the sweep
# respawned the brain underneath it.


@pytest.mark.parametrize(
    "entry, explicit, expected",
    [
        # Nothing running, nobody slept it → anyone may wake.
        (None, False, True),
        (None, True, True),
        # A deliberate Sleep: only an explicit ask overrides it.
        ({"state": "asleep"}, False, False),
        ({"state": "asleep"}, True, True),
        # Mid-sweep: nobody may wake, however explicit.
        ({"state": "consolidating"}, False, False),
        ({"state": "consolidating"}, True, False),
        ({"state": "stopping"}, True, False),
        ({"state": "pausing_pod"}, True, False),
        # A FAILED sleep is not a hold.
        ({"state": "error"}, False, True),
        ({"state": "error"}, True, True),
    ],
)
def test_may_wake(entry, explicit, expected):
    assert gw._may_wake(entry, explicit=explicit) is expected


def test_explicit_wake_is_never_stricter_than_a_passive_one():
    """The old asymmetry: two independently maintained sets meant a state could be
    reconnect-wakeable but explicit-wake-refused — the user's 'wake up' refused
    while a stray background poll succeeded."""
    states = [None, "asleep", "error", "consolidating", "stopping", "pausing_pod", "waking"]
    for st in states:
        entry = None if st is None else {"state": st}
        passive = gw._may_wake(entry, explicit=False)
        explicit = gw._may_wake(entry, explicit=True)
        assert not (passive and not explicit), f"{st}: passive wakes but explicit does not"


def test_hold_states_are_derived_from_the_sweep_states():
    """Adding a phase to _set_sleep must not require editing a second list."""
    assert gw._SLEEP_SWEEP_STATES | {"asleep"} == gw._SLEEP_HOLD_STATES


def test_a_new_sweep_phase_is_refused_by_every_caller(monkeypatch):
    """A phase nobody has taught the guard about must fail closed, not open."""
    monkeypatch.setattr(
        gw, "_SLEEP_SWEEP_STATES", gw._SLEEP_SWEEP_STATES | {"flushing"}, raising=True
    )
    assert gw._may_wake({"state": "flushing"}, explicit=True) is False
    assert gw._may_wake({"state": "flushing"}, explicit=False) is False
