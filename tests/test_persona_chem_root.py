"""persona_chem's chemistry.json root resolves at call time (plan 3.6).

A multi-tenant boot sets SECOND_BRAIN_PATH to the HOME persona's dir
(…/personas/<home>), and the import-time snapshot nested every other persona's
chemistry under personas/<home>/personas/<slug>/ while its persona.json sat at
personas/<slug>/. The root now follows personas.personas_dir(); a legacy nested
file is relocated once; the `_PERSONAS_ROOT` test override still wins; the kill
switch restores the snapshot root."""

from __future__ import annotations

import json

import pytest

from brain import persona_chem as pc
from brain import personas
from brain.settings import settings


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    """A persona-scoped SECOND_BRAIN_PATH, exactly as run.py sets it per tenant."""
    home = tmp_path / "personas" / "home_p"
    home.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(home))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(pc, "_PERSONAS_ROOT", None)
    monkeypatch.setitem(settings._data, "persona_chem_root_resolve", 1)
    return tmp_path


def test_sibling_resolution_matches_the_spec_dir(scoped):
    assert pc._personas_root() == scoped / "personas" == personas.personas_dir()
    assert pc._path("Captain Ahab") == scoped / "personas" / "captain_ahab" / "chemistry.json"
    assert pc._path("home_p") == scoped / "personas" / "home_p" / "chemistry.json"
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x"})
    assert (scoped / "personas" / "ahab" / "persona.json").is_file()
    assert (scoped / "personas" / "ahab" / "chemistry.json").is_file(), "beside its spec"
    assert not (scoped / "personas" / "home_p" / "personas").exists(), "no nesting"


def test_legacy_nested_file_relocated_once(scoped):
    legacy = scoped / "personas" / "home_p" / "personas" / "ahab" / "chemistry.json"
    legacy.parent.mkdir(parents=True)
    state = {"resting": {"DA": 0.42}, "current": {"DA": 0.9}, "updated": "t"}
    legacy.write_text(json.dumps(state))
    target = scoped / "personas" / "ahab" / "chemistry.json"
    assert pc._path("ahab") == target
    assert target.is_file() and not legacy.exists()
    assert json.loads(target.read_text())["current"]["DA"] == 0.9, "moved, not reseeded"
    assert pc.load("ahab")["resting"]["DA"] == 0.42
    # A second legacy file never overwrites the live one.
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"resting": {"DA": 0.1}, "current": {"DA": 0.1}}))
    pc._path("ahab")
    assert json.loads(target.read_text())["current"]["DA"] == 0.9 and legacy.exists()


def test_override_still_wins(scoped, tmp_path, monkeypatch):
    override = tmp_path / "elsewhere"
    monkeypatch.setattr(pc, "_PERSONAS_ROOT", override)
    assert pc._path("ahab") == override / "ahab" / "chemistry.json"
    # No relocation is attempted under an override.
    legacy = scoped / "personas" / "home_p" / "personas" / "ahab" / "chemistry.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    pc._path("ahab")
    assert legacy.exists()


def test_kill_switch_restores_the_snapshot_root(scoped, monkeypatch):
    monkeypatch.setitem(settings._data, "persona_chem_root_resolve", 0)
    assert pc._personas_root() == pc._LEGACY_PERSONAS_ROOT
    assert pc._path("ahab") == pc._LEGACY_PERSONAS_ROOT / "ahab" / "chemistry.json"


def test_unscoped_root_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setattr(pc, "_PERSONAS_ROOT", None)
    monkeypatch.setitem(settings._data, "persona_chem_root_resolve", 1)
    assert pc._path("ahab") == tmp_path / "personas" / "ahab" / "chemistry.json"
