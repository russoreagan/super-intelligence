"""Pod pool — the pure decision layer for scaling the shared GPU pod horizontally.

Operator decision (2026-09-12): "at the basic level all individuals go into the same
queue and the pod scales as needed to support that." One queue, N identical pods.

This module holds every decision as a pure function so it is testable without a
gateway, a RunPod key or a clock:

  aggregate_pressure  — fold the per-process pressure files (brain/pod_pressure) onto
                        the pods their processes are assigned to; utilisation per pod
                        = Σ busy_s_1m(consumers) / (60 × OLLAMA_NUM_PARALLEL).
  decide_scale        — hold | up | down:<pod_id> | none, with dwell times (a hot minute
                        is not a trend), a cooldown after a scale event, the daily
                        budget (exhausted → never up, sleep the largest index first),
                        and the tier gate (no full-tier brain → nothing to scale for).
  assign              — consumer → pod, least-loaded and STICKY (a consumer keeps its
                        pod while that pod is ready; no mass reshuffle on every tick).
  drain_plan          — where a draining pod's consumers go.
  pool_file_body /    — the shape of tenants/.runpod_pool.json (gateway writes, brains
  resolve_pool_host     read) and the consumer-side lookup, in one place so the two
                        sides cannot drift.

Pod 0 keeps its existing lifecycle: pod_budget.should_hold_pod decides 0→1 on the
demand signal and 1→0 on the use signal exactly as before. decide_scale only ever
adds pods above pod 0 and only ever removes pods above pod 0. That keeps the
single-pod deployment byte-identical in behaviour when max_pods == 1.

Assignment reuses brain/pod_scheduler.PodScheduler (the existing pure allocator) in
its 'spread' strategy rather than duplicating the bookkeeping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from brain.pod_scheduler import PodScheduler

# Legacy pod name; pool pods p2..pN are `<POOL_POD_NAME>-p<i+1>`. The name is the
# discovery key on RunPod (exact match), so every pod in the pool needs its own.
POOL_POD_NAME = "ollama-brain"
# Consumers per pool pod. Unbounded on purpose: the pool is one shared queue, and the
# semaphore in each brain (local_max_concurrent) plus OLLAMA_NUM_PARALLEL on the pod
# bound concurrency; assignment only balances queue depth.
_UNBOUNDED = 1 << 30
# A pressure sample older than this is a dead or reaped process, not a quiet one.
STALE_SAMPLE_S = 180.0


def pod_name_for(index: int) -> str:
    return POOL_POD_NAME if index == 0 else f"{POOL_POD_NAME}-p{index + 1}"


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "").strip()
        return int(float(raw)) if raw else default
    except ValueError:
        return default


@dataclass
class PoolConfig:
    """Scaling dials. Defaults chosen 2026-09-12 (plan §10.6); every one has an env
    override so an operator can retune without a deploy."""

    min_pods: int = 0  # 0 = wake-on-demand for pod 0 (should_hold_pod), nothing pinned
    max_pods: int = 3
    parallel: int = 2  # OLLAMA_NUM_PARALLEL on pool pods; utilisation denominator
    up_util: float = 0.75  # any pod this busy (1-min) ...
    up_wait_p95_s: float = 8.0  # ... or with slot waits this long ...
    up_after_s: float = 300.0  # ... sustained this long → add a pod
    down_util: float = 0.20  # a pod this idle ...
    down_after_s: float = 900.0  # ... for this long → drain + remove it
    cooldown_s: float = 600.0  # after a scale event, no further scale-UP for this long
    grace_s: float = 600.0  # pod 0 idle grace (mirrors BRAIN_POD_IDLE_GRACE_S)
    drain_s: float = 90.0  # after consumers are moved, wait this long before terminating

    @classmethod
    def from_env(cls) -> PoolConfig:
        d = cls()
        return cls(
            min_pods=max(0, _env_int("BRAIN_POOL_MIN_PODS", d.min_pods)),
            max_pods=max(1, _env_int("BRAIN_POOL_MAX_PODS", d.max_pods)),
            parallel=max(1, _env_int("RUNPOD_NUM_PARALLEL", d.parallel)),
            up_util=_env_float("BRAIN_POOL_UP_UTIL", d.up_util),
            up_wait_p95_s=_env_float("BRAIN_POOL_UP_WAIT_P95_S", d.up_wait_p95_s),
            up_after_s=_env_float("BRAIN_POOL_UP_AFTER_S", d.up_after_s),
            down_util=_env_float("BRAIN_POOL_DOWN_UTIL", d.down_util),
            down_after_s=_env_float("BRAIN_POOL_DOWN_AFTER_S", d.down_after_s),
            cooldown_s=_env_float("BRAIN_POOL_COOLDOWN_S", d.cooldown_s),
            grace_s=_env_float("BRAIN_POD_IDLE_GRACE_S", d.grace_s),
            drain_s=_env_float("BRAIN_POOL_DRAIN_S", d.drain_s),
        )


@dataclass
class PodSample:
    """One pod as the reconciler sees it this tick: identity, lifecycle state, and the
    pressure folded onto it by aggregate_pressure."""

    pod_id: str
    index: int  # slot in the pool; 0 = the legacy "ollama-brain" pod
    state: str = "off"  # runpod_manager.POD_STATES + "draining"
    host: str | None = None
    name: str = ""
    kind: str = "pool"  # pool | standalone | org (the latter two arrive with §10.4)
    gpu: str | None = None
    cost_per_hr: float | None = None
    parallel: int = 2
    consumers: int = 0
    busy_frac: float = 0.0
    wait_p95_s: float = 0.0
    up_since: float | None = None
    drain_since: float | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def held(self) -> bool:
        """Costing money (or about to): anything but off/failed."""
        return self.state not in ("off", "failed")

    def as_dict(self) -> dict:
        return {
            "pod_id": self.pod_id,
            "index": self.index,
            "host": self.host,
            "kind": self.kind,
            "name": self.name or pod_name_for(self.index),
            "state": self.state,
            "gpu": self.gpu,
            "cost_per_hr": self.cost_per_hr,
            "parallel": self.parallel,
            "consumers": self.consumers,
            "busy_frac_5m": round(self.busy_frac, 3),
            "wait_p95_s": round(self.wait_p95_s, 3),
        }


@dataclass
class PodPressure:
    pod_id: str
    consumers: int = 0
    calls_1m: int = 0
    busy_s_1m: float = 0.0
    busy_frac: float = 0.0
    wait_p95_s: float = 0.0
    inflight: int = 0
    fail_1m: int = 0
    demand_age_s: float | None = None
    use_age_s: float | None = None


@dataclass
class PoolPressure:
    pods: dict[str, PodPressure] = field(default_factory=dict)
    unassigned: list[str] = field(default_factory=list)
    samples: int = 0
    demand_age_s: float | None = None  # youngest demand across every process
    use_age_s: float | None = None  # youngest productive use across every process


def _min_age(cur: float | None, ts: float | None, now: float) -> float | None:
    if ts is None:
        return cur
    age = max(0.0, now - float(ts))
    return age if cur is None or age < cur else cur


def aggregate_pressure(
    samples: dict[str, dict],
    assignments: dict[str, str],
    now: float,
    parallel: int,
    *,
    max_age_s: float = STALE_SAMPLE_S,
) -> PoolPressure:
    """Fold per-process pressure snapshots onto pods.

    `samples` is proc_key → the body of that process's pressure file (see
    brain/pod_pressure.write_snapshot); `assignments` is proc_key → pod_id. A process
    with no assignment is reported in `unassigned` (it is on the legacy host, i.e. pod
    0, or it has nothing to talk to) and its demand/use still count pool-wide, because
    an unassigned brain asking for the pod is exactly what should wake pod 0."""
    out = PoolPressure()
    denom = 60.0 * max(1, int(parallel))
    for key, body in samples.items():
        try:
            ts = float(body.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if now - ts > max_age_s:
            continue
        out.samples += 1
        out.demand_age_s = _min_age(out.demand_age_s, body.get("demand_ts"), now)
        out.use_age_s = _min_age(out.use_age_s, body.get("use_ts"), now)
        pid = assignments.get(key)
        if pid is None:
            out.unassigned.append(key)
            continue
        pp = out.pods.setdefault(pid, PodPressure(pod_id=pid))
        pp.consumers += 1
        pp.calls_1m += int(body.get("calls_1m") or 0)
        pp.busy_s_1m += float(body.get("busy_s_1m") or 0.0)
        pp.inflight += int(body.get("inflight") or 0)
        pp.fail_1m += int(body.get("fail_1m") or 0)
        pp.wait_p95_s = max(pp.wait_p95_s, float(body.get("wait_p95_s") or 0.0))
        pp.demand_age_s = _min_age(pp.demand_age_s, body.get("demand_ts"), now)
        pp.use_age_s = _min_age(pp.use_age_s, body.get("use_ts"), now)
    for pp in out.pods.values():
        pp.busy_frac = min(1.0, pp.busy_s_1m / denom)
    return out


def apply_pressure(pods: list[PodSample], pressure: PoolPressure) -> None:
    """Stamp the aggregated pressure onto the samples decide_scale will read."""
    for p in pods:
        pp = pressure.pods.get(p.pod_id)
        p.consumers = pp.consumers if pp else 0
        p.busy_frac = pp.busy_frac if pp else 0.0
        p.wait_p95_s = pp.wait_p95_s if pp else 0.0


@dataclass
class ScaleHistory:
    """Dwell-time memory the caller keeps across ticks. decide_scale mutates it."""

    hot_since: float | None = None
    cold_since: dict[str, float] = field(default_factory=dict)
    last_scale_ts: float = 0.0


def _largest(pods: list[PodSample]) -> PodSample:
    return max(pods, key=lambda p: p.index)


def decide_scale(
    pods: list[PodSample],
    history: ScaleHistory,
    cfg: PoolConfig,
    now: float,
    *,
    over_budget: bool,
    cooldown_until: float,
    full_tier_brains: int,
) -> str:
    """One scaling verdict per tick for the pods ABOVE pod 0.

    Returns:
      'up'            — bring up the next pool slot,
      'down:<pod_id>' — drain that pod, then terminate it,
      'hold'          — the pool is sized right (or a dwell is still counting),
      'none'          — nothing held and nothing to do (pod 0's wake is should_hold_pod's).

    Rules, in priority order:
      • budget spent → never up; sleep the LARGEST index first (pod 0 last, and pod 0
        only through this path when it is the only pod — should_hold_pod also sleeps it
        on over_budget, so the two agree);
      • no full-tier brain → nothing to scale for: pods above 0 come down, pod 0 is
        should_hold_pod's call;
      • below min_pods → up (respecting cooldown);
      • any ready pod hot (busy ≥ up_util or slot-wait p95 ≥ up_wait_p95_s) for
        up_after_s, every held pod already ready (capacity that is still booting is
        capacity, not a reason for more), below max_pods, cooldown elapsed → up;
      • a ready pod above 0 idle (busy ≤ down_util) for down_after_s, pool above
        min_pods, AND the rest of the pool can absorb its load without going hot
        (otherwise draining it just triggers the next scale-up: oscillation) → down.
    """
    held = sorted([p for p in pods if p.held], key=lambda p: p.index)
    n = len(held)
    cooling = now < cooldown_until

    if n == 0:
        if cfg.min_pods > 0 and full_tier_brains > 0 and not over_budget and not cooling:
            return "up"
        return "none"

    if over_budget:
        history.hot_since = None
        return f"down:{_largest(held).pod_id}"

    above0 = [p for p in held if p.index > 0]
    if full_tier_brains <= 0:
        history.hot_since = None
        return f"down:{_largest(above0).pod_id}" if above0 else "none"

    if n < cfg.min_pods:
        return "hold" if cooling else "up"

    ready = [p for p in held if p.ready]
    all_ready = len(ready) == n
    hot = any(p.busy_frac >= cfg.up_util or p.wait_p95_s >= cfg.up_wait_p95_s for p in ready)
    if hot:
        if history.hot_since is None:
            history.hot_since = now
        sustained = now - history.hot_since >= cfg.up_after_s
        if sustained and all_ready and n < cfg.max_pods and not cooling:
            history.hot_since = None
            history.last_scale_ts = now
            return "up"
        return "hold"
    history.hot_since = None

    # Scale-down: only pods above 0, only when the pool stays above min_pods, and only
    # when the remaining pods can take the load without crossing the up threshold.
    total_busy = sum(p.busy_frac for p in ready)
    cold_ids: set[str] = set()
    for p in sorted([p for p in ready if p.index > 0], key=lambda p: -p.index):
        if p.busy_frac > cfg.down_util:
            continue
        cold_ids.add(p.pod_id)
        since = history.cold_since.setdefault(p.pod_id, now)
        if now - since < cfg.down_after_s or n <= cfg.min_pods:
            continue
        remaining = len(ready) - 1
        if remaining > 0 and total_busy / remaining >= cfg.up_util:
            continue  # merging its load would make the rest hot — keep it
        history.cold_since.pop(p.pod_id, None)
        history.last_scale_ts = now
        return f"down:{p.pod_id}"
    for pid in list(history.cold_since):
        if pid not in cold_ids:
            del history.cold_since[pid]
    return "hold"


def assign(consumers: list[str], pods: list[PodSample], current: dict[str, str]) -> dict[str, str]:
    """consumer → pod_id over the READY pods. Sticky first: a consumer already on a
    ready pod keeps it. Newcomers (and consumers whose pod went away) go to the
    least-loaded pod, ties to the lowest index. A consumer is absent from the result
    when no pod is ready — it then follows the legacy host file (pod 0 or off)."""
    ready = sorted([p for p in pods if p.ready], key=lambda p: p.index)
    if not ready or not consumers:
        return {}
    sched = PodScheduler(default_capacity=_UNBOUNDED, strategy="spread")
    for p in ready:
        sched.register_pod(p.pod_id, model="pool", host=p.host or p.pod_id)
    ready_ids = {p.pod_id for p in ready}
    out: dict[str, str] = {}
    for c in consumers:
        cur = current.get(c)
        if cur in ready_ids:
            sched.confirm_pod(c, cur)
            out[c] = cur
    for c in consumers:
        if c in out:
            continue
        placement = sched.allocate(c, model="pool")
        if placement.mode == "assigned" and placement.pod_id:
            out[c] = placement.pod_id
    return out


def drain_plan(
    pod_id: str, assignments: dict[str, str], pods: list[PodSample]
) -> dict[str, str | None]:
    """Where the consumers of `pod_id` go: spread over the other ready pods, least
    loaded first, given everyone else stays put. None when no other pod is ready
    (the consumer drops to the legacy host file)."""
    movers = [c for c, p in assignments.items() if p == pod_id]
    if not movers:
        return {}
    others = sorted([p for p in pods if p.ready and p.pod_id != pod_id], key=lambda p: p.index)
    if not others:
        return dict.fromkeys(movers)
    sched = PodScheduler(default_capacity=_UNBOUNDED, strategy="spread")
    other_ids = set()
    for p in others:
        sched.register_pod(p.pod_id, model="pool", host=p.host or p.pod_id)
        other_ids.add(p.pod_id)
    for c, p in assignments.items():
        if p in other_ids:
            sched.confirm_pod(c, p)
    out: dict[str, str | None] = {}
    for c in movers:
        placement = sched.allocate(c, model="pool")
        out[c] = placement.pod_id if placement.mode == "assigned" else None
    return out


# ── the pool file (gateway → brains) ─────────────────────────────────────────


def pool_file_body(
    pods: list[PodSample],
    assignments: dict[str, str],
    standalone: dict[str, dict] | None = None,
    *,
    now: float,
    fallback: dict[str, dict] | None = None,
) -> dict:
    """tenants/.runpod_pool.json — plan §10.2. `standalone` (proc_key → {pod_id, host,
    state}) is empty until the premium tier lands; the key is reserved here so the
    consumer lookup below is already final."""
    return {
        "ts": now,
        "pods": [p.as_dict() for p in sorted(pods, key=lambda p: p.index)],
        "assignments": dict(assignments),
        "standalone": dict(standalone or {}),
        # Consumers whose dedicated pod is not serving right now (booting, failed,
        # budget-paused): they ride the pool via `assignments`; this says why, for
        # GET /v1/personas/{p}/placement's `live.pod_state = fallback_pool`.
        "fallback": dict(fallback or {}),
    }


def resolve_pool_host(data: dict | None, key: str, legacy_host: str | None) -> str | None:
    """Consumer side. Which host should process `key` talk to?

    Returns the host URL; "" meaning the pod is OFF (adopt the fail-fast sentinel);
    or `legacy_host` (the .runpod_host file's content, None if absent) when the pool
    file says nothing about this process. Precedence: a standalone entry for the key,
    else its pool assignment, else legacy. A pod that is assigned but absent or not
    ready reads as off — a brain must never time out against a pod that is booting,
    draining or gone."""
    if not isinstance(data, dict):
        return legacy_host
    standalone = data.get("standalone") or {}
    if isinstance(standalone, dict) and key in standalone:
        sa = standalone.get(key) or {}
        host = str(sa.get("host") or "")
        return host if host and sa.get("state") == "ready" else ""
    assignments = data.get("assignments") or {}
    pid = assignments.get(key) if isinstance(assignments, dict) else None
    if pid is None:
        return legacy_host
    pods = {p.get("pod_id"): p for p in (data.get("pods") or []) if isinstance(p, dict)}
    pod = pods.get(pid)
    if not pod or pod.get("state") != "ready" or not pod.get("host"):
        return ""
    return str(pod["host"])
