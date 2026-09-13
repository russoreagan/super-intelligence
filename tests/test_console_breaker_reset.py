"""Console routes added by the 2026-09-13 audit: the provider-breaker reset, the
seed-only learning-mode change the new select posts, and the persona catalogue
page the workspace now pages through."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import brain.model_router as mr  # noqa: E402
from brain import learning_mode, org_settings  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402


class _Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


def _mk_router():
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._provider_outage = {}
    return r


def test_router_reset_clears_hold_and_strikes():
    r = _mk_router()
    assert r.note_provider_error("anthropic", _Err("credit balance is too low", 400)) == "billing"
    assert r.provider_blocked("anthropic")
    assert r.reset_provider_breaker("anthropic") is True
    assert r.provider_blocked("anthropic") is None and r.provider_outages() == {}
    assert r.reset_provider_breaker("anthropic") is False
    # After a manual reset the next failure is strike 1 again (base hold), not 2.
    r.note_provider_error("anthropic", _Err("credit balance is too low", 400))
    assert r.provider_outages()["anthropic"]["strikes"] == 1


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(
        learning_mode, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl"
    )
    from brain.ui.server import UIServer

    router = _mk_router()
    router.note_provider_error("google", _Err("api key not valid", 400))
    server = UIServer(
        emitter_queue=asyncio.Queue(),
        provider_fn=router.provider_outages,
        provider_reset_fn=router.reset_provider_breaker,
    )
    client = TestClient(server._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    return client, as_role, router


def test_breaker_reset_is_org_admin_only(console):
    client, as_role, router = console
    as_role("member")
    assert client.post("/providers/google/reset").status_code == 403
    assert router.provider_blocked("google")


def test_breaker_reset_clears_the_running_router(console):
    client, as_role, router = console
    as_role("admin")
    r = client.post("/providers/google/reset")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["provider"] == "google" and body["cleared"] is True
    assert body["breaker"] == {}
    assert router.provider_blocked("google") is None
    # Idempotent: a second reset reports nothing was held.
    assert client.post("/providers/google/reset").json()["cleared"] is False


def test_breaker_reset_rejects_unknown_providers(console):
    client, as_role, _router = console
    as_role("admin")
    assert client.post("/providers/sky-net/reset").status_code == 400


def test_breaker_reset_without_a_router_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: True)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    from brain.ui.server import UIServer

    client = TestClient(UIServer(emitter_queue=asyncio.Queue())._build_app())
    assert client.post("/providers/anthropic/reset").status_code == 503


def test_seed_only_change_posts_without_confirm(console, monkeypatch):
    client, as_role, _router = console
    monkeypatch.setattr(org_settings, "refresh", lambda force=False: ("isolated", "default"))
    monkeypatch.setattr(org_settings, "set_instance_seed", lambda seed: ("isolated", seed))
    monkeypatch.setattr(learning_mode, "hypotheses_present", lambda: False)
    as_role("member")
    assert client.post("/org/learning_mode", json={"instance_seed": "current"}).status_code == 403
    as_role("admin")
    r = client.post("/org/learning_mode", json={"instance_seed": "current"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == ["instance_seed"] and body["instance_seed"] == "current"
    assert body["learning_mode"] == "isolated"
    assert client.post("/org/learning_mode", json={"instance_seed": "bogus"}).status_code == 400


def test_persona_catalogue_pages_with_offset_and_limit(console, monkeypatch):
    client, as_role, _router = console
    from brain import personas

    entries = [{"id": f"p{i}", "name": f"P{i}", "custom": True} for i in range(7)]
    monkeypatch.setattr(
        personas,
        "list_for_ui",
        lambda limit=200, offset=0, q=None: (entries[offset : offset + limit], len(entries)),
    )
    as_role("member")  # configuration, not content — any member
    r = client.get("/personas/catalogue?offset=5&limit=3")
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["personas"]] == ["p5", "p6"]
    assert body["total"] == 7 and body["offset"] == 5 and body["limit"] == 3
