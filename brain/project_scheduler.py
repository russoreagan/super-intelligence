"""project_scheduler — pure selection over the agent-scoped project queue.

The DMN's standing work ("projects") lives in the agent_projects table (migration
034), one row per project, each linked to an AGENT (persona × mandate). One brain
process serves a roster of personas and runs ONE background job at a time, so
something has to decide, across every agent it serves, which project runs next.
That decision is this module, and it is deliberately a pure function of the rows:
no I/O, no hidden state, no clock of its own — so every property it claims
(no starvation, determinism, priority respect) is one unit test, not an argument.

Three layers, in order of authority:

  1. ELIGIBILITY — hard gates. Only `ready` rows (a `pending` row whose backoff has
     passed counts as ready). The owning agent must exist, be enabled, and be
     full-tier: a lite agent is on-demand only and never gets idle work
     (brain/session_setup.py — "lite-tier brain runs no idle thinking loop"). An
     agent over its daily USD cap is EXCLUDED, never merely down-scored — money is
     a gate, not a score term (brain/budget.py: chemistry may modulate effort, it
     must never widen money). One project in flight per agent.

  2. STARVE GUARD — the structural fairness guarantee. If any agent with eligible
     work has not been served in `max_agent_wait_s`, the candidate pool collapses
     to those agents' projects. Four lines, independent of every weight below, so a
     retune of the score can never reintroduce starvation.

  3. SCORE — urgency leads, spend-fairness is a soft correction:
        W_BLOCK    × how much other open work this unblocks
      + W_WAITING  × the user is waiting on it
      + W_DEADLINE × deadline pressure
      + W_PRIORITY × the user-set priority band
      + W_URGENCY  × LLM-appraised urgency (0 until the intake pass ships)
      + W_FAIR     × spend deficit vs the median agent (bounded ±1, never a cap)
      + age        — the ONE unbounded term, +1/`age_tau_s` per second, and it only
                     accrues while the row is ready. Blocked/deferred/running time
                     is the user's latency or the machine's, not the scheduler's.
     Every other term is bounded, so any ready project becomes the argmax within
     S_SPAN × age_tau_s of neglect. That is the item-level guarantee and
     `age_tau_s` is its single knob.

Tie-break is (-score, ready_at, id): a total order, so the result is invariant to
the order the rows arrived in. There is no rotation index to get out of step.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass

# ── Score weights ────────────────────────────────────────────────────────────
W_BLOCK = 1.2
W_WAITING = 1.2
W_DEADLINE = 0.8
W_PRIORITY = 1.0
W_URGENCY = 0.6
W_FAIR = 0.5

# 0 critical · 1 primary · 2 normal · 3 background — replaces the `(PRIMARY)` regex.
PRIORITY_WEIGHT: dict[int, float] = {0: 1.0, 1: 0.7, 2: 0.4, 3: 0.15}
DEFAULT_PRIORITY = 2
# Deadline pressure ramps over the last DEADLINE_LEAD_S and may overshoot to
# DEADLINE_MAX once past due — an overdue item keeps climbing, gently.
DEADLINE_LEAD_S = 3 * 86_400.0
DEADLINE_MAX = 1.25
# One "spend unit" for the fairness term.
SPEND_REF_USD = 1.0

# The bounded part of the score spans exactly this much; the aging term needs at
# most S_SPAN × age_tau_s to lift any ready item over any other. Tests pin it.
STATIC_MAX = (
    W_BLOCK
    + W_WAITING
    + W_DEADLINE * DEADLINE_MAX
    + W_PRIORITY * max(PRIORITY_WEIGHT.values())
    + W_URGENCY * 1.0
    + W_FAIR * 1.0
)
STATIC_MIN = W_PRIORITY * min(PRIORITY_WEIGHT.values()) - W_FAIR * 1.0
S_SPAN = STATIC_MAX - STATIC_MIN

# States. Only READY is selectable; PENDING becomes ready when its backoff passes.
READY = "ready"
RUNNING = "running"
PENDING = "pending"
BLOCKED = "blocked"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
STATES = (READY, RUNNING, PENDING, BLOCKED, DONE, FAILED, CANCELLED)
OPEN_STATES = (READY, RUNNING, PENDING, BLOCKED)


@dataclass(frozen=True)
class Config:
    max_agent_wait_s: float = 6 * 3600.0
    age_tau_s: float = 86_400.0
    per_agent_in_flight: int = 1


DEFAULT_CONFIG = Config()


@dataclass(frozen=True)
class Project:
    """The ranker's view of one agent_projects row. Timestamps are epoch seconds."""

    id: str
    persona: str
    mandate_id: str
    title: str
    task: str
    state: str = READY
    priority: int = DEFAULT_PRIORITY
    user_waiting: bool = False
    unblocks: tuple[str, ...] = ()
    deadline_at: float | None = None
    urgency_score: float | None = None
    ready_at: float = 0.0
    deferred_until: float | None = None
    last_started_at: float | None = None
    in_flight_task_id: str = ""
    max_runs: int = 1
    runs: int = 0

    @property
    def agent_id(self) -> str:
        return agent_id_for(self.persona, self.mandate_id)


@dataclass(frozen=True)
class AgentInfo:
    agent_id: str
    tier: str = "full"
    enabled: bool = True
    spend_today_usd: float = 0.0
    daily_cap_usd: float | None = None  # None = no per-agent cap
    # permissions.answer_only: the agent is pure Q&A — no background work, so
    # its projects stay on the ledger unworked until the flag is cleared.
    answer_only: bool = False


@dataclass(frozen=True)
class Capacity:
    in_flight: int = 0
    cap: int = 1


@dataclass(frozen=True)
class Selection:
    project: Project
    score: float
    forced: bool  # the starve guard chose the pool
    reason: str  # "starve_guard" | "score"


# ── Small pure helpers ───────────────────────────────────────────────────────


def agent_id_for(persona: str, mandate_id: str) -> str:
    """ "persona.mandate" — or the bare persona when there is no mandate (local dev)."""
    p = str(persona or "").strip()
    m = str(mandate_id or "").strip()
    return f"{p}.{m}" if m else p


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


_WORD_RE = re.compile(r"[a-z0-9]+")


def dedup_key(task: str) -> str:
    """Content key for "is this goal already running" — order-insensitive word set."""
    words = sorted(set(_WORD_RE.findall(str(task or "").lower())))
    return hashlib.sha1(" ".join(words).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def from_row(row: dict) -> Project:
    """Build a Project from a store record (agent_projects_store normalizes times)."""

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    try:
        priority = int(row.get("priority", DEFAULT_PRIORITY))
    except (TypeError, ValueError):
        priority = DEFAULT_PRIORITY
    if priority not in PRIORITY_WEIGHT:
        priority = DEFAULT_PRIORITY
    unblocks = row.get("unblocks") or ()
    if isinstance(unblocks, str):
        unblocks = (unblocks,)
    return Project(
        id=str(row.get("id") or ""),
        persona=str(row.get("persona") or ""),
        mandate_id=str(row.get("mandate_id") or ""),
        title=str(row.get("title") or ""),
        task=str(row.get("task") or ""),
        state=str(row.get("state") or READY),
        priority=priority,
        user_waiting=bool(row.get("user_waiting")),
        unblocks=tuple(str(u) for u in unblocks),
        deadline_at=_f(row.get("deadline_at")),
        urgency_score=_f(row.get("urgency_score")),
        ready_at=_f(row.get("ready_at")) or 0.0,
        deferred_until=_f(row.get("deferred_until")),
        last_started_at=_f(row.get("last_started_at")),
        in_flight_task_id=str(row.get("in_flight_task_id") or ""),
        max_runs=int(row.get("max_runs", 1) or 0),
        runs=int(row.get("runs", 0) or 0),
    )


# ── Layer 1: eligibility ─────────────────────────────────────────────────────


def effective_state(p: Project, now: float) -> str:
    """A PENDING row whose backoff has passed is READY for selection; the
    store flips the stored state when the row is claimed."""
    if p.state == PENDING and (p.deferred_until is None or p.deferred_until <= now):
        return READY
    return p.state


def effective_ready_at(p: Project, now: float) -> float:
    """When the aging clock started: for a promoted PENDING row, when its backoff
    ended — not when it was first readied before the failure."""
    if p.state == PENDING and p.deferred_until is not None:
        return max(p.ready_at, p.deferred_until)
    return p.ready_at


def eligible(
    p: Project,
    agent: AgentInfo | None,
    now: float,
    *,
    in_flight_agents: frozenset[str] = frozenset(),
    in_flight_keys: frozenset[str] = frozenset(),
    cfg: Config = DEFAULT_CONFIG,
) -> bool:
    if effective_state(p, now) != READY:
        return False
    if not p.task.strip():
        return False
    if agent is None or not agent.enabled or agent.answer_only:
        return False
    if (agent.tier or "lite") != "full":
        return False
    if agent.daily_cap_usd is not None and agent.spend_today_usd >= agent.daily_cap_usd:
        return False
    if p.agent_id in in_flight_agents and cfg.per_agent_in_flight <= 1:
        return False
    return dedup_key(p.task) not in in_flight_keys


# ── Layer 3: score ───────────────────────────────────────────────────────────


def static_score(
    p: Project,
    agent: AgentInfo,
    now: float,
    *,
    open_ids: frozenset[str] = frozenset(),
    spend_median_usd: float = 0.0,
) -> float:
    """Every term bounded: this is what the aging term is proved against."""
    unblocks_open = sum(1 for u in p.unblocks if u in open_ids and u != p.id)
    block = min(1.0, unblocks_open / 2.0)
    waiting = 1.0 if p.user_waiting else 0.0
    deadline = 0.0
    if p.deadline_at is not None:
        deadline = clamp(1.0 - (p.deadline_at - now) / DEADLINE_LEAD_S, 0.0, DEADLINE_MAX)
    prio = PRIORITY_WEIGHT.get(p.priority, PRIORITY_WEIGHT[DEFAULT_PRIORITY])
    urgency = clamp(p.urgency_score, 0.0, 1.0) if p.urgency_score is not None else 0.0
    fair = clamp((spend_median_usd - agent.spend_today_usd) / SPEND_REF_USD, -1.0, 1.0)
    return (
        W_BLOCK * block
        + W_WAITING * waiting
        + W_DEADLINE * deadline
        + W_PRIORITY * prio
        + W_URGENCY * urgency
        + W_FAIR * fair
    )


def age_points(p: Project, now: float, cfg: Config = DEFAULT_CONFIG) -> float:
    """Unbounded, monotone, and ONLY while the row is effectively ready."""
    if effective_state(p, now) != READY:
        return 0.0
    return max(0.0, now - effective_ready_at(p, now)) / cfg.age_tau_s


def score(
    p: Project,
    agent: AgentInfo,
    now: float,
    *,
    open_ids: frozenset[str] = frozenset(),
    spend_median_usd: float = 0.0,
    cfg: Config = DEFAULT_CONFIG,
) -> float:
    return static_score(
        p, agent, now, open_ids=open_ids, spend_median_usd=spend_median_usd
    ) + age_points(p, now, cfg)


# ── Layer 2 + selection ──────────────────────────────────────────────────────


def last_served(agent_id: str, projects: list[Project]) -> float | None:
    """When this agent last had a project step start; None = never."""
    stamps = [p.last_started_at for p in projects if p.agent_id == agent_id and p.last_started_at]
    return max(stamps) if stamps else None


def select(
    projects: list[Project],
    agents: dict[str, AgentInfo],
    capacity: Capacity,
    now: float,
    *,
    cfg: Config = DEFAULT_CONFIG,
    in_flight_agents: frozenset[str] = frozenset(),
    in_flight_keys: frozenset[str] = frozenset(),
) -> Selection | None:
    """The next project to start, or None. Pure: identical inputs → identical output,
    and a None result implies nothing about the inputs was touched."""
    if capacity.in_flight >= capacity.cap:
        return None
    ready = [
        p
        for p in projects
        if eligible(
            p,
            agents.get(p.agent_id),
            now,
            in_flight_agents=in_flight_agents,
            in_flight_keys=in_flight_keys,
            cfg=cfg,
        )
    ]
    if not ready:
        return None

    # Layer 2: the guarantee. An agent that has eligible work and has waited past
    # the SLA (or has never been served) preempts the score entirely.
    candidate_agents = {p.agent_id for p in ready}
    starved = set()
    for a in candidate_agents:
        served = last_served(a, projects)
        if served is None or now - served > cfg.max_agent_wait_s:
            starved.add(a)
    forced = bool(starved) and starved != candidate_agents
    pool = [p for p in ready if p.agent_id in starved] if starved else ready

    # Layer 3: the score, over the pool.
    open_ids = frozenset(p.id for p in projects if effective_state(p, now) in OPEN_STATES)
    spends = [agents[a].spend_today_usd for a in candidate_agents]
    spend_median = statistics.median(spends) if spends else 0.0

    def _key(p: Project):
        s = score(
            p,
            agents[p.agent_id],
            now,
            open_ids=open_ids,
            spend_median_usd=spend_median,
            cfg=cfg,
        )
        return (-s, effective_ready_at(p, now), p.id)

    best = min(pool, key=_key)
    best_score = -_key(best)[0]
    return Selection(
        project=best,
        score=best_score,
        forced=forced,
        reason="starve_guard" if forced else "score",
    )
