"""
"Sense of You" endpoint contract (/user-model on the UI server).

Sleep consolidation routes facts BY SPEAKER: every voice-identified companion
turn and every engine turn (speaker_name = end_user_id, which is required on
API sessions) lands in a per-person user_<slug>.md — only speakerless turns
land in user.md. The tab used to read user.md alone, so a persona used mostly
over the API (or by a named primary user) showed an empty "Sense of You"
despite a fully populated model. These tests pin the fixed contract: the
endpoint returns user.md PLUS the per-person models, skipping profiles that
are still the untouched template.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def ui_client(tmp_path, monkeypatch):
    """A TestClient over the UI app, auth off, SchemaStore on a temp dir."""
    monkeypatch.setenv("BRAIN_AUTH_DISABLED", "true")
    import brain.second_brain.store as store_mod

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "SCHEMA_DIR", schema_dir)

    from brain.ui.server import UIServer

    server = UIServer(emitter_queue=asyncio.Queue())
    return TestClient(server._build_app()), store_mod.SchemaStore()


def test_user_model_includes_per_speaker_models(ui_client):
    """An engine/voice speaker's learned profile must surface, name and all."""
    client, store = ui_client
    store.write("user.md", "# User\n\n## Known facts\n- Likes short answers\n")
    store.write(
        "user_ada.md",
        "# User: Ada\n\n## Known facts\n- Ada ships Rust services\n\n"
        "## Communication style\n- (learning…)\n",
    )

    data = client.get("/user-model").json()

    assert "Likes short answers" in data["content"]
    assert len(data["speakers"]) == 1
    sp = data["speakers"][0]
    assert sp["file"] == "user_ada.md"
    assert sp["name"] == "Ada"
    assert "Ada ships Rust services" in sp["content"]


def test_user_model_skips_untouched_templates(ui_client):
    """A profile created on first sighting but never learned into stays hidden."""
    client, store = ui_client
    store.ensure_speaker_schema("Bob")  # writes the pristine template

    data = client.get("/user-model").json()

    assert data["speakers"] == []


def test_user_model_learned_relationship_counts_as_substance(ui_client):
    """Any fact line beyond the template boilerplate makes the profile visible —
    including a relationship tier that has moved past 'new'."""
    client, store = ui_client
    store.ensure_speaker_schema("Cara")
    fname = store.speaker_filename("Cara")
    store.write(fname, store.read(fname).replace("- Familiarity: new", "- Familiarity: friend"))

    data = client.get("/user-model").json()

    assert [s["file"] for s in data["speakers"]] == [fname]


def test_user_model_name_falls_back_to_slug(ui_client):
    """A headerless per-speaker file still gets a usable display name."""
    client, store = ui_client
    store.write("user_end_user_42.md", "## Known facts\n- Trades options weekly\n")

    data = client.get("/user-model").json()

    assert data["speakers"][0]["name"] == "end_user_42"


def test_user_model_empty_store_shape(ui_client):
    """No models at all → stable empty shape the UI can rely on."""
    client, _ = ui_client

    data = client.get("/user-model").json()

    assert data["content"] == ""
    assert data["speakers"] == []
