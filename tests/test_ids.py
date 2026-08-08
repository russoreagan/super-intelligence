"""
Identifier validation, and the filename collision it was hiding.

`end_user_id` is the only identifier in the API supplied wholesale by an outside
caller, and it was the least validated: non-empty string, nothing else — while every
sibling id (persona, mandate, skill) was regex-checked. It reaches an LLM prompt, a
Supabase Vault name, a SQL predicate and a derived filename.
"""

from __future__ import annotations

import pytest

from brain import ids
from brain.second_brain.store import SchemaStore


# ── end_user_id ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "u_8821",
        "user@example.com",
        "9f2c1ab7-d4e0-5b63-9f2c-1ab7d4e05b63",
        "Customer.Name+tag",
        "a",
        "x" * 128,
    ],
)
def test_accepts_realistic_partner_ids(value):
    """Deliberately wider than a slug: emails and UUIDs are what partners actually
    key their customers on."""
    assert ids.valid_end_user_id(value) == value


@pytest.mark.parametrize(
    "value,why",
    [
        ("with space", "whitespace"),
        ("line\nbreak", "newline forges structure in tool_log.md, read back as prompt context"),
        ("has:colon", "vault names concatenate on ':' (migration 012)"),
        ("quote'inject", "LanceDB predicates are built by concatenation"),
        ("pct%wild", "SQL wildcard"),
        ("../escape", "path traversal"),
        ("back\\slash", "path separator"),
        ("", "empty"),
        ("x" * 129, "unbounded length reaches vault names and log lines"),
        (None, "missing"),
    ],
)
def test_rejects_injection_shaped_ids(value, why):
    with pytest.raises(ValueError):
        ids.valid_end_user_id(value)


def test_rejects_rather_than_sanitises():
    """Normalising would silently merge two distinct customers onto one identity —
    their memory, chemistry and connector tokens — which is worse than a 400."""
    with pytest.raises(ValueError):
        ids.valid_end_user_id("a:b")


def test_surrounding_whitespace_is_trimmed_not_rejected():
    assert ids.valid_end_user_id("  u_1  ") == "u_1"


# ── shared shapes ───────────────────────────────────────────────────────────
def test_sibling_modules_share_one_definition():
    """mandates and skills had byte-identical validators; personas had its own copy."""
    from brain import mandates, personas, skills_registry

    assert mandates.MANDATE_ID_RE is ids.ID_RE
    assert skills_registry.SKILL_ID_RE is ids.ID_RE
    assert personas.PERSONA_SLUG_RE is ids.SLUG_RE


def test_shared_shapes_still_enforce_their_own_exceptions():
    """One regex, but each module keeps its own error type so HTTP mapping is
    unchanged."""
    from brain import mandates, personas, skills_registry

    with pytest.raises(mandates.MandateError):
        mandates._valid_id("Bad Id")
    with pytest.raises(skills_registry.SkillError):
        skills_registry._valid_id("Bad Id")
    with pytest.raises(personas.PersonaError):
        personas.valid_slug("Bad Slug")


def test_persona_slugs_reject_dashes():
    """agent_id splits on the first dot and the slug is a directory name, so persona
    slugs are narrower than mandate/skill ids."""
    assert ids.SLUG_RE.match("captain_ahab")
    assert not ids.SLUG_RE.match("captain-ahab")
    assert ids.ID_RE.match("research-lead")


# ── speaker filename collisions ─────────────────────────────────────────────
# The slug folds every non-alphanumeric run to "_" and truncates to 32 chars, so on
# its own it mapped different customers onto ONE profile document — they read each
# other's personal facts and preferences.


def _fn(name: str) -> str:
    return SchemaStore(persona="").speaker_filename(name)


def test_punctuation_variants_do_not_collide():
    assert _fn("user@a.com") != _fn("user_a_com")


def test_long_ids_sharing_a_prefix_do_not_collide():
    a = "customer_" + "x" * 40 + "_alpha"
    b = "customer_" + "x" * 40 + "_beta"
    assert a[:32] == b[:32], "precondition: identical within the truncation window"
    assert _fn(a) != _fn(b)


def test_case_variants_do_not_collide():
    assert _fn("Alice") != _fn("alice_2")


def test_is_deterministic():
    assert _fn("user@a.com") == _fn("user@a.com")


def test_stays_a_legal_schema_filename():
    """_validate_filename silently returns "" on a mismatch, so a filename that fails
    its regex would disable the profile rather than error."""
    assert SchemaStore._FILENAME_RE.match(_fn("user@a.com"))
    assert SchemaStore._FILENAME_RE.match(_fn(""))


def test_keeps_a_human_readable_prefix():
    assert _fn("alice").startswith("user_alice_")
