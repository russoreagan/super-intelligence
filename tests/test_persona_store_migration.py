"""
One-time persona_store → spec-file migration (personas.migrate_persona_store).

The settings UI's legacy catalogue blob (persona_store settings key) folds into
the per-persona spec files that are now canonical for both the UI and the engine
API: customs become full specs, built-in entries become override specs (their
chemistry lifted out of the saved knob snapshot), existing spec files win over
blob entries, and the run is marker-gated idempotent.
"""

from __future__ import annotations

import json

import pytest


class _FakeSettings:
    def __init__(self, data: dict):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self, patch=None):
        self.data.update(patch or {})


@pytest.fixture()
def persona_fs(tmp_path, monkeypatch):
    from brain import persona_chem

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    return tmp_path


def _wire(monkeypatch, blob: dict | str, extra: dict | None = None) -> _FakeSettings:
    import brain.settings as bs
    from brain import personas

    raw = blob if isinstance(blob, str) else json.dumps(blob)
    fake = _FakeSettings({"persona_store": raw, **(extra or {})})
    monkeypatch.setattr(bs, "settings", fake)
    monkeypatch.setattr(personas, "_migration_checked", False)
    return fake


def test_blob_folds_into_specs(persona_fs, monkeypatch):
    from brain import personas

    fake = _wire(
        monkeypatch,
        {
            "My Character": {
                "custom": True,
                "tag": "T",
                "note": "N",
                "chem": {"DA": 0.7},
                "vals": {"emotional_reactivity_scale": 1.3},
            },
            # built-in override: chemistry lives only inside the knob snapshot
            "The Sage": {"custom": False, "vals": {"chem_baseline_DA": 0.66, "chem_init_DA": 0.66}},
        },
    )
    assert personas.migrate_persona_store() == 2

    spec = personas.read_spec("my_character")
    assert spec and spec["display_name"] == "My Character"
    assert spec["baseline"]["DA"] == 0.7
    assert spec["vals"]["emotional_reactivity_scale"] == 1.3
    assert spec["tag"] == "T" and spec["note"] == "N"

    sage = personas.read_spec("the_sage")
    assert sage and sage["baseline"]["DA"] == 0.66
    assert sage["display_name"] == "The Sage"  # identity stays canonical

    # marker stamped → later calls are no-ops even with the flag reset
    assert fake.data["persona_store_migrated"]
    monkeypatch.setattr(personas, "_migration_checked", False)
    assert personas.migrate_persona_store() == 0


def test_existing_spec_wins_over_blob(persona_fs, monkeypatch):
    from brain import personas

    personas.upsert("my_character", {"display_name": "API Truth", "baseline": {"DA": 0.2}})
    _wire(monkeypatch, {"My Character": {"custom": True, "chem": {"DA": 0.9}}})
    assert personas.migrate_persona_store() == 0
    spec = personas.read_spec("my_character")
    assert spec["display_name"] == "API Truth" and spec["baseline"]["DA"] == 0.2


def test_bad_blob_and_bad_entries_do_not_block(persona_fs, monkeypatch):
    from brain import personas

    fake = _wire(monkeypatch, "not json {{{")
    assert personas.migrate_persona_store() == 0
    assert fake.data["persona_store_migrated"]  # still marked done

    _wire(
        monkeypatch,
        {
            "Fine One": {"custom": True, "chem": {"DA": 0.5}},
            "Bad One": "not-a-dict",
        },
    )
    assert personas.migrate_persona_store() == 1
    assert personas.read_spec("fine_one") is not None


def test_marker_short_circuits(persona_fs, monkeypatch):
    from brain import personas

    _wire(
        monkeypatch,
        {"My Character": {"custom": True, "chem": {"DA": 0.7}}},
        extra={"persona_store_migrated": "2026-08-30T00:00:00+00:00"},
    )
    assert personas.migrate_persona_store() == 0
    assert personas.read_spec("my_character") is None
