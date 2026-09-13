"""Org permission ceilings: one module behind the owner-key API and the console.

ADMIN_ONLY_KEYS = agents.PERMISSION_KEYS | {partner_cloud_daily_usd_budget,
dmn_enabled}. GET /v1/org/permissions is readable by any key; PUT is owner-only,
validates keys, coerces to the declared settings type, jails filesystem roots to
the tenant volume, and clears the per-agent answer_only cache. The console's
POST /settings strips EVERY ceiling key for a non-admin member — until now only
motor_* plus two keys were stripped, so any org member could rewrite the org's
cloud budget with a hand-crafted POST.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import agents
from brain import org_permissions as op
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.settings import settings


@pytest.fixture
def saved(monkeypatch):
    """Capture settings.save patches instead of writing settings.json."""
    patches: list[dict] = []

    def _save(patch=None):
        if patch:
            settings.update(patch)
            patches.append(dict(patch))

    monkeypatch.setattr(settings, "save", _save)
    for k in (
        "answer_only",
        "motor_enable_shell",
        "partner_cloud_daily_usd_budget",
        "dmn_enabled",
        *PRIVACY_AND_DMN_KEYS,
    ):
        monkeypatch.setitem(settings._data, k, settings._data.get(k))
    return patches


# Audit 2026-09-13: the read-path content policy and the org-wide DMN levers were
# ordinary preferences, so any org member could switch the content gate off.
PRIVACY_AND_DMN_KEYS = (
    "content_read_policy",
    "content_read_audit",
    "content_read_audit_window_s",
    "dmn_isolated_roster",
    "dmn_active_roster_days",
    "dmn_pause_after_idle_s",
)


def test_admin_only_keys_cover_ceilings_and_org_switches():
    assert agents.PERMISSION_KEYS <= op.ADMIN_ONLY_KEYS
    assert {"partner_cloud_daily_usd_budget", "dmn_enabled", "answer_only"} <= op.ADMIN_ONLY_KEYS
    assert set(PRIVACY_AND_DMN_KEYS) <= op.ADMIN_ONLY_KEYS
    assert "sleep_check_interval_s" not in op.ADMIN_ONLY_KEYS


def test_read_returns_every_key(saved):
    out = op.read()
    assert set(out) == set(op.ADMIN_ONLY_KEYS)
    assert out["answer_only"] == 0


def test_sanitize_rejects_unknown_keys_and_coerces(saved):
    with pytest.raises(op.OrgPermissionsError):
        op.sanitize({"sleep_check_interval_s": 1})
    clean, dropped = op.sanitize(
        {
            "answer_only": True,
            "partner_cloud_daily_usd_budget": "7.5",
            "motor_allowed_commands": ["ls", "cat"],
        }
    )
    assert clean == {
        "answer_only": 1,
        "partner_cloud_daily_usd_budget": 7.5,
        "motor_allowed_commands": "ls\ncat",
    }
    assert dropped == []
    with pytest.raises(op.OrgPermissionsError):
        op.sanitize({"partner_cloud_daily_usd_budget": "lots"})


def test_write_jails_dirs_and_clears_answer_only_cache(saved, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setitem(settings._data, "motor_allowed_dirs", "")
    inside = tmp_path / "workspace"
    inside.mkdir()
    agents._answer_only_cache["p.m"] = (1e12, False)
    out = op.write({"answer_only": 1, "motor_allowed_dirs": [str(inside), "/etc"]})
    assert out["permissions"]["answer_only"] == 1
    assert out["permissions"]["motor_allowed_dirs"] == str(inside)
    assert out["dropped_paths"] == ["/etc"]
    assert saved[-1]["answer_only"] == 1
    assert "p.m" not in agents._answer_only_cache


# ── routes ───────────────────────────────────────────────────────────────────


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False},
        "Bearer ko": {"partner_id": None, "owner": True},
    }.get(authorization)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


def test_get_is_readable_by_any_key(client, saved):
    r = client.get("/v1/org/permissions", headers={"Authorization": "Bearer kp"})
    assert r.status_code == 200
    assert set(r.json()["permissions"]) == set(op.ADMIN_ONLY_KEYS)
    assert r.json()["keys"] == sorted(op.ADMIN_ONLY_KEYS)


def test_put_is_owner_only_and_validates(client, saved):
    hdr_p = {"Authorization": "Bearer kp"}
    hdr_o = {"Authorization": "Bearer ko"}
    assert (
        client.put("/v1/org/permissions", headers=hdr_p, json={"answer_only": 1}).status_code == 403
    )
    r = client.put("/v1/org/permissions", headers=hdr_o, json={"nope": 1})
    assert r.status_code == 400 and "nope" in r.json()["detail"]
    r = client.put("/v1/org/permissions", headers=hdr_o, json={"answer_only": True})
    assert r.status_code == 200
    assert r.json()["permissions"]["answer_only"] == 1
    assert saved[-1] == {"answer_only": 1}


def test_put_accepts_content_policy_and_dmn_levers(client, saved):
    """The console and the owner API must not disagree: PUT used to 400 on
    content_read_policy while the console let any member save it."""
    hdr_o = {"Authorization": "Bearer ko"}
    body = {
        "content_read_policy": False,
        "content_read_audit": 0,
        "content_read_audit_window_s": 60,
        "dmn_isolated_roster": "all",
        "dmn_active_roster_days": 3,
        "dmn_pause_after_idle_s": 3600,
    }
    r = client.put("/v1/org/permissions", headers=hdr_o, json=body)
    assert r.status_code == 200, r.text
    perms = r.json()["permissions"]
    assert perms["content_read_policy"] == 0 and perms["content_read_audit"] == 0
    assert perms["content_read_audit_window_s"] == 60.0
    assert perms["dmn_isolated_roster"] == "all" and perms["dmn_active_roster_days"] == 3
    assert perms["dmn_pause_after_idle_s"] == 3600.0
    assert saved[-1]["content_read_policy"] == 0
    # GET reflects the write; a partner key may still not write it.
    g = client.get("/v1/org/permissions", headers={"Authorization": "Bearer kp"})
    assert g.json()["permissions"]["dmn_isolated_roster"] == "all"
    p = client.put(
        "/v1/org/permissions",
        headers={"Authorization": "Bearer kp"},
        json={"content_read_policy": 0},
    )
    assert p.status_code == 403


def test_put_is_a_registered_owner_route():
    from brain.api.reference import is_owner_route

    assert is_owner_route("PUT", "/v1/org/permissions")
    assert not is_owner_route("GET", "/v1/org/permissions")


# ── console tightening ───────────────────────────────────────────────────────


def test_console_strips_every_ceiling_key_for_non_admin(monkeypatch, tmp_path):
    """The live gap: a non-admin org member could POST cloud_daily_usd_budget."""
    from brain.ui import auth as ui_auth
    from brain.ui import server as ui_server

    monkeypatch.setenv("BRAIN_AUTH_DISABLED", "true")  # build without a cookie gate
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)
    import brain.second_brain.store as store_mod
    from brain import persona_chem, personas

    (tmp_path / "schema").mkdir()
    monkeypatch.setattr(store_mod, "SCHEMA_DIR", tmp_path / "schema")
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    monkeypatch.setattr(personas, "_migration_checked", False)

    received: list[dict] = []

    class _Settings:
        def __init__(self):
            self.d = {"cloud_daily_usd_budget": 20.0, "some_pref": 1}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def all(self):
            return dict(self.d)

        def save(self, patch=None):
            received.append(dict(patch or {}))
            self.d.update(patch or {})

        def reset_to_defaults(self):
            pass

    import asyncio

    import brain.settings as settings_mod

    fake = _Settings()
    monkeypatch.setattr(settings_mod, "settings", fake)
    server = ui_server.UIServer(emitter_queue=asyncio.Queue())
    c = TestClient(server._build_app())
    # Auth "on" for the gate check inside the handler, but the caller is neither
    # platform admin nor org admin.
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)  # skip the cookie gate
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: False)
    body = {
        "cloud_daily_usd_budget": 9999.0,
        "partner_cloud_daily_usd_budget": 9999.0,
        "answer_only": 0,
        "dmn_enabled": 0,
        "motor_enable_shell": 1,
        "some_pref": 2,
        # The read-path privacy switches and the org-wide DMN levers (audit
        # 2026-09-13): a member's POST must not reach settings.json.
        "content_read_policy": 0,
        "content_read_audit": 0,
        "content_read_audit_window_s": 1,
        "dmn_isolated_roster": "all",
        "dmn_active_roster_days": 999,
        "dmn_pause_after_idle_s": 1,
    }
    r = c.post("/settings", json=body)
    assert r.status_code == 200, r.text
    assert fake.d["cloud_daily_usd_budget"] == 20.0
    assert "partner_cloud_daily_usd_budget" not in fake.d
    assert "dmn_enabled" not in fake.d and "motor_enable_shell" not in fake.d
    for k in PRIVACY_AND_DMN_KEYS:
        assert k not in fake.d, k
        assert all(k not in patch for patch in received), k
    assert fake.d["some_pref"] == 2  # ordinary preferences still save
