"""
The UI half of the unified persona store (brain/ui/server.py).

GET /settings serves the spec-file catalogue (folding any legacy persona_store
blob in first), POST /settings routes a `persona_spec` rider to the spec store
on both save shapes, and DELETE /settings/personas/{slug} deletes a custom /
restores a built-in's defaults — so the settings UI and the engine API read and
write ONE persona catalogue.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


class _FakeSettings:
    def __init__(self, data: dict | None = None):
        self.data = {"persona_name": "The Admin", **(data or {})}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def all(self):
        return dict(self.data)

    def save(self, patch=None):
        self.data.update(patch or {})

    def reset_to_defaults(self):
        self.data = {}


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """TestClient over the UI app: auth off, persona state + schema store in
    tmp, the settings singleton faked (no real settings.json writes)."""
    monkeypatch.setenv("BRAIN_AUTH_DISABLED", "true")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)

    import brain.second_brain.store as store_mod
    import brain.settings as settings_mod
    from brain import persona_chem, personas

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "SCHEMA_DIR", schema_dir)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    fake = _FakeSettings()
    monkeypatch.setattr(settings_mod, "settings", fake)
    monkeypatch.setattr(personas, "_migration_checked", False)

    from brain.ui.server import UIServer

    server = UIServer(emitter_queue=asyncio.Queue())
    return TestClient(server._build_app()), fake, tmp_path


def test_get_settings_serves_unified_catalogue_and_migrates_blob(ui):
    client, fake, tmp = ui
    fake.data["persona_store"] = json.dumps(
        {"Story Guy": {"custom": True, "tag": "Legacy", "chem": {"DA": 0.71}}}
    )
    r = client.get("/settings")
    assert r.status_code == 200
    cat = {p["slug"]: p for p in r.json()["personas"]}
    # built-ins present with canonical baselines; the blob entry became a spec
    assert cat["the_sage"]["builtin"] is True and cat["the_sage"]["baseline"]
    assert cat["story_guy"]["builtin"] is False
    assert cat["story_guy"]["display_name"] == "Story Guy"
    assert cat["story_guy"]["baseline"]["DA"] == 0.71
    assert cat["story_guy"]["tag"] == "Legacy"
    assert (tmp / "personas" / "story_guy" / "persona.json").exists()
    assert fake.data["persona_store_migrated"]


def test_config_save_writes_spec(ui):
    client, fake, tmp = ui
    body = {
        "config_persona": "captain_ahab",
        "config_chem": {"DA": 0.4},
        "config_chem_init": {"DA": 0.4},
        "persona_spec": {
            "slug": "captain_ahab",
            "display_name": "Captain Ahab",
            "tag": "Custom",
            "note": "",
            "baseline": {"DA": 0.4},
            "vals": {"emotional_reactivity_scale": 1.1},
        },
    }
    r = client.post("/settings", json=body)
    assert r.status_code == 200, r.text
    spec = json.loads((tmp / "personas" / "captain_ahab" / "persona.json").read_text())
    assert spec["display_name"] == "Captain Ahab"
    assert spec["vals"]["emotional_reactivity_scale"] == 1.1


def test_active_save_routes_spec_rider_out_of_settings(ui):
    client, fake, tmp = ui
    r = client.post(
        "/settings",
        json={
            "persona_name": "The Admin",  # no switch — same as running
            "persona_spec": {"slug": "the_admin", "baseline": {"NE": 0.6}, "vals": {"k": 1}},
        },
    )
    assert r.status_code == 200, r.text
    spec = json.loads((tmp / "personas" / "the_admin" / "persona.json").read_text())
    assert spec["baseline"]["NE"] == 0.6
    assert "persona_spec" not in fake.data  # never lands in settings.json


def test_invalid_spec_fails_loudly(ui):
    client, _, _ = ui
    r = client.post(
        "/settings",
        json={
            "config_persona": "the_sage",
            "persona_spec": {"slug": "the_sage", "display_name": "Nope"},
        },
    )
    assert r.status_code == 400
    assert "persona spec" in r.json()["detail"]


def test_delete_route_custom_and_builtin_restore(ui):
    client, fake, tmp = ui
    from brain import persona_chem, personas

    personas.upsert("story_guy", {"display_name": "Story Guy", "baseline": {"DA": 0.7}})
    personas.upsert("the_sage", {"baseline": {"DA": 0.95}})

    # custom → gone
    assert client.delete("/settings/personas/story_guy").status_code == 200
    assert not (tmp / "personas" / "story_guy" / "persona.json").exists()
    assert client.delete("/settings/personas/story_guy").status_code == 404

    # builtin → restore defaults: override removed, resting back to canonical
    r = client.delete("/settings/personas/the_sage")
    assert r.status_code == 200 and r.json()["restored"] is True
    assert not (tmp / "personas" / "the_sage" / "persona.json").exists()
    chem = json.loads((tmp / "personas" / "the_sage" / "chemistry.json").read_text())
    assert abs(chem["resting"]["DA"] - persona_chem.PERSONA_CHEMISTRY["The Sage"]["DA"]) < 1e-9
    assert client.delete("/settings/personas/the_sage").status_code == 404
