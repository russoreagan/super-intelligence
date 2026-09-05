"""brain/project_scheduler — the pure ranker's contract.

Every property here is the reason a weight can be tuned without fear: the guarantees
(no starvation, determinism, priority respect, aging monotonicity, money as a gate)
are asserted directly against the function, not argued from the constants.
"""

from __future__ import annotations

import random

from brain import project_scheduler as ps

NOW = 1_800_000_000.0
DAY = 86_400.0
FULL = ps.AgentInfo(agent_id="p.full")


def _p(pid, agent="p.full", **kw):
    persona, _, mandate = agent.partition(".")
    base = {
        "id": pid,
        "persona": persona,
        "mandate_id": mandate,
        "title": pid,
        "task": f"do {pid}",
        "ready_at": NOW - 60,
    }
    base.update(kw)
    return ps.Project(**base)


def _agents(*ids, **overrides):
    out = {a: ps.AgentInfo(agent_id=a) for a in ids}
    out.update(overrides)
    return out


def _select(projects, agents=None, now=NOW, **kw):
    agents = agents if agents is not None else _agents(*{p.agent_id for p in projects})
    return ps.select(projects, agents, ps.Capacity(0, 1), now, **kw)


# ── Determinism ─────────────────────────────────────────────────────────────


def test_identical_inputs_identical_output():
    ps_ = [_p("a"), _p("b", priority=1), _p("c", user_waiting=True)]
    assert _select(ps_) == _select(ps_)


def test_input_order_does_not_matter():
    ps_ = [_p(f"x{i}", priority=i % 4, ready_at=NOW - i * 100) for i in range(12)]
    ref = _select(ps_).project.id
    for seed in range(20):
        shuffled = list(ps_)
        random.Random(seed).shuffle(shuffled)
        assert _select(shuffled).project.id == ref


def test_none_result_touches_nothing():
    """A full slot returns None — and select() is pure, so nothing else changed."""
    ps_ = [_p("a")]
    agents = _agents("p.full")
    before = (tuple(ps_), dict(agents))
    assert ps.select(ps_, agents, ps.Capacity(in_flight=1, cap=1), NOW) is None
    assert (tuple(ps_), dict(agents)) == before


# ── Starvation-freedom: agent level ─────────────────────────────────────────


def _simulate(projects, agents, cfg, dispatches, *, step_s=600.0):
    """Drive the scheduler: each dispatch runs the chosen project, which comes back
    READY at the back of its agent's queue. Returns the per-agent max wall-clock gap."""
    now = NOW
    last = dict.fromkeys(agents)
    gaps = dict.fromkeys(agents, 0.0)
    rows = list(projects)
    for _ in range(dispatches):
        sel = ps.select(rows, agents, ps.Capacity(0, 1), now, cfg=cfg)
        assert sel is not None
        a = sel.project.agent_id
        if last[a] is not None:
            gaps[a] = max(gaps[a], now - last[a])
        last[a] = now
        rows = [
            ps.Project(**{**p.__dict__, "last_started_at": now, "ready_at": now})
            if p.id == sel.project.id
            else p
            for p in rows
        ]
        now += step_s
    return gaps


def test_no_agent_waits_past_the_sla_under_continuous_load():
    cfg = ps.Config(max_agent_wait_s=6 * 3600)
    rows = []
    for a in ("a.m", "b.m", "c.m", "d.m"):
        rows += [_p(f"{a}-{i}", agent=a, priority=0 if a == "a.m" else 3) for i in range(5)]
    agents = _agents("a.m", "b.m", "c.m", "d.m")
    gaps = _simulate(rows, agents, cfg, 1000)
    assert all(g <= cfg.max_agent_wait_s + 600 for g in gaps.values()), gaps


def test_adversarial_weights_still_served_inside_the_sla():
    """One agent with critical, user-waiting, deadline-pressed items; another with
    background items and a big spend deficit against it. The guard, not the score,
    is what serves the second one."""
    cfg = ps.Config(max_agent_wait_s=6 * 3600)
    hot = [
        _p(f"hot{i}", agent="hot.m", priority=0, user_waiting=True, deadline_at=NOW + 3600)
        for i in range(8)
    ]
    cold = [_p(f"cold{i}", agent="cold.m", priority=3) for i in range(2)]
    agents = _agents("hot.m", "cold.m", **{"cold.m": ps.AgentInfo("cold.m", spend_today_usd=50.0)})
    gaps = _simulate(hot + cold, agents, cfg, 600)
    assert gaps["cold.m"] <= cfg.max_agent_wait_s + 600


def test_never_served_agent_is_starved_and_preempts():
    served = _p("s", agent="s.m", priority=0, user_waiting=True, last_started_at=NOW - 60)
    fresh = _p("n", agent="n.m", priority=3)
    sel = _select([served, fresh])
    assert sel.project.id == "n"
    assert sel.forced and sel.reason == "starve_guard"


def test_guard_is_a_no_op_when_everyone_is_starved():
    a = _p("a", agent="a.m", priority=3)
    b = _p("b", agent="b.m", priority=0)
    sel = _select([a, b])
    assert sel.project.id == "b"
    assert not sel.forced and sel.reason == "score"


# ── Starvation-freedom: item level ──────────────────────────────────────────


def test_background_item_eventually_beats_a_continuously_readied_critical_item():
    cfg = ps.Config(age_tau_s=DAY)
    crit = _p("crit", priority=0, user_waiting=True, deadline_at=NOW, ready_at=NOW)
    bg = _p("bg", priority=3, ready_at=NOW)
    horizon = ps.S_SPAN * cfg.age_tau_s
    assert _select([crit, bg], now=NOW).project.id == "crit"
    # Keep re-readying crit at each tick; bg's age is what changes.
    later = NOW + horizon + 1
    crit2 = ps.Project(**{**crit.__dict__, "ready_at": later})
    assert _select([crit2, bg], now=later).project.id == "bg"


def test_s_span_matches_the_static_score_extremes():
    """The theorem's constant must not rot: S_SPAN == max(static) − min(static)."""
    hi = _p(
        "hi",
        priority=0,
        user_waiting=True,
        deadline_at=NOW - 10 * DAY,
        unblocks=("x", "y"),
        urgency_score=1.0,
    )
    lo = _p("lo", priority=3)
    agents = {"p.full": ps.AgentInfo("p.full", spend_today_usd=0.0)}
    open_ids = frozenset({"x", "y"})
    s_hi = ps.static_score(hi, agents["p.full"], NOW, open_ids=open_ids, spend_median_usd=10.0)
    s_lo = ps.static_score(
        lo, ps.AgentInfo("p.full", spend_today_usd=10.0), NOW, spend_median_usd=0.0
    )
    assert abs(s_hi - ps.STATIC_MAX) < 1e-9
    assert abs(s_lo - ps.STATIC_MIN) < 1e-9
    assert abs((s_hi - s_lo) - ps.S_SPAN) < 1e-9


# ── Priority / urgency respect ──────────────────────────────────────────────


def test_higher_priority_wins_all_else_equal():
    assert _select([_p("n", priority=2), _p("p", priority=1)]).project.id == "p"


def test_user_waiting_outranks_a_fresh_higher_priority_item():
    assert (
        _select([_p("crit", priority=0), _p("wait", priority=2, user_waiting=True)]).project.id
        == "wait"
    )


def test_unblocking_open_work_outranks_a_fresh_higher_priority_item():
    blocker = _p("blocker", priority=2, unblocks=("dep1", "dep2"))
    dep1 = _p("dep1", priority=3, state=ps.BLOCKED)
    dep2 = _p("dep2", priority=3, state=ps.BLOCKED)
    crit = _p("crit", priority=0)
    assert _select([blocker, dep1, dep2, crit]).project.id == "blocker"


def test_unblocking_finished_work_counts_for_nothing():
    blocker = _p("blocker", priority=2, unblocks=("gone",))
    gone = _p("gone", priority=3, state=ps.DONE)
    crit = _p("crit", priority=0)
    assert _select([blocker, gone, crit]).project.id == "crit"


def test_deadline_pressure_ramps_and_overshoots_gently():
    far = _p("far", deadline_at=NOW + 30 * DAY)
    soon = _p("soon", deadline_at=NOW + DAY)
    overdue = _p("overdue", deadline_at=NOW - 30 * DAY)
    a = FULL
    s_far, s_soon, s_over = (ps.static_score(p, a, NOW) for p in (far, soon, overdue))
    assert s_far < s_soon < s_over
    assert s_over - s_far <= ps.W_DEADLINE * ps.DEADLINE_MAX + 1e-9


def test_llm_urgency_is_clamped():
    a = FULL
    hi = ps.static_score(_p("x", urgency_score=99.0), a, NOW)
    one = ps.static_score(_p("x", urgency_score=1.0), a, NOW)
    neg = ps.static_score(_p("x", urgency_score=-5.0), a, NOW)
    zero = ps.static_score(_p("x", urgency_score=0.0), a, NOW)
    assert hi == one and neg == zero


# ── Spend fairness is soft ──────────────────────────────────────────────────


def test_spend_deficit_nudges_but_never_gates():
    rich = ps.AgentInfo("rich.m", spend_today_usd=20.0)
    poor = ps.AgentInfo("poor.m", spend_today_usd=0.0)
    a = _p("a", agent="rich.m", priority=2, last_started_at=NOW - 1)
    b = _p("b", agent="poor.m", priority=2, last_started_at=NOW - 1)
    assert _select([a, b], {"rich.m": rich, "poor.m": poor}).project.id == "b"
    # Real urgency on the rich agent's item still wins — fairness is a nudge, not a cap.
    a2 = ps.Project(**{**a.__dict__, "user_waiting": True})
    assert _select([a2, b], {"rich.m": rich, "poor.m": poor}).project.id == "a"


def test_over_daily_cap_is_excluded_not_downscored():
    capped = ps.AgentInfo("c.m", spend_today_usd=5.0, daily_cap_usd=5.0)
    a = _p("a", agent="c.m", priority=0, user_waiting=True)
    b = _p("b", agent="p.full", priority=3)
    sel = _select([a, b], {"c.m": capped, "p.full": FULL})
    assert sel.project.id == "b"


# ── Aging ───────────────────────────────────────────────────────────────────


def test_age_is_monotone_while_ready():
    p = _p("a", ready_at=NOW)
    s1 = ps.score(p, FULL, NOW + 100)
    s2 = ps.score(p, FULL, NOW + 200)
    assert s2 > s1


def test_age_is_constant_while_not_ready():
    for state in (ps.BLOCKED, ps.RUNNING, ps.DONE, ps.FAILED, ps.CANCELLED):
        p = _p("a", state=state, ready_at=NOW)
        assert ps.score(p, FULL, NOW + 100) == ps.score(p, FULL, NOW + 10 * DAY)
    pending = _p("a", state=ps.PENDING, ready_at=NOW, deferred_until=NOW + 10 * DAY)
    assert ps.score(pending, FULL, NOW + 100) == ps.score(pending, FULL, NOW + DAY)


def test_promoted_pending_ages_from_the_end_of_its_backoff():
    p = _p("a", state=ps.PENDING, ready_at=NOW - 30 * DAY, deferred_until=NOW - 60)
    assert ps.effective_state(p, NOW) == ps.READY
    assert abs(ps.age_points(p, NOW) - 60 / DAY) < 1e-9


# ── Eligibility / lifecycle ─────────────────────────────────────────────────


def test_only_ready_is_selectable():
    for state in ps.STATES:
        p = _p("a", state=state, deferred_until=NOW + DAY)
        ok = ps.eligible(p, FULL, NOW)
        assert ok == (state == ps.READY), state


def test_pending_becomes_eligible_once_its_backoff_passes():
    p = _p("a", state=ps.PENDING, deferred_until=NOW - 1)
    assert ps.eligible(p, FULL, NOW)


def test_lite_disabled_and_unknown_agents_are_never_selected():
    p = _p("a", agent="x.m")
    assert not ps.eligible(p, ps.AgentInfo("x.m", tier="lite"), NOW)
    assert not ps.eligible(p, ps.AgentInfo("x.m", enabled=False), NOW)
    assert not ps.eligible(p, None, NOW)


def test_per_agent_in_flight_and_dedup_gates():
    p = _p("a", agent="x.m")
    assert not ps.eligible(p, ps.AgentInfo("x.m"), NOW, in_flight_agents=frozenset({"x.m"}))
    assert not ps.eligible(
        p, ps.AgentInfo("x.m"), NOW, in_flight_keys=frozenset({ps.dedup_key(p.task)})
    )
    assert ps.eligible(p, ps.AgentInfo("x.m"), NOW)


def test_empty_task_is_not_work():
    assert not ps.eligible(_p("a", task="   "), FULL, NOW)


def test_from_row_is_tolerant():
    p = ps.from_row(
        {
            "id": "x",
            "persona": "p",
            "mandate_id": "m",
            "priority": "9",
            "unblocks": "one",
            "max_runs": None,
        }
    )
    assert p.priority == ps.DEFAULT_PRIORITY and p.unblocks == ("one",) and p.max_runs == 0
