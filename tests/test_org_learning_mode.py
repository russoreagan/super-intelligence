"""Org learning mode: the account setting, its cache semantics, and the switch.

organizations.learning_mode (migration 037) decides whether a persona is one
learning identity shared across customers (consolidated) or a separate individual
(isolated). brain/org_settings.py reads it with a 60 s TTL cache and never lets a
read error silently replace a value that was read once; brain/learning_mode.switch
implements the governance event exactly as the guide's §20 table states it.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import learning_mode as lm
from brain import org_settings as os_
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _Res:
    def __init__(self, data):
        self.data = data


class _FakeSb:
    """A minimal PostgREST double: one organizations row, optional failure modes."""

    def __init__(self, row: dict | None):
        self.row = row
        self.fail_reads = False
        self.fail_updates = False
        self.updates: list[dict] = []
        self.sessions: list[dict] = []
        self.owners_table_missing = False
        self._table = ""
        self._op = ""
        self._patch: dict = {}

    def table(self, name):
        self._table = name
        self._op = ""
        return self

    def select(self, *a, **k):
        self._op = "select"
        return self

    def update(self, patch):
        self._op = "update"
        self._patch = dict(patch)
        return self

    def upsert(self, *a, **k):
        self._op = "upsert"
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._table == "organizations":
            if self._op == "select":
                if self.fail_reads:
                    raise RuntimeError("db down")
                return _Res([dict(self.row)] if self.row is not None else [])
            if self._op == "update":
                if self.fail_updates:
                    raise RuntimeError("column does not exist")
                self.updates.append(self._patch)
                self.row = {**(self.row or {}), **self._patch}
                return _Res([dict(self.row)])
        if self._table == "api_sessions":
            return _Res(list(self.sessions))
        if self._table == "persona_owners":
            if self.owners_table_missing:
                raise RuntimeError("relation persona_owners does not exist")
            return _Res([])
        return _Res([])


@pytest.fixture
def sb(monkeypatch, tmp_path):
    from brain.second_brain import supabase_client

    fake = _FakeSb({"id": "org-1", "learning_mode": "consolidated", "instance_seed": "default"})
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_visionary")
    os_.invalidate()
    yield fake
    os_.invalidate()


# ── org_settings cache semantics ─────────────────────────────────────────────


def test_reads_row_and_caches(sb, monkeypatch):
    assert os_.learning_mode() == "consolidated"
    assert os_.instance_seed() == "default"
    sb.row["learning_mode"] = "isolated"
    # Cached: a change on the row is not seen until the TTL elapses.
    assert os_.learning_mode() == "consolidated"
    monkeypatch.setattr(os_, "_TTL_S", 0.0)
    assert os_.learning_mode() == "isolated"


def test_pre_migration_row_reads_as_default(sb):
    sb.row = {"id": "org-1"}  # 037 not applied: columns absent
    assert os_.learning_mode() == "consolidated"
    assert os_.instance_seed() == "default"


def test_read_error_keeps_last_known_value(sb, monkeypatch):
    assert os_.learning_mode() == "consolidated"
    sb.fail_reads = True
    monkeypatch.setattr(os_, "_TTL_S", 0.0)
    assert os_.learning_mode() == "consolidated"  # last-known, not a silent default
    assert not os_.is_isolated()


def test_never_read_is_unknown_and_fails_closed_for_gates(sb):
    sb.fail_reads = True
    assert os_.learning_mode() == os_.UNKNOWN
    assert os_.is_isolated() is True  # leak gates withhold
    assert os_.is_isolated_known() is False  # refusals do not fire


def test_local_backend_is_consolidated(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    os_.invalidate()
    assert os_.learning_mode() == "consolidated"
    with pytest.raises(os_.OrgSettingsError):
        os_.set_learning_mode("isolated", "default")


def test_set_learning_mode_writes_and_refreshes(sb):
    assert os_.set_learning_mode("isolated", "current") == ("isolated", "current")
    assert sb.updates[-1] == {"learning_mode": "isolated", "instance_seed": "current"}
    assert os_.learning_mode() == "isolated"


def test_set_learning_mode_pre_migration_raises(sb):
    sb.fail_updates = True
    with pytest.raises(os_.OrgSettingsError, match="037"):
        os_.set_learning_mode("isolated", "default")


def test_home_persona_exemption(sb):
    assert os_.is_home("the_visionary")
    assert os_.is_home("The Visionary")
    assert os_.is_home("")
    assert not os_.is_home("captain_ahab__buyer1")


# ── the switch ───────────────────────────────────────────────────────────────


def _audit_lines():
    p = lm.audit_log_path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_switch_ignores_bodies_without_switch_fields(sb):
    assert lm.switch({"answer_only": 1}) is None


def test_switch_to_isolated_requires_confirm_and_seed(sb, monkeypatch):
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: [])
    with pytest.raises(lm.SwitchError) as e:
        lm.switch({"learning_mode": "isolated"})
    assert e.value.status == 400 and "confirm" in e.value.payload["detail"]
    with pytest.raises(lm.SwitchError) as e:
        lm.switch({"learning_mode": "isolated", "confirm": True})
    assert e.value.status == 400 and "instance_seed" in e.value.payload["detail"]
    assert sb.updates == []  # nothing written on a refused switch


def test_switch_to_isolated_reports_templates_and_multi_owner(sb, monkeypatch):
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: ["ahab", "ishmael"])
    sb.sessions = [
        {"agent_id": "ahab.role", "end_user_id": "u1"},
        {"agent_id": "ahab.role", "end_user_id": "u2"},
        {"agent_id": "ishmael.role", "end_user_id": "u3"},
        {"agent_id": "the_visionary.role", "end_user_id": "u4"},
        {"agent_id": "the_visionary.role", "end_user_id": "u5"},
    ]
    out = lm.switch(
        {"learning_mode": "isolated", "confirm": True, "instance_seed": "current"},
        {"owner": True, "partner_id": None, "key_id": "k1"},
    )
    assert out["learning_mode"] == "isolated" and out["instance_seed"] == "current"
    assert out["previous"] == "consolidated"
    assert out["personas_with_learned_state"] == ["ahab", "ishmael"]
    assert [m["persona"] for m in out["multi_owner_personas"]] == ["ahab"]  # home excluded
    assert out["multi_owner_personas"][0]["status"] == "multi-owner, cannot be bound"
    assert any("persona_ownership_binding: on" in c for c in out["changed"] if c)
    assert sb.updates[-1] == {"learning_mode": "isolated", "instance_seed": "current"}
    rec = _audit_lines()[-1]
    assert rec["event"] == "learning_mode_changed"
    assert rec["from"] == "consolidated" and rec["to"] == "isolated"
    assert rec["instance_seed"] == "current"
    assert rec["actor"]["key_id"] == "k1" and rec["actor"]["owner"] is True
    assert rec["personas_with_learned_state"] == ["ahab", "ishmael"]


def test_switch_to_isolated_reports_unenforceable_binding_pre_migration(sb, monkeypatch):
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: [])
    sb.owners_table_missing = True
    out = lm.switch({"learning_mode": "isolated", "confirm": True, "instance_seed": "default"})
    assert any("NOT enforceable" in c for c in out["changed"] if c)


def test_switch_back_refused_while_learned_state_exists(sb, monkeypatch):
    sb.row["learning_mode"] = "isolated"
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: ["ahab__b1"])
    with pytest.raises(lm.SwitchError) as e:
        lm.switch({"learning_mode": "consolidated", "confirm": True})
    assert e.value.status == 409
    assert e.value.payload["personas"] == ["ahab__b1"]
    assert "purge or archive first" in e.value.payload["detail"]
    assert sb.updates == []


def test_switch_back_force_is_audit_logged_with_personas(sb, monkeypatch):
    sb.row["learning_mode"] = "isolated"
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: ["ahab__b1", "ahab__b2"])
    out = lm.switch(
        {"learning_mode": "consolidated", "confirm": True, "force": True},
        {"owner": True, "partner_id": None},
    )
    assert out["learning_mode"] == "consolidated" and out["forced"] is True
    assert out["personas_with_learned_state"] == ["ahab__b1", "ahab__b2"]
    rec = _audit_lines()[-1]
    assert rec["force"] is True
    assert rec["personas_with_learned_state"] == ["ahab__b1", "ahab__b2"]


def test_switch_back_without_learned_state_needs_only_confirm(sb, monkeypatch):
    sb.row["learning_mode"] = "isolated"
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: [])
    with pytest.raises(lm.SwitchError) as e:
        lm.switch({"learning_mode": "consolidated"})
    assert e.value.status == 400
    out = lm.switch({"learning_mode": "consolidated", "confirm": True})
    assert out["learning_mode"] == "consolidated"


def test_seed_only_change_is_audited_without_confirm(sb):
    out = lm.switch({"instance_seed": "current"})
    assert out["changed"] == ["instance_seed"] and out["instance_seed"] == "current"
    assert _audit_lines()[-1]["event"] == "instance_seed_changed"
    # Same mode, same seed → a no-op report.
    out = lm.switch({"learning_mode": "consolidated", "instance_seed": "current"})
    assert out["changed"] == []


def test_switch_rejects_bad_values(sb):
    with pytest.raises(lm.SwitchError):
        lm.switch({"learning_mode": "strict", "confirm": True})
    with pytest.raises(lm.SwitchError):
        lm.switch({"learning_mode": "isolated", "confirm": True, "instance_seed": "latest"})
    with pytest.raises(lm.SwitchError):
        lm.switch({"learning_mode": "isolated", "confirm": "maybe"})


def test_switch_refuses_when_row_unreadable(sb):
    sb.fail_reads = True
    with pytest.raises(os_.OrgSettingsError):
        lm.switch({"learning_mode": "isolated", "confirm": True, "instance_seed": "default"})


def test_hypotheses_purge(sb, tmp_path):
    p = lm.hypotheses_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"hypotheses": []}')
    assert lm.hypotheses_present()
    out = lm.purge_hypotheses({"owner": True})
    assert out["purged"] is True and not p.exists()
    assert _audit_lines()[-1]["event"] == "hypotheses_purged"
    assert lm.purge_hypotheses({"owner": True})["purged"] is False


# ── API surfaces ─────────────────────────────────────────────────────────────


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


@pytest.fixture
def client(sb, monkeypatch):
    from brain.settings import settings

    monkeypatch.setattr(settings, "save", lambda patch=None: settings.update(patch or {}))
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


def test_whoami_and_permissions_expose_mode(client):
    r = client.get("/v1/whoami", headers={"Authorization": "Bearer kp"})
    assert r.status_code == 200
    assert r.json()["learning_mode"] == "consolidated"
    assert r.json()["instance_seed"] == "default"
    r = client.get("/v1/org/permissions", headers={"Authorization": "Bearer kp"})
    assert r.json()["learning_mode"] == "consolidated"
    assert r.json()["hypotheses_present"] is False


def test_put_switch_semantics_over_http(client, sb, monkeypatch):
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: ["ahab"])
    hdr = {"Authorization": "Bearer ko"}
    # Partner keys cannot switch.
    r = client.put(
        "/v1/org/permissions",
        headers={"Authorization": "Bearer kp"},
        json={"learning_mode": "isolated", "confirm": True, "instance_seed": "default"},
    )
    assert r.status_code == 403
    # 400 without instance_seed; nothing else in the body applied either.
    r = client.put(
        "/v1/org/permissions", headers=hdr, json={"learning_mode": "isolated", "confirm": True}
    )
    assert r.status_code == 400 and "instance_seed" in r.json()["detail"]
    r = client.put(
        "/v1/org/permissions",
        headers=hdr,
        json={"learning_mode": "isolated", "confirm": True, "instance_seed": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["learning_mode"] == "isolated"
    assert body["switch"]["personas_with_learned_state"] == ["ahab"]
    assert "permissions" in body
    # 409 back while learned state exists, listing the personas (flat body).
    r = client.put(
        "/v1/org/permissions", headers=hdr, json={"learning_mode": "consolidated", "confirm": True}
    )
    assert r.status_code == 409
    assert r.json()["personas"] == ["ahab"]
    # force works and is reported.
    r = client.put(
        "/v1/org/permissions",
        headers=hdr,
        json={"learning_mode": "consolidated", "confirm": True, "force": True},
    )
    assert r.status_code == 200 and r.json()["switch"]["forced"] is True


def test_put_pre_migration_is_503_not_silent(client, sb, monkeypatch):
    monkeypatch.setattr(lm, "personas_with_learned_state", lambda: [])
    sb.fail_updates = True
    r = client.put(
        "/v1/org/permissions",
        headers={"Authorization": "Bearer ko"},
        json={"learning_mode": "isolated", "confirm": True, "instance_seed": "default"},
    )
    assert r.status_code == 503 and "037" in r.json()["detail"]


def test_delete_hypotheses_is_owner_only(client, sb):
    p = lm.hypotheses_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    assert (
        client.delete("/v1/org/hypotheses", headers={"Authorization": "Bearer kp"}).status_code
        == 403
    )
    r = client.delete("/v1/org/hypotheses", headers={"Authorization": "Bearer ko"})
    assert r.status_code == 200 and r.json()["purged"] is True
    from brain.api.reference import is_owner_route

    assert is_owner_route("DELETE", "/v1/org/hypotheses")


def test_settings_defaults_declared():
    from brain.settings import DEFAULTS

    assert DEFAULTS["sleep_scan_all_personas"] == 0
    assert DEFAULTS["self_model_deid"] == 1
    assert DEFAULTS["engine_lane_scoping"] == 1
    assert DEFAULTS["persona_ownership_binding"] == 1
