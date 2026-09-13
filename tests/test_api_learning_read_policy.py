"""GET /v1/learning/{stories,wiring,summary} consult the read-path content policy.

Audit 2026-09-13: the three routes only checked the bearer key, while the console
gates the same stories as `learning_stories` and the owner-key self-model /
user-model views go through brain/read_policy. An owner key on an isolated org
could read every buyer's companion's learning stories by naming the persona.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import learning_mode, org_settings, read_policy
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.settings import settings

ROUTES = ("/v1/learning/stories", "/v1/learning/wiring", "/v1/learning/summary")
OWNER = {"Authorization": "Bearer ko"}
PARTNER = {"Authorization": "Bearer kp"}


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "k1"},
        "Bearer ko": {"partner_id": None, "owner": True},
    }.get(authorization)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setitem(settings._data, "content_read_policy", 1)
    monkeypatch.setitem(settings._data, "content_read_audit", 1)
    monkeypatch.setitem(settings._data, "content_read_audit_window_s", 300)
    monkeypatch.setattr(read_policy, "_recent_reads", {})
    monkeypatch.setattr(
        learning_mode, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl"
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            learning_runner=lambda kind, **kw: {"kind": kind, **kw},
        )
    )
    return TestClient(app)


def _mode(monkeypatch, mode):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: mode)


def _audit_lines(tmp_path):
    p = tmp_path / "governance_audit.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []


@pytest.mark.parametrize("route", ROUTES)
def test_isolated_non_home_persona_is_withheld_from_the_owner(client, monkeypatch, route):
    _mode(monkeypatch, "isolated")
    r = client.get(route, params={"persona": "ahab"}, headers=OWNER)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["detail"] == "isolated_persona"
    assert r.json()["detail"]["persona"] == "ahab"


@pytest.mark.parametrize("route", ROUTES)
def test_isolated_home_persona_is_readable_and_audited(client, monkeypatch, tmp_path, route):
    _mode(monkeypatch, "isolated")
    r = client.get(route, params={"persona": "home_p"}, headers=OWNER)
    assert r.status_code == 200, r.text
    assert r.json()["persona"] == "home_p"
    recs = [x for x in _audit_lines(tmp_path) if x["event"] == "content_read"]
    assert recs and recs[-1]["kind"] == "learning_stories" and recs[-1]["route"] == route
    assert recs[-1]["persona"] == "home_p" and recs[-1]["scope"] == "owner_lane"


def test_isolated_unscoped_owner_read_is_the_home_persona(client, monkeypatch):
    """No ?persona= reads home; the policy must not deny it as 'unscoped'."""
    _mode(monkeypatch, "isolated")
    r = client.get("/v1/learning/stories", headers=OWNER)
    assert r.status_code == 200, r.text
    assert r.json()["persona"] == ""  # the runner still receives the unscoped read


@pytest.mark.parametrize("route", ROUTES)
def test_consolidated_owner_reads_any_persona(client, monkeypatch, route):
    _mode(monkeypatch, "consolidated")
    r = client.get(route, params={"persona": "ahab"}, headers=OWNER)
    assert r.status_code == 200, r.text
    assert r.json()["persona"] == "ahab"


@pytest.mark.parametrize("route", ROUTES)
def test_partner_key_is_not_an_org_admin(client, monkeypatch, route):
    _mode(monkeypatch, "consolidated")
    r = client.get(route, headers=PARTNER)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["detail"] == "org_admin_required"


def test_unknown_org_mode_fails_closed(client, monkeypatch):
    _mode(monkeypatch, org_settings.UNKNOWN)
    r = client.get("/v1/learning/summary", params={"persona": "ahab"}, headers=OWNER)
    assert r.status_code == 403 and r.json()["detail"]["detail"] == "org_mode_unknown"


def test_unwired_surface_still_501s_after_the_gate(monkeypatch):
    _mode(monkeypatch, "consolidated")
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    r = TestClient(app).get("/v1/learning/stories", headers=OWNER)
    assert r.status_code == 501
