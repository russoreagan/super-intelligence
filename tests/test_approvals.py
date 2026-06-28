"""Offline tests for the PendingApprovals ledger (record → approve/skip → resume)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    import brain.clusters.approvals as mod

    importlib.reload(mod)
    monkeypatch.setattr(mod, "APPROVALS_PATH", tmp_path / "approvals.json")
    return mod.PendingApprovals()


def test_record_and_list_pending(approvals):
    a = approvals.record("send_email", {"to": "x@y.com"}, reason="would send communication")
    assert a.status == "pending"
    pend = approvals.pending()
    assert len(pend) == 1 and pend[0]["tool"] == "send_email"
    assert "x@y.com" in pend[0]["preview"]


def test_record_dedupes_same_action(approvals):
    a1 = approvals.record("write_file", {"path": "a.py", "content": "x"})
    a2 = approvals.record("write_file", {"path": "a.py", "content": "x"})
    assert a1.id == a2.id
    assert len(approvals.pending()) == 1


def test_approve_then_consume_resumes_once(approvals):
    a = approvals.record("delete_journal", {"id": 7})
    assert approvals.approve(a.id) is not None
    assert approvals.pending() == []
    # Same action is now pre-approved exactly once...
    assert approvals.is_approved("delete_journal", {"id": 7}) is True
    # ...and the approval is consumed — a replay is not allowed.
    assert approvals.is_approved("delete_journal", {"id": 7}) is False


def test_skip_removes_from_pending_without_approval(approvals):
    a = approvals.record("bash", {"command": "rm -rf /"})
    assert approvals.skip(a.id) is True
    assert approvals.pending() == []
    assert approvals.is_approved("bash", {"command": "rm -rf /"}) is False


def test_unapproved_action_is_not_allowed(approvals):
    approvals.record("post_briefing", {"content": "hi"})
    assert approvals.is_approved("post_briefing", {"content": "hi"}) is False


def test_persists_across_instances(approvals, tmp_path, monkeypatch):
    import brain.clusters.approvals as mod

    approvals.record("edit", {"path": "x.py"}, reason="edits existing content")
    # New instance reads the same file.
    again = mod.PendingApprovals()
    assert len(again.pending()) == 1 and again.pending()[0]["tool"] == "edit"


def test_end_user_scoping_isolates_tenants(approvals):
    a_alice = approvals.record("send_email", {"to": "1"}, end_user_id="alice")
    a_bob = approvals.record("send_email", {"to": "2"}, end_user_id="bob")
    # Each end-user sees only their own; the owner (None) sees both.
    assert [p["id"] for p in approvals.pending("alice")] == [a_alice.id]
    assert [p["id"] for p in approvals.pending("bob")] == [a_bob.id]
    assert len(approvals.pending()) == 2
    # Bob cannot approve Alice's action; Alice can.
    assert approvals.approve(a_alice.id, end_user_id="bob") is None
    assert approvals.approve(a_alice.id, end_user_id="alice") is not None
    # Bob cannot skip Alice's (already approved) item, and scoping holds for skip.
    assert approvals.skip(a_bob.id, end_user_id="alice") is False
    assert approvals.skip(a_bob.id, end_user_id="bob") is True


def test_owner_can_resolve_any_end_user(approvals):
    a = approvals.record("delete_x", {}, end_user_id="carol")
    # end_user_id=None (owner / brain UI) is unscoped.
    assert approvals.approve(a.id, end_user_id=None) is not None


def test_include_autonomous_surfaces_owner_lane_to_a_scoped_query(approvals):
    # The brain queues an action while unattended (no engine end-user → "" lane)...
    auto = approvals.record("send_briefing", {"content": "hi"}, end_user_id="")
    # ...plus an action raised inside the trading app's own session.
    own = approvals.record("place_order", {"symbol": "AAPL"}, end_user_id="russ:trading")

    # A plain scoped query sees only its own item — the away/autonomous one is hidden,
    # which is exactly why it only showed up in the owner UI before.
    assert [p["id"] for p in approvals.pending("russ:trading")] == [own.id]

    # With include_autonomous, the owner-key tenant query sees BOTH its own and the
    # autonomous lane, but still NOT another end-user's items.
    approvals.record("send_email", {"to": "x"}, end_user_id="someone:chat")
    visible = {p["id"] for p in approvals.pending("russ:trading", include_autonomous=True)}
    assert visible == {own.id, auto.id}

    # And it can resolve the autonomous-lane item, which a plain scoped resolve cannot.
    assert approvals.approve(auto.id, end_user_id="russ:trading") is None
    assert approvals.approve(auto.id, end_user_id="russ:trading", include_autonomous=True) is not None


def test_approved_expires_after_ttl(approvals, monkeypatch):
    import brain.clusters.approvals as mod

    a = approvals.record("send_dm", {"to": "bob"})
    approvals.approve(a.id)
    # Force the approval to look stale.
    for it in approvals._items:
        it.resolved_at = 1.0
    monkeypatch.setattr(mod.time, "time", lambda: 1.0 + mod.APPROVAL_TTL_S + 1)
    assert approvals.is_approved("send_dm", {"to": "bob"}) is False
