"""Persona self-model Values are persona-authored, not the shared base list.

The base template's generic values ("Warmth as a default, not a feature") read
wrong on half the roster — the Adversary, Cynic, and Stoic are not
warmth-as-default temperaments. Every default persona now authors its own
Values; the base list remains only as a fallback for personas without one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.seed_persona_selfmd import BASE, P, compose, composed_docs


def test_every_default_persona_authors_values():
    missing = [name for name, spec in P.items() if not str(spec.get("values", "")).strip()]
    assert not missing, f"personas without authored values: {missing}"


def test_composed_docs_carry_persona_values_not_base_list():
    base_text = BASE.read_text(encoding="utf-8")
    for name, spec in P.items():
        doc = compose(name, base_text)
        values_section = doc.split("## Values")[1]
        first_bullet = str(spec["values"]).strip().splitlines()[0]
        assert first_bullet in values_section, f"{name} lost its authored values"
        # The base template's signature generic bullet must not leak through.
        assert "Warmth as a default, not a feature" not in values_section, name


def test_values_stay_distinct_across_personas():
    docs = composed_docs()
    values = {slug: doc.split("## Values")[1].strip() for slug, doc in docs.items()}
    assert len(set(values.values())) == len(values), "two personas share identical Values"
