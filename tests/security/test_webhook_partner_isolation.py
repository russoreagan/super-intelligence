"""
Webhook registration and job routing are partner-scoped.

A webhook is a place another partner's job data could be delivered, so registration,
listing, deletion and delivery-history must never cross the partner boundary — and a
job event must reach only the initiating partner's endpoints (owner-registered hooks
excepted, which deliberately get everything).
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


class _Store:
    """In-memory partner_webhooks + delivery ledger behind the PostgREST chain the
    webhooks module uses, plus the three RPCs."""

    def __init__(self):
        self.hooks: dict[str, dict] = {}
        self.deliveries: list[dict] = []

    # RPC surface
    def rpc(self, name, params):
        self._rpc = (name, params)
        return self

    def table(self, name):
        return _Table(self, name)

    def execute(self):
        name, p = self._rpc
        if name == "set_partner_webhook":
            self.hooks[p["p_id"]] = {
                "id": p["p_id"],
                "partner_id": p["p_partner_id"],
                "url": p["p_url"],
                "events": p["p_events"],
                "active": True,
                "disabled_reason": "",
                "consecutive_failures": 0,
                "created_ts": "now",
            }
            return type("R", (), {"data": None})()
        if name == "delete_partner_webhook":
            existed = self.hooks.pop(p["p_id"], None) is not None
            return type("R", (), {"data": existed})()
        return type("R", (), {"data": None})()


class _Table:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._eq = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, row):
        self._insert = row
        return self

    def execute(self):
        if self.name == "partner_webhooks":
            rows = list(self.store.hooks.values())
            if "id" in self._eq:
                rows = [r for r in rows if r["id"] == self._eq["id"]]
            return type("R", (), {"data": rows})()
        if self.name == "webhook_deliveries":
            if hasattr(self, "_insert"):
                self.store.deliveries.append(self._insert)
                return type("R", (), {"data": [self._insert]})()
            rows = [
                d
                for d in self.store.deliveries
                if d.get("webhook_id") == self._eq.get("webhook_id")
            ]
            return type("R", (), {"data": rows})()
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
    from brain.second_brain import supabase_client

    store = _Store()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: store)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    # Let any https URL through the SSRF guard in these API-level tests.
    from brain import net_guard

    monkeypatch.setattr(net_guard, "validate_url", lambda u, **k: ["1.2.3.4"])

    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app), store


A = {"Authorization": "Bearer ka"}
B = {"Authorization": "Bearer kb"}
OWNER = {"Authorization": "Bearer ko"}


def _register(client, headers, url="https://hooks.example.com/e"):
    return client.post("/v1/webhooks", headers=headers, json={"url": url})


def test_register_returns_the_secret_once(env):
    client, _ = env
    r = _register(client, A)
    assert r.status_code == 200
    body = r.json()
    assert body["secret"].startswith("whsec_")
    # The listing never carries it back.
    listed = client.get("/v1/webhooks", headers=A).json()["webhooks"]
    assert all("secret" not in w for w in listed)


def test_a_partner_only_sees_its_own_webhooks(env):
    client, _ = env
    _register(client, A)
    _register(client, B)
    a_hooks = client.get("/v1/webhooks", headers=A).json()["webhooks"]
    assert len(a_hooks) == 1 and a_hooks[0]["partner_id"] == "A"


def test_a_partner_cannot_delete_anothers_webhook(env):
    client, store = env
    wid = _register(client, A).json()["id"]
    assert client.delete(f"/v1/webhooks/{wid}", headers=B).status_code == 404
    assert wid in store.hooks  # untouched
    assert client.delete(f"/v1/webhooks/{wid}", headers=A).status_code == 200


def test_a_partner_cannot_read_anothers_deliveries(env):
    client, _ = env
    wid = _register(client, A).json()["id"]
    assert client.get(f"/v1/webhooks/{wid}/deliveries", headers=B).status_code == 404
    assert client.get(f"/v1/webhooks/{wid}/deliveries", headers=A).status_code == 200


def test_a_non_https_url_is_refused(env, monkeypatch):
    from brain import net_guard

    def _boom(u, **k):
        raise net_guard.UnsafeUrlError("http not allowed")

    monkeypatch.setattr(net_guard, "validate_url", _boom)
    client, _ = env
    r = client.post("/v1/webhooks", headers=A, json={"url": "http://insecure.example.com"})
    assert r.status_code == 400


def test_owner_can_register_and_sees_all(env):
    client, _ = env
    _register(client, A)
    _register(client, OWNER)
    owner_view = client.get("/v1/webhooks", headers=OWNER).json()["webhooks"]
    assert len(owner_view) == 2


# ── routing: enqueue reaches the right partner ──────────────────────────────
def test_enqueue_routes_to_the_owning_partner_only(env):
    client, store = env
    from brain.api import webhooks

    _register(client, A)  # partner A hook
    _register(client, B)  # partner B hook
    # A job that belongs to partner A.
    webhooks.enqueue("job.completed", {"data": {"job_id": "j1"}}, "A")
    targets = {d["webhook_id"] for d in store.deliveries}
    a_ids = {h["id"] for h in store.hooks.values() if h["partner_id"] == "A"}
    b_ids = {h["id"] for h in store.hooks.values() if h["partner_id"] == "B"}
    assert targets & a_ids
    assert not (targets & b_ids), "partner B must not receive partner A's job"


def test_owner_registered_hook_gets_self_directed_jobs(env):
    client, store = env
    from brain.api import webhooks

    _register(client, OWNER)  # partner_id '' — org-wide
    _register(client, A)
    # A self-directed job (no partner).
    webhooks.enqueue("job.completed", {"data": {"job_id": "j2"}}, "")
    targets = {d["webhook_id"] for d in store.deliveries}
    owner_ids = {h["id"] for h in store.hooks.values() if h["partner_id"] == ""}
    a_ids = {h["id"] for h in store.hooks.values() if h["partner_id"] == "A"}
    assert targets & owner_ids
    assert not (targets & a_ids), "a partner hook must not get self-directed work"
