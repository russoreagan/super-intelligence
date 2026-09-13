"""Gateway pool reconciler — one tick over the RunPodPool, as a function.

The old reconciler was a closure inside gateway.main() driving ONE RunPodManager:
bill uptime, read the demand/use files, should_hold_pod, ensure_running or pause.
This keeps that contract for pod 0 exactly (0→1 on demand, 1→0 on disuse, budget,
churn cooldown) and adds the pool around it:

  1. failover      — probe every held pod; a dead one is released and its consumers
                     fall back into the assignment step below (plan §10.5).
  2. bill          — uptime per HELD pod per tick, so pod_daily_usd_budget caps the
                     pool, converting at the highest held rate. The ceiling itself is
                     read each tick (runtime file > bundled settings — see
                     pod_budget._settings_budget_usd), so a superadmin edit is live
                     on the next tick without a restart.
  3. assign        — live process keys → ready pods, sticky and least-loaded. Only
                     newcomers, orphans of a dead/drained pod, and scale-ups move.
  4. pressure      — fold the per-process pressure files onto pods; the youngest
                     demand/use across pressure files AND the legacy .pod_demand /
                     .pod_used touches feed pod 0's decision, so a brain that predates
                     the pressure file still wakes the pod.
  5. pod 0         — should_hold_pod, unchanged. Not held → grace → record_sleep →
                     pause the WHOLE pool (nothing anywhere is producing).
  6. pods above 0  — pod_pool.decide_scale: up (scale_up), down (drain_plan → move
                     consumers → drain; terminate once drain_s has passed), hold.
  7. publish       — the pool file and the legacy host file.

Pure enough to test with a fake pool and a fake provisioner: every dependency is a
parameter or a module the tests already monkeypatch (pod_budget, provisioner files).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from brain import pod_budget, pod_pool, pod_pressure

logger = logging.getLogger(__name__)


@dataclass
class ReconcileState:
    """Cross-tick memory for pod 0 (the pool keeps its own ScaleHistory)."""

    idle_since: float | None = None
    pod0_up_since: float | None = None
    last_tick: float = field(default_factory=time.time)
    # What the last tick observed, so next_deadline() can schedule the pause /
    # cooldown / rollover without another tick having to look.
    use_seen_at: float | None = None
    demand_seen_at: float | None = None
    over_budget: bool = False
    pod0_held: bool = False


def _youngest(*ages: float | None) -> float | None:
    vals = [a for a in ages if a is not None]
    return min(vals) if vals else None


async def reconcile_tick(
    pool,
    provisioner,
    state: ReconcileState,
    *,
    now: float | None = None,
    pressure_dir: Path | None = None,
) -> dict:
    """One reconciler tick. Returns what it did (for logs and tests)."""
    from brain.provisioner import pod_demand_age_s, pod_use_age_s

    cfg = pool.cfg
    ts = time.time() if now is None else now
    elapsed, state.last_tick = ts - state.last_tick, ts
    report: dict = {"actions": [], "decision": None}

    # 1. failover — a dead pod is released before anything is billed or assigned to it.
    try:
        probes = await pool.probe_all()
        dead = [pid for pid, ok in probes.items() if not ok]
        if dead:
            report["actions"].append(f"released_dead:{','.join(dead)}")
    except Exception as e:
        logger.debug("[pool] probe_all failed: %s", e)

    pods = pool.pods(ts)
    held = [p for p in pods if p.held]

    # 2. bill first, decide second — uptime per held pod, at the highest held rate.
    if held:
        pod_budget.set_rate_per_hr(pool._cost_per_hr)
        for _ in held:
            pod_budget.record_uptime(elapsed)

    # 3. assignment — sticky, least-loaded, ready pods only. A consumer whose own
    # dedicated pod is READY (placement_control → pool.standalone) is not the
    # pool's: it neither takes a slot nor feeds the pool's pressure.
    dedicated = set(getattr(pool, "standalone", None) or {})
    consumers = [k for k in provisioner.keys_for_all() if k not in dedicated]
    before = dict(pool.assignments)
    pool.assignments = pod_pool.assign(consumers, pods, before)
    moved = {k for k, v in pool.assignments.items() if before.get(k) != v}
    if moved:
        report["actions"].append(f"assigned:{len(moved)}")

    # 4. pressure — per pod, plus pool-wide demand/use (youngest of every channel).
    samples = {k: v for k, v in pod_pressure.read_all(pressure_dir).items() if k not in dedicated}
    agg = pod_pool.aggregate_pressure(samples, pool.assignments, ts, cfg.parallel)
    pod_pool.apply_pressure(pods, agg)
    demand_age = _youngest(pod_demand_age_s(), agg.demand_age_s)
    use_age = _youngest(pod_use_age_s(), agg.use_age_s)
    full = provisioner.full_count()
    over_budget = pod_budget.exhausted()
    state.use_seen_at = (ts - use_age) if use_age is not None else None
    state.demand_seen_at = (ts - demand_age) if demand_age is not None else None
    state.over_budget = bool(over_budget)
    report.update(
        {
            "held": len(held),
            "ready": sum(1 for p in pods if p.ready),
            "consumers": len(consumers),
            "demand_age_s": demand_age,
            "use_age_s": use_age,
            "full_tier_brains": full,
            "over_budget": over_budget,
        }
    )

    # 5. pod 0 — the existing wake/sleep contract, verbatim.
    pod0_up = pool._pod_id is not None
    if pod0_up and state.pod0_up_since is None:
        state.pod0_up_since = ts
    if not pod0_up:
        state.pod0_up_since = None
    hold0 = pod_budget.should_hold_pod(
        full_tier_brains=full,
        demand_age_s=demand_age,
        grace_s=cfg.grace_s,
        over_budget=over_budget,
        pod_is_up=pod0_up,
        use_age_s=use_age,
        up_for_s=(ts - state.pod0_up_since) if state.pod0_up_since else None,
        cooldown_active=pod_budget.cooldown_remaining_s() > 0,
    )
    paused = False
    if hold0:
        state.idle_since = None
        if pod_budget.budget_seconds() == 0 and not pod0_up:
            logger.warning(
                "[gateway] waking pool pod 0 with pod_daily_usd_budget=0 "
                "(UNCAPPED GPU spend — set a ceiling on the Fleet page or "
                "PUT /__fleet/pod_budget)"
            )
        await pool.ensure_min()
        report["actions"].append("ensure_min")
    else:
        if state.idle_since is None:
            state.idle_since = ts
        # Budget exhaustion sleeps immediately — the grace period damps demand
        # flapping, and waiting it out would bill past a ceiling already breached.
        due = over_budget or ts - state.idle_since >= cfg.grace_s
        if due and held:
            if over_budget:
                st = pod_budget.status()
                logger.warning(
                    "[gateway] GPU budget spent ($%.2f/$%.2f today, %.0f min at $%.2f/hr) — "
                    "sleeping the pool (%d pod%s) until UTC rollover",
                    st["usd_today"],
                    st["usd_budget"],
                    st["minutes_used"],
                    st["rate_per_hr"],
                    len(held),
                    "" if len(held) == 1 else "s",
                )
            else:
                logger.info(
                    "[gateway] pool idle — no output for %s (demand %s, full-tier brains=%d) "
                    "— sleeping %d pod%s",
                    f"{use_age:.0f}s" if use_age is not None else "ever",
                    f"{demand_age:.0f}s ago" if demand_age is not None else "none",
                    full,
                    len(held),
                    "" if len(held) == 1 else "s",
                )
            # Arm the churn guard from whether this session actually produced anything.
            produced = use_age is not None and use_age <= cfg.grace_s
            pod_budget.record_sleep(produced)
            if not produced:
                logger.warning(
                    "[gateway] pool produced nothing this session — backing off %.0f min "
                    "before honouring the next wake",
                    pod_budget.cooldown_remaining_s() / 60.0,
                )
            await pool.pause()
            paused = True
            report["actions"].append("pause_all")

    # 6. pods above 0 — scale on pressure. Skipped on the tick that slept everything.
    if not paused:
        decision = pod_pool.decide_scale(
            pods,
            pool.history,
            cfg,
            ts,
            over_budget=over_budget,
            cooldown_until=pool.cooldown_until,
            full_tier_brains=full,
        )
        report["decision"] = decision
        if decision == "up":
            new_id = await pool.scale_up()
            report["actions"].append(f"scale_up:{new_id or 'failed'}")
        elif decision.startswith("down:"):
            pid = decision[5:]
            if pool.index_of(pid) == 0:
                pass  # pod 0 comes down only through should_hold_pod above
            elif not pool.is_draining(pid):
                plan = pod_pool.drain_plan(pid, pool.assignments, pods)
                for consumer, target in plan.items():
                    if target:
                        pool.assignments[consumer] = target
                    else:
                        pool.assignments.pop(consumer, None)
                pool.drain(pid)
                report["actions"].append(f"drain:{pid}:{len(plan)}")
        for pid in pool.draining_due(ts):
            await pool.terminate(pid)
            report["actions"].append(f"terminate:{pid}")

    # 7. publish — the pool file for pool-aware brains, the legacy file for the rest.
    state.pod0_held = pool._pod_id is not None
    pool.publish(ts)
    return report


def next_deadline(pool, state: ReconcileState, now: float) -> float | None:
    """The earliest moment the pool needs a tick with no event: pod 0's
    pause-after-grace (anchored on the last output, or on wake), the end of a
    churn cooldown while demand is pending, a scale dwell running out, a drain
    coming due, the UTC rollover that refills the platform budget."""
    from brain.gateway.reconciler import earliest, next_utc_midnight

    cfg = pool.cfg
    dls: list[float | None] = []
    if state.pod0_held:
        if state.idle_since is not None:
            dls.append(state.idle_since + cfg.grace_s)
        anchor = max(state.use_seen_at or 0.0, state.pod0_up_since or 0.0)
        if anchor > 0:
            dls.append(anchor + cfg.grace_s)
    else:
        cd = pod_budget.cooldown_remaining_s()
        if cd > 0 and state.demand_seen_at is not None:
            dls.append(now + cd)
    if state.over_budget:
        dls.append(next_utc_midnight(now))
    hist = getattr(pool, "history", None)
    if hist is not None:
        if getattr(hist, "hot_since", None):
            dls.append(hist.hot_since + cfg.up_after_s)
        for since in (getattr(hist, "cold_since", None) or {}).values():
            dls.append(since + cfg.down_after_s)
    drain = getattr(pool, "_drain_since", None) or {}
    for since in drain.values():
        dls.append(since + cfg.drain_s)
    return earliest(*dls)
