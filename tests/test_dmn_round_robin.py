"""
Round-robin DMN: one process, one idle loop, rotating which persona it thinks as.

Proves the two properties the design hinges on (reports/round_robin_dmn_design.md):
  1. Per-persona transient state is ISOLATED — a thought (and its open-thread / session
     buffer) generated while bound to persona A never appears in persona B's bundle, and
     vice-versa. This is the no-cross-bleed guarantee.
  2. The tick interval scales with the roster size (so N personas don't starve each other)
     but is clamped to a floor, and a single-persona roster reproduces the prior interval
     exactly (regression invariant). Rotation is fair round-robin, home first.

The LLM is never called: _process_thought is handed parsed monologue metadata directly,
exactly like the other DMN tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.dmn import DMN_MIN_TICK_INTERVAL, DefaultModeNetwork, IdlePhase
from brain.second_brain.store import _persona_key, bind_persona
from brain.settings import settings


def _meta(**over) -> dict:
    base = {
        "angle": None,
        "spoken_form": None,
        "task_goal": None,
        "is_propose": False,
        "is_plan": False,
        "defer_text": None,
        "defer_urgency": "high",
        "defer_tags": [],
        "chem_delta": {},
        "open_thread": False,
        "advance_thread_id": "",
        "conclude_thread_id": "",
        "conclusion": "",
        "conclusion_confidence": "confident",
        "bears_on": [],
        "bearing": "",
    }
    base.update(over)
    return base


def _make_dmn(home: str = "the_analyst"):
    """A bare DMN double (bypasses __init__) wired only with what _process_thought needs,
    plus the round-robin bookkeeping. Per-persona transient attrs are NOT pre-seeded — the
    _PerPersona descriptor lazily creates a fresh bundle per bound persona, which is exactly
    what we want to assert isolation over."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    # Round-robin bookkeeping (normally set in __init__).
    dmn._pstate = {}
    dmn._home = home
    dmn._hydrated_personas = set()
    dmn._roster_cache = []
    dmn._roster_ts = 0.0
    dmn._rr_idx = 0
    # Shared (non-per-persona) collaborators + state.
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._bus.neuromod.snapshot = MagicMock(return_value={"DA": 0.5})
    dmn._bus.neuromod.add = MagicMock()
    dmn._bus.hormonal.add = MagicMock()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    hip.encode_deferred_question = AsyncMock()
    schema = MagicMock()
    schema.read = MagicMock(return_value="")
    schema.upsert_section = AsyncMock()
    hip._schema = schema
    dmn._hippocampus = hip
    dmn._parietal = None
    dmn._obs = MagicMock()
    dmn._running = True
    dmn._session_id = "test"
    dmn._suppressed_count = 0
    dmn._session_thought_limit = 200
    dmn._append_deferred_thought = MagicMock()
    return dmn


# ── 1. No cross-bleed: each persona accrues only its own stream of thought ────────


@pytest.mark.asyncio
async def test_rotation_isolates_recent_thoughts_and_threads():
    dmn = _make_dmn()

    # Bound to persona A: a plain thought + an open thread.
    with bind_persona("the_analyst"):
        await dmn._process_thought("Analysts weigh evidence carefully.", _meta(angle="rigor"), "a1")
        await dmn._process_thought(
            "Should I quantify the model's calibration?",
            _meta(open_thread=True, angle="calibration", bears_on=["calibration-q"]),
            "a2",
        )

    # Bound to persona B: a different thought + a different open thread.
    with bind_persona("the_trader"):
        await dmn._process_thought("Momentum is fading on the open.", _meta(angle="momentum"), "b1")
        await dmn._process_thought(
            "Is the breakout volume real?",
            _meta(open_thread=True, angle="volume", bears_on=["volume-q"]),
            "b2",
        )

    # Each persona sees ONLY its own thoughts.
    with bind_persona("the_analyst"):
        a_thoughts = list(dmn._recent_thoughts)
        a_threads = [t.summary for t in dmn._open_threads]
        a_buf = [e["thought"] for e in dmn._session_thought_buf]
    with bind_persona("the_trader"):
        t_thoughts = list(dmn._recent_thoughts)
        t_threads = [t.summary for t in dmn._open_threads]
        t_buf = [e["thought"] for e in dmn._session_thought_buf]

    assert any("Analysts weigh" in x for x in a_thoughts)
    assert all("Momentum" not in x and "breakout" not in x for x in a_thoughts), a_thoughts
    assert any("Momentum is fading" in x for x in t_thoughts)
    assert all("Analysts" not in x and "calibration" not in x for x in t_thoughts), t_thoughts

    # Open-thread ledgers are disjoint.
    assert len(a_threads) == 1 and "calibration" in a_threads[0].lower(), a_threads
    assert len(t_threads) == 1 and "breakout" in t_threads[0].lower(), t_threads

    # Session buffers (handed to sleep consolidation) are disjoint too.
    assert a_buf and all("Momentum" not in x for x in a_buf), a_buf
    assert t_buf and all("Analysts" not in x for x in t_buf), t_buf

    # Two distinct bundles exist, keyed by canonical slug; neither leaked into the other.
    assert _persona_key("the_analyst") in dmn._pstate
    assert _persona_key("the_trader") in dmn._pstate
    assert (
        dmn._pstate[_persona_key("the_analyst")]["_recent_thoughts"]
        is not (dmn._pstate[_persona_key("the_trader")]["_recent_thoughts"])
    )


@pytest.mark.asyncio
async def test_thought_count_is_per_persona():
    dmn = _make_dmn()
    with bind_persona("the_analyst"):
        for i in range(3):
            await dmn._process_thought(
                f"analyst musing number {i} about evidence", _meta(), f"a{i}"
            )
        a_count = dmn._thought_count
    with bind_persona("the_trader"):
        await dmn._process_thought("trader musing about the tape", _meta(), "b0")
        t_count = dmn._thought_count
    # _process_thought does not itself bump _thought_count (the tick does), but any per-tick
    # counter writes must not bleed: each persona's counter is independent of the other's.
    with bind_persona("the_analyst"):
        dmn._thought_count += 10
    with bind_persona("the_trader"):
        assert dmn._thought_count == t_count, "trader counter moved when analyst's did"
    with bind_persona("the_analyst"):
        assert dmn._thought_count == a_count + 10


# ── 2. Home/slug share one bundle; unbound falls back to home ─────────────────────


def test_home_display_name_and_slug_share_one_bundle():
    dmn = _make_dmn(home="The Analyst")
    with bind_persona("The Analyst"):
        dmn._recent_thoughts.append("home-thought")
    # The slug of the home display name must resolve to the SAME bundle.
    with bind_persona("the_analyst"):
        assert list(dmn._recent_thoughts) == ["home-thought"]
    # Unbound access falls back to home.
    assert list(dmn._recent_thoughts) == ["home-thought"]


# ── 3. Adaptive cadence: interval scales with roster size, clamped to floor ───────


def _interval_dmn(roster: list[str]):
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._home = roster[0] if roster else "home"
    dmn._roster_cache = []
    dmn._roster_ts = 0.0
    dmn._rr_idx = 0
    dmn._backoff_mult = 1.0
    dmn._idle_phase = lambda *a, **k: IdlePhase.ENGAGED  # active phase → target == base
    dmn._roster = lambda: roster
    return dmn


def test_interval_scales_with_roster_and_respects_floor():
    base = float(settings.get("dmn_interval") or 15)
    floor = float(settings.get("dmn_min_tick_interval") or DMN_MIN_TICK_INTERVAL)

    def expected(n):
        return max(floor, base / n)

    # Single persona → exactly the base interval (regression invariant: base >= floor).
    assert base >= floor
    assert abs(_interval_dmn(["home"])._current_interval() - base) < 1e-9

    # Growing roster shortens the interval down to the floor.
    for roster in (["h", "b"], ["h", "b", "c"], [f"p{i}" for i in range(12)]):
        got = _interval_dmn(roster)._current_interval()
        assert abs(got - expected(len(roster))) < 1e-9, (len(roster), got)

    # Large roster is clamped, never below the floor.
    assert abs(_interval_dmn([f"p{i}" for i in range(50)])._current_interval() - floor) < 1e-9


def test_backoff_multiplies_the_scaled_interval():
    base = float(settings.get("dmn_interval") or 15)
    floor = float(settings.get("dmn_min_tick_interval") or DMN_MIN_TICK_INTERVAL)
    dmn = _interval_dmn(["h", "b"])
    dmn._backoff_mult = 3.0
    assert abs(dmn._current_interval() - max(floor, base / 2) * 3.0) < 1e-9


# ── 4. Round-robin selection is fair and home-first ───────────────────────────────


def test_next_persona_round_robins_home_first():
    dmn = _make_dmn(home="home_p")
    dmn._roster = lambda: ["home_p", "b", "c"]
    picks = [dmn._next_persona() for _ in range(7)]
    assert picks == ["home_p", "b", "c", "home_p", "b", "c", "home_p"], picks


def test_roster_falls_back_to_home_only_without_agents_backend():
    # No Supabase agents table reachable in the unit-test env → roster must degrade to
    # [home], i.e. behave exactly like today's single-persona DMN.
    dmn = _make_dmn(home="solo_persona")
    roster = dmn._roster()
    assert roster == ["solo_persona"], roster


def test_suppressed_ticks_do_not_burn_a_rotation_slot():
    # _next_persona is only called when a tick fires, so the cursor advances per-fired-tick.
    # Verify the cursor is monotonic and wraps, independent of roster caching.
    dmn = _make_dmn(home="h")
    dmn._roster = lambda: ["h", "x"]
    first = dmn._next_persona()
    second = dmn._next_persona()
    third = dmn._next_persona()
    assert (first, second, third) == ("h", "x", "h")


# ── 3. Self-initiated tasks are AGENT-level, attribution is not ──────────────────
#
# The counterpart to property 1. A THOUGHT belongs to the persona that had it and must
# not bleed; a self-initiated TASK is work for the lane, and the single task worker that
# executes it runs unbound (i.e. on home). While the queue lived in the per-persona
# bundle those two facts contradicted each other: a task produced on a rotated tick was
# written to that persona's queue and the worker only ever read home's, so it was
# stranded and aged out of a maxlen deque unseen. Found 2026-08-22 in production — two
# substantive goals queued, neither ever executed.


@pytest.mark.asyncio
async def test_self_tasks_from_any_persona_reach_the_shared_queue():
    """A task thought of by a rotated persona is visible to the unbound worker."""
    dmn = _make_dmn(home="the_analyst")
    dmn._self_task_q = __import__("collections").deque(maxlen=8)

    with bind_persona("the_visionary"):
        await dmn._process_thought(
            "Check whether the ingest job still writes duplicate rows.",
            _meta(task_goal="Check the ingest job for duplicate rows."),
            "v1",
        )

    # Unbound — exactly how _task_worker_loop calls it.
    task = dmn.take_self_task()
    assert task is not None, "a task queued by a rotated persona must reach the worker"
    assert task["goal"] == "Check the ingest job for duplicate rows."


@pytest.mark.asyncio
async def test_self_task_carries_the_persona_that_thought_of_it():
    """Shared queue, per-persona attribution: the executor binds this so completion
    rewards and memory writes stay with the originating persona, matching what the
    agent lane already does."""
    dmn = _make_dmn(home="the_analyst")
    dmn._self_task_q = __import__("collections").deque(maxlen=8)

    with bind_persona("the_visionary"):
        await dmn._process_thought(
            "Draft the onboarding copy revision.",
            _meta(task_goal="Draft the onboarding copy revision."),
            "v2",
        )
    with bind_persona("the_analyst"):
        await dmn._process_thought(
            "Re-run the calibration check on last week's numbers.",
            _meta(task_goal="Re-run the calibration check."),
            "a2",
        )

    first = dmn.take_self_task()
    second = dmn.take_self_task()
    assert _persona_key(first["persona"]) == _persona_key("the_visionary")
    assert _persona_key(second["persona"]) == _persona_key("the_analyst")


def test_self_task_persona_survives_the_queue_round_trip(tmp_path, monkeypatch):
    """origin_persona reaches the executor, and a queue written before the field
    existed still loads (from_dict drops unknown keys)."""
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()
    q.enqueue("Draft the onboarding copy.", source="self", origin_persona="the_visionary")

    reloaded = tq.PersistentTaskQueue()
    task = reloaded.take_next()
    assert task is not None
    assert task.origin_persona == "the_visionary"
    # Owner lane — the persona rides on the task itself, not on an agent identity.
    assert task.origin_channel == "owner"
    assert task.origin_agent_id == ""

    legacy = tq.Task.from_dict({"id": "x", "goal": "g", "source": "self"})
    assert legacy.origin_persona == ""


def test_self_directed_work_stays_serial():
    """Agent-level must not mean parallel. One worker takes one task and awaits it, and
    the motor cortex refuses a second concurrent job on top of that."""
    from brain.settings import settings as _s

    assert int(_s.get("motor_max_concurrent_jobs")) == 1


def test_deferred_count_counts_only_parked_not_due_tasks(tmp_path, monkeypatch):
    """The saturation gate reads deferred_count() as "work already waiting that the
    lane isn't allowed to run". A deferred task whose backoff has elapsed is ready
    work (pending), not backlog — it must not keep the intake gate closed."""
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()
    t = q.enqueue("scan the wires", source="self")
    assert q.deferred_count() == 0
    q.take_next()
    q.mark_deferred(t.id, backoff_s=600.0)
    assert q.deferred_count() == 1
    assert not q.has_pending()  # parked reads as idle to the worker…
    # …and once due it counts as ready work again, not as parked backlog.
    for x in q._tasks:
        if x.id == t.id:
            x.not_before = 0.0
    assert q.deferred_count() == 0
    assert q.has_pending()


def test_saturated_lane_stops_draining_fresh_dmn_ideas(tmp_path, monkeypatch):
    """Generation-side gate: the rate caps bound EXECUTION, and the worker refills
    from the DMN whenever nothing is due — so once the daily cap was hit, parked
    tasks backing off read as idle and the worker kept minting fresh ideas that
    could only ever be rate-limit-deferred (2026-08-23: the queue grew all day
    instead of the work). Saturated = parked backlog OR no free rate-cap slot."""
    import brain.clusters.task_queue as tq
    from brain.session_loops import _LoopsMixin

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()

    class _Motor:
        saturated = False

        def autonomy_saturated(self):
            return self.saturated

    sess = _LoopsMixin.__new__(_LoopsMixin)
    sess._task_queue = q
    sess.motor = _Motor()

    assert sess._self_work_saturated() is False
    # A parked (not-yet-due) deferral closes the intake…
    t = q.enqueue("scan the wires", source="self")
    q.take_next()
    q.mark_deferred(t.id, backoff_s=600.0)
    assert sess._self_work_saturated() is True
    # …and so do exhausted rate caps, even with an empty queue.
    q.clear_all()
    assert sess._self_work_saturated() is False
    sess.motor.saturated = True
    assert sess._self_work_saturated() is True
    # Fails open: a broken probe must never silence self-directed work entirely.
    sess.motor = None
    sess._task_queue = None
    assert sess._self_work_saturated() is False


def test_roster_reads_tiers_from_the_listing_not_per_persona(monkeypatch):
    # One list_agents() call must be the roster's ONLY Supabase read: tiers come from
    # the rows in hand (agents.effective_tiers). The per-persona effective_tier()
    # re-query was ~16 requests per refresh for a 15-persona org.
    from brain import agents

    dmn = _make_dmn(home="home_p")
    rows = [
        {"persona": "b", "enabled": True, "tier": "full"},
        {"persona": "c", "enabled": True, "tier": "lite"},
        {"persona": "c", "enabled": True, "tier": "full"},  # full dominates
        {"persona": "d", "enabled": True, "tier": "lite"},  # lite-only → excluded
        {"persona": "e", "enabled": False, "tier": "full"},  # disabled → excluded
    ]
    monkeypatch.setattr(agents, "list_agents", lambda: rows)

    def _no_per_persona_query(_p):
        raise AssertionError("roster must not issue per-persona tier queries")

    monkeypatch.setattr(agents, "effective_tier", _no_per_persona_query)
    assert dmn._roster() == ["home_p", "b", "c"]


# ── 5. Isolated org: a shared queue over the ACTIVE personas ────────────────────
#
# An isolated org (organizations.learning_mode) used to rotate the home persona only,
# so a purchase persona never thought idle. Now `dmn_isolated_roster` picks the rule:
# home (kill switch), active (home + personas a human talked to in the last
# dmn_active_roster_days, per-persona stamp) or all (as consolidated).


@pytest.fixture
def isolated_org(monkeypatch, tmp_path):
    from brain import agents, human_activity, org_settings

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb" / "personas" / "home_p"))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_PERSONA_PINNED", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setattr(human_activity, "_persona_last_write_ts", {})
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "active")
    monkeypatch.setitem(settings._data, "dmn_active_roster_days", 7)
    rows = [
        {"persona": "b", "enabled": True, "tier": "full", "mandate_id": "m"},
        {"persona": "c", "enabled": True, "tier": "full", "mandate_id": "m"},
        {"persona": "d", "enabled": True, "tier": "lite", "mandate_id": "m"},
        {"persona": "home_p", "enabled": True, "tier": "full", "mandate_id": "m"},
    ]
    monkeypatch.setattr(agents, "list_agents", lambda **kw: rows)
    return human_activity


def test_isolated_active_roster_includes_fresh_and_excludes_stale(isolated_org):
    ha = isolated_org
    now = __import__("time").time()
    ha.stamp_persona("b", now - 3600.0, force=True)  # talked to an hour ago
    ha.stamp_persona("c", now - 10 * 86400.0, force=True)  # ten days ago → stale
    dmn = _make_dmn(home="home_p")
    assert dmn._roster() == ["home_p", "b"]
    # Rotation and hydration follow the computed roster, not "home only".
    assert [dmn._next_persona() for _ in range(3)] == ["home_p", "b", "home_p"]


def test_isolated_active_roster_is_home_only_until_someone_talks(isolated_org):
    dmn = _make_dmn(home="home_p")
    assert dmn._roster() == ["home_p"]


def test_isolated_home_mode_is_the_kill_switch(isolated_org, monkeypatch):
    ha = isolated_org
    ha.stamp_persona("b", force=True)
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "home")
    assert _make_dmn(home="home_p")._roster() == ["home_p"]


def test_isolated_all_mode_rotates_every_full_tier_persona(isolated_org, monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "all")
    assert _make_dmn(home="home_p")._roster() == ["home_p", "b", "c"]  # d is lite


def test_stamp_older_than_the_window_drops_a_persona_out(isolated_org, monkeypatch):
    ha = isolated_org
    now = __import__("time").time()
    ha.stamp_persona("b", now - 8 * 86400.0, force=True)
    assert _make_dmn(home="home_p")._roster() == ["home_p"]
    monkeypatch.setitem(settings._data, "dmn_active_roster_days", 30)
    assert _make_dmn(home="home_p")._roster() == ["home_p", "b"]
    monkeypatch.setitem(settings._data, "dmn_active_roster_days", 0)  # 0 = everyone
    assert _make_dmn(home="home_p")._roster() == ["home_p", "b", "c"]


def test_pinned_process_wins_over_the_active_rule(isolated_org, monkeypatch):
    isolated_org.stamp_persona("b", force=True)
    monkeypatch.setenv("BRAIN_PERSONA_PINNED", "1")
    assert _make_dmn(home="home_p")._roster() == ["home_p"]


def test_cadence_thins_with_the_active_count_but_keeps_the_floor(isolated_org):
    ha = isolated_org
    ha.stamp_persona("b", force=True)
    ha.stamp_persona("c", force=True)
    dmn = _make_dmn(home="home_p")
    dmn._backoff_mult = 1.0
    dmn._idle_phase = lambda *a, **k: IdlePhase.ENGAGED
    base = float(settings.get("dmn_interval") or 15)
    floor = float(settings.get("dmn_min_tick_interval") or DMN_MIN_TICK_INTERVAL)
    assert abs(dmn._current_interval() - max(floor, base / 3)) < 1e-9


@pytest.mark.asyncio
async def test_hydrate_accepts_roster_members_and_refuses_the_rest(isolated_org):
    isolated_org.stamp_persona("b", force=True)
    dmn = _make_dmn(home="home_p")
    dmn._load_novelty = MagicMock()
    dmn._load_threads = AsyncMock()
    dmn._load_routing_weights = MagicMock()
    dmn._load_projects = MagicMock()
    await dmn._hydrate("b")  # active → hydrated
    await dmn._hydrate("c")  # not on the roster → no dmn_state for it from this loop
    await dmn._hydrate("home_p")
    assert dmn._hydrated_personas == {"b", "home_p"}
    assert dmn._load_novelty.call_count == 2


def test_project_eligibility_follows_the_roster(isolated_org, monkeypatch):
    from brain import agent_projects_store as store

    isolated_org.stamp_persona("b", force=True)
    monkeypatch.setattr(store, "_backend", lambda: "supabase")
    monkeypatch.setattr(store, "agent_spend_today", lambda: {})
    dmn = _make_dmn(home="home_p")
    info = dmn._project_agents([])
    assert set(info) == {"home_p.m", "b.m"}, set(info)
    # …and `home` mode narrows it back to the home persona's agents.
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "home")
    dmn = _make_dmn(home="home_p")
    assert set(dmn._project_agents([])) == {"home_p.m"}


def test_pause_stamps_the_persona_bound_for_the_turn(isolated_org, monkeypatch):
    from brain import human_activity as ha

    monkeypatch.setattr(ha, "_last_write_ts", 0.0)
    dmn = _make_dmn(home="home_p")
    with bind_persona("b"):
        dmn.pause()
    assert ha.persona_last_turn_ts("b") is not None
    assert ha.persona_last_turn_ts("home_p") is None
    dmn.pause()  # a companion turn binds nothing → home
    assert ha.persona_last_turn_ts("home_p") is not None
    assert ha.last_turn_ts() is not None  # the org-level clock still moves
    # AI-internal pauses stamp nothing.
    ha._persona_last_write_ts.clear()
    (isolated_org.org_state_root() / "personas" / "c").mkdir(parents=True, exist_ok=True)
    with bind_persona("c"):
        dmn.pause(stamp_activity=False)
    assert ha.persona_last_turn_ts("c") is None


# ── 6. Answer-only agents never turn idle ideas into jobs ───────────────────────


def test_background_answer_only_gate(monkeypatch):
    from brain import agents
    from brain.session_loops import _background_answer_only

    monkeypatch.setitem(settings._data, "answer_only", 0)
    flags = {"b.m": True, "home_p.m": False}
    monkeypatch.setattr(agents, "answer_only", lambda aid: flags[aid])
    assert _background_answer_only("b.m") is True
    assert _background_answer_only("home_p.m") is False
    assert _background_answer_only("") is False  # no agent resolved → org rule only
    monkeypatch.setitem(settings._data, "answer_only", 1)
    assert _background_answer_only("home_p.m") is True
    assert _background_answer_only("") is True
    # Fails open like agents.answer_only: a store error never silences a normal agent.
    monkeypatch.setitem(settings._data, "answer_only", 0)

    def _boom(_aid):
        raise RuntimeError("store down")

    monkeypatch.setattr(agents, "answer_only", _boom)
    assert _background_answer_only("b.m") is False


@pytest.mark.asyncio
async def test_worker_drops_self_tasks_of_answer_only_agents(tmp_path, monkeypatch):
    """An answer-only agent may keep thinking idle; its ideas must never be enqueued."""
    import asyncio
    import types

    import brain.clusters.task_queue as tq
    from brain import agents, session_loops
    from brain.session_loops import _LoopsMixin

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    monkeypatch.setitem(settings._data, "answer_only", 0)
    monkeypatch.setattr(agents, "owning_agent_id", lambda p: f"{p}.m")
    monkeypatch.setattr(agents, "answer_only", lambda aid: aid == "b.m")

    ideas = [
        {"goal": "quiet idea", "persona": "b", "reflex_depth": 0},
        {"goal": "loud idea", "persona": "home_p", "reflex_depth": 0},
    ]
    dmn = MagicMock()
    dmn.dormant = False
    dmn.take_self_task = lambda: ideas.pop(0) if ideas else None
    dmn.next_project = lambda: None

    # Fast, finite loop: sleep is instant and the fourth call cancels the worker.
    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > 3:
            raise asyncio.CancelledError

    fake_asyncio = types.SimpleNamespace(
        sleep=_sleep, CancelledError=asyncio.CancelledError, create_task=asyncio.create_task
    )
    monkeypatch.setattr(session_loops, "asyncio", fake_asyncio)

    sess = _LoopsMixin.__new__(_LoopsMixin)
    sess._task_queue = tq.PersistentTaskQueue()
    sess.dmn = dmn
    sess.motor = None
    sess.pns = types.SimpleNamespace(is_speaking=True)  # never reaches execution
    sess._ui_message_queue = asyncio.Queue()
    await sess._task_worker_loop()

    goals = [t.goal for t in sess._task_queue._tasks]
    assert goals == ["loud idea"], goals
    assert sess._task_queue._tasks[0].origin_agent_id == "home_p.m"


def test_project_agents_carry_the_answer_only_permission(isolated_org, monkeypatch):
    """The scheduler's agent record reads answer_only from the permissions column
    already in the fetched rows — no extra query, and the ranker then skips it."""
    from brain import agent_projects_store as store
    from brain import agents

    isolated_org.stamp_persona("b", force=True)
    rows = [
        {"persona": "home_p", "enabled": True, "tier": "full", "mandate_id": "m"},
        {
            "persona": "b",
            "enabled": True,
            "tier": "full",
            "mandate_id": "m",
            "permissions": {"answer_only": "true"},
        },
    ]
    monkeypatch.setattr(agents, "list_agents", lambda **kw: rows)
    monkeypatch.setattr(store, "_backend", lambda: "supabase")
    monkeypatch.setattr(store, "agent_spend_today", lambda: {})
    info = _make_dmn(home="home_p")._project_agents([])
    assert info["b.m"].answer_only is True and info["home_p.m"].answer_only is False


@pytest.mark.asyncio
async def test_worker_releases_a_project_step_for_an_answer_only_agent(tmp_path, monkeypatch):
    """Belt and braces under the scheduler: the local backend has no permissions
    in its agent records, and a flag set inside the 60 s agent cache would still
    let one step through. The worker releases the claimed row instead."""
    import asyncio
    import types

    import brain.clusters.task_queue as tq
    from brain import agents, session_loops
    from brain.session_loops import _LoopsMixin

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    monkeypatch.setitem(settings._data, "answer_only", 0)
    monkeypatch.setattr(agents, "answer_only", lambda aid: aid == "b.m")

    rows = [
        {"id": "p1", "task": "quiet step", "persona": "b", "agent_id": "b.m"},
        {"id": "p2", "task": "loud step", "persona": "home_p", "agent_id": "home_p.m"},
    ]
    dmn = MagicMock()
    dmn.dormant = False
    dmn.take_self_task = lambda: None
    dmn.next_project = lambda: rows.pop(0) if rows else None
    dmn.release_project = MagicMock()
    dmn.note_project_started = MagicMock()

    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        session_loops,
        "asyncio",
        types.SimpleNamespace(
            sleep=_sleep, CancelledError=asyncio.CancelledError, create_task=asyncio.create_task
        ),
    )
    sess = _LoopsMixin.__new__(_LoopsMixin)
    sess._task_queue = tq.PersistentTaskQueue()
    sess.dmn = dmn
    sess.motor = None
    sess.pns = types.SimpleNamespace(is_speaking=True)
    sess._ui_message_queue = asyncio.Queue()
    await sess._task_worker_loop()

    assert [t.goal for t in sess._task_queue._tasks] == ["loud step"]
    dmn.release_project.assert_called_once_with("p1")
    dmn.note_project_started.assert_called_once()

    # Org-wide answer-only: no clock-in at all — the scheduler is not even asked.
    # (clear_all marks the ledger's tasks failed rather than deleting them.)
    sess._task_queue.clear_all()
    assert not sess._task_queue.has_pending()
    monkeypatch.setitem(settings._data, "answer_only", 1)
    asked = {"n": 0}

    def _next():
        asked["n"] += 1
        return None

    dmn.next_project = _next
    calls["n"] = 0
    await sess._task_worker_loop()
    assert asked["n"] == 0 and not sess._task_queue.has_pending()
