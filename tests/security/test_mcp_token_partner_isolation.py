"""
One partner must not reach another partner's customers' connectors.

`end_user_mcp_tokens` is keyed (org_id, end_user_id, server_name) and the three MCP
token routes scoped to the org and nothing finer, while discarding the partner
context entirely. In an org with two partners that gave partner A, for any
end_user_id it could guess or had seen:

  • enumeration — which third-party services B's customer had linked;
  • OVERWRITE — the store RPC is an upsert, so A could point server_url at an
    attacker-controlled MCP endpoint with an attacker token, which B's agent then
    builds a vault against on its next refresh. A full connector hijack;
  • deletion — silently breaking B's integration.

The registry (migration 029) is the fix. These tests assert both the status code and
that the underlying RPC never fired: a 403 that still performed the write would be
the same vulnerability with a nicer response.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router

ORG = "org-1"


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("echo", {})


class _Recorder:
    """Stands in for Supabase: records RPCs, and serves the end_users registry from a
    dict so ownership can be arranged per test."""

    def __init__(self, owners: dict[str, str | None]):
        self.owners = owners
        self.rpcs: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    # RPC surface (store / delete tokens)
    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return self

    def execute(self):
        return type("R", (), {"data": []})()

    # Table surface (end_users registry + the token list read)
    def table(self, name):
        return _Table(self, name)


class _Table:
    def __init__(self, rec: _Recorder, name: str):
        self.rec = rec
        self.name = name
        self._eq: dict = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def is_(self, col, val):
        return self

    def limit(self, *a, **k):
        return self

    def upsert(self, row, **k):
        # first-writer-wins
        self.rec.owners.setdefault(row["end_user_id"], row.get("partner_id"))
        return self

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        if self.name == "end_users":
            if getattr(self, "_delete", False):
                self.rec.deleted.append(self._eq.get("end_user_id"))
                return type("R", (), {"data": []})()
            euid = self._eq.get("end_user_id")
            if euid in self.rec.owners:
                return type("R", (), {"data": [{"partner_id": self.rec.owners[euid]}]})()
            return type("R", (), {"data": []})()
        # end_user_mcp_tokens list read
        return type("R", (), {"data": []})()


def _resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    return {
        "ka": {"partner_id": "A", "owner": False},
        "kb": {"partner_id": "B", "owner": False},
        "ko": {"partner_id": None, "owner": True},
    }.get(tok)


@pytest.fixture
def env(monkeypatch):
    """Customer u_A belongs to partner A."""
    from brain.second_brain import supabase_client

    rec = _Recorder({"u_A": "A"})
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: rec)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: ORG)

    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            purge_runner=_fake_purge,
        )
    )
    return TestClient(app), rec


async def _fake_purge(end_user_id):
    return {"ok": True, "end_user_id": end_user_id, "deleted": {}}


A = {"Authorization": "Bearer ka"}
B = {"Authorization": "Bearer kb"}
OWNER = {"Authorization": "Bearer ko"}

TOKEN_BODY = {
    "end_user_id": "u_A",
    "server_name": "gmail",
    "server_url": "https://evil.example.com",
    "access_token": "attacker",
}


def test_partner_cannot_overwrite_another_partners_connector(env):
    """The hijack. Must refuse AND must not have called the upsert RPC."""
    client, rec = env
    r = client.post("/v1/mcp/tokens", headers=B, json=TOKEN_BODY)
    assert r.status_code == 403
    assert not [n for n, _ in rec.rpcs if n == "set_end_user_mcp_token"]


def test_partner_cannot_enumerate_another_partners_connectors(env):
    client, _ = env
    r = client.get("/v1/mcp/tokens/u_A", headers=B)
    # 404, not 403 — a 403 confirms the id exists, which is the leak itself.
    assert r.status_code == 404


def test_partner_cannot_delete_another_partners_connector(env):
    client, rec = env
    r = client.delete("/v1/mcp/tokens/u_A/gmail", headers=B)
    assert r.status_code == 404
    assert not [n for n, _ in rec.rpcs if n == "delete_end_user_mcp_token"]


def test_owning_partner_still_works(env):
    """The lock must not break the legitimate case."""
    client, rec = env
    r = client.post("/v1/mcp/tokens", headers=A, json=TOKEN_BODY)
    assert r.status_code == 200
    assert [n for n, _ in rec.rpcs if n == "set_end_user_mcp_token"]


def test_owner_key_reaches_every_customer(env):
    client, _ = env
    assert client.get("/v1/mcp/tokens/u_A", headers=OWNER).status_code == 200


def test_claiming_a_fresh_customer_is_allowed(env):
    """A partner may claim an id nobody owns — that is how customers get registered."""
    client, rec = env
    body = dict(TOKEN_BODY, end_user_id="u_new")
    assert client.post("/v1/mcp/tokens", headers=B, json=body).status_code == 200
    assert rec.owners["u_new"] == "B"


# ── session ownership ───────────────────────────────────────────────────────


def test_partner_cannot_open_a_session_as_another_partners_customer(env):
    """Otherwise the hijack just moves: sessions carry that customer's memory,
    relationship and chemistry."""
    client, _ = env
    r = client.post("/v1/sessions", headers=B, json={"end_user_id": "u_A"})
    assert r.status_code == 403


def test_first_writer_wins_on_session_open(env):
    client, rec = env
    assert client.post("/v1/sessions", headers=B, json={"end_user_id": "u_B"}).status_code == 200
    assert rec.owners["u_B"] == "B"
    # A cannot now take it.
    assert client.post("/v1/sessions", headers=A, json={"end_user_id": "u_B"}).status_code == 403


# ── erasure ─────────────────────────────────────────────────────────────────


def test_partner_can_erase_its_own_customer(env):
    """GDPR: the partner is the data controller and previously had no route at all."""
    client, rec = env
    r = client.delete("/v1/end_users/u_A", headers=A)
    assert r.status_code == 200
    assert "u_A" in rec.deleted, "the ownership row must be released too"


def test_partner_cannot_erase_another_partners_customer(env):
    client, rec = env
    assert client.delete("/v1/end_users/u_A", headers=B).status_code == 404
    assert not rec.deleted
