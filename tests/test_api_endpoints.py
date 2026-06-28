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

    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        self.calls.append((message, end_user_id, mandate_id))
        return (
            f"echo: {message}",
            {
                "emotion": "warm",
                "user_emotion": "curious",
                "hormonal": {"OXT": 0.3},
                "appraisal": "SECRET",
            },
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
    tok = (
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    )
    return tok in keys


_AUTH = {"Authorization": "Bearer sk_test_123"}


def test_requires_api_key():
    c = _client(_FakeRunner())
    assert c.post("/v1/sessions", json={"end_user_id": "cust-1"}).status_code == 401
    assert (
        c.post(
            "/v1/sessions",
            json={"end_user_id": "cust-1"},
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


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
    # Chemistry is deliberately withheld from partners — only the mood OUTPUT crosses
    # the boundary (see commit 2fed0fa and brain.api._affect.mood_from_affect), so the
    # affect model can't be reverse-engineered. The fake supplies hormonal + appraisal
    # in the source affect precisely to prove they're stripped here.
    assert "hormonal" not in mood
    assert "neuromod" not in mood
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
    assert (
        c.post(f"/v1/sessions/{sid}/turns", json={"message": "  "}, headers=_AUTH).status_code
        == 400
    )


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
    monkeypatch.setattr(
        mandates, "list_mandates", lambda include_inactive=False: [{"id": k} for k in store]
    )
    monkeypatch.setattr(
        mandates,
        "assign",
        lambda persona, mid, sort_order=0: {"persona": persona, "mandate_id": mid},
    )

    c = _client(_FakeRunner())
    # missing role_text → 400
    assert c.put("/v1/mandates/billing", json={}, headers=_AUTH).status_code == 400
    # valid create → version 1, re-PUT → version 2
    assert (
        c.put("/v1/mandates/billing", json={"role_text": "a"}, headers=_AUTH).json()["version"] == 1
    )
    assert (
        c.put("/v1/mandates/billing", json={"role_text": "b"}, headers=_AUTH).json()["version"] == 2
    )
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
    r = c.post(
        "/v1/sessions", json={"end_user_id": "c1", "agent_id": "the_analyst.billing"}, headers=_AUTH
    )
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
    r = c.post(
        "/v1/sessions", json={"end_user_id": "c1", "agent_id": "other.billing"}, headers=_AUTH
    )
    assert r.status_code == 409


def test_agent_id_unknown_404(monkeypatch):
    from brain import agents

    def _boom(aid):
        raise agents.AgentNotFound("nope")

    monkeypatch.setattr(agents, "resolve", _boom)
    c = _client(_FakeRunner())
    r = c.post(
        "/v1/sessions", json={"end_user_id": "c1", "agent_id": "the_analyst.ghost"}, headers=_AUTH
    )
    assert r.status_code == 404


def test_agents_list_and_ceilings(monkeypatch):
    from brain import agents
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(
        agents,
        "list_agents",
        lambda: [
            {
                "agent_id": "the_analyst.billing",
                "persona": "the_analyst",
                "mandate_id": "billing",
                "permissions": {},
            }
        ],
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
    monkeypatch.setattr(
        mandates, "assign", lambda p, m, *a, **k: calls.setdefault("assign", (p, m))
    )
    monkeypatch.setattr(agents, "set_name", lambda aid, n: calls.setdefault("name", (aid, n)))
    monkeypatch.setattr(
        agents, "set_permissions", lambda aid, perms: calls.setdefault("perms", (aid, perms))
    )
    monkeypatch.setattr(
        agents,
        "get",
        lambda aid: {
            "agent_id": aid,
            "name": "Billing",
            "permissions": {"cloud_daily_usd_budget": 20000},
        },
    )

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

    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return (
            "I need your OK to send that email.",
            {
                "emotion": "attentive",
                "pending": {"task": "send email", "description": "Email Bob", "is_write": True},
            },
        )


def _client2(turn_runner, confirm_runner=None, keys=None):
    keys = keys or {"sk_test_123"}
    registry = ApiSessionRegistry(now_fn=lambda: 1.0, id_fn=lambda: "sess_p")
    app = FastAPI()
    app.include_router(
        build_api_router(
            turn_runner, registry, auth=lambda h: _ok(h, keys), confirm_runner=confirm_runner
        )
    )
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


def _client_approvals(list_runner=None, resolve_runner=None, keys=None):
    keys = keys or {"sk_test_123"}
    registry = ApiSessionRegistry(now_fn=lambda: 1.0, id_fn=lambda: "sess_ap")
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            registry,
            auth=lambda h: _ok(h, keys),
            approvals_list_runner=list_runner,
            approval_resolve_runner=resolve_runner,
        )
    )
    return TestClient(app)


def test_approvals_list_and_resolve_route_to_runner():
    seen = {}

    def _list(end_user_id, include_autonomous):
        seen["list"] = (end_user_id, include_autonomous)
        return [{"id": "ap1", "tool": "send_email", "reason": "would send communication"}]

    def _resolve(approval_id, end_user_id, approve, include_autonomous):
        seen["resolve"] = (approval_id, end_user_id, approve, include_autonomous)
        return {"ok": True, "tool": "send_email"}

    c = _client_approvals(list_runner=_list, resolve_runner=_resolve)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.get(f"/v1/sessions/{sid}/approvals", headers=_AUTH)
    assert r.status_code == 200 and r.json()["approvals"][0]["id"] == "ap1"
    # Scoped to the session's end-user; owner-key caller also gets the autonomous lane.
    assert seen["list"] == ("c1", True)
    r2 = c.post(
        f"/v1/sessions/{sid}/approvals/ap1/resolve", json={"approve": True}, headers=_AUTH
    )
    assert r2.status_code == 200 and r2.json()["approved"] is True and r2.json()["ok"] is True
    assert seen["resolve"] == ("ap1", "c1", True, True)  # end_user enforced; owner sees autonomous


def test_approvals_require_auth_and_runner():
    c = _client_approvals(resolve_runner=None)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    assert c.get(f"/v1/sessions/{sid}/approvals").status_code == 401  # no key
    # resolve with no runner wired → 501
    assert (
        c.post(f"/v1/sessions/{sid}/approvals/ap1/resolve", json={}, headers=_AUTH).status_code
        == 501
    )


def test_end_user_purge_routes_to_runner():
    purged = {}

    async def _purge(end_user_id):
        purged["id"] = end_user_id
        return {"ok": True, "end_user_id": end_user_id, "deleted": {"episodes": 3}}

    keys = {"sk_test_123"}
    registry = ApiSessionRegistry(id_fn=lambda: "s1")
    app = FastAPI()
    app.include_router(
        build_api_router(_FakeRunner(), registry, auth=lambda h: _ok(h, keys), purge_runner=_purge)
    )
    c = TestClient(app)
    # an open session for this end_user should be evicted from the registry
    registry.create("cust-9")
    r = c.delete("/v1/end_users/cust-9", headers=_AUTH)
    assert r.status_code == 200 and r.json()["deleted"]["episodes"] == 3
    assert purged["id"] == "cust-9"
    assert registry.get("s1") is None  # forgotten


def test_end_user_purge_unavailable_without_runner():
    c = _client(_FakeRunner())
    assert c.delete("/v1/end_users/x", headers=_AUTH).status_code == 501


def test_end_user_purge_requires_auth():
    async def _purge(e):
        return {}

    keys = {"sk_test_123"}
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(), ApiSessionRegistry(), auth=lambda h: _ok(h, keys), purge_runner=_purge
        )
    )
    assert TestClient(app).delete("/v1/end_users/x").status_code == 401


def _partner_resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else authorization
    )
    return {
        "ka": {"partner_id": "A", "owner": False},
        "kb": {"partner_id": "B", "owner": False},
        "ko": {"partner_id": None, "owner": True},
    }.get(tok)


def _scoped_client(runner=None):
    registry = ApiSessionRegistry(id_fn=lambda: "sx")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner or _FakeRunner(),
            registry,
            auth=lambda h: _partner_resolver(h) is not None,
            resolver=_partner_resolver,
        )
    )
    return TestClient(app)


def test_partner_can_only_drive_own_sessions():
    c = _scoped_client()
    A = {"Authorization": "Bearer ka"}
    B = {"Authorization": "Bearer kb"}
    OWNER = {"Authorization": "Bearer ko"}
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=A).json()["session_id"]
    assert c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=A).status_code == 200
    assert c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=B).status_code == 403
    assert (
        c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=OWNER).status_code
        == 200
    )  # owner sees all


def test_partner_keys_owner_only(monkeypatch):
    from brain.api import auth as _a
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(
        _a,
        "mint_partner_key",
        lambda pid, label=None: {"id": "k1", "partner_id": pid, "token": "sk_secret"},
    )
    c = _scoped_client()
    # a partner key cannot mint
    assert (
        c.post(
            "/v1/partner_keys", json={"partner_id": "X"}, headers={"Authorization": "Bearer ka"}
        ).status_code
        == 403
    )
    # the owner can, and gets the plaintext token once
    r = c.post("/v1/partner_keys", json={"partner_id": "X"}, headers={"Authorization": "Bearer ko"})
    assert r.status_code == 200 and r.json()["token"] == "sk_secret"


def test_turn_stream_emits_inner_life_then_done():
    import asyncio

    class _Source:
        def __init__(self):
            self.taps = set()

        def add_tap(self, q):
            self.taps.add(q)

        def remove_tap(self, q):
            self.taps.discard(q)

        def push(self, ev):
            # Mirror the real ActivationEmitter: stamp the routing lane (the route
            # binds it around the turn) so the stream's per-session filter sees it.
            from brain.ui.emitter import ActivationEmitter

            ActivationEmitter._stamp_lane(ev)
            for q in list(self.taps):
                q.put_nowait(ev)

    source = _Source()

    async def runner(message, end_user_id, mandate_id=None, persona=None):
        for ev in (
            {"type": "turn_start", "turn_id": "t1", "user_input": message},
            {"type": "stream_thought", "thought": "thinking it over"},
            {"type": "emotion", "emotion": "warm"},
            {"type": "turn_end", "turn_id": "t1", "response": "hello there"},
        ):
            source.push(ev)
            await asyncio.sleep(0)
        return ("hello there", {"emotion": "warm"})

    keys = {"sk_test_123"}
    registry = ApiSessionRegistry(id_fn=lambda: "ss")
    app = FastAPI()
    app.include_router(
        build_api_router(runner, registry, auth=lambda h: _ok(h, keys), event_source=source)
    )
    c = TestClient(app)
    registry.create("c1")
    with c.stream(
        "POST", "/v1/sessions/ss/turns/stream", json={"message": "hi"}, headers=_AUTH
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = "".join(r.iter_text())
    assert "event: stream_thought" in body
    assert "event: emotion" in body
    assert "event: done" in body
    assert "hello there" in body
    assert not source.taps  # tap removed after the stream closes


def test_turn_stream_unavailable_without_source():
    # No event_source and no emitter import path resolvable in the router → 501.
    c = _client(_FakeRunner())
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    # The real emitter singleton may import; accept either streaming (200) or 501,
    # but a missing-source build must not 500.
    with c.stream(
        "POST", f"/v1/sessions/{sid}/turns/stream", json={"message": "hi"}, headers=_AUTH
    ) as r:
        assert r.status_code in (200, 501)


class _FakeConsolidator:
    """Records reasons it was asked to consolidate with; returns a scripted status."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result or {"ran": True, "turns": 3}

    async def __call__(self, reason):
        self.calls.append(reason)
        return self._result


def _client_with_consolidate(runner, consolidator, *, keys=None):
    keys = keys or {"sk_test_123"}
    registry = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner, registry, auth=lambda h: _ok(h, keys), consolidate_runner=consolidator
        )
    )
    return TestClient(app)


def test_consolidate_runs_for_session_and_passes_reason():
    cons = _FakeConsolidator()
    c = _client_with_consolidate(_FakeRunner(), cons)
    sid = c.post("/v1/sessions", json={"end_user_id": "cust-1"}, headers=_AUTH).json()["session_id"]

    r = c.post(f"/v1/sessions/{sid}/consolidate", json={"reason": "debate_end"}, headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["consolidation"] == {"ran": True, "turns": 3}
    assert cons.calls == ["debate_end"]


def test_consolidate_defaults_reason_and_allows_empty_body():
    cons = _FakeConsolidator()
    c = _client_with_consolidate(_FakeRunner(), cons)
    sid = c.post("/v1/sessions", json={"end_user_id": "c"}, headers=_AUTH).json()["session_id"]

    r = c.post(f"/v1/sessions/{sid}/consolidate", headers=_AUTH)
    assert r.status_code == 200
    assert cons.calls == ["api"]


def test_consolidate_requires_auth_and_known_session():
    cons = _FakeConsolidator()
    c = _client_with_consolidate(_FakeRunner(), cons)
    sid = c.post("/v1/sessions", json={"end_user_id": "c"}, headers=_AUTH).json()["session_id"]

    assert c.post(f"/v1/sessions/{sid}/consolidate").status_code == 401
    assert c.post("/v1/sessions/nope/consolidate", headers=_AUTH).status_code == 404
    assert cons.calls == []


def test_consolidate_501_when_runner_not_wired():
    # No consolidate_runner passed → endpoint reports unavailable, never 500.
    c = _client(_FakeRunner())
    sid = c.post("/v1/sessions", json={"end_user_id": "c"}, headers=_AUTH).json()["session_id"]
    assert c.post(f"/v1/sessions/{sid}/consolidate", headers=_AUTH).status_code == 501


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


# ── POST /v1/extract — sessionless structured extraction ──────────────────────────


class _FakeExtractRunner:
    """Records (input, schema, instructions, name); returns a scripted dict or raises."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = {"has_signal": False} if result is None else result
        self._raises = raises

    async def __call__(self, input_text, schema, instructions, name):
        self.calls.append((input_text, schema, instructions, name))
        if self._raises is not None:
            raise self._raises
        return self._result


def _extract_client(extract_runner, *, keys=None, handler=False):
    keys = keys or {"sk_test_123"}
    app = FastAPI()
    if handler:
        from fastapi.responses import JSONResponse

        from brain.model_router import CloudBudgetExceeded

        async def _h(_req, exc):  # noqa: ANN001
            return JSONResponse(status_code=402, content={"detail": str(exc)})

        app.add_exception_handler(CloudBudgetExceeded, _h)
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "s"),
            auth=lambda h: _ok(h, keys),
            extract_runner=extract_runner,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_extract_requires_api_key():
    c = _extract_client(_FakeExtractRunner())
    r = c.post("/v1/extract", json={"input": "x", "schema": {"type": "object"}})
    assert r.status_code == 401


def test_extract_returns_structured_data_and_forwards_args():
    runner = _FakeExtractRunner(result={"has_signal": True, "symbol": "NVDA"})
    c = _extract_client(runner)
    r = c.post(
        "/v1/extract",
        json={
            "input": "NVDA looks cheap",
            "schema": {"type": "object"},
            "instructions": "pull a signal",
            "name": "signal",
        },
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"has_signal": True, "symbol": "NVDA"}
    assert runner.calls == [("NVDA looks cheap", {"type": "object"}, "pull a signal", "signal")]


def test_extract_validates_input_and_schema():
    c = _extract_client(_FakeExtractRunner())
    obj = {"type": "object"}
    assert c.post("/v1/extract", json={"schema": obj}, headers=_AUTH).status_code == 400
    assert c.post("/v1/extract", json={"input": "x"}, headers=_AUTH).status_code == 400
    assert c.post("/v1/extract", json={"input": "  ", "schema": obj}, headers=_AUTH).status_code == 400


def test_extract_501_when_unwired():
    c = _extract_client(None)
    r = c.post("/v1/extract", json={"input": "x", "schema": {"type": "object"}}, headers=_AUTH)
    assert r.status_code == 501


def test_extract_budget_exceeded_maps_to_402():
    from brain.model_router import CloudBudgetExceeded

    runner = _FakeExtractRunner(raises=CloudBudgetExceeded("over daily budget"))
    c = _extract_client(runner, handler=True)
    r = c.post("/v1/extract", json={"input": "x", "schema": {"type": "object"}}, headers=_AUTH)
    assert r.status_code == 402
    assert "budget" in r.json()["detail"]


def test_apiserver_registers_budget_handler():
    from brain.api import ApiServer
    from brain.model_router import CloudBudgetExceeded

    s = ApiServer(_FakeRunner())
    assert CloudBudgetExceeded in s.app.exception_handlers
