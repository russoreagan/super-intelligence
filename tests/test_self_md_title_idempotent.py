"""
self.md title + routing: a persona write SETS "# Self-Model — <name>" (never
appends) and lands in that persona's own self.md, never in the shared
second_brain/schema/self.md scaffold every persona is composed from.

Regression for 2026-10: brain/run.py's load_dotenv(override=True) flipped
BRAIN_STORAGE_BACKEND to supabase after store.py had frozen its backend as
local. personas re-read the env, handed self.md to a (local, persona-blind)
SchemaStore, and every PUT wrote over the scaffold — whose title then grew a
" — home_p — ishmael — ahab" suffix per test run.
"""

from __future__ import annotations

import shutil

import pytest

from brain import personas
from brain.persona_key import persona_state_root

_SLUGS = ("ahab", "ishmael", "home_p")


@pytest.fixture()
def scaffold(tmp_path, monkeypatch):
    """A tmp copy of the real scaffold, wired in as BOTH the compose source and
    the local SchemaStore's schema dir — so a leak shows up here instead of
    corrupting the tracked file."""
    from brain import persona_chem
    from brain.second_brain import store

    schema_dir = tmp_path / "shared_schema"
    schema_dir.mkdir()
    base = schema_dir / "self.md"
    shutil.copyfile(personas._BASE_SELF_MD, base)
    monkeypatch.setattr(personas, "_BASE_SELF_MD", base)
    monkeypatch.setattr(store, "SCHEMA_DIR", schema_dir)
    monkeypatch.setattr(store, "_STORAGE_BACKEND", "local")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "root"))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    return base


def _title(text: str) -> str:
    return text.splitlines()[0]


def test_compose_sets_the_title_once_even_from_a_suffixed_scaffold(scaffold):
    original = scaffold.read_text()
    scaffold.write_text(
        original.replace("# Self-Model", "# Self-Model — home_p — ishmael — ahab — home_p", 1)
    )
    for slug in _SLUGS:
        spec = {"slug": slug, "display_name": slug.title()}
        once = personas.compose_self_md(spec)
        assert _title(once) == f"# Self-Model — {slug.title()}"
        assert personas.compose_self_md(spec) == once


def test_compose_of_a_clean_scaffold_is_unchanged_output(scaffold):
    # persona_audit treats any drift from compose_self_md as learned state, so the
    # idempotent title must not change what a clean scaffold composes to.
    text = personas.compose_self_md({"slug": "ahab", "display_name": "Ahab"})
    assert _title(text) == "# Self-Model — Ahab"
    assert text.count("# Self-Model") == 1


def test_repeated_writes_for_several_personas_leave_titles_and_scaffold_alone(
    scaffold, monkeypatch
):
    # The leak's precondition: the env says supabase, SchemaStore is local.
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "supabase")
    before = scaffold.read_text()
    for _ in range(2):
        for slug in _SLUGS:
            personas.upsert(slug, {"display_name": slug, "disposition": f"I am {slug}."})

    assert scaffold.read_text() == before, "a persona write reached the shared scaffold"
    for slug in _SLUGS:
        own = (persona_state_root(slug) / "schema" / "self.md").read_text()
        assert _title(own) == f"# Self-Model — {slug}"
        assert f"I am {slug}." in own


def test_a_route_onto_the_scaffold_itself_is_refused(scaffold, monkeypatch):
    # Bare local run: the home persona's state root IS the scaffold's root.
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(scaffold.parent.parent))
    scaffold.parent.rename(scaffold.parent.parent / "schema")
    moved = scaffold.parent.parent / "schema" / "self.md"
    monkeypatch.setattr(personas, "_BASE_SELF_MD", moved)
    before = moved.read_text()
    personas.upsert("home_p", {"display_name": "home_p", "disposition": "Home."})
    assert moved.read_text() == before
