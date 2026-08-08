"""
Org configuration is partner-READABLE and owner-WRITABLE.

Mandates, agents and personas are org-level objects with no per-partner ownership
column, and every write route used to accept any valid partner key. In a
multi-partner org that meant partner A could rewrite the mandate charter text and
persona disposition that partner B's live sessions run on (both reach the prompt),
retune B's agents to `tier: lite`, or simply delete them.

The fix is a write lock, not a curtain: partners still need to read the roster to
resolve an agent_id, so GETs stay open.

Table-driven on purpose. Per-route hand-written tests rot as routes are added; this
fails the moment a new write route forgets the gate.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("ok", {"emotion": "warm"})


def _resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    return {
        "kp": {"partner_id": "A", "owner": False},
        "ko": {"partner_id": None, "owner": True},
    }.get(tok)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


PARTNER = {"Authorization": "Bearer kp"}
OWNER = {"Authorization": "Bearer ko"}

# (method, concrete path, body) for every org-config WRITE.
WRITES = [
    ("PUT", "/v1/mandates/research_lead", {"role_text": "pwned"}),
    ("DELETE", "/v1/mandates/research_lead", None),
    ("PUT", "/v1/personas/the_visionary/mandates/research_lead", {}),
    ("DELETE", "/v1/personas/the_visionary/mandates/research_lead", None),
    ("PUT", "/v1/agents/the_visionary.research_lead", {"tier": "lite"}),
    ("DELETE", "/v1/agents/the_visionary.research_lead", None),
    ("PUT", "/v1/personas/captain_ahab", {"display_name": "pwned"}),
    ("DELETE", "/v1/personas/captain_ahab", None),
]

READS = [
    "/v1/mandates",
    "/v1/agents",
    "/v1/personas",
]


@pytest.mark.parametrize("method,path,body", WRITES, ids=lambda v: v if isinstance(v, str) else "")
def test_partner_cannot_write_org_config(client, method, path, body):
    r = client.request(method, path, headers=PARTNER, json=body)
    assert r.status_code == 403, f"{method} {path} was writable by a partner key"
    assert r.json()["detail"] == "owner credential required"


@pytest.mark.parametrize("method,path,body", WRITES, ids=lambda v: v if isinstance(v, str) else "")
def test_owner_is_not_blocked_by_the_gate(client, method, path, body):
    """The owner may fail for other reasons (no Supabase in this test), but must
    never be refused by the authorisation gate itself."""
    r = client.request(method, path, headers=OWNER, json=body)
    assert r.status_code != 403


@pytest.mark.parametrize("path", READS)
def test_partner_can_still_read_org_config(client, path):
    assert client.get(path, headers=PARTNER).status_code != 403


def test_unauthenticated_is_401_not_403(client):
    r = client.put("/v1/mandates/x", headers={"Authorization": "Bearer nope"}, json={})
    assert r.status_code == 401
