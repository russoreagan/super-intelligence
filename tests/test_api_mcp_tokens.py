"""
GET /v1/mcp/tokens/{end_user_id} — cross-tenant isolation.

Regression: the endpoint selected from end_user_mcp_tokens filtered ONLY on
end_user_id. That column is partner-chosen free text and not globally unique (the
PK is (org_id, end_user_id, server_name)), so a guessable id — an email, "user_1"
— returned every org's rows: which third-party services their end-users had
connected, the server URLs, and expiry.

RLS had been hiding the mistake. It no longer does: the live project signs JWTs
asymmetrically, so the gateway can't mint an org token and the provisioner falls
back to the service-role key, which bypasses RLS (brain/gateway/org_token.py,
brain/provisioner.py). The fake below models exactly that mode — it applies the
filters the code writes and nothing else — so these tests fail if the in-query org
filter is ever dropped again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry

_THIS_ORG = "11111111-1111-4111-8111-111111111111"
_OTHER_ORG = "22222222-2222-4222-8222-222222222222"

# The same end_user_id in two orgs — legitimate under the composite PK, and the
# whole reason the missing filter leaked.
_SHARED_END_USER = "user_1"

_ROWS = [
    {
        "org_id": _THIS_ORG,
        "end_user_id": _SHARED_END_USER,
        "server_name": "jira",
        "server_url": "https://jira.ours.example",
        "expires_at": None,
        "secret_id": "aaaa-ours",
    },
    {
        "org_id": _OTHER_ORG,
        "end_user_id": _SHARED_END_USER,
        "server_name": "salesforce",
        "server_url": "https://sf.rival.example",
        "expires_at": None,
        "secret_id": "bbbb-theirs",
    },
    {
        "org_id": _OTHER_ORG,
        "end_user_id": _SHARED_END_USER,
        "server_name": "gdrive",
        "server_url": "https://drive.rival.example",
        "expires_at": None,
        "secret_id": "cccc-theirs",
    },
]


class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._filters: dict[str, object] = {}
        self._cols: list[str] | None = None

    def select(self, cols: str):
        self._cols = [c.strip() for c in cols.split(",")]
        return self

    def eq(self, col: str, val):
        self._filters[col] = val
        return self

    def execute(self):
        out = [r for r in self._rows if all(r.get(k) == v for k, v in self._filters.items())]
        if self._cols is not None:
            out = [{k: r.get(k) for k in self._cols} for r in out]
        return SimpleNamespace(data=out)


class _FakeSupabase:
    """PostgREST under the SERVICE-ROLE key: honours the query's own filters and
    enforces no row-level security of its own. This is production today."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, name: str):
        assert name == "end_user_mcp_tokens"
        return _FakeQuery(self._rows)


@pytest.fixture
def client(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _FakeSupabase(_ROWS))
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: _THIS_ORG)

    async def _runner(message, end_user_id, mandate_id=None, persona=None):
        return ("ok", {})

    app = FastAPI()
    app.include_router(
        build_api_router(_runner, ApiSessionRegistry(), auth=lambda h: bool(h)),
    )
    return TestClient(app)


_AUTH = {"Authorization": "Bearer sk_test_123"}


def test_returns_only_this_orgs_connections(client):
    r = client.get(f"/v1/mcp/tokens/{_SHARED_END_USER}", headers=_AUTH)
    assert r.status_code == 200
    names = [c["server_name"] for c in r.json()["connections"]]
    assert names == ["jira"]


def test_never_leaks_another_orgs_metadata(client):
    """The leak was metadata, not tokens — assert on the metadata itself."""
    body = client.get(f"/v1/mcp/tokens/{_SHARED_END_USER}", headers=_AUTH).text
    assert "rival.example" not in body, "another org's server URL leaked"
    assert "salesforce" not in body
    assert "gdrive" not in body


def test_never_returns_the_vault_secret_id(client):
    body = client.get(f"/v1/mcp/tokens/{_SHARED_END_USER}", headers=_AUTH).text
    assert "secret_id" not in body
    assert "aaaa-ours" not in body


def test_unknown_end_user_returns_empty_not_other_orgs(client):
    r = client.get("/v1/mcp/tokens/nobody", headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["connections"] == []


def test_requires_auth(client):
    assert client.get(f"/v1/mcp/tokens/{_SHARED_END_USER}").status_code == 401
