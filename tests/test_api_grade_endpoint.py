"""Engine API — POST /v1/sessions/{id}/turns/{turn_id}/grade.

The partner-facing entry point for the external-verdict reward channel. Tested via
FastAPI TestClient against the router with FAKE runners (no brain needed): the turn
response now surfaces turn_id (the handle to grade), the grade route is auth-gated
and ownership-scoped, and it forwards (turn_id, grade, end_user_id, persona, source)
to the brain's grade_runner.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        # affect now carries turn_id (process_turn stamps it); the route surfaces it.
        return (f"echo: {message}", {"emotion": "warm", "user_emotion": "curious", "turn_id": "turn_xyz"})


class _FakeGrader:
    """Records the args the route forwards and returns a scripted result."""

    def __init__(self):
        self.calls = []

    def __call__(self, turn_id, grade, end_user_id, persona, source):
        self.calls.append((turn_id, grade, end_user_id, persona, source))
        return {"ok": True, "grade": 1.0 if grade and grade > 0 else -1.0, "applied_live": True}


def _ok(authorization, keys):
    if not authorization:
        return False
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    return tok in keys


def _client(runner, grader=None, *, keys=None, resolver=None):
    keys = keys or {"sk_test_123"}
    registry = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            registry,
            auth=lambda h: _ok(h, keys),
            grade_runner=grader,
            resolver=resolver,
        )
    )
    c = TestClient(app)
    c._registry = registry  # let tests reach the live session objects
    return c


_AUTH = {"Authorization": "Bearer sk_test_123"}


def _open_session(c, end_user_id="cust-1", agent_id=None):
    body = {"end_user_id": end_user_id}
    if agent_id:
        body["agent_id"] = agent_id
    r = c.post("/v1/sessions", json=body, headers=_AUTH)
    assert r.status_code == 200
    return r.json()["session_id"]


# ── turn_id is now surfaced to the partner ───────────────────────────────────
def test_turn_response_surfaces_turn_id():
    c = _client(_FakeRunner())
    sid = _open_session(c)
    r = c.post(f"/v1/sessions/{sid}/turns", json={"message": "hello"}, headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["turn_id"] == "turn_xyz"  # the handle to grade this turn


# ── the grade route ──────────────────────────────────────────────────────────
def test_grade_forwards_to_runner_with_session_context():
    grader = _FakeGrader()
    c = _client(_FakeRunner(), grader)
    sid = _open_session(c, end_user_id="cust-42")

    r = c.post(
        f"/v1/sessions/{sid}/turns/turn_xyz/grade",
        json={"grade": 1, "source": "partner_thumbs"},
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # The route resolves end_user_id (and persona) from the session and forwards them,
    # so the brain can bind the right customer's chemistry.
    assert grader.calls == [("turn_xyz", 1, "cust-42", "", "partner_thumbs")]


def test_grade_forwards_persona_from_agent_id():
    grader = _FakeGrader()
    c = _client(_FakeRunner(), grader)
    sid = _open_session(c)
    # Simulate a Path B session whose agent names a persona, without needing a real
    # agent in the catalog: set agent_id directly on the registered session.
    # 'the_visionary.trading_bull' → persona 'the_visionary' via _session_persona.
    c._registry.get(sid).agent_id = "the_visionary.trading_bull"
    c.post(f"/v1/sessions/{sid}/turns/t1/grade", json={"grade": -1}, headers=_AUTH)
    assert grader.calls[0][3] == "the_visionary"  # persona forwarded
    assert grader.calls[0][4] == "api"  # default source


def test_grade_requires_api_key():
    c = _client(_FakeRunner(), _FakeGrader())
    sid = _open_session(c)
    r = c.post(f"/v1/sessions/{sid}/turns/t1/grade", json={"grade": 1})
    assert r.status_code == 401


def test_grade_unknown_session_404():
    c = _client(_FakeRunner(), _FakeGrader())
    r = c.post("/v1/sessions/nope/turns/t1/grade", json={"grade": 1}, headers=_AUTH)
    assert r.status_code == 404


def test_grade_missing_grade_400():
    c = _client(_FakeRunner(), _FakeGrader())
    sid = _open_session(c)
    r = c.post(f"/v1/sessions/{sid}/turns/t1/grade", json={}, headers=_AUTH)
    assert r.status_code == 400


def test_grade_not_wired_returns_501():
    c = _client(_FakeRunner(), grader=None)  # no grade_runner injected
    sid = _open_session(c)
    r = c.post(f"/v1/sessions/{sid}/turns/t1/grade", json={"grade": 1}, headers=_AUTH)
    assert r.status_code == 501


def test_grade_other_partners_session_403():
    """A partner can only grade turns in its own sessions."""
    grader = _FakeGrader()
    # Two partners share the process; resolver maps each key to a partner_id.
    resolver = lambda h: {  # noqa: E731
        "partner_id": "p-owner" if "owner" in (h or "") else "p-other",
        "owner": False,
    }
    keys = {"sk_owner", "sk_other"}
    c = _client(_FakeRunner(), grader, keys=keys, resolver=resolver)

    sid = c.post(
        "/v1/sessions",
        json={"end_user_id": "cust-1"},
        headers={"Authorization": "Bearer sk_owner"},
    ).json()["session_id"]

    # The other partner cannot grade this session.
    r = c.post(
        f"/v1/sessions/{sid}/turns/t1/grade",
        json={"grade": 1},
        headers={"Authorization": "Bearer sk_other"},
    )
    assert r.status_code == 403
    assert grader.calls == []  # never reached the brain
