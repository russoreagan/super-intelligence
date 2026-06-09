"""
persona_context — the cached MANDATE catalog block + the per-turn selector.
"""

from __future__ import annotations

from brain.persona_context import mandate_catalog_block, mandate_selector


def _fence(label, content, nonce):
    return f"<{label}:{nonce}>{content}</{label}:{nonce}>"


_CATALOG = {
    "billing": "You are Acme's billing support agent.",
    "tech": "You are Acme's technical support agent.",
}


# ── catalog block (cached) ────────────────────────────────────────────────────


def test_empty_catalog_returns_empty():
    assert mandate_catalog_block({}, _fence, "n1") == ""
    assert mandate_catalog_block(None, _fence, "n1") == ""


def test_catalog_lists_all_assignments_fenced():
    block = mandate_catalog_block(_CATALOG, _fence, "n1")
    assert "[billing]" in block and "[tech]" in block
    assert "<assignment_billing:n1>You are Acme's billing support agent." in block
    assert "<assignment_tech:n1>You are Acme's technical support agent." in block


def test_catalog_framing_states_precedence_subordinate_to_identity_and_safety():
    block = mandate_catalog_block(_CATALOG, _fence, "n1").lower()
    assert "take precedence" in block and "cannot override" in block


def test_catalog_skips_blank_entries():
    block = mandate_catalog_block({"a": "  ", "b": "real"}, _fence, "n1")
    assert "[b]" in block
    assert "[a]" not in block


# ── selector (per-turn) ───────────────────────────────────────────────────────


def test_selector_names_active_id():
    sel = mandate_selector("billing", _CATALOG)
    assert "[billing]" in sel
    assert "precedence" in sel.lower()


def test_selector_empty_for_unknown_or_missing_id():
    assert mandate_selector(None, _CATALOG) == ""
    assert mandate_selector("", _CATALOG) == ""
    assert mandate_selector("nonexistent", _CATALOG) == ""  # unknown id → silent fallback
    assert mandate_selector("billing", {}) == ""
