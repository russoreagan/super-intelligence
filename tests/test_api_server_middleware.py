"""
App-level middleware on the engine's own FastAPI app.

`ApiServer` is the only place with app-level configuration, and nothing drove it
end-to-end — the whole suite builds routers directly, so anything registered on the
app itself was effectively untested. These cover the two things that live there: the
request-id stamp and the body cap backstop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brain.api.server import ApiServer


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("ok", {"emotion": "warm"})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BRAIN_API_KEYS", "sk_test")
    server = ApiServer(_FakeRunner())
    return TestClient(server.app if hasattr(server, "app") else server._app)


AUTH = {"Authorization": "Bearer sk_test"}


def test_every_response_carries_a_request_id(client):
    r = client.get("/v1/capabilities", headers=AUTH)
    assert r.headers.get("X-Request-Id")


def test_error_responses_carry_one_too(client):
    """The case that matters — a support request is about a failure."""
    r = client.get("/v1/capabilities")  # 401
    assert r.status_code == 401
    assert r.headers.get("X-Request-Id")


def test_a_caller_supplied_id_is_echoed(client):
    r = client.get("/v1/capabilities", headers={**AUTH, "X-Request-Id": "trace-abc-123"})
    assert r.headers["X-Request-Id"] == "trace-abc-123"


@pytest.mark.parametrize(
    "hostile",
    [
        "x" * 200,  # unbounded length in every log line
        "bad\x7fid",  # DEL — ASCII, so it survives header encoding
        "\x01\x02",  # control characters
    ],
)
def test_a_hostile_id_is_replaced_not_propagated(client, hostile):
    """Otherwise a caller writes arbitrary text into our logs. Note the transport
    already blocks non-ASCII (so bidi overrides cannot arrive here at all) — this
    covers what can."""
    r = client.get("/v1/capabilities", headers={**AUTH, "X-Request-Id": hostile})
    assert r.headers["X-Request-Id"] != hostile
    assert r.headers["X-Request-Id"]


def test_oversized_body_is_refused(client, monkeypatch):
    from brain.api import limits

    monkeypatch.setattr(limits, "MAX_BODY_BYTES", 100)
    r = client.post("/v1/sessions", headers=AUTH, content=b"x" * 5000)
    assert r.status_code == 413
