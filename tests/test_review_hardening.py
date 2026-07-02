"""
Hardening pass from the 2026-07 holistic review.

Covers: the CMA mid-flight budget backstop (stop + defer signal + session kill),
loud unmetered-spend accounting, the agent_jobs boot reconcile (mirror split-brain
repair), the tightened boot-recovery cap (1 retry then quarantine), pending-approval
expiry, the bake-time tenant-root jail for motor dirs, and structured (non-silent)
criteria-checker / verifier outages in the motor job loop.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from brain.clusters.cma_executor import CMAExecutor
from brain.clusters.motor_cortex import MotorCortexCluster


# ── Fakes ──────────────────────────────────────────────────────────────────
class FakeRouter:
    def __init__(self, exhausted=False, bg=False, autonomous_usd=0.0):
        self._exhausted = exhausted
        self._bg_mode = bg
        self._usd = autonomous_usd
        self._bg_defer_reason = None
        self.unmetered_notes = 0

    def cloud_budget_exhausted(self):
        return self._exhausted

    def autonomous_usd_today(self):
        return self._usd

    def autonomous_soft_cleared(self):
        return False

    def note_unmetered_spend_suspected(self):
        self.unmetered_notes += 1


def _bare_cma(router) -> CMAExecutor:
    cma = CMAExecutor.__new__(CMAExecutor)
    cma._router = router
    cma._session_id = None
    cma._user_sessions = {}
    cma._client = None
    return cma


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    import brain.autonomy.budget as bud

    monkeypatch.setattr(
        bud,
        "_settings",
        type(
            "S",
            (),
            {
                "get": staticmethod(
                    lambda k, d=None: {
                        "autonomous_soft_usd": 30.0,
                        "autonomous_hard_usd": 50.0,
                    }.get(k, d)
                )
            },
        )(),
    )
    yield


# ── CMA mid-flight budget backstop ──────────────────────────────────────────
def test_budget_stop_none_when_under_budget():
    cma = _bare_cma(FakeRouter())

    async def _noop():
        return None

    cma._meter_session_usage = _noop
    assert asyncio.run(cma._budget_stop_check()) is None


def test_budget_stop_on_daily_cap():
    cma = _bare_cma(FakeRouter(exhausted=True))

    async def _noop():
        return None

    cma._meter_session_usage = _noop
    reason = asyncio.run(cma._budget_stop_check())
    assert reason and "daily cloud budget" in reason


def test_budget_stop_on_autonomous_hard_cap_sets_defer_signal():
    from brain.autonomy import DeferReason

    router = FakeRouter(bg=True, autonomous_usd=51.0)
    cma = _bare_cma(router)

    async def _noop():
        return None

    cma._meter_session_usage = _noop
    reason = asyncio.run(cma._budget_stop_check())
    assert reason and "hard spend cap" in reason
    # Background trip raises the router's one-shot defer so the motor checkpoints.
    assert router._bg_defer_reason is DeferReason.BUDGET_SOFT_PAUSE


def test_budget_stop_fails_open_when_check_breaks():
    cma = _bare_cma(FakeRouter(exhausted=True))

    async def _boom():
        raise RuntimeError("usage read failed")

    cma._meter_session_usage = _boom
    assert asyncio.run(cma._budget_stop_check()) is None  # never kills a healthy run


def test_stop_session_clears_warm_caches():
    class FakeSessions:
        def __init__(self):
            self.deleted = []

        async def delete(self, sid):
            self.deleted.append(sid)

    class FakeClient:
        def __init__(self):
            self.beta = type("B", (), {})()
            self.beta.sessions = FakeSessions()

    cma = _bare_cma(FakeRouter())
    cma._client = FakeClient()
    cma._session_id = "sid-1"
    cma._user_sessions = {"agent:vault": "sid-1", "other": "sid-2"}
    asyncio.run(cma._stop_session("sid-1"))
    assert cma._client.beta.sessions.deleted == ["sid-1"]
    assert cma._session_id is None
    assert cma._user_sessions == {"other": "sid-2"}


# ── Unmetered-spend accounting is loud ───────────────────────────────────────
def test_metering_failure_ticks_unmetered_counter():
    class BoomSessions:
        async def retrieve(self, sid):
            raise RuntimeError("api down")

    class FakeClient:
        def __init__(self):
            self.beta = type("B", (), {})()
            self.beta.sessions = BoomSessions()

    router = FakeRouter()
    cma = _bare_cma(router)
    cma._client = FakeClient()
    cma._active_sid = "sid-9"
    asyncio.run(cma._meter_session_usage())  # must not raise
    assert router.unmetered_notes == 1


def test_router_unmetered_counter_increments():
    from brain.model_router import ModelRouter

    r = ModelRouter.__new__(ModelRouter)
    assert r.unmetered_spend_suspected == 0
    r.note_unmetered_spend_suspected()
    r.note_unmetered_spend_suspected()
    assert r.unmetered_spend_suspected == 2


# ── agent_jobs boot reconcile (mirror split-brain repair) ────────────────────
class FakeJobStore:
    def __init__(self, records):
        self._records = records  # job_id -> full record

    def list_recent(self, limit=20):
        return [
            {"job_id": jid, "state": rec.get("state"), "success": rec.get("success")}
            for jid, rec in self._records.items()
        ][:limit]

    def get(self, job_id):
        return self._records.get(job_id)


def _fake_sb_read(monkeypatch, remote_rows):
    """Point agent_jobs_store._sb at a fake client whose read returns remote_rows."""
    from brain import agent_jobs_store as ajs

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def in_(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": self._rows})()

    class FakeClient:
        def __init__(self, rows):
            self._rows = rows

        def table(self, name):
            return FakeQuery(self._rows)

    monkeypatch.setattr(ajs, "_sb", lambda: (FakeClient(remote_rows), "org-1"))
    return ajs


def test_reconcile_upserts_missing_and_stale_rows(monkeypatch):
    ajs = _fake_sb_read(
        monkeypatch,
        [
            {"job_id": "done-ok", "state": "completed"},
            {"job_id": "stuck", "state": "running"},
        ],
    )
    upserted = []
    monkeypatch.setattr(ajs, "upsert", lambda rec: upserted.append(rec["job_id"]) or True)
    store = FakeJobStore(
        {
            "done-ok": {"job_id": "done-ok", "state": "completed", "success": True},
            "stuck": {"job_id": "stuck", "state": "completed", "success": True},
            "missing": {"job_id": "missing", "state": "failed", "success": False},
        }
    )
    fixed = ajs.reconcile(store)
    assert fixed == 2
    assert sorted(upserted) == ["missing", "stuck"]  # present+terminal row untouched


def test_reconcile_noop_in_local_mode(monkeypatch):
    from brain import agent_jobs_store as ajs

    monkeypatch.setattr(ajs, "_sb", lambda: None)
    assert ajs.reconcile(FakeJobStore({"j": {"job_id": "j"}})) == 0


# ── Boot recovery: 1 retry then quarantine ───────────────────────────────────
def test_default_recovery_cap_is_one():
    import brain.clusters.task_queue as tq

    if os.environ.get("BRAIN_MAX_JOB_RECOVERIES"):
        pytest.skip("recovery cap overridden in this environment")
    assert tq.MAX_RECOVERY_ATTEMPTS == 1


def test_second_recovery_quarantines(monkeypatch, tmp_path):
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    monkeypatch.setattr(tq, "MAX_RECOVERY_ATTEMPTS", 1)
    q = tq.PersistentTaskQueue()
    q._tasks = [
        tq.Task(id="fresh", goal="g1", status="running", recovery_count=0),
        tq.Task(id="poison", goal="g2", status="running", recovery_count=1),
    ]
    recovered = q.recover_interrupted()
    assert [t.id for t in recovered] == ["fresh"]
    poison = next(t for t in q._tasks if t.id == "poison")
    assert poison.status == "failed" and poison.success is False


# ── Pending approvals expire ─────────────────────────────────────────────────
def test_stale_pending_approval_expires(monkeypatch, tmp_path):
    import brain.clusters.approvals as mod

    monkeypatch.setattr(mod, "APPROVALS_PATH", tmp_path / "approvals.json")
    pa = mod.PendingApprovals()
    item = pa.record("send_email", {"to": "x@y.z"}, reason="r")
    item.created_at = time.time() - mod.PENDING_TTL_S - 60  # backdate past the TTL
    assert pa.pending() == []  # auto-skipped, not shown
    stored = next(a for a in pa._items if a.id == item.id)
    assert stored.status == "skipped"


def test_expired_twin_does_not_dedupe_block_fresh_request(monkeypatch, tmp_path):
    import brain.clusters.approvals as mod

    monkeypatch.setattr(mod, "APPROVALS_PATH", tmp_path / "approvals.json")
    pa = mod.PendingApprovals()
    old = pa.record("send_email", {"to": "x@y.z"})
    old.created_at = time.time() - mod.PENDING_TTL_S - 60
    fresh = pa.record("send_email", {"to": "x@y.z"})  # same signature, after expiry
    assert fresh.id != old.id
    assert [a["id"] for a in pa.pending()] == [fresh.id]


def test_live_pending_approval_is_kept(monkeypatch, tmp_path):
    import brain.clusters.approvals as mod

    monkeypatch.setattr(mod, "APPROVALS_PATH", tmp_path / "approvals.json")
    pa = mod.PendingApprovals()
    item = pa.record("send_email", {"to": "x@y.z"})
    assert [a["id"] for a in pa.pending()] == [item.id]


# ── Bake-time tenant-root jail (symlink TOCTOU) ──────────────────────────────
def test_jail_keeps_inside_and_drops_symlink_escape(monkeypatch, tmp_path):
    from brain import security

    root = tmp_path / "tenant"
    root.mkdir()
    (root / "workdir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside)  # inside-the-root path that RESOLVES outside
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(root / "settings.json"))
    kept = security.jail_dirs_to_tenant_root(
        [str(root / "workdir"), str(escape), "/etc"], label="test"
    )
    assert kept == [str(root / "workdir")]


def test_jail_fails_closed_without_tenant_root(monkeypatch, tmp_path):
    from brain import security

    monkeypatch.delenv("BRAIN_SETTINGS_PATH", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_PATH", raising=False)
    assert security.jail_dirs_to_tenant_root([str(tmp_path)], label="test") == []


# ── Structured checker/verifier outages (no silent green-light) ──────────────
class _FakeCell:
    def __init__(self, raw):
        self._raw = raw
        self.calls = 0

    def reset_turn(self, turn_id):
        pass

    async def call(self, messages):
        self.calls += 1
        return self._raw


def _bare_motor() -> MotorCortexCluster:
    return MotorCortexCluster.__new__(MotorCortexCluster)


def test_criteria_checker_outage_reports_unchecked():
    m = _bare_motor()
    m._criteria_checker = _FakeCell("")  # cell failure → empty string
    story = {"description": "d", "acceptance_criteria": ["c1"]}
    verified, unmet, checked = asyncio.run(m._check_story_criteria(story, "out", "t1"))
    assert verified is False and checked is False
    assert unmet and "unavailable" in unmet[0]


def test_criteria_checker_real_verdict_passes_through():
    m = _bare_motor()
    m._criteria_checker = _FakeCell('{"verified": true, "unmet": []}')
    story = {"description": "d", "acceptance_criteria": ["c1"]}
    verified, unmet, checked = asyncio.run(m._check_story_criteria(story, "out", "t1"))
    assert verified is True and checked is True and unmet == []


def test_no_criteria_path_checks_error_status():
    m = _bare_motor()
    ok, unmet, checked = asyncio.run(m._check_story_criteria({}, "fine output", "t1"))
    assert ok is True and checked is True
    bad, _, _ = asyncio.run(m._check_story_criteria({}, "[error] nope", "t1"))
    assert bad is False


def test_verifier_outage_is_not_approval():
    m = _bare_motor()
    m._verifier = _FakeCell("")
    approved, issues = asyncio.run(m._verify_job("g", "crit", [], [], "j1"))
    assert approved is False
    assert "unavailable" in issues


# ── Canonical persona slug (one implementation, per-site fallbacks) ──────────
def test_persona_slug_canonical():
    from brain.persona_key import persona_slug

    assert persona_slug("The Visionary") == "the_visionary"
    assert persona_slug("the_visionary") == "the_visionary"  # idempotent
    assert persona_slug("  Tortured-Artist! ") == "tortured_artist"
    assert persona_slug("") == "" and persona_slug(None) == ""
    assert persona_slug("", "default") == "default"
    assert persona_slug(None, "unnamed") == "unnamed"


def test_persona_slug_call_sites_agree():
    # The historical bug class: hosted raw display name vs local slug forking one
    # persona into two stores. Every wrapper must produce the same key.
    from brain.persona_chem import _slug as chem_slug
    from brain.provisioner import _persona_slug as prov_slug
    from brain.second_brain.store import _persona_key as store_key

    for raw in ("The Visionary", "the_visionary"):
        assert chem_slug(raw) == prov_slug(raw) == store_key(raw) == "the_visionary"
