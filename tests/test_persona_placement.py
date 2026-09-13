"""Persona placement (migration 038): the store, the org caps, and the owner API.

A placement row is the premium-tier entitlement: `dedicated` promises a process of
its own (pinned past the reaper), `pod` says which GPU it talks to (pool /
standalone / org). The store is migration-safe (table missing → "no placements",
one log line), every query is org-scoped, and the API answers 400 (built-in / home /
bad body), 404 (unknown), 409 (over the org's max_dedicated_instances), 402
(standalone/org pod with gpu_daily_usd_budget 0), all owner-only and audit-logged.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import learning_mode as lm
from brain import org_settings as os_
from brain import persona_placement as pp
from brain import personas
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _Res:
    def __init__(self, data):
        self.data = data


class _FakeSb:
    """A minimal PostgREST double: one organizations row plus a persona_placement
    table keyed (org_id, persona). `placement_missing` simulates 038 unapplied."""

    def __init__(self, org_row: dict):
        self.org_row = org_row
        self.placements: dict[tuple[str, str], dict] = {}
        self.placement_missing = False
        self.org_updates: list[dict] = []
        self.fail_org_update = False
        self.queries: list[tuple[str, str, list]] = []
        self._table = ""
        self._op = ""
        self._filters: list = []
        self._payload = None

    def table(self, name):
        self._table, self._op, self._filters, self._payload = name, "", [], None
        return self

    def select(self, *a, **k):
        self._op = "select"
        return self

    def update(self, patch):
        self._op, self._payload = "update", dict(patch)
        return self

    def upsert(self, row, **k):
        self._op, self._payload = "upsert", dict(row)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def _f(self, col):
        for c, v in self._filters:
            if c == col:
                return v
        return None

    def execute(self):
        self.queries.append((self._table, self._op, list(self._filters)))
        if self._table == "organizations":
            if self._op == "select":
                if self._f("id") == self.org_row.get("id"):
                    return _Res([dict(self.org_row)])
                return _Res([])
            if self._op == "update":
                if self.fail_org_update:
                    raise RuntimeError("column gpu_daily_usd_budget does not exist")
                self.org_updates.append(self._payload)
                self.org_row.update(self._payload)
                return _Res([dict(self.org_row)])
        if self._table == "persona_placement":
            if self.placement_missing:
                raise RuntimeError("relation persona_placement does not exist")
            org = self._f("org_id")
            if self._op == "upsert":
                org = (self._payload or {}).get("org_id")
            assert org is not None, "every persona_placement query must be org-scoped"
            if self._op == "select":
                persona = self._f("persona")
                rows = [
                    dict(r)
                    for (o, p), r in self.placements.items()
                    if o == org and (persona is None or p == persona)
                ]
                return _Res(rows)
            if self._op == "upsert":
                row = dict(self._payload)
                key = (row["org_id"], row["persona"])
                prev = self.placements.get(key, {"created_at": "2026-09-12T00:00:00+00:00"})
                self.placements[key] = {**prev, **row}
                return _Res([dict(self.placements[key])])
            if self._op == "delete":
                key = (org, self._f("persona"))
                gone = self.placements.pop(key, None)
                return _Res([gone] if gone else [])
        return _Res([])


@pytest.fixture
def sb(monkeypatch, tmp_path):
    from brain import persona_chem
    from brain.second_brain import supabase_client

    fake = _FakeSb({"id": "org-1", "learning_mode": "consolidated", "instance_seed": "default"})
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    monkeypatch.delenv("BRAIN_MAX_DEDICATED", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.delenv("BRAIN_RUNPOD_HOST_FILE", raising=False)
    monkeypatch.delenv("BRAIN_RUNPOD_POOL_FILE", raising=False)
    monkeypatch.setattr(pp, "_warned_missing", False)
    os_.invalidate()
    pp.invalidate_cache()
    yield fake
    os_.invalidate()
    pp.invalidate_cache()


# ── store ────────────────────────────────────────────────────────────────────


def test_store_missing_table_reads_as_no_placements_with_one_log_line(sb, caplog):
    sb.placement_missing = True
    with caplog.at_level(logging.WARNING, logger="brain.persona_placement"):
        assert pp.get("ahab") is None
        assert pp.list_for_org() == []
        assert pp.list_for("org-1") == []
        assert pp.dedicated_count() == 0
        assert pp.registry_available() is False
    hints = [r for r in caplog.records if "038_persona_placement_and_gpu" in r.getMessage()]
    assert len(hints) == 1  # one line, then quiet
    with pytest.raises(pp.PlacementUnavailable):
        pp.upsert("ahab", pp.validate({"mode": "dedicated"}))


def test_store_upsert_get_list_delete_are_org_scoped(sb):
    row = pp.upsert("ahab", pp.validate({"mode": "dedicated", "pod": "pool"}), created_by="ko")
    assert row["persona"] == "ahab" and row["mode"] == "dedicated" and row["placed"] is True
    assert row["always_on"] is True and row["expired"] is False
    assert pp.get("ahab")["created_by"] == "ko"
    # A row of another org is invisible.
    sb.placements[("org-2", "zed")] = {
        "org_id": "org-2",
        "persona": "zed",
        "mode": "dedicated",
        "pod": "pool",
    }
    assert [r["persona"] for r in pp.list_for_org()] == ["ahab"]
    assert pp.dedicated_count() == 1 and pp.dedicated_count(exclude="ahab") == 0
    assert pp.delete("ahab") == 1 and pp.get("ahab") is None
    assert pp.delete("ahab") == 0
    for table, op, filters in sb.queries:
        if table == "persona_placement" and op != "upsert":  # upsert stamps org_id on the row
            assert ("org_id", "org-1") in filters


def test_validate_and_expiry():
    with pytest.raises(pp.PlacementError):
        pp.validate({"mode": "sharded"})
    with pytest.raises(pp.PlacementError):
        pp.validate({"pod": "moon"})
    with pytest.raises(pp.PlacementError):
        pp.validate({"paid_until": "soon"})
    with pytest.raises(pp.PlacementError):
        pp.validate({"nope": 1})
    with pytest.raises(pp.PlacementError):
        pp.validate({"always_on": "maybe"})
    f = pp.validate({"pod": "org", "paid_until": "2026-12-31T00:00:00Z", "always_on": "no"})
    assert f == {
        "mode": "dedicated",
        "pod": "org",
        "gpu_type": None,
        "always_on": False,
        "paid_until": "2026-12-31T00:00:00+00:00",
    }
    assert pp.is_expired({"paid_until": "2020-01-01T00:00:00+00:00"})
    assert not pp.is_expired({"paid_until": "2999-01-01T00:00:00+00:00"})
    assert not pp.is_expired({"paid_until": None})
    assert not pp.validate({}).get("paid_until")


def test_gateway_cache_keeps_last_known_on_read_error(sb, monkeypatch):
    sb.placements[("org-1", "ahab")] = {
        "org_id": "org-1",
        "persona": "ahab",
        "mode": "dedicated",
        "pod": "standalone",
    }
    rows = pp.cached_for("org-1", client=sb)
    assert [r["persona"] for r in rows] == ["ahab"]
    sb.placement_missing = True
    assert pp.cached_for("org-1", client=sb, ttl_s=0.0) == rows  # last-known, not []
    assert pp.cached_for("org-9", client=sb) == []


# ── org caps ─────────────────────────────────────────────────────────────────


def test_org_caps_read_from_row_with_env_fallback(sb, monkeypatch):
    assert os_.max_dedicated_instances() == 0
    assert os_.gpu_daily_usd_budget() == 0.0
    monkeypatch.setenv("BRAIN_MAX_DEDICATED", "3")
    assert personas.capacity_limits()["max_dedicated_instances"] == 3
    sb.org_row.update({"max_dedicated_instances": 7, "gpu_daily_usd_budget": "12.5"})
    os_.invalidate()
    assert os_.max_dedicated_instances() == 7
    assert os_.gpu_daily_usd_budget() == 12.5
    assert personas.capacity_limits()["max_dedicated_instances"] == 7
    d = lm.describe()
    assert d["max_dedicated_instances"] == 7 and d["gpu_daily_usd_budget"] == 12.5


def test_gateway_org_caps_cache_and_provisioner_cap(sb, monkeypatch):
    from brain import provisioner as prov

    assert os_.cached_org_caps("org-1") is None
    sb.org_row["max_dedicated_instances"] = 2
    assert os_.refresh_org_caps("org-1", client=sb)["max_dedicated_instances"] == 2
    assert os_.cached_org_caps("org-1")["max_dedicated_instances"] == 2

    class _P:
        proc = type("X", (), {"poll": staticmethod(lambda: None)})()
        tier = "full"

    p = prov.Provisioner.__new__(prov.Provisioner)
    p._procs = {"org-1::a": _P(), "org-1::b": _P()}
    monkeypatch.setattr(prov, "MAX_DEDICATED", 3)
    monkeypatch.setattr(prov, "MAX_TENANTS", 0)
    assert p.dedicated_cap("org-1") == 2  # the org row wins over the env
    assert p.dedicated_cap("org-9") == 3  # never-read org: deployment default
    assert "2/2" in p.capacity_refusal("org-1", "c")
    assert p.capacity_refusal("org-9", "c") is None


# ── API ──────────────────────────────────────────────────────────────────────


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


P = {"Authorization": "Bearer kp"}
OWN = {"Authorization": "Bearer ko"}


@pytest.fixture
def client(sb):
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    c = TestClient(app)
    for slug in ("ahab", "ishmael", "home_p"):
        assert (
            c.put(f"/v1/personas/{slug}", headers=OWN, json={"display_name": slug}).status_code
            == 200
        )
    return c


def _audit_events():
    p = lm.audit_log_path()
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_placement_is_owner_only(client):
    assert client.get("/v1/personas/ahab/placement", headers=P).status_code == 403
    assert client.post("/v1/personas/ahab/placement", headers=P, json={}).status_code == 403
    assert client.delete("/v1/personas/ahab/placement", headers=P).status_code == 403
    from brain.api.reference import is_owner_route

    for m in ("GET", "POST", "DELETE"):
        assert is_owner_route(m, "/v1/personas/{persona}/placement")


def test_get_unplaced_reads_as_shared_on_pool(client, sb):
    r = client.get("/v1/personas/ahab/placement", headers=OWN)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "shared" and body["pod"] == "pool" and body["placed"] is False
    assert body["live"] == {"instance": "shared", "pod_state": "unknown", "host_kind": "pool"}
    assert client.get("/v1/personas/nobody/placement", headers=OWN).status_code == 404


def test_post_codes_400_404_409_402(client, sb, monkeypatch):
    # 400: built-in and home personas cannot be placed.
    r = client.post("/v1/personas/the_visionary/placement", headers=OWN, json={"mode": "dedicated"})
    assert r.status_code == 400 and "built-in" in r.json()["detail"]
    r = client.post("/v1/personas/home_p/placement", headers=OWN, json={"mode": "dedicated"})
    assert r.status_code == 400 and "home" in r.json()["detail"]
    # 400: malformed body.
    r = client.post("/v1/personas/ahab/placement", headers=OWN, json={"pod": "moon"})
    assert r.status_code == 400
    # 404: unknown persona.
    assert client.post("/v1/personas/nobody/placement", headers=OWN, json={}).status_code == 404
    # 402: standalone pod while the org has no GPU budget.
    r = client.post("/v1/personas/ahab/placement", headers=OWN, json={"pod": "standalone"})
    assert r.status_code == 402 and "gpu_daily_usd_budget" in r.json()["detail"]
    r = client.post("/v1/personas/ahab/placement", headers=OWN, json={"pod": "org"})
    assert r.status_code == 402
    assert sb.placements == {}
    # 409: over the org cap (org row = 1 beats the env default of 3).
    sb.org_row["max_dedicated_instances"] = 1
    os_.invalidate()
    assert client.post("/v1/personas/ahab/placement", headers=OWN, json={}).status_code == 200
    r = client.post("/v1/personas/ishmael/placement", headers=OWN, json={"mode": "dedicated"})
    assert r.status_code == 409 and "1/1" in r.json()["detail"]
    # Re-placing the same persona is not "over cap" (it holds the slot already).
    assert (
        client.post("/v1/personas/ahab/placement", headers=OWN, json={"pod": "pool"}).status_code
        == 200
    )
    # Env fallback when the row says 0.
    sb.org_row["max_dedicated_instances"] = 0
    os_.invalidate()
    monkeypatch.setenv("BRAIN_MAX_DEDICATED", "1")
    assert client.post("/v1/personas/ishmael/placement", headers=OWN, json={}).status_code == 409
    monkeypatch.setenv("BRAIN_MAX_DEDICATED", "0")  # uncapped
    assert client.post("/v1/personas/ishmael/placement", headers=OWN, json={}).status_code == 200


def test_post_upserts_audits_and_delete_removes(client, sb):
    sb.org_row["gpu_daily_usd_budget"] = 10.0
    os_.invalidate()
    r = client.post(
        "/v1/personas/ahab/placement",
        headers=OWN,
        json={"mode": "dedicated", "pod": "standalone", "paid_until": "2999-01-01T00:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persona"] == "ahab" and body["mode"] == "dedicated" and body["pod"] == "standalone"
    assert (
        body["placed"] is True and body["expired"] is False and body["live"]["instance"] == "shared"
    )
    assert sb.placements[("org-1", "ahab")]["created_by"] == "ko"
    ev = [e for e in _audit_events() if e["event"] == "persona_placement_set"]
    assert len(ev) == 1 and ev[0]["persona"] == "ahab" and ev[0]["actor"]["owner"] is True
    assert ev[0]["from"] is None and ev[0]["to"]["pod"] == "standalone"

    r = client.get("/v1/personas/ahab/placement", headers=OWN)
    assert r.json()["pod"] == "standalone" and r.json()["placed"] is True

    r = client.delete("/v1/personas/ahab/placement", headers=OWN)
    assert r.status_code == 200 and r.json()["removed"] == 1 and r.json()["mode"] == "shared"
    assert ("org-1", "ahab") not in sb.placements
    assert [e["event"] for e in _audit_events()][-1] == "persona_placement_removed"
    assert client.delete("/v1/personas/ahab/placement", headers=OWN).json()["removed"] == 0
    assert client.delete("/v1/personas/nobody/placement", headers=OWN).status_code == 404
    assert client.delete("/v1/personas/home_p/placement", headers=OWN).status_code == 400


def test_expired_placement_reads_as_shared(client, sb):
    sb.placements[("org-1", "ahab")] = {
        "org_id": "org-1",
        "persona": "ahab",
        "mode": "dedicated",
        "pod": "pool",
        "paid_until": "2020-01-01T00:00:00+00:00",
    }
    body = client.get("/v1/personas/ahab/placement", headers=OWN).json()
    assert body["mode"] == "shared" and body["expired"] is True and body["placed"] is True


def test_post_pre_migration_is_503_not_silent(client, sb):
    sb.placement_missing = True
    r = client.post("/v1/personas/ahab/placement", headers=OWN, json={})
    assert r.status_code == 503 and "038" in r.json()["detail"]


def test_live_view_reads_placement_and_pool_files(client, sb, tmp_path, monkeypatch):
    place = tmp_path / ".placement.json"
    place.write_text(json.dumps({"promoted": ["ahab"], "ts": 1}))
    monkeypatch.setenv("BRAIN_PLACEMENT_FILE", str(place))
    from brain import placement_client

    monkeypatch.setattr(placement_client, "_cached_at", 0.0)
    host = tmp_path / ".runpod_host"
    host.write_text("http://pod:11434")
    monkeypatch.setenv("BRAIN_RUNPOD_HOST_FILE", str(host))
    # Legacy single-pod world: no pool file yet.
    live = client.get("/v1/personas/ahab/placement", headers=OWN).json()["live"]
    assert live == {"instance": "dedicated", "pod_state": "ready", "host_kind": "pool"}
    # Pool file with a standalone pod for this instance's process key.
    (tmp_path / ".runpod_pool.json").write_text(
        json.dumps(
            {
                "pods": [
                    {"pod_id": "p0", "kind": "pool", "state": "ready"},
                    {"pod_id": "s1", "kind": "standalone", "state": "warming"},
                ],
                "assignments": {"org-1::ishmael": "p0"},
                "standalone": {"org-1::ahab": {"pod_id": "s1", "host": "h", "state": "warming"}},
            }
        )
    )
    live = client.get("/v1/personas/ahab/placement", headers=OWN).json()["live"]
    assert live == {"instance": "dedicated", "pod_state": "warming", "host_kind": "standalone"}
    live = client.get("/v1/personas/ishmael/placement", headers=OWN).json()["live"]
    assert live == {"instance": "shared", "pod_state": "ready", "host_kind": "pool"}


def test_put_org_permissions_sets_gpu_budget(client, sb):
    r = client.put("/v1/org/permissions", headers=P, json={"gpu_daily_usd_budget": 5})
    assert r.status_code == 403
    r = client.put("/v1/org/permissions", headers=OWN, json={"gpu_daily_usd_budget": "lots"})
    assert r.status_code == 400
    r = client.put("/v1/org/permissions", headers=OWN, json={"gpu_daily_usd_budget": -1})
    assert r.status_code == 400
    r = client.put("/v1/org/permissions", headers=OWN, json={"gpu_daily_usd_budget": 12.5})
    assert r.status_code == 200, r.text
    assert r.json()["gpu_daily_usd_budget"] == 12.5
    assert sb.org_updates == [{"gpu_daily_usd_budget": 12.5}]
    assert [e["event"] for e in _audit_events()] == ["gpu_daily_usd_budget_changed"]
    assert client.get("/v1/org/permissions", headers=P).json()["gpu_daily_usd_budget"] == 12.5
    # Now a standalone placement is allowed.
    assert (
        client.post("/v1/personas/ahab/placement", headers=OWN, json={"pod": "org"}).status_code
        == 200
    )
    sb.fail_org_update = True
    r = client.put("/v1/org/permissions", headers=OWN, json={"gpu_daily_usd_budget": 1})
    assert r.status_code == 503 and "038" in r.json()["detail"]
