"""
persona_context — the cached MANDATE catalog block + the per-turn selector.
"""

from __future__ import annotations

from brain.persona_context import mandate_catalog_block, mandate_selector


def _fence(label, content, nonce):
    return f"<{label}:{nonce}>{content}</{label}:{nonce}>"


# Catalog entries are now {"text": ..., "conduct": ...} dicts (plain strings still
# accepted for backward compat with tests that don't exercise conduct rules).
_CATALOG = {
    "billing": {"text": "You are Acme's billing support agent.", "conduct": None},
    "tech": {"text": "You are Acme's technical support agent.", "conduct": None},
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
    cat = {"a": {"text": "  ", "conduct": None}, "b": {"text": "real", "conduct": None}}
    block = mandate_catalog_block(cat, _fence, "n1")
    assert "[b]" in block
    assert "[a]" not in block


# ── conduct rules ─────────────────────────────────────────────────────────────


def test_conduct_scalar_values_rendered_as_key_value_bullets():
    cat = {
        "support": {
            "text": "You are a support agent.",
            "conduct": {"tone": "Always respond formally.", "scope": "Billing topics only."},
        }
    }
    block = mandate_catalog_block(cat, _fence, "n1")
    assert "Conduct rules:" in block
    assert "• tone: Always respond formally." in block
    assert "• scope: Billing topics only." in block


def test_conduct_appears_after_role_text():
    cat = {
        "support": {
            "text": "You are a support agent.",
            "conduct": {"tone": "Be formal."},
        }
    }
    block = mandate_catalog_block(cat, _fence, "n1")
    role_pos = block.index("<assignment_support")
    conduct_pos = block.index("Conduct rules:")
    assert conduct_pos > role_pos


def test_conduct_list_values_exploded_without_key_label():
    cat = {
        "agent": {
            "text": "You help customers.",
            "conduct": {"rules": ["Be concise.", "Never discuss competitors."]},
        }
    }
    block = mandate_catalog_block(cat, _fence, "n1")
    assert "• Be concise." in block
    assert "• Never discuss competitors." in block
    assert "• rules:" not in block  # key not used as a bullet label when value is a list


def test_conduct_absent_when_none():
    block = mandate_catalog_block(_CATALOG, _fence, "n1")
    assert "Conduct rules:" not in block


def test_conduct_absent_when_empty_dict():
    cat = {"agent": {"text": "You help customers.", "conduct": {}}}
    block = mandate_catalog_block(cat, _fence, "n1")
    assert "Conduct rules:" not in block


def test_catalog_accepts_plain_string_values_backward_compat():
    """Plain string catalog values still render; no conduct section produced."""
    cat = {"legacy": "You are a legacy agent."}
    block = mandate_catalog_block(cat, _fence, "n1")
    assert "[legacy]" in block
    assert "You are a legacy agent." in block
    assert "Conduct rules:" not in block


# ── selector (per-turn) ───────────────────────────────────────────────────────


def test_selector_names_active_id():
    sel = mandate_selector("billing", _CATALOG)
    assert "[billing]" in sel
    assert "precedence" in sel.lower()


def test_selector_empty_for_unknown_or_missing_id():
    assert mandate_selector(None, _CATALOG) == ""
    assert mandate_selector("", _CATALOG) == ""
    assert mandate_selector("nonexistent", _CATALOG) == ""
    assert mandate_selector("billing", {}) == ""
