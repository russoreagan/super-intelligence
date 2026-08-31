"""
Owner-API persona endpoints — PUT/GET/DELETE /v1/personas (brain/personas.py).

Tested via FastAPI TestClient against the router with a fake turn-runner (no
brain needed), with all persona state routed into a tmp dir: auth is enforced,
a custom persona round-trips through PUT → GET → list → DELETE, built-ins are
protected, the baseline is validated/floored, and the self.md composition swaps
the authored sections in without touching the safety scaffold.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return f"echo: {message}", {"emotion": "warm"}


def _client():
    registry = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    app = FastAPI()
    app.include_router(
        build_api_router(_FakeRunner(), registry, auth=lambda h: h == "Bearer sk_test_123")
    )
    return TestClient(app)


_AUTH = {"Authorization": "Bearer sk_test_123"}


@pytest.fixture()
def persona_fs(tmp_path, monkeypatch):
    """Route every persona-state path into tmp: the spec/self.md resolver
    (SECOND_BRAIN_PATH, call-time) and persona_chem's file root (import-time)."""
    from brain import persona_chem

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    return tmp_path


def test_persona_routes_require_api_key(persona_fs):
    c = _client()
    assert c.get("/v1/personas").status_code == 401
    assert c.put("/v1/personas/x", json={}).status_code == 401
    assert c.delete("/v1/personas/x").status_code == 401


def test_persona_crud_roundtrip(persona_fs):
    c = _client()
    body = {
        "display_name": "Captain Ahab",
        "disposition": "Consumed, magnetic, unbending.",
        "speaking": "- Grand, biblical cadence",
        "baseline": {"DA": 0.45, "NE": 0.55, "CORT": 0.30, "GABA": 0.18},
    }
    r = c.put("/v1/personas/captain_ahab", json=body, headers=_AUTH)
    assert r.status_code == 200, r.text
    spec = r.json()
    assert spec["slug"] == "captain_ahab"
    assert spec["display_name"] == "Captain Ahab"
    assert spec["version"] == 1
    assert spec["baseline"]["NE"] == 0.55
    # unset channels fall back to the neutral default profile
    assert 0.0 <= spec["baseline"]["OXT"] <= 1.0

    # spec + chemistry + self.md landed on disk
    assert (persona_fs / "personas" / "captain_ahab" / "persona.json").exists()
    chem = json.loads((persona_fs / "personas" / "captain_ahab" / "chemistry.json").read_text())
    assert chem["resting"]["NE"] == 0.55
    assert chem["current"]["NE"] == 0.55  # fresh persona: mood starts at baseline

    # GET single + list
    got = c.get("/v1/personas/captain_ahab", headers=_AUTH).json()
    assert got["builtin"] is False and got["display_name"] == "Captain Ahab"
    listed = c.get("/v1/personas", headers=_AUTH).json()
    slugs = {p["slug"] for p in listed["personas"]}
    assert "captain_ahab" in slugs and "the_analyst" in slugs
    assert listed["limits"]["max_dedicated_instances"] >= 1

    # idempotent re-PUT bumps version, keeps omitted fields
    r2 = c.put("/v1/personas/captain_ahab", json={"baseline": {"DA": 0.5}}, headers=_AUTH)
    assert r2.json()["version"] == 2
    assert r2.json()["display_name"] == "Captain Ahab"
    assert r2.json()["baseline"]["NE"] == 0.55

    # DELETE removes the spec; a second delete 404s
    assert c.delete("/v1/personas/captain_ahab", headers=_AUTH).status_code == 200
    assert not (persona_fs / "personas" / "captain_ahab" / "persona.json").exists()
    assert c.delete("/v1/personas/captain_ahab", headers=_AUTH).status_code == 404
    assert c.get("/v1/personas/captain_ahab", headers=_AUTH).status_code == 404


def test_builtin_identity_protected_overrides_allowed(persona_fs):
    """Built-in slugs accept OVERRIDE specs (baseline/tag/note/vals) but keep
    identity canonical; DELETE restores defaults."""
    import json as _json

    from brain import persona_chem

    c = _client()
    r = c.get("/v1/personas/the_sage", headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["builtin"] is True and r.json()["overridden"] is False
    # identity text + display name stay canonical
    assert (
        c.put("/v1/personas/the_sage", json={"disposition": "edgy"}, headers=_AUTH).status_code
        == 400
    )
    assert (
        c.put("/v1/personas/the_sage", json={"display_name": "Sagey"}, headers=_AUTH).status_code
        == 400
    )
    # nothing to restore yet
    assert c.delete("/v1/personas/the_sage", headers=_AUTH).status_code == 404

    # override: temperament + UI metadata + knob snapshot
    r = c.put(
        "/v1/personas/the_sage",
        json={
            "baseline": {"DA": 0.75},
            "tag": "Tweaked",
            "vals": {"emotional_reactivity_scale": 1.2},
        },
        headers=_AUTH,
    )
    assert r.status_code == 200, r.text
    spec = r.json()
    canon = persona_chem.PERSONA_CHEMISTRY["The Sage"]
    assert spec["display_name"] == "The Sage"  # not overridable
    assert spec["baseline"]["DA"] == 0.75
    # unset channels default to the built-in's CANONICAL chemistry, not the neutral profile
    assert spec["baseline"]["OXT"] == canon["OXT"]
    got = c.get("/v1/personas/the_sage", headers=_AUTH).json()
    assert got["builtin"] is True and got["overridden"] is True
    assert got["vals"]["emotional_reactivity_scale"] == 1.2
    chem = _json.loads((persona_fs / "personas" / "the_sage" / "chemistry.json").read_text())
    assert chem["resting"]["DA"] == 0.75

    # DELETE = restore defaults: spec gone, resting back to canonical, persona stays
    assert c.delete("/v1/personas/the_sage", headers=_AUTH).status_code == 200
    assert c.get("/v1/personas/the_sage", headers=_AUTH).json()["overridden"] is False
    chem = _json.loads((persona_fs / "personas" / "the_sage" / "chemistry.json").read_text())
    assert abs(chem["resting"]["DA"] - canon["DA"]) < 1e-9
    assert c.delete("/v1/personas/the_sage", headers=_AUTH).status_code == 404


def test_vals_validated_and_round_tripped(persona_fs):
    c = _client()
    assert c.put("/v1/personas/knobs", json={"vals": "nope"}, headers=_AUTH).status_code == 400
    assert c.put("/v1/personas/knobs", json={"vals": {"a": []}}, headers=_AUTH).status_code == 400
    r = c.put(
        "/v1/personas/knobs", json={"vals": {"a": 1, "b": "x", "dropped": None}}, headers=_AUTH
    )
    assert r.status_code == 200 and r.json()["vals"] == {"a": 1, "b": "x"}


def test_list_for_ui_is_the_unified_catalogue(persona_fs):
    from brain import personas

    personas.upsert(
        "captain_ahab",
        {"display_name": "Captain Ahab", "baseline": {"NE": 0.7}, "tag": "Whale business"},
    )
    personas.upsert("the_sage", {"baseline": {"DA": 0.8}})
    by = {row["slug"]: row for row in personas.list_for_ui()}
    assert by["captain_ahab"]["builtin"] is False
    assert by["captain_ahab"]["display_name"] == "Captain Ahab"
    assert by["captain_ahab"]["baseline"]["NE"] == 0.7
    assert by["captain_ahab"]["tag"] == "Whale business"
    assert by["the_sage"]["builtin"] is True and by["the_sage"]["overridden"] is True
    assert by["the_sage"]["baseline"]["DA"] == 0.8
    assert by["the_visionary"]["overridden"] is False
    assert by["the_visionary"]["baseline"]  # canonical chemistry present for un-overridden


def test_validation_rejects_bad_input(persona_fs):
    c = _client()
    assert c.put("/v1/personas/Bad Slug", json={}, headers=_AUTH).status_code == 400
    assert c.put("/v1/personas/has.dot", json={}, headers=_AUTH).status_code == 400
    r = c.put("/v1/personas/ok_slug", json={"baseline": {"XYZ": 0.5}}, headers=_AUTH)
    assert r.status_code == 400 and "channel" in r.json()["detail"]
    r = c.put("/v1/personas/ok_slug", json={"baseline": {"DA": "hot"}}, headers=_AUTH)
    assert r.status_code == 400
    r = c.put("/v1/personas/ok_slug", json={"disposition": 42}, headers=_AUTH)
    assert r.status_code == 400


def test_baseline_floored_and_clamped(persona_fs):
    from brain import persona_chem

    c = _client()
    r = c.put(
        "/v1/personas/flatliner",
        json={"baseline": {"GABA": 0.0, "DA": 7.0}},
        headers=_AUTH,
    )
    spec = r.json()
    assert spec["baseline"]["GABA"] == persona_chem.GABA_RESTING_FLOOR
    # resting is a setpoint — live dynamics need headroom above it, so the API
    # enforces the same 0.8 ceiling as the UI's chemistry sliders
    assert spec["baseline"]["DA"] == persona_chem.RESTING_CEILING


def test_self_md_composed_with_character_sections(persona_fs):
    c = _client()
    c.put(
        "/v1/personas/test_character",
        json={
            "display_name": "Test Character",
            "disposition": "A creature of pure narrative.",
            "speaking": "- Speaks in stage directions",
            "baseline": {"DA": 0.5},
        },
        headers=_AUTH,
    )
    doc = (persona_fs / "personas" / "test_character" / "schema" / "self.md").read_text()
    assert "# Self-Model — Test Character" in doc
    assert "A creature of pure narrative." in doc
    assert "Speaks in stage directions" in doc
    assert "DA=0.50" in doc
    # the safety scaffold survives authored content
    assert "Guiding principles (non-negotiable)" in doc

    # chemistry-only update must NOT rewrite the identity document
    doc_before = doc
    c.put("/v1/personas/test_character", json={"baseline": {"NE": 0.9}}, headers=_AUTH)
    doc_after = (persona_fs / "personas" / "test_character" / "schema" / "self.md").read_text()
    assert doc_after == doc_before


def test_provisioner_materializes_spec_baseline(persona_fs, tmp_path, monkeypatch):
    """The spawn-time stamp: a custom spec's baseline lands in the instance
    settings dict; a built-in falls back to the canonical table."""
    import brain.provisioner as prov
    from brain import personas

    personas.upsert("story_villain", {"baseline": {"DA": 0.11, "CORT": 0.44}})
    # read_spec_under takes the org state root (parent of personas/)
    spec = personas.read_spec_under(tmp_path, "story_villain")
    assert spec and spec["baseline"]["CORT"] == 0.44

    monkeypatch.setattr(prov, "tenant_state_root", lambda uid, persona=None: tmp_path)
    data: dict = {}
    prov._materialize_persona_baseline(data, "org1", "story_villain")
    assert data["chem_baseline_CORT"] == 0.44
    assert data["chem_init_DA"] == 0.11

    table: dict = {}
    prov._materialize_persona_baseline(table, "org1", "the_analyst")
    assert table["chem_baseline_ACh"] == 0.35  # The Analyst's canonical resting

    # A LEGACY spec written before the resting envelope was enforced (over the
    # ceiling, GABA below the floor) still boots a dedicated instance inside it.
    from brain import persona_chem

    legacy = tmp_path / "personas" / "legacy_hothead" / "persona.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"slug": "legacy_hothead", "baseline": {"NE": 0.95, "GABA": 0.01}})
    )
    stamped: dict = {}
    prov._materialize_persona_baseline(stamped, "org1", "legacy_hothead")
    assert stamped["chem_baseline_NE"] == persona_chem.RESTING_CEILING
    assert stamped["chem_baseline_GABA"] == persona_chem.GABA_RESTING_FLOOR
