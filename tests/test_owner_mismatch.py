"""
owner_mismatch — the membership-aware tenant pin (BRAIN_ORG_ID).

Owner / personal-org caller (sub == org id) is the fast path (no DB). A multi-member
org defers to brain.org.is_member. Fail-closed on missing sub / lookup error.
"""

from __future__ import annotations

import brain.org as org
import brain.ui.auth as auth


def test_no_org_pin_is_noop(monkeypatch):
    monkeypatch.delenv("BRAIN_ORG_ID", raising=False)
    monkeypatch.delenv("BRAIN_USER_ID", raising=False)
    assert auth.owner_mismatch({"sub": "anyone"}) is False


def test_owner_sub_matches_org_fast_path(monkeypatch):
    monkeypatch.setenv("BRAIN_ORG_ID", "org1")
    # sub == org id short-circuits before any membership lookup
    monkeypatch.setattr(org, "is_member", lambda *a, **k: False)
    assert auth.owner_mismatch({"sub": "org1"}) is False


def test_member_of_org_allowed(monkeypatch):
    monkeypatch.setenv("BRAIN_ORG_ID", "org1")
    monkeypatch.setattr(org, "is_member", lambda sub, oid, **k: sub == "alice" and oid == "org1")
    assert auth.owner_mismatch({"sub": "alice"}) is False


def test_non_member_rejected(monkeypatch):
    monkeypatch.setenv("BRAIN_ORG_ID", "org1")
    monkeypatch.setattr(org, "is_member", lambda *a, **k: False)
    assert auth.owner_mismatch({"sub": "stranger"}) is True


def test_missing_sub_rejected(monkeypatch):
    monkeypatch.setenv("BRAIN_ORG_ID", "org1")
    assert auth.owner_mismatch({}) is True
    assert auth.owner_mismatch(None) is True


def test_lookup_error_fails_closed(monkeypatch):
    monkeypatch.setenv("BRAIN_ORG_ID", "org1")

    def _boom(*a, **k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(org, "is_member", _boom)
    assert auth.owner_mismatch({"sub": "someone"}) is True


def test_falls_back_to_brain_user_id(monkeypatch):
    """When only the legacy BRAIN_USER_ID is set, it's used as the org id."""
    monkeypatch.delenv("BRAIN_ORG_ID", raising=False)
    monkeypatch.setenv("BRAIN_USER_ID", "legacy-uid")
    assert auth.owner_mismatch({"sub": "legacy-uid"}) is False
    monkeypatch.setattr(org, "is_member", lambda *a, **k: False)
    assert auth.owner_mismatch({"sub": "other"}) is True
