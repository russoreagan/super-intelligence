"""Erasure tombstone (migration 035): 410 for an OWN erased id, 404 for a foreign one.

Before: DELETE /v1/end_users/{id} dropped the ownership row last, so the owning
partner's second erase — or any later read of the id — was a 404, exactly what a
foreign partner's id returns. A partner discharging its erasure obligations could
not tell "already erased" from "never yours". Now the row is stamped `erased_at`
and never deleted: the owner's later requests get 410 with the stamp, a foreign id
still gets 404 (ownership is checked before the tombstone is consulted), and the
owning partner reopening a SESSION for the id clears the stamp — a fresh start on
the same handle. The three MCP-token routes refuse an erased id with 410 too.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api import end_users as eu
from brain.api.server import build_api_router

ORG = "org-1"


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("echo", {})


class _Recorder:
    """Supabase stand-in serving the end_users registry as dict rows (with
    `erased_at`), recording updates/deletes and every RPC."""

    def __init__(self, rows: dict[str, dict]):
        self.rows = rows  # end_user_id → {"partner_id", "erased_at"}
        self.rpcs: list[tuple[str, dict]] = []
        self.updates: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return self

    def execute(self):
        return type("R", (), {"data": []})()

    def table(self, name):
        return _Table(self, name)


class _Table:
    def __init__(self, rec, name):
        self.rec, self.name, self._eq, self._op, self._payload = rec, name, {}, "select", None

    def select(self, *a, **k):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def is_(self, col, val):
        self._eq[col] = None
        return self

    def limit(self, *a, **k):
        return self

    def upsert(self, row, **k):
        self.rec.rows.setdefault(
            row["end_user_id"], {"partner_id": row.get("partner_id"), "erased_at": None}
        )
        self._op = "upsert"
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        def _r(data):
            return type("R", (), {"data": data})()

        if self.name != "end_users":
            return _r([])
        euid = self._eq.get("end_user_id")
        if self._op == "update":
            self.rec.updates.append((euid, dict(self._payload)))
            if euid in self.rec.rows:
                self.rec.rows[euid].update(self._payload)
                return _r([dict(self.rec.rows[euid])])
            return _r([])
        if self._op == "delete":
            self.rec.deleted.append(euid)
            self.rec.rows.pop(euid, None)
            return _r([])
        if euid is not None:
            row = self.rec.rows.get(euid)
            return _r([{"end_user_id": euid, **row}] if row else [])
        pid = self._eq.get("partner_id")  # list_for_partner
        return _r(
            [{"end_user_id": k, **v} for k, v in self.rec.rows.items() if v["partner_id"] == pid]
        )


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


async def _fake_purge(end_user_id):
    return {"ok": True, "end_user_id": end_user_id, "deleted": {"episodes": 1}}


@pytest.fixture
def env(monkeypatch):
    from brain.second_brain import supabase_client

    rec = _Recorder(
        {
            "u_live": {"partner_id": "A", "erased_at": None},
            "u_gone": {"partner_id": "A", "erased_at": "2026-09-01T00:00:00+00:00"},
        }
    )
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


A = {"Authorization": "Bearer ka"}
B = {"Authorization": "Bearer kb"}
OWNER = {"Authorization": "Bearer ko"}


# ── the module ───────────────────────────────────────────────────────────────


def test_forget_tombstones_instead_of_deleting(env):
    _, rec = env
    stamp = eu.forget("u_live")
    assert stamp and rec.updates and rec.updates[-1][1]["erased_at"] == stamp
    assert rec.deleted == []
    assert rec.rows["u_live"]["erased_at"] == stamp
    assert eu.erased_at("u_live") == stamp
    # Ownership outlives the data.
    assert eu.owner_of("u_live") == (True, "A")


def test_forget_falls_back_to_delete_pre_migration(env, monkeypatch):
    """Column absent → the update is refused → today's row delete, so the code
    deploys before `supabase db push` and simply lacks the 410 until then."""
    _, rec = env

    def _boom(self, payload):
        raise RuntimeError("column end_users.erased_at does not exist")

    monkeypatch.setattr(_Table, "update", _boom)
    assert eu.forget("u_live") is None
    assert rec.deleted == ["u_live"]


def test_list_for_partner_hides_tombstones(env):
    assert eu.list_for_partner("A") == ["u_live"]


def test_claim_revives_only_for_the_owner_on_session_open(env):
    _, rec = env
    # Another partner: first-writer-wins says A still owns it, and it stays erased.
    assert eu.claim("u_gone", "B", revive_if_owned=True) == "A"
    assert rec.rows["u_gone"]["erased_at"]
    # Plain claim (the MCP-token path) never revives, even for the owner.
    assert eu.claim("u_gone", "A") == "A"
    assert rec.rows["u_gone"]["erased_at"]
    # The owning partner opening a session does.
    assert eu.claim("u_gone", "A", revive_if_owned=True) == "A"
    assert rec.rows["u_gone"]["erased_at"] is None


# ── the routes ───────────────────────────────────────────────────────────────


def test_second_erase_of_own_customer_is_410_with_stamp(env):
    client, rec = env
    r = client.delete("/v1/end_users/u_live", headers=A)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["erased_at"] == rec.rows["u_live"]["erased_at"]
    r2 = client.delete("/v1/end_users/u_live", headers=A)
    assert r2.status_code == 410
    assert r2.json() == {"detail": "end_user erased", "erased_at": rec.rows["u_live"]["erased_at"]}


def test_foreign_erased_id_stays_404(env):
    client, _ = env
    assert client.delete("/v1/end_users/u_gone", headers=B).status_code == 404


def test_owner_key_sees_410_too(env):
    client, _ = env
    assert client.delete("/v1/end_users/u_gone", headers=OWNER).status_code == 410


def test_mcp_token_routes_refuse_an_erased_customer(env):
    client, rec = env
    body = {
        "end_user_id": "u_gone",
        "server_name": "gmail",
        "server_url": "https://mcp.example.com",
        "access_token": "tok",
    }
    assert client.post("/v1/mcp/tokens", headers=A, json=body).status_code == 410
    assert not [n for n, _ in rec.rpcs if n == "set_end_user_mcp_token"]
    assert client.get("/v1/mcp/tokens/u_gone", headers=A).status_code == 410
    assert client.delete("/v1/mcp/tokens/u_gone/gmail", headers=A).status_code == 410
    assert not [n for n, _ in rec.rpcs if n == "delete_end_user_mcp_token"]
    # Foreign partner: still 404/403, never 410 (no existence leak).
    assert client.get("/v1/mcp/tokens/u_gone", headers=B).status_code == 404
    assert client.post("/v1/mcp/tokens", headers=B, json=body).status_code == 403


def test_reopening_a_session_revives_and_re_erase_works(env):
    client, rec = env
    r = client.post("/v1/sessions", headers=A, json={"end_user_id": "u_gone"})
    assert r.status_code == 200
    assert rec.rows["u_gone"]["erased_at"] is None
    assert client.delete("/v1/end_users/u_gone", headers=A).status_code == 200
    assert client.delete("/v1/end_users/u_gone", headers=A).status_code == 410


def test_reopening_someone_elses_erased_customer_is_still_refused(env):
    client, rec = env
    r = client.post("/v1/sessions", headers=B, json={"end_user_id": "u_gone"})
    assert r.status_code == 403
    assert rec.rows["u_gone"]["erased_at"]  # untouched
