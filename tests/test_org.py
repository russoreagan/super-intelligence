"""
Org-tenancy resolution — org_id_for_user / is_member, with a fake Supabase client.
"""

from __future__ import annotations

import types

from brain.org import is_member, org_id_for_user


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
