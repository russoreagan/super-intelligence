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
    assert (
        approvals.approve(auto.id, end_user_id="russ:trading", include_autonomous=True) is not None
    )


def test_approved_expires_after_ttl(approvals, monkeypatch):
    import brain.clusters.approvals as mod

    a = approvals.record("send_dm", {"to": "bob"})
    approvals.approve(a.id)
    # Force the approval to look stale.
    for it in approvals._items:
        it.resolved_at = 1.0
    monkeypatch.setattr(mod.time, "time", lambda: 1.0 + mod.APPROVAL_TTL_S + 1)
    assert approvals.is_approved("send_dm", {"to": "bob"}) is False


# ── Job-scope grants (one approval clears the whole task) ─────────────────────


def test_grant_token_valid_nonconsuming_and_revocable(approvals):
    tok = approvals.grant_for("task_ab12")
    # Non-consuming: valid any number of times while the job runs.
    assert approvals.token_valid(tok) is True
    assert approvals.token_valid(tok) is True
    # Never shows up as a pending card.
    assert approvals.pending() == []
    approvals.revoke_token(tok)
    assert approvals.token_valid(tok) is False


def test_grant_token_expires_after_ttl(approvals, monkeypatch):
    import time as _time

    import brain.clusters.approvals as mod

    tok = approvals.grant_for("task_ab12")
    real = _time.time()
    monkeypatch.setattr(mod.time, "time", lambda: real + mod.GRANT_TTL_S + 1)
    assert approvals.token_valid(tok) is False


def test_unknown_or_empty_token_is_invalid(approvals):
    assert approvals.token_valid("") is False
    assert approvals.token_valid("nope") is False


def test_resolve_siblings_settles_same_job_pending_only(approvals):
    kept = approvals.record("cloud_write", {"task": "step 1"}, turn_id="task_j1")
    sib = approvals.record("cloud_write", {"task": "step 2"}, turn_id="task_j1")
    other_job = approvals.record("cloud_write", {"task": "elsewhere"}, turn_id="task_j2")
    approvals.approve(kept.id)
    resolved = approvals.resolve_siblings("task_j1", exclude_id=kept.id)
    assert resolved == [sib.id]
    # The other job's card is untouched; this job's siblings are gone.
    pend_ids = [p["id"] for p in approvals.pending()]
    assert pend_ids == [other_job.id]


def test_grant_survives_restart(approvals, tmp_path, monkeypatch):
    import brain.clusters.approvals as mod

    tok = approvals.grant_for("task_j1")
    monkeypatch.setattr(mod, "APPROVALS_PATH", tmp_path / "approvals.json")
    fresh = mod.PendingApprovals()
    assert fresh.token_valid(tok) is True


# ── approve_action → job grant → _gate_action (session-level flow) ────────────


class _FakeQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, goal, source="user", priority=1, reflex_depth=0, approval_token=""):
        self.enqueued.append({"goal": goal, "approval_token": approval_token})
        return object()  # non-None = accepted


def _make_loops_stub(approvals):
    from brain.session_loops import _LoopsMixin

    class _Stub(_LoopsMixin):
        def __init__(self):
            self._approvals = approvals
            self._task_queue = _FakeQueue()
            self._emitter = None
            self._job_approval_token = ""

    return _Stub()


def test_one_approval_clears_the_whole_task(approvals):
    """The full ping-pong fix: approving ONE action from a job re-queues the work
    with a job-scope grant, settles the job's other pending cards, and the gate
    then allows EVERY ask the re-run raises — no per-write round-trips."""
    import asyncio

    stub = _make_loops_stub(approvals)
    # The job raised two writes before parking in awaiting_approval.
    a1 = approvals.record("cloud_write", {"task": "write report part 1"}, turn_id="task_j9")
    approvals.record("cloud_write", {"task": "write report part 2"}, turn_id="task_j9")

    out = stub.approve_action(a1.id)
    assert out["ok"] is True
    # Sibling card settled too — nothing left pending anywhere.
    assert approvals.pending() == []
    # The re-queued task carries a live grant.
    (queued,) = stub._task_queue.enqueued
    token = queued["approval_token"]
    assert approvals.token_valid(token) is True

    # The re-run holds the token: every ask is allowed, however it's phrased,
    # and the grant is NOT consumed between calls.
    stub._job_approval_token = token
    assert (
        asyncio.run(stub._gate_action({"tool": "cloud_write", "input": {"task": "x"}})) == "allow"
    )
    assert (
        asyncio.run(stub._gate_action({"tool": "cloud_write", "input": {"task": "y"}})) == "allow"
    )

    # Job ends → grant revoked → the next ask gates again.
    approvals.revoke_token(token)
    stub._job_approval_token = ""
    verdict = asyncio.run(stub._gate_action({"tool": "cloud_write", "input": {"task": "z"}}))
    assert verdict == "deny"
    assert len(approvals.pending()) == 1
