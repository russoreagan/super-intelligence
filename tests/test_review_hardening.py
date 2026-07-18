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


# ── Canonical chemistry→effort curve (brain/budget.py) ───────────────────────
def _legacy_curve(chem, base, gain, lo, hi):
    if not chem:
        return base
    da = float(chem.get("DA", 0.5))
    cort = float(chem.get("CORT", 0.5))
    shift = (da - 0.5) * gain - (cort - 0.5) * gain
    return max(lo, min(hi, base + int(round(shift))))


def test_chem_budget_matches_legacy_motor_curves_exactly():
    from brain.budget import chem_budget

    grid = [x / 10 for x in range(0, 11)]
    for da in grid:
        for cort in grid:
            chem = {"DA": da, "CORT": cort}
            assert chem_budget(chem, base=3, gain=2.0, lo=1, hi=5) == _legacy_curve(
                chem, 3, 2.0, 1, 5
            )
            assert chem_budget(chem, base=12, gain=6.0, lo=6, hi=20) == _legacy_curve(
                chem, 12, 6.0, 6, 20
            )


def test_chem_budget_resting_and_empty():
    from brain.budget import chem_budget

    assert chem_budget({}, base=3, gain=2.0, lo=1, hi=5) == 3
    assert chem_budget(None, base=12, gain=6.0, lo=6, hi=20) == 12
    assert chem_budget({"DA": 0.5, "CORT": 0.5}, base=3, gain=2.0, lo=1, hi=5) == 3


def test_motor_budgets_use_canonical_curve():
    m = _bare_motor()
    hot = {"DA": 1.0, "CORT": 0.0}
    calm = {"DA": 0.0, "CORT": 1.0}
    assert m._effective_budget(hot) == 5 and m._effective_budget(calm) == 1
    assert m._effective_job_budget(hot) == 18 and m._effective_job_budget(calm) == 6


# ── Settings load: unknown keys warned, bad values don't nuke the rest ───────
def test_settings_load_warns_unknown_and_survives_bad_values(monkeypatch, tmp_path, caplog):
    import json
    import logging

    import brain.settings as st

    # A real numeric key from the schema, fed an uncoercible value.
    num_key = next(k for k, v in st.DEFAULTS.items() if isinstance(v, float))
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "persona_name": "The Analyst",  # valid
                num_key: "not-a-number",  # coercion fails
                "presona_name": "typo",  # unknown key
            }
        )
    )
    monkeypatch.setattr(st, "SETTINGS_PATH", p)
    with caplog.at_level(logging.WARNING):
        s = st.Settings()
    assert s.get("persona_name") == "The Analyst"  # good override survived
    assert s.get(num_key) == st.DEFAULTS[num_key]  # bad value fell back, not fatal
    text = caplog.text
    assert "presona_name" in text and "unknown key" in text
    assert num_key in text and "cannot coerce" in text


# ── Persona misattribution fix: temperament follows the BOUND persona ─────────
def test_active_or_home_persona_prefers_bound():
    from brain.persona_key import active_or_home_persona
    from brain.second_brain.store import bind_persona

    home = active_or_home_persona()  # settings fallback (whatever the process has)
    with bind_persona("the_adversary"):
        assert active_or_home_persona() == "the_adversary"
    assert active_or_home_persona() == home  # reset on exit


def test_reward_weight_uses_bound_persona_not_home(monkeypatch):
    # The audit's failure mode: a bound agent turn must get ITS persona's reward
    # valuation, not the home persona's. the_analyst and the_empath value
    # "connection" differently in _PERSONA_REWARD_WEIGHTS — assert the resolved
    # weights differ and match a direct per-persona lookup.
    from brain.neuron import _PERSONA_REWARD_WEIGHTS, reward_weight
    from brain.persona_key import active_or_home_persona
    from brain.second_brain.store import bind_persona
    from brain.settings import settings

    monkeypatch.setattr(settings, "get", lambda k, d=None: "the_analyst" if k == "persona_name" else d)
    analyst_w = _PERSONA_REWARD_WEIGHTS["the_analyst"]["connection"]
    empath_w = _PERSONA_REWARD_WEIGHTS["the_empath"]["connection"]
    assert analyst_w != empath_w, "test personas must differ on this source"

    assert reward_weight(active_or_home_persona(), "connection") == pytest.approx(analyst_w)
    with bind_persona("the_empath"):
        assert reward_weight(active_or_home_persona(), "connection") == pytest.approx(empath_w)


def test_dmn_reward_persona_delegates_to_shared_resolver():
    from brain.dmn import DefaultModeNetwork
    from brain.second_brain.store import bind_persona

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._home = "home_persona"
    with bind_persona("the_sage"):
        assert dmn._reward_persona() == "the_sage"


def test_bus_is_bound_distinguishes_client_pair():
    from brain.bus import Bus

    bus = Bus()
    assert bus.is_bound is False
    with bus.bind(bus.new_chem()):
        assert bus.is_bound is True
    assert bus.is_bound is False
    with bus.bind(bus.resting_chem):  # explicit resting bind is NOT a client bind
        assert bus.is_bound is False


def test_planning_hint_resolves_by_slug():
    m = _bare_motor()
    from brain.second_brain.store import bind_persona

    with bind_persona("the_visionary"):  # slug form — used to miss the display-name keys
        assert "Visionary" in m._persona_planning_hint()
    with bind_persona("The Visionary"):  # display form still works
        assert "Visionary" in m._persona_planning_hint()
    with bind_persona("the_unknown_custom"):
        assert m._persona_planning_hint() == ""


# ── Premise-audit fixes: DA provenance, per-job cap, criteria novelty gate ────
def test_da_source_tally_splits_intrinsic_and_external():
    from brain.bus import Neuromodulators

    nm = Neuromodulators()
    nm.add("DA", 0.10)  # default = intrinsic (self-administered)
    nm.add("DA", 0.05, source="external")  # user/world grounded
    nm.add("DA", -0.02, source="external")  # penalties count by magnitude
    nm.add("GABA", 0.30)  # non-DA writes never enter the tally
    t = nm.da_source_tally()
    assert t["intrinsic"] == pytest.approx(0.10)
    assert t["external"] == pytest.approx(0.07)


def test_external_grade_opens_external_da_lane():
    # The only reality-grounded reward channel: an external grade on a LIVE turn
    # must move DA, and it must land in the EXTERNAL bucket (not self-graded/
    # intrinsic), bounded to the configured nudge. Fails when
    # external_grade_da_nudge defaults to 0.0. The turn must be live in the trace
    # buffer — a grade on an unknown/consolidated turn moves nothing (round-2
    # hardening A3; see test_external_grade_channel for that guard).
    from brain.bus import Bus
    from brain.observability.timeline import TurnTrace
    from brain.session_loops import _LoopsMixin

    s = _LoopsMixin.__new__(_LoopsMixin)
    s.bus = Bus()
    s._session_traces_full = [TurnTrace(turn_id="turn-1", session_id="s", user_input="hi")]
    s._eval_logger = None
    before = s.bus.da_source_tally()
    res = s.api_grade_turn("turn-1", 1, source="user_thumbs")  # thumbs up → g=1.0
    after = s.bus.da_source_tally()

    assert res["ok"]
    assert res["applied_live"] is True
    # lane is open AND bounded: external bucket moved by the nudge, intrinsic did not
    assert after["external"] - before["external"] == pytest.approx(0.15)
    assert after["intrinsic"] == pytest.approx(before["intrinsic"])


def test_bus_da_tally_aggregates_resting_and_client_pairs():
    from brain.bus import Bus

    bus = Bus()
    bus.resting_chem.neuromod.add("DA", 0.10)  # brain's own idle reward
    pair = bus.new_chem()
    pair.neuromod.add("DA", 0.04, source="external")  # a customer's warmth

    class FakeReg:
        _live = {"u1": pair}

    bus._chem_registry = FakeReg()
    total = bus.da_source_tally()
    assert total["intrinsic"] == pytest.approx(0.10)
    assert total["external"] == pytest.approx(0.04)


def test_criteria_novelty_gate_blocks_thin_and_duplicate():
    from brain.clusters.motor_cortex import MotorCortexCluster as M

    seen: list[frozenset] = []
    ok, toks = M._criteria_reward_eligible(
        ["response contains three ticker symbols with current prices"], seen
    )
    assert ok
    seen.append(toks)
    # Copy-pasted (verbatim/reordered) criteria — the realistic farming case → no reward
    dup, toks2 = M._criteria_reward_eligible(
        ["response contains three ticker symbols with current prices"], seen
    )
    assert not dup
    # Too thin/generic → no reward
    thin, _ = M._criteria_reward_eligible(["output is not empty"], [])
    assert not thin
    # Genuinely different criteria still eligible
    ok2, _ = M._criteria_reward_eligible(
        ["summary cites at least two distinct news sources published today"], seen
    )
    assert ok2


def test_accomplishment_respects_per_job_intrinsic_cap(monkeypatch):
    from brain.session_turn import _TurnMixin  # the mixin that defines the reward
    from brain.settings import settings

    values = {
        "accomplishment_expected_medium": 6.0,
        "emotional_reactivity_scale": 1.0,
        "accomplishment_base": 0.07,
        "job_intrinsic_da_cap": 0.10,
        "accomplishment_fail_ratio": 0.40,
        "correctness_5ht_drain": 0.02,
    }
    monkeypatch.setattr(settings, "get", lambda k, d=None: values.get(k, d if d is not None else 1.0))

    class FakeNM:
        def __init__(self):
            self.adds = []

        def add(self, ch, v, source="intrinsic", **attribution):
            self.adds.append((ch, v))

    class FakeBus:
        def __init__(self):
            self.neuromod = FakeNM()
            self.hormonal = FakeNM()

    class S(_TurnMixin):
        pass

    s = S.__new__(S)
    s.bus = FakeBus()
    # Job already paid itself 0.08 mid-run; cap 0.10 → at most 0.02 more.
    s._emit_accomplishment_reward(
        {"success": True, "complexity": "medium", "productive_steps": 6,
         "predictions_confirmed": 2, "intrinsic_da_spent": 0.08}
    )
    das = [v for ch, v in s.bus.neuromod.adds if ch == "DA"]
    assert das and das[0] <= 0.02 + 1e-9
    # Fresh job with no mid-run spend gets the normal (larger) reward.
    s2 = S.__new__(S)
    s2.bus = FakeBus()
    s2._emit_accomplishment_reward(
        {"success": True, "complexity": "medium", "productive_steps": 6,
         "predictions_confirmed": 2, "intrinsic_da_spent": 0.0}
    )
    das2 = [v for ch, v in s2.bus.neuromod.adds if ch == "DA"]
    assert das2 and das2[0] > das[0]


def test_colony_trail_apply_default_is_live():
    from brain.settings import DEFAULTS

    assert DEFAULTS["colony_trail_apply"] == 1
