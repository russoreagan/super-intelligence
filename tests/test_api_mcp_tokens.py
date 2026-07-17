"""
Engine API — per-end-user MCP token routes (POST/GET/DELETE /v1/mcp/tokens).

Regression guard for the "inert in production" bug: in prod the tenant pod holds
the Supabase SERVICE-ROLE key (the project signs JWTs asymmetrically, so the
gateway can't mint an org token). A service-role JWT has no `sub`, so the RPCs'
`auth.uid()` is NULL and they used to fail closed. The fix threads the pod's own
org id (supabase_client.get_org_id()) as an explicit p_org_id, and scopes the
direct GET read by org_id in-query.

These tests use a fake Supabase client that RECORDS every rpc/table call, so they
assert the wiring (org id is threaded, GET is org-filtered) without a database.
The SQL-level behavior of the RPCs is covered by
tests/security/test_mcp_token_rpc_scoping.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router

POD_ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _RecordingClient:
    """Captures rpc() calls and table() query chains for assertions."""

    def __init__(self, *, rpc_data=None):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.table_calls: list[dict] = []
        self._rpc_data = True if rpc_data is None else rpc_data

    # rpc(name, params).execute()
    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        outer = self

        class _Exec:
            def execute(self_inner):
                return type("R", (), {"data": outer._rpc_data})()

        return _Exec()

    # table(name).select(cols).eq(k, v).eq(k, v).execute()
    def table(self, name):
        rec = {"table": name, "select": None, "eq": []}
        self.table_calls.append(rec)

        class _Q:
            def select(self_inner, cols):
                rec["select"] = cols
                return self_inner

            def eq(self_inner, k, v):
                rec["eq"].append((k, v))
                return self_inner

            def execute(self_inner):
                return type("R", (), {"data": []})()

        return _Q()


@pytest.fixture()
def client(monkeypatch):
    from brain.second_brain import supabase_client

    recorder = _RecordingClient()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: recorder)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: POD_ORG)

    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            auth=lambda h: bool(h),
            resolver=lambda h: {"partner_id": None, "owner": True},
        )
    )
    c = TestClient(app)
    c._recorder = recorder  # type: ignore[attr-defined]
    return c


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("echo", {})


AUTH = {"Authorization": "Bearer k"}


# ── POST /v1/mcp/tokens ──────────────────────────────────────────────────────


def test_post_threads_pod_org_id(client):
    r = client.post(
        "/v1/mcp/tokens",
        headers=AUTH,
        json={
            "end_user_id": "user-1",
            "server_name": "jira",
            "server_url": "https://mcp.atlassian.com",
            "access_token": "tok-abc",
        },
    )
    assert r.status_code == 200, r.text
    assert len(client._recorder.rpc_calls) == 1
    name, params = client._recorder.rpc_calls[0]
    assert name == "set_end_user_mcp_token"
    # The crux: the pod's own org is threaded so the RPC resolves under the
    # service-key fallback where auth.uid() is NULL.
    assert params["p_org_id"] == POD_ORG
    assert params["p_end_user_id"] == "user-1"
    assert params["p_server_name"] == "jira"
    assert params["p_token"] == "tok-abc"


def test_post_validates_before_touching_db(client):
    r = client.post("/v1/mcp/tokens", headers=AUTH, json={"end_user_id": "  "})
    assert r.status_code == 400
    assert client._recorder.rpc_calls == []


def test_post_fails_closed_when_org_unset(client, monkeypatch):
    from brain.second_brain import supabase_client

    def _no_org():
        raise RuntimeError("No org_id set.")

    monkeypatch.setattr(supabase_client, "get_org_id", _no_org)
    r = client.post(
        "/v1/mcp/tokens",
        headers=AUTH,
        json={
            "end_user_id": "user-1",
            "server_name": "jira",
            "server_url": "https://x",
            "access_token": "tok",
        },
    )
    # No org context → 500, never a silent org-less write.
    assert r.status_code == 500


# ── GET /v1/mcp/tokens/{end_user_id} ─────────────────────────────────────────


def test_get_is_org_scoped_in_query(client):
    r = client.get("/v1/mcp/tokens/user-1", headers=AUTH)
    assert r.status_code == 200, r.text
    assert len(client._recorder.table_calls) == 1
    rec = client._recorder.table_calls[0]
    assert rec["table"] == "end_user_mcp_tokens"
    # Both an explicit org_id filter (isolation under service-key mode) and the
    # end_user_id filter must be present.
    assert ("org_id", POD_ORG) in rec["eq"]
    assert ("end_user_id", "user-1") in rec["eq"]


# ── DELETE /v1/mcp/tokens/{end_user_id}/{server_name} ────────────────────────


def test_delete_threads_pod_org_id(client):
    r = client.delete("/v1/mcp/tokens/user-1/jira", headers=AUTH)
    assert r.status_code == 200, r.text
    name, params = client._recorder.rpc_calls[0]
    assert name == "delete_end_user_mcp_token"
    assert params["p_org_id"] == POD_ORG
    assert params["p_end_user_id"] == "user-1"
    assert params["p_server_name"] == "jira"


def test_delete_missing_returns_404(monkeypatch):
    from brain.second_brain import supabase_client

    recorder = _RecordingClient(rpc_data=[])  # RPC returned false → not found
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: recorder)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: POD_ORG)

    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            auth=lambda h: bool(h),
            resolver=lambda h: {"partner_id": None, "owner": True},
        )
    )
    c = TestClient(app)
    r = c.delete("/v1/mcp/tokens/user-1/jira", headers=AUTH)
    assert r.status_code == 404
