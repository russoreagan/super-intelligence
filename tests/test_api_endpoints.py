"""
Engine API endpoints — POST /v1/sessions and /v1/sessions/{id}/turns.

Tested via FastAPI TestClient against the router with a FAKE turn-runner (so no
brain is needed): auth is enforced fail-closed, a session binds an end_user_id, a
turn routes the message + that end_user_id to the runner and surfaces the mood, and
inputs are validated.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _FakeRunner:
    """Records (message, end_user_id) and returns a scripted (text, affect)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, message, end_user_id):
        self.calls.append((message, end_user_id))
        return (
            f"echo: {message}",
            {"emotion": "warm", "user_emotion": "curious", "hormonal": {"OXT": 0.3}, "appraisal": "SECRET"},
        )


def _client(runner, *, keys=None):
    keys = keys or {"sk_test_123"}
    registry = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    app = FastAPI()
    # inject a fixed key set so the test doesn't depend on env
    app.include_router(build_api_router(runner, registry, auth=lambda h: _ok(h, keys)))
    return TestClient(app)


def _ok(authorization, keys):
    if not authorization:
        return False
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    return tok in keys


_AUTH = {"Authorization": "Bearer sk_test_123"}


def test_requires_api_key():
    c = _client(_FakeRunner())
    assert c.post("/v1/sessions", json={"end_user_id": "cust-1"}).status_code == 401
    assert c.post("/v1/sessions", json={"end_user_id": "cust-1"}, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_create_session_and_run_turn_routes_end_user_id():
    runner = _FakeRunner()
    c = _client(runner)

    r = c.post("/v1/sessions", json={"end_user_id": "cust-1", "agent_id": "empath"}, headers=_AUTH)
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert sid == "sess_abc"
    assert r.json()["end_user_id"] == "cust-1"

    r2 = c.post(f"/v1/sessions/{sid}/turns", json={"message": "hello"}, headers=_AUTH)
    assert r2.status_code == 200
    body = r2.json()
    assert body["response"] == "echo: hello"
    # the turn ran as the session's end_user
    assert runner.calls == [("hello", "cust-1")]


def test_turn_surfaces_mood_but_not_internal_fields():
    c = _client(_FakeRunner())
    sid = c.post("/v1/sessions", json={"end_user_id": "cust-1"}, headers=_AUTH).json()["session_id"]
    mood = c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=_AUTH).json()["mood"]
    assert mood["emotion"] == "warm"
    assert mood["user_emotion"] == "curious"
    assert mood["hormonal"] == {"OXT": 0.3}
    assert "appraisal" not in mood  # internal affect fields are not leaked


def test_turn_on_unknown_session_404():
    c = _client(_FakeRunner())
    r = c.post("/v1/sessions/nope/turns", json={"message": "hi"}, headers=_AUTH)
    assert r.status_code == 404


def test_validation_errors():
    c = _client(_FakeRunner())
    # missing end_user_id
    assert c.post("/v1/sessions", json={}, headers=_AUTH).status_code == 400
    sid = c.post("/v1/sessions", json={"end_user_id": "cust-1"}, headers=_AUTH).json()["session_id"]
    # empty message
    assert c.post(f"/v1/sessions/{sid}/turns", json={"message": "  "}, headers=_AUTH).status_code == 400


def test_fail_closed_when_no_keys_configured(monkeypatch):
    """With no configured keys, the default check_bearer denies everything."""
    monkeypatch.delenv("BRAIN_API_KEYS", raising=False)
    monkeypatch.delenv("BRAIN_API_KEY", raising=False)
    from brain.api import auth as _auth

    monkeypatch.setattr(_auth, "configured_keys", lambda: set())
    from brain.api.auth import check_bearer

    registry = ApiSessionRegistry()
    app = FastAPI()
    app.include_router(build_api_router(_FakeRunner(), registry, auth=check_bearer))
    c = TestClient(app)
    # no BRAIN_API_KEY in env → fail closed even with a bearer header
    assert c.post("/v1/sessions", json={"end_user_id": "x"}, headers=_AUTH).status_code == 401


def test_apiserver_app_serves_routes_with_env_key(monkeypatch):
    """End-to-end against the real ApiServer app + the default env-based auth."""
    monkeypatch.setenv("BRAIN_API_KEY", "sk_live_xyz")
    from brain.api.server import ApiServer

    runner = _FakeRunner()
    c = TestClient(ApiServer(runner).app)
    h = {"Authorization": "Bearer sk_live_xyz"}
    sid = c.post("/v1/sessions", json={"end_user_id": "cust-9"}, headers=h).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/turns", json={"message": "yo"}, headers=h)
    assert r.status_code == 200
    assert r.json()["response"] == "echo: yo"
    assert runner.calls == [("yo", "cust-9")]
