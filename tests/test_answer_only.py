"""
Answer-only turns — the brain-side enforcement the 2026-07-03 debate exposed.

A turn declared answer_only (API session/turn option) or run by an agent whose
permissions mark it answer_only is pure Q&A: requires_action is neutralized
(no motor dispatch → no muscle-memory open-loop) and FollowThrough never
enqueues. Opt-in and declared, never inferred: unflagged turns/agents are
byte-for-byte unchanged. Alongside it, FollowThrough's reactive commitments now
enqueue as source="commitment" — subject to the autonomy rate caps, spend gate,
and self-style dedup instead of masquerading as user-awaited work.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import agents
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.session_turn import _effective_answer_only
from brain.turn_ctx import bind_turn, current_turn

# ── turn context carries the declaration ──────────────────────────────────────


def test_bind_turn_carries_answer_only_and_defaults_false():
    assert current_turn()["answer_only"] is False  # owner lane: autonomy untouched
    with bind_turn("agent", session_id="s1", answer_only=True):
        assert current_turn()["answer_only"] is True
    assert current_turn()["answer_only"] is False  # reset with the turn


# ── effective-flag resolution (turn declaration OR agent permission) ──────────


def test_effective_answer_only_from_turn_ctx():
    with bind_turn("agent", session_id="s1", answer_only=True):
        assert _effective_answer_only({}) is True
    assert _effective_answer_only({}) is False


def test_effective_answer_only_from_agent_permission(monkeypatch):
    monkeypatch.setattr(agents, "answer_only", lambda aid: aid == "the_visionary.trading_bull")
    assert _effective_answer_only({"agent_id": "the_visionary.trading_bull"}) is True
    assert _effective_answer_only({"agent_id": "the_visionary.research_lead"}) is False
    assert _effective_answer_only({}) is False  # owner turns never consult the store


def test_effective_answer_only_fails_open(monkeypatch):
    def _boom(aid):
        raise RuntimeError("store down")

    monkeypatch.setattr(agents, "answer_only", _boom)
    # A store hiccup must not silence a normal agent's tools.
    assert _effective_answer_only({"agent_id": "p.m"}) is False


# ── agent permission key ───────────────────────────────────────────────────────


def test_answer_only_is_a_known_permission_key():
    # set_permissions strips unknown keys — answer_only must survive the clean.
    assert "answer_only" in agents.PERMISSION_KEYS
    assert agents._clean_permissions({"answer_only": True, "bogus": 1}) == {"answer_only": True}


def test_agents_answer_only_reads_truthy_and_caches(monkeypatch):
    calls = []

    def _perms(aid):
        calls.append(aid)
        return {"answer_only": "true"}

    monkeypatch.setattr(agents, "permissions", _perms)
    agents._answer_only_cache.clear()
    assert agents.answer_only("p.m") is True
    assert agents.answer_only("p.m") is True  # second read served from the TTL cache
    assert calls == ["p.m"]  # hot turn path: one Supabase round-trip, not one per turn
    agents._answer_only_cache.clear()


def test_agents_answer_only_fails_open(monkeypatch):
    def _boom(aid):
        raise RuntimeError("no store")

    monkeypatch.setattr(agents, "permissions", _boom)
    agents._answer_only_cache.clear()
    assert agents.answer_only("p.m") is False
    agents._answer_only_cache.clear()


# ── API surface: session-sticky flag + per-turn override ──────────────────────


class _CtxRunner:
    """Records the bound turn context at call time (what the brain would see)."""

    def __init__(self):
        self.seen: list[bool] = []

    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        self.seen.append(current_turn()["answer_only"])
        return f"echo: {message}", {"emotion": "neutral"}


def _client(runner):
    registry = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    app = FastAPI()
    app.include_router(build_api_router(runner, registry, auth=lambda h: bool(h)))
    return TestClient(app)


_AUTH = {"Authorization": "Bearer sk_test"}


def test_session_answer_only_sticks_to_every_turn():
    runner = _CtxRunner()
    c = _client(runner)
    r = c.post("/v1/sessions", json={"end_user_id": "u1", "answer_only": True}, headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["answer_only"] is True
    sid = r.json()["session_id"]
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "round 1"}, headers=_AUTH)
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "round 2"}, headers=_AUTH)
    assert runner.seen == [True, True]


def test_turn_body_overrides_session_default_both_ways():
    runner = _CtxRunner()
    c = _client(runner)
    sid = c.post("/v1/sessions", json={"end_user_id": "u1"}, headers=_AUTH).json()["session_id"]
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "a"}, headers=_AUTH)
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "b", "answer_only": True}, headers=_AUTH)
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "c"}, headers=_AUTH)
    assert runner.seen == [False, True, False]  # per-turn, never sticky via the body


def test_answer_only_must_be_boolean():
    c = _client(_CtxRunner())
    r = c.post("/v1/sessions", json={"end_user_id": "u1", "answer_only": "yes"}, headers=_AUTH)
    assert r.status_code == 400
    sid = c.post("/v1/sessions", json={"end_user_id": "u1"}, headers=_AUTH).json()["session_id"]
    r2 = c.post(f"/v1/sessions/{sid}/turns", json={"message": "x", "answer_only": 1}, headers=_AUTH)
    assert r2.status_code == 400


def test_unflagged_session_is_unchanged():
    runner = _CtxRunner()
    c = _client(runner)
    r = c.post("/v1/sessions", json={"end_user_id": "u1"}, headers=_AUTH)
    assert r.json()["answer_only"] is False
    sid = r.json()["session_id"]
    c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=_AUTH)
    assert runner.seen == [False]


# ── the gate: requires_action neutralized, features stamped ──────────────────


def test_gate_neutralizes_requires_action_under_answer_only():
    # The exact transform _process_turn_body applies once _effective_answer_only
    # is true: stamp the turn and clear requires_action BEFORE frontal/motor read
    # it — no goal deposit, no motor planning, no open-loop.
    features = {"requires_action": True, "raw_text": "Round 2 (audit their claims)"}
    with bind_turn("agent", session_id="s", answer_only=True):
        assert _effective_answer_only(features) is True
    # unflagged: untouched
    assert _effective_answer_only(features) is False
    assert features["requires_action"] is True


# ── FollowThrough commitments: capped, gated, deduped ─────────────────────────


def test_commitment_source_dedups_like_self(tmp_path, monkeypatch):
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()
    t = q.enqueue("check AAPL price outlook against the latest data", source="commitment")
    assert t is not None
    q.mark_done(t.id, success=True)
    # A repetitive conversation re-extracts a near-identical goal next turn —
    # the self-style recency dedup must swallow it.
    dup = q.enqueue("check AAPL price outlook against latest data", source="commitment")
    assert dup is None


def test_user_source_still_bypasses_recency_dedup(tmp_path, monkeypatch):
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()
    t = q.enqueue("summarize the quarterly report", source="user")
    assert t is not None
    q.mark_done(t.id, success=True)
    # The user explicitly asking again IS a new request — no recency dedup.
    again = q.enqueue("summarize the quarterly report again please", source="user")
    assert again is not None


def test_commitment_jobs_do_not_bypass_rate_caps():
    # The bypass predicate motor uses at execute_internal_job entry: only true
    # user-awaited work skips the rolling-window/session caps and the spend gate.
    for source, awaited in (("user", True), ("commitment", False), ("self", False)):
        assert (source == "user") is awaited


# ── session persistence round-trips the flag (redeploy survival) ─────────────


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._f = {}

    def upsert(self, row, on_conflict=None):
        self._pending = row
        return self

    def select(self, *a):
        return self

    def eq(self, k, v):
        self._f[k] = v
        return self

    def execute(self):
        if getattr(self, "_pending", None):
            self._rows[(self._pending["org_id"], self._pending["session_id"])] = self._pending
            self._pending = None
            return type("R", (), {"data": []})()
        match = [
            r
            for r in self._rows.values()
            if r["org_id"] == self._f.get("org_id")
            and r["session_id"] == self._f.get("session_id")
        ]
        return type("R", (), {"data": match})()


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeTable(self._rows)


def test_answer_only_survives_redeploy(monkeypatch):
    from brain.second_brain import supabase_client

    rows: dict = {}
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _FakeClient(rows))
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")

    reg = ApiSessionRegistry(id_fn=lambda: "sess1")
    reg.create("cust-1", answer_only=True)
    assert rows[("org-1", "sess1")]["answer_only"] is True

    reg2 = ApiSessionRegistry()  # fresh memory = the redeploy case
    loaded = reg2.get("sess1")
    assert loaded is not None and loaded.answer_only is True
