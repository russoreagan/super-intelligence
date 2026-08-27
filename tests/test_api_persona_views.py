"""
Owner-gated persona evolution views — GET /v1/personas/{persona}/self-model,
/user-model, /chemistry (brain/persona_models.py).

Tested via FastAPI TestClient against the router with a fake turn-runner and an
injected resolver (partner vs owner), with schema/chemistry state routed into a
tmp dir: partner keys are refused, unknown personas 404, the user-model filters
untouched speaker templates and parses the relationship fields, and the
chemistry view returns resting/current plus parsed client_chem pairs.
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


def _resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    return {
        "kp": {"partner_id": "A", "owner": False},
        "ko": {"partner_id": None, "owner": True},
    }.get(tok)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


PARTNER = {"Authorization": "Bearer kp"}
OWNER = {"Authorization": "Bearer ko"}

VIEWS = ["self-model", "user-model", "chemistry"]


@pytest.fixture
def persona_fs(tmp_path, monkeypatch):
    """Route every read the views touch into tmp: SchemaStore's local SCHEMA_DIR
    (module global, ignores persona on the filesystem backend), persona_chem's
    file root, and persona_state_root (SECOND_BRAIN_PATH, call-time)."""
    from brain import persona_chem
    from brain.second_brain import store as store_mod

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(store_mod, "SCHEMA_DIR", schema_dir)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    return tmp_path


def test_views_require_api_key(client, persona_fs):
    for view in VIEWS:
        assert client.get(f"/v1/personas/the_visionary/{view}").status_code == 401


def test_views_refuse_partner_keys(client, persona_fs):
    for view in VIEWS:
        r = client.get(f"/v1/personas/the_visionary/{view}", headers=PARTNER)
        assert r.status_code == 403, view


def test_views_404_unknown_persona(client, persona_fs):
    for view in VIEWS:
        r = client.get(f"/v1/personas/no_such_persona/{view}", headers=OWNER)
        assert r.status_code == 404, view


def test_self_model_returns_content(client, persona_fs):
    (persona_fs / "schema" / "self.md").write_text("# Self\n\nI chase the horizon.\n")
    r = client.get("/v1/personas/the_visionary/self-model", headers=OWNER)
    assert r.status_code == 200
    body = r.json()
    assert body["persona"] == "the_visionary"
    assert body["display_name"] == "The Visionary"
    assert "I chase the horizon." in body["content"]


def test_user_model_filters_templates_and_parses_relationship(client, persona_fs):
    schema = persona_fs / "schema"
    (schema / "user.md").write_text("# User\n- Prefers directness\n")
    # A speaker that has learned something, with relationship fields.
    (schema / "user_the_adversary_1a2b3c4d.md").write_text(
        "# User: the_adversary\n"
        "- Score: 12\n"
        "- Familiarity: acquainted\n"
        "- Argues in good faith, concedes slowly\n"
    )
    # An untouched template — must be filtered out.
    (schema / "user_stranger_9f9f9f9f.md").write_text(
        "# User: stranger\n- (learning…)\n- Familiarity: new\n- Score: 0\n"
    )
    r = client.get("/v1/personas/the_visionary/user-model", headers=OWNER)
    assert r.status_code == 200
    body = r.json()
    assert "Prefers directness" in body["content"]
    assert [s["file"] for s in body["speakers"]] == ["user_the_adversary_1a2b3c4d.md"]
    speaker = body["speakers"][0]
    assert speaker["name"] == "the_adversary"
    assert speaker["affection"] == 12
    assert speaker["familiarity"] == "acquainted"


def test_chemistry_returns_state_and_pairs(client, persona_fs):
    # Seed the persona chemistry file the loader reads.
    from brain import persona_chem

    chem_dir = persona_fs / "personas" / "the_visionary"
    chem_dir.mkdir(parents=True)
    resting = dict.fromkeys(persona_chem.CHANNELS, 0.5)
    current = {**resting, "DA": 0.8}
    (chem_dir / "chemistry.json").write_text(
        json.dumps({"resting": resting, "current": current, "updated": "2026-08-15T00:00:00"})
    )
    # One persisted client_chem pair (FileChemStore payload shape) + one
    # unreadable file that must be skipped, not fatal. Resolve the dir the way
    # the code does: the HOME persona's state lives at the root itself.
    from brain.persona_key import persona_state_root

    pair_dir = persona_state_root("The Visionary") / "client_chem"
    pair_dir.mkdir(parents=True, exist_ok=True)
    (pair_dir / "aaaa.json").write_text(
        json.dumps(
            {
                "key": "The Visionary:the_adversary",
                "snapshot": {"nm": {"DA": 0.6}},
                "last_seen": 1700000000.0,
            }
        )
    )
    (pair_dir / "bbbb.json").write_text("{not json")
    r = client.get("/v1/personas/the_visionary/chemistry", headers=OWNER)
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "The Visionary"
    assert body["current"]["DA"] == 0.8
    assert body["resting"]["GABA"] == 0.5
    assert body["pairs"] == [
        {
            "end_user_id": "the_adversary",
            "snapshot": {"nm": {"DA": 0.6}},
            "last_seen": 1700000000.0,
        }
    ]


def test_chemistry_seeds_builtin_when_absent(client, persona_fs):
    """A built-in persona with no chemistry.json yet gets seeded from its
    canonical baseline rather than 404ing — matches persona_chem.load()."""
    r = client.get("/v1/personas/the_empath/chemistry", headers=OWNER)
    assert r.status_code == 200
    body = r.json()
    assert body["pairs"] == []
    assert set(body["resting"]) == set(body["current"])
    assert body["resting"]  # non-empty channel map
