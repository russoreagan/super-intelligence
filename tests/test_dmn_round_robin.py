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
