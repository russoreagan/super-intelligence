"""
The bearer gate must never turn a database blip into an owner.

`_require` used to run auth TWICE — `check_bearer` and then the resolver, each its
own Supabase query — and mapped a `None` second result to `{"owner": True}`. Since
the partner lookup swallowed every exception, a timeout or connection reset between
the two queries promoted an ordinary partner key to full org owner: key minting,
GDPR purge, DMN control, the autonomous approval lane.

It failed open precisely when the infrastructure was already unhealthy, which is
when nobody is reading logs. These tests pin the three properties that close it:
a backend error is a 503 and never a context, a resolver that returns None is a 401,
and the resolver runs exactly once per request.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.auth import AuthBackendError
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("ok", {"emotion": "warm"})


def _client(resolver, *, auth=None):
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=auth or (lambda h: bool(h)),
            resolver=resolver,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


_PARTNER = {"partner_id": "A", "owner": False}
_HDR = {"Authorization": "Bearer ka"}


def test_resolver_error_is_503_not_owner():
    """The regression. A backend failure must not authenticate anybody."""

    def _boom(_authorization):
        raise AuthBackendError("connection reset")

    r = _client(_boom).post("/v1/sessions", headers=_HDR, json={"end_user_id": "u"})
    assert r.status_code == 503
    assert "auth backend" in r.json()["detail"]


def test_resolver_error_does_not_reach_owner_gated_routes():
    """The same blip against the most dangerous route in the API."""

    def _boom(_authorization):
        raise AuthBackendError("timeout")

    r = _client(_boom).post("/v1/partner_keys", headers=_HDR, json={"partner_id": "evil"})
    assert r.status_code == 503


def test_no_owner_fallback_when_resolver_injected():
    """auth() passing while the resolver finds nothing is a 401, not an owner."""
    c = _client(lambda _h: None, auth=lambda _h: True)
    r = c.post("/v1/sessions", headers=_HDR, json={"end_user_id": "u"})
    assert r.status_code == 401


def test_partner_context_is_preserved():
    """The fix must not quietly widen or narrow a legitimate partner."""
    c = _client(lambda _h: dict(_PARTNER))
    r = c.post("/v1/sessions", headers=_HDR, json={"end_user_id": "u"})
    assert r.status_code == 200
    # An owner-gated route stays closed to this key.
    assert c.get("/v1/dmn", headers=_HDR).status_code == 403


def test_resolver_runs_exactly_once_per_request():
    """Two lookups per request was both the escalation window and 2x the query load
    on a path with no rate limiting."""
    calls = {"resolver": 0, "auth": 0}

    def _resolver(_h):
        calls["resolver"] += 1
        return dict(_PARTNER)

    def _auth(_h):
        calls["auth"] += 1
        return True

    c = _client(_resolver, auth=_auth)
    assert c.post("/v1/sessions", headers=_HDR, json={"end_user_id": "u"}).status_code == 200
    assert calls["resolver"] == 1
    # `auth` is redundant once the resolver is the gate; it must not be a second query.
    assert calls["auth"] == 0


@pytest.mark.parametrize(
    "header",
    [
        "sk_bare_token_no_scheme",
        "Basic sk_bare_token_no_scheme",
        "Token sk_bare_token_no_scheme",
    ],
)
def test_only_bearer_is_a_credential(header):
    """Accepting any header value as the token widened what counts as a
    credential-bearing request and taught clients to put secrets in odd headers."""
    from brain.api.auth import _extract_token

    assert _extract_token(header) is None


def test_bearer_is_case_insensitive_and_trimmed():
    from brain.api.auth import _extract_token

    assert _extract_token("bearer  sk_x  ") == "sk_x"
    assert _extract_token("BEARER sk_x") == "sk_x"
    assert _extract_token("Bearer ") is None
    assert _extract_token(None) is None
