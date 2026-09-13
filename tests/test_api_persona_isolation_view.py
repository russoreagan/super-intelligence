"""GET /v1/personas/{p}/isolation — the audit snapshot (guide §20)."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import org_settings, persona_audit, personas
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.persona_key import persona_state_root


@pytest.fixture
def fs(tmp_path, monkeypatch):
    from brain import persona_chem

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "Unbending."})
    return tmp_path


def test_snapshot_shape_and_fingerprint_stability(fs, monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    snap = persona_audit.snapshot("ahab")
    assert snap["persona"] == "ahab" and snap["learning_mode"] == "isolated"
    assert snap["is_home"] is False and snap["in_dmn_roster"] is False
    assert snap["owner_end_user_id"] is None
    assert snap["files"]["chemistry.json"]["sha256"]
    assert snap["files"]["wiring.json"] is None
    assert snap["documents"]["self.md"]["bytes"] > 0
    assert snap["ledgers"] == {"learning_ledger.jsonl": 0, "learning_stories.jsonl": 0}
    assert snap["counts"] == {}  # local backend: no tables
    fp = snap["fingerprint"]
    # Untouched → identical fingerprint (mtimes are excluded on purpose).
    assert persona_audit.snapshot("ahab")["fingerprint"] == fp
    root = persona_state_root("ahab")
    (root / "wiring.json").write_text('[{"src": "a", "tgt": "b", "w": 0.5, "pol": 1}]')
    snap2 = persona_audit.snapshot("ahab")
    assert snap2["fingerprint"] != fp and snap2["files"]["wiring.json"]["bytes"] > 0
    (root / "learning_stories.jsonl").write_text('{"claim": "x"}\n')
    snap3 = persona_audit.snapshot("ahab")
    assert snap3["ledgers"]["learning_stories.jsonl"] == 1
    assert snap3["fingerprint"] != snap2["fingerprint"]


def test_roster_membership_follows_the_active_rule(fs, monkeypatch):
    """`in_dmn_roster` mirrors dmn._roster: in an isolated org a persona is on the
    shared loop while a human has talked to it in the last dmn_active_roster_days;
    `home` mode excludes everyone but home. `last_human_turn_ts` is reported."""
    from brain import human_activity
    from brain.settings import settings

    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setattr(human_activity, "_persona_last_write_ts", {})
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "active")
    monkeypatch.setitem(settings._data, "dmn_active_roster_days", 7)
    snap = persona_audit.snapshot("ahab")
    assert snap["in_dmn_roster"] is False and snap["last_human_turn_ts"] is None
    fp = snap["fingerprint"]
    human_activity.stamp_persona("ahab", 1_700_000_000.0, force=True)
    monkeypatch.setattr(human_activity, "persona_active", lambda p, d, now=None: True)
    snap = persona_audit.snapshot("ahab")
    assert snap["in_dmn_roster"] is True
    assert snap["last_human_turn_ts"] == 1_700_000_000.0
    assert snap["fingerprint"] == fp  # activity is not learned state
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "home")
    assert persona_audit.snapshot("ahab")["in_dmn_roster"] is False


def test_home_is_always_in_roster_and_learned_state_detection(fs, monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    assert persona_audit.snapshot("home_p")["in_dmn_roster"] is True
    assert persona_audit.has_learned_state("ahab") is False
    (persona_state_root("ahab") / "chunks.json").write_text('{"chunks": {"x": 1}}')
    assert persona_audit.has_learned_state("ahab") is True


def test_self_md_drift_counts_as_learned_state(fs):
    assert persona_audit.has_learned_state("ahab") is False
    p = persona_state_root("ahab") / "schema" / "self.md"
    p.write_text(p.read_text().replace("## History summary\n", "## History summary\n\nLived.\n"))
    assert persona_audit.has_learned_state("ahab") is True


def test_supabase_counts_are_head_counts(fs, monkeypatch):
    from brain.second_brain import supabase_client

    seen: list = []

    class _Q:
        def __init__(self):
            self._t = ""

        def table(self, n):
            self._t = n
            return self

        def select(self, *a, **k):
            seen.append((self._t, k))
            return self

        def eq(self, *a):
            return self

        def limit(self, *a):
            return self

        def execute(self):
            if self._t == "episodes":
                raise RuntimeError("down")
            return type("R", (), {"count": 7, "data": None})()

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _Q())
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    snap = persona_audit.snapshot("ahab")
    assert snap["counts"]["wiring_edges"] == 7
    assert str(snap["counts"]["episodes"]).startswith("error")
    assert any(k.get("head") is True and k.get("count") == "exact" for _t, k in seen)
    # Errors never enter the fingerprint; ints do.
    canonical_ok = json.dumps({k: v for k, v in snap["counts"].items() if isinstance(v, int)})
    assert "wiring_edges" in canonical_ok


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False},
        "Bearer ko": {"partner_id": None, "owner": True},
    }.get(authorization)


def test_route_is_owner_only_and_404s(fs):
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
    assert (
        c.get("/v1/personas/ahab/isolation", headers={"Authorization": "Bearer kp"}).status_code
        == 403
    )
    assert (
        c.get("/v1/personas/nobody/isolation", headers={"Authorization": "Bearer ko"}).status_code
        == 404
    )
    r = c.get("/v1/personas/ahab/isolation", headers={"Authorization": "Bearer ko"})
    assert r.status_code == 200 and r.json()["fingerprint"]
    from brain.api.reference import is_owner_route

    assert is_owner_route("GET", "/v1/personas/{persona}/isolation")
