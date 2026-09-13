"""Persona capacity (plan §3.7): the 10000 default and the indexed cap check.

`custom_count()` is what the clone route compares against `max_personas`. With
the persona index answering it is one head count; without it (local mode, the
table missing, the kill switch) it stays the on-disk spec scan.
"""

from __future__ import annotations

import json

import pytest

from brain import persona_index, personas


@pytest.fixture
def fs(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_MAX_PERSONAS", raising=False)
    monkeypatch.delenv("BRAIN_MAX_TENANTS", raising=False)
    for slug in ("ahab", "ahab_p1", "ahab_p2"):
        d = tmp_path / "personas" / slug
        d.mkdir(parents=True)
        (d / "persona.json").write_text(json.dumps({"slug": slug, "display_name": slug}))
    return tmp_path


def test_max_personas_defaults_to_10000(fs, monkeypatch):
    from brain import persona_placement

    monkeypatch.setattr(persona_placement, "effective_max_dedicated", lambda: 3)
    limits = personas.capacity_limits()
    assert limits["max_personas"] == 10000
    assert limits["max_live_brains"] == 25
    monkeypatch.setenv("BRAIN_MAX_PERSONAS", "0")
    assert personas.capacity_limits()["max_personas"] == 0  # 0 = uncapped


def test_custom_count_uses_the_index_when_it_answers(fs, monkeypatch):
    scanned = []
    orig = personas._read_all_specs

    def _scan():
        scanned.append(1)
        return orig()

    monkeypatch.setattr(personas, "_read_all_specs", _scan)
    monkeypatch.setattr(persona_index, "count_custom", lambda: 7)
    assert personas.custom_count() == 7
    assert scanned == []  # no volume walk when the index answered


def test_custom_count_falls_back_to_the_spec_scan(fs, monkeypatch):
    # Index disabled outright (no Supabase in this fixture): the real count_custom
    # answers None and the volume is counted.
    assert persona_index.count_custom() is None
    assert personas.custom_count() == 3
    assert personas.custom_count_on_disk() == 3
    monkeypatch.setattr(persona_index, "count_custom", lambda: None)
    assert personas.custom_count() == 3
    # The disk count never consults the index (the boot reconcile relies on it).
    monkeypatch.setattr(persona_index, "count_custom", lambda: 99)
    assert personas.custom_count() == 99 and personas.custom_count_on_disk() == 3


def test_custom_count_survives_an_index_error(fs, monkeypatch):
    def _boom():
        raise RuntimeError("index down")

    monkeypatch.setattr(persona_index, "count_custom", _boom)
    assert personas.custom_count() == 3
