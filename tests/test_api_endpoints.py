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
    """Records (message, end_user_id, mandate_id) and returns a scripted (text, affect)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, message, end_user_id, mandate_id=None):
        self.calls.append((message, end_user_id, mandate_id))
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

    r = c.post("/v1/sessions", json={"end_user_id": "cust-1"}, headers=_AUTH)
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert sid == "sess_abc"
    assert r.json()["end_user_id"] == "cust-1"

    r2 = c.post(f"/v1/sessions/{sid}/turns", json={"message": "hello"}, headers=_AUTH)
    assert r2.status_code == 200
    body = r2.json()
    assert body["response"] == "echo: hello"
    # the turn ran as the session's end_user (no mandate set here)
    assert runner.calls == [("hello", "cust-1", None)]


def test_mandate_id_flows_from_session_to_turn():
    runner = _FakeRunner()
    c = _client(runner)
    sid = c.post(
        "/v1/sessions",
        json={"end_user_id": "cust-7", "mandate_id": "billing"},
        headers=_AUTH,
    ).json()["session_id"]
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "help"}, headers=_AUTH)
    # the per-session mandate_id (the catalog selector) is carried into every turn
    assert runner.calls == [("help", "cust-7", "billing")]


def test_invalid_mandate_id_type_rejected():
    c = _client(_FakeRunner())
    r = c.post("/v1/sessions", json={"end_user_id": "x", "mandate_id": 123}, headers=_AUTH)
    assert r.status_code == 400


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


def test_mandate_routes_require_api_key():
    c = _client(_FakeRunner())
    assert c.get("/v1/mandates").status_code == 401
    assert c.put("/v1/mandates/x", json={"role_text": "y"}).status_code == 401


def test_mandate_routes_503_when_storage_off(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    c = _client(_FakeRunner())
    assert c.get("/v1/mandates", headers=_AUTH).status_code == 503
    assert c.put("/v1/mandates/x", json={"role_text": "y"}, headers=_AUTH).status_code == 503


def test_mandate_crud_routes_with_fake_backend(monkeypatch):
    from brain import mandates
    from brain.mandates import MandateError
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    store = {}

    def _upsert(mid, role_text, conduct_rules=None, reward_weights=None):
        if not mandates.MANDATE_ID_RE.match(mid):
            raise MandateError("bad id")
        row = {"id": mid, "role_text": role_text, "version": store.get(mid, 0) + 1, "active": True}
        store[mid] = row["version"]
        return row

    monkeypatch.setattr(mandates, "upsert_mandate", _upsert)
    monkeypatch.setattr(mandates, "list_mandates", lambda include_inactive=False: [{"id": k} for k in store])
    monkeypatch.setattr(mandates, "assign", lambda persona, mid, sort_order=0: {"persona": persona, "mandate_id": mid})

    c = _client(_FakeRunner())
    # missing role_text → 400
    assert c.put("/v1/mandates/billing", json={}, headers=_AUTH).status_code == 400
    # valid create → version 1, re-PUT → version 2
    assert c.put("/v1/mandates/billing", json={"role_text": "a"}, headers=_AUTH).json()["version"] == 1
    assert c.put("/v1/mandates/billing", json={"role_text": "b"}, headers=_AUTH).json()["version"] == 2
    # bad slug → MandateError → 400
    assert c.put("/v1/mandates/Bad Slug", json={"role_text": "a"}, headers=_AUTH).status_code == 400
    # assignment route
    r = c.put("/v1/personas/the_analyst/mandates/billing", json={}, headers=_AUTH)
    assert r.status_code == 200 and r.json()["mandate_id"] == "billing"


def test_agent_id_resolves_to_mandate(monkeypatch):
    """agent_id is resolved to (persona, mandate); the session runs that mandate."""
    from brain import agents

    monkeypatch.setattr(agents, "resolve", lambda aid: ("the_analyst", "billing"))
    runner = _FakeRunner()
    c = _client(runner)
    r = c.post("/v1/sessions", json={"end_user_id": "c1", "agent_id": "the_analyst.billing"}, headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["mandate_id"] == "billing"
    sid = r.json()["session_id"]
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=_AUTH)
    assert runner.calls == [("hi", "c1", "billing")]


def test_agent_id_cross_persona_409(monkeypatch):
    from brain import agents

    def _boom(aid):
        raise agents.AgentPersonaMismatch("wrong persona")

    monkeypatch.setattr(agents, "resolve", _boom)
    c = _client(_FakeRunner())
    r = c.post("/v1/sessions", json={"end_user_id": "c1", "agent_id": "other.billing"}, headers=_AUTH)
    assert r.status_code == 409


def test_agent_id_unknown_404(monkeypatch):
    from brain import agents

    def _boom(aid):
        raise agents.AgentNotFound("nope")

    monkeypatch.setattr(agents, "resolve", _boom)
    c = _client(_FakeRunner())
    r = c.post("/v1/sessions", json={"end_user_id": "c1", "agent_id": "the_analyst.ghost"}, headers=_AUTH)
    assert r.status_code == 404


def test_agents_list_and_ceilings(monkeypatch):
    from brain import agents
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(
        agents, "list_agents",
        lambda: [{"agent_id": "the_analyst.billing", "persona": "the_analyst", "mandate_id": "billing", "permissions": {}}],
    )
    c = _client(_FakeRunner())
    r = c.get("/v1/agents", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["agents"][0]["agent_id"] == "the_analyst.billing"
    assert "cloud_daily_usd_budget" in body["ceilings"]  # org ceiling exposed


def test_agents_503_when_storage_off(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    c = _client(_FakeRunner())
    assert c.get("/v1/agents", headers=_AUTH).status_code == 503


def test_agent_upsert_sets_name_and_permissions(monkeypatch):
    from brain import agents, mandates
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    calls = {}
    monkeypatch.setattr(mandates, "assign", lambda p, m, *a, **k: calls.setdefault("assign", (p, m)))
    monkeypatch.setattr(agents, "set_name", lambda aid, n: calls.setdefault("name", (aid, n)))
    monkeypatch.setattr(agents, "set_permissions", lambda aid, perms: calls.setdefault("perms", (aid, perms)))
    monkeypatch.setattr(agents, "get", lambda aid: {"agent_id": aid, "name": "Billing", "permissions": {"cloud_daily_usd_budget": 20000}})

    c = _client(_FakeRunner())
    r = c.put(
        "/v1/agents/the_analyst.billing",
        json={"name": "Billing", "permissions": {"cloud_daily_usd_budget": 20000}},
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Billing"
    assert calls["assign"] == ("the_analyst", "billing")  # pairing created
    assert calls["perms"][1]["cloud_daily_usd_budget"] == 20000


def test_agent_delete(monkeypatch):
    from brain import mandates
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(mandates, "unassign", lambda p, m: (p, m) == ("the_analyst", "billing"))
    c = _client(_FakeRunner())
    assert c.delete("/v1/agents/the_analyst.billing", headers=_AUTH).status_code == 200
    assert c.delete("/v1/agents/the_analyst.ghost", headers=_AUTH).status_code == 404


class _PendingRunner:
    """Returns a turn whose affect parks a pending cloud write."""

    async def __call__(self, message, end_user_id, mandate_id=None):
        return ("I need your OK to send that email.",
                {"emotion": "attentive", "pending": {"task": "send email", "description": "Email Bob", "is_write": True}})


def _client2(turn_runner, confirm_runner=None, keys=None):
    keys = keys or {"sk_test_123"}
    registry = ApiSessionRegistry(now_fn=lambda: 1.0, id_fn=lambda: "sess_p")
    app = FastAPI()
    app.include_router(build_api_router(turn_runner, registry, auth=lambda h: _ok(h, keys), confirm_runner=confirm_runner))
    return TestClient(app)


def test_pending_write_surfaces_confirmation_then_confirms():
    confirms = {}

    async def _confirm(pending, end_user_id, mandate_id, approve):
        confirms["called"] = (pending["task"], approve)
        return ("Sent." if approve else "Cancelled.", {"emotion": "neutral"})

    c = _client2(_PendingRunner(), confirm_runner=_confirm)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/turns", json={"message": "email bob"}, headers=_AUTH)
    assert r.json()["confirmation"]["required"] is True
    assert r.json()["confirmation"]["description"] == "Email Bob"
    # approve it
    r2 = c.post(f"/v1/sessions/{sid}/confirm", json={"approve": True}, headers=_AUTH)
    assert r2.status_code == 200 and r2.json()["response"] == "Sent."
    assert confirms["called"] == ("send email", True)
    # nothing pending now → 409
    assert c.post(f"/v1/sessions/{sid}/confirm", json={}, headers=_AUTH).status_code == 409


def test_confirm_unavailable_without_runner():
    c = _client2(_PendingRunner(), confirm_runner=None)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "x"}, headers=_AUTH)
    assert c.post(f"/v1/sessions/{sid}/confirm", json={}, headers=_AUTH).status_code == 501


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
    assert runner.calls == [("yo", "cust-9", None)]
