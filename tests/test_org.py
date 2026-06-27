"""
Org-tenancy resolution — org_id_for_user / is_member, with a fake Supabase client.
"""

from __future__ import annotations

import types

import brain.org as org_mod
from brain.org import is_member, membership_role, org_id_for_user
from brain.ui.auth import is_org_admin


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._filters: dict = {}

    def select(self, *_a):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = [r for r in self._rows if all(r.get(k) == v for k, v in self._filters.items())]
        return types.SimpleNamespace(data=rows)


class _FakeClient:
    """Minimal supabase-py stand-in over an in-memory memberships list."""

    def __init__(self, memberships):
        self._m = memberships

    def table(self, name):
        return _FakeTable(self._m if name == "memberships" else [])


_PERSONAL = _FakeClient([{"user_id": "russ", "org_id": "russ", "role": "admin"}])
_PARTNER = _FakeClient(
    [
        {"user_id": "alice", "org_id": "acme", "role": "admin"},
        {"user_id": "bob", "org_id": "acme", "role": "member"},
    ]
)


def test_personal_org_id_equals_user_id():
    assert org_id_for_user("russ", client=_PERSONAL) == "russ"


def test_member_resolves_to_their_org():
    assert org_id_for_user("alice", client=_PARTNER) == "acme"
    assert org_id_for_user("bob", client=_PARTNER) == "acme"


def test_prefers_admin_membership_when_multiple():
    multi = _FakeClient(
        [
            {"user_id": "carol", "org_id": "org_member", "role": "member"},
            {"user_id": "carol", "org_id": "org_admin", "role": "admin"},
        ]
    )
    assert org_id_for_user("carol", client=multi) == "org_admin"


def test_no_membership_returns_none():
    assert org_id_for_user("nobody", client=_PARTNER) is None
    assert org_id_for_user("", client=_PARTNER) is None


def test_is_member_true_only_for_real_membership():
    assert is_member("bob", "acme", client=_PARTNER) is True
    assert is_member("bob", "other_org", client=_PARTNER) is False
    assert is_member("stranger", "acme", client=_PARTNER) is False
    assert is_member("bob", "", client=_PARTNER) is False


def test_failures_degrade_gracefully():
    class _Boom:
        def table(self, *_a):
            raise RuntimeError("supabase down")

    assert org_id_for_user("russ", client=_Boom()) is None
    assert is_member("russ", "russ", client=_Boom()) is False


def test_membership_role_resolves_and_fails_closed():
    assert membership_role("alice", "acme", client=_PARTNER) == "admin"
    assert membership_role("bob", "acme", client=_PARTNER) == "member"
    assert membership_role("bob", "other", client=_PARTNER) is None
    assert membership_role("stranger", "acme", client=_PARTNER) is None
    assert membership_role("bob", "", client=_PARTNER) is None

    class _Boom:
        def table(self, *_a):
            raise RuntimeError("supabase down")

    assert membership_role("alice", "acme", client=_Boom()) is None


# ── is_org_admin: tenant-scoped admin, distinct from the platform super-user ──
_ADMIN_CLAIMS = {"sub": "carol", "app_metadata": {"is_admin": True}}


def _pin_org(monkeypatch, org_id):
    monkeypatch.setenv("BRAIN_ORG_ID", org_id)
    monkeypatch.delenv("BRAIN_USER_ID", raising=False)
    monkeypatch.delenv("BRAIN_ADMIN_EMAILS", raising=False)


def test_org_admin_unpinned_is_true(monkeypatch):
    # No tenant pin (local/dev single-user) → permissive, matches owner_mismatch.
    monkeypatch.delenv("BRAIN_ORG_ID", raising=False)
    monkeypatch.delenv("BRAIN_USER_ID", raising=False)
    monkeypatch.delenv("BRAIN_ADMIN_EMAILS", raising=False)
    assert is_org_admin({"sub": "anyone"}) is True


def test_platform_admin_is_always_org_admin(monkeypatch):
    # A platform super-user administers any org its process is pinned to.
    _pin_org(monkeypatch, "someone_elses_org")
    assert is_org_admin(_ADMIN_CLAIMS) is True


def test_personal_org_owner_is_org_admin(monkeypatch):
    _pin_org(monkeypatch, "russ")
    assert is_org_admin({"sub": "russ"}) is True  # sub == org_id, no DB hit


def test_shared_org_admin_member_passes(monkeypatch):
    _pin_org(monkeypatch, "acme")
    monkeypatch.setattr(org_mod, "membership_role", lambda u, o: "admin")
    assert is_org_admin({"sub": "alice"}) is True


def test_shared_org_plain_member_denied(monkeypatch):
    _pin_org(monkeypatch, "acme")
    monkeypatch.setattr(org_mod, "membership_role", lambda u, o: "member")
    assert is_org_admin({"sub": "bob"}) is False


def test_pinned_org_no_sub_denied(monkeypatch):
    _pin_org(monkeypatch, "acme")
    assert is_org_admin({}) is False


def test_org_admin_lookup_failure_fails_closed(monkeypatch):
    _pin_org(monkeypatch, "acme")

    def _boom(_u, _o):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(org_mod, "membership_role", _boom)
    assert is_org_admin({"sub": "bob"}) is False
