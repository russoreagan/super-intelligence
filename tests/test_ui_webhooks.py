"""Console webhooks panel (API workspace → Webhooks): the org-admin routes that
mirror the owner-key /v1/webhooks family so an admin can see what is registered,
whether deliveries land, and revoke one — without minting an owner key."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain.api import webhooks  # noqa: E402
from brain.second_brain import supabase_client  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402

_HOOKS = [
    {
        "id": "wh_a",
        "partner_id": "",
        "url": "https://hooks.acme.example.com/e",
        "events": ["job"],
        "active": True,
        "disabled_reason": "",
        "consecutive_failures": 0,
        "created_ts": "2026-09-01T00:00:00+00:00",
        "secret_id": "should-never-leave",
    },
    {
        "id": "wh_b",
        "partner_id": "acme",
        "url": "https://acme.example.com/hook",
        "events": ["job"],
        "active": False,
        "disabled_reason": "repeated_delivery_failure",
        "consecutive_failures": 5,
        "created_ts": "2026-09-02T00:00:00+00:00",
    },
]
_DELIVERIES = [
    {
        "id": "dlv_1",
        "webhook_id": "wh_a",
        "event_id": "evt_1",
        "event_type": "job.completed",
        "state": "delivered",
        "attempts": 1,
        "last_status": 200,
        "last_error": "",
        "next_attempt_ts": "2026-09-03T00:00:00+00:00",
        "created_ts": "2026-09-03T00:00:00+00:00",
    },
    {
        "id": "dlv_2",
        "webhook_id": "wh_a",
        "event_id": "evt_2",
        "event_type": "job.failed",
        "state": "failed",
        "attempts": 3,
        "last_status": 503,
        "last_error": "upstream 503",
        "next_attempt_ts": "2026-09-04T00:00:00+00:00",
        "created_ts": "2026-09-04T00:00:00+00:00",
    },
]


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    """PostgREST chain double: filters by every .eq(), honours .order()/.limit()."""

    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.filters: list[tuple[str, object]] = []
        self.n: int | None = None
        self.desc = False

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self.desc = desc
        return self

    def limit(self, n):
        self.n = n
        return self

    def execute(self):
        rows = [
            dict(r)
            for r in self.db.tables[self.table_name]
            if all(r.get(c) == v for c, v in self.filters if c != "org_id")
        ]
        if self.desc:
            rows.sort(key=lambda r: r.get("created_ts", ""), reverse=True)
        if self.n is not None:
            rows = rows[: self.n]
        self.db.calls.append((self.table_name, list(self.filters), self.n))
        return _Res(rows)


class _FakeClient:
    def __init__(self, hooks, deliveries):
        self.tables = {"partner_webhooks": hooks, "webhook_deliveries": deliveries}
        self.calls: list = []
        self.rpcs: list = []

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        db = self

        class _R:
            def execute(self_inner):
                if name == "delete_partner_webhook":
                    before = len(db.tables["partner_webhooks"])
                    db.tables["partner_webhooks"] = [
                        h for h in db.tables["partner_webhooks"] if h["id"] != params["p_id"]
                    ]
                    return _Res(len(db.tables["partner_webhooks"]) < before)
                return _Res(None)

        return _R()


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    db = _FakeClient([dict(h) for h in _HOOKS], [dict(d) for d in _DELIVERIES])
    monkeypatch.setattr(webhooks, "_sb", lambda: (db, "org1"))
    from brain.ui.server import UIServer

    client = TestClient(UIServer(emitter_queue=asyncio.Queue())._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    return client, as_role, db


# ── gating ──────────────────────────────────────────────────────────────────
def test_members_see_nothing_and_cannot_act(console):
    client, as_role, db = console
    as_role("member")
    r = client.get("/webhooks")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "webhooks": []}
    assert client.post("/webhooks/wh_a/revoke").status_code == 403
    assert client.get("/webhooks/wh_a/deliveries").status_code == 403
    # Nothing touched the store on the member path.
    assert db.calls == [] and db.rpcs == []
    assert [h["id"] for h in db.tables["partner_webhooks"]] == ["wh_a", "wh_b"]


def test_list_is_off_without_the_hosted_backend(console, monkeypatch):
    client, as_role, _db = console
    as_role("admin")
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    assert client.get("/webhooks").json() == {"enabled": False, "webhooks": []}


# ── GET /webhooks ───────────────────────────────────────────────────────────
def test_admin_list_carries_last_delivery_and_never_the_secret(console):
    client, as_role, _db = console
    as_role("admin")
    r = client.get("/webhooks")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    by_id = {w["id"]: w for w in body["webhooks"]}
    assert set(by_id) == {"wh_a", "wh_b"}
    # The owner ctx sees the org-wide AND the partner-registered webhook.
    assert by_id["wh_b"]["partner_id"] == "acme"
    assert by_id["wh_b"]["active"] is False
    assert by_id["wh_b"]["disabled_reason"] == "repeated_delivery_failure"
    # Newest delivery wins, with the fields the panel renders.
    last = by_id["wh_a"]["last_delivery"]
    assert last["state"] == "failed" and last["attempts"] == 3
    assert last["last_status"] == 503 and last["last_error"] == "upstream 503"
    assert by_id["wh_b"]["last_delivery"] is None
    for w in body["webhooks"]:
        assert "secret" not in w and "secret_id" not in w
    assert "whsec_" not in r.text and "should-never-leave" not in r.text


# ── POST /webhooks/{id}/revoke ──────────────────────────────────────────────
def test_admin_revoke_deletes_the_webhook_and_its_secret_together(console):
    client, as_role, db = console
    as_role("admin")
    r = client.post("/webhooks/wh_b/revoke")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "id": "wh_b", "deleted": True}
    # The store's delete RPC removes row + Vault secret in one call, org-stamped.
    assert db.rpcs == [("delete_partner_webhook", {"p_id": "wh_b", "p_org_id": "org1"})]
    assert [h["id"] for h in db.tables["partner_webhooks"]] == ["wh_a"]
    # Gone from the list afterwards.
    assert [w["id"] for w in client.get("/webhooks").json()["webhooks"]] == ["wh_a"]


def test_revoke_of_an_unknown_id_is_404_and_touches_nothing(console):
    client, as_role, db = console
    as_role("admin")
    assert client.post("/webhooks/wh_nope/revoke").status_code == 404
    assert db.rpcs == []


# ── GET /webhooks/{id}/deliveries ───────────────────────────────────────────
def test_admin_deliveries_are_newest_first_with_a_clamped_limit(console):
    client, as_role, db = console
    as_role("admin")
    r = client.get("/webhooks/wh_a/deliveries?limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "wh_a"
    assert [d["id"] for d in body["deliveries"]] == ["dlv_2", "dlv_1"]
    assert body["deliveries"][0]["last_error"] == "upstream 503"
    # The delivery read is scoped to this webhook and honours the limit.
    table, filters, n = db.calls[-1]
    assert table == "webhook_deliveries" and ("webhook_id", "wh_a") in filters and n == 50
    # Out-of-range and garbage limits fall back to something sane rather than 500.
    assert client.get("/webhooks/wh_a/deliveries?limit=9999").status_code == 200
    assert db.calls[-1][2] == 200
    assert client.get("/webhooks/wh_a/deliveries?limit=abc").status_code == 200
    assert db.calls[-1][2] == 50


def test_deliveries_of_an_unknown_id_is_404(console):
    client, as_role, _db = console
    as_role("admin")
    assert client.get("/webhooks/wh_nope/deliveries").status_code == 404
