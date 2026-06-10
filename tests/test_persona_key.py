"""
Persona key normalization — hosted (raw display name) and local (slug) must resolve
the schema/episode store to the SAME key, or memory splits (the "nice to meet you"
bug: real memory under 'the_visionary', hosted reading empty 'The Visionary').
"""

from __future__ import annotations

import brain.second_brain.store as store


def test_persona_key_slugifies_display_name():
    assert store._persona_key("The Visionary") == "the_visionary"


def test_persona_key_idempotent_on_slug():
    assert store._persona_key("the_visionary") == "the_visionary"
    assert store._persona_key("the_poet") == "the_poet"


def test_persona_key_empty_defaults():
    assert store._persona_key("") == "default"
    assert store._persona_key(None) == "default"


def _bypass(cls, persona=""):
    s = cls.__new__(cls)
    s._persona = persona
    return s


def test_sb_persona_slugifies_explicit_raw_name():
    s = _bypass(store.SchemaStore, "The Visionary")
    assert s._sb_persona() == "the_visionary"
    e = _bypass(store.EpisodicStore, "The Visionary")
    assert e._sb_persona() == "the_visionary"


def test_sb_persona_slugifies_env_name(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Visionary")
    assert _bypass(store.SchemaStore, "")._sb_persona() == "the_visionary"
    assert _bypass(store.EpisodicStore, "")._sb_persona() == "the_visionary"


def test_local_slug_unchanged():
    """The local companion (already slug) is idempotent — no regression."""
    assert _bypass(store.SchemaStore, "the_visionary")._sb_persona() == "the_visionary"
