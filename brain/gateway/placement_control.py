"""Placement desired-state loop (plan §10.4 / §10.5) — the consumer of `persona_placement`.

A row in `persona_placement` (migration 038) is an ENTITLEMENT: "this persona gets a
brain process of its own, and that process talks to this GPU". Nothing else in the
system reads the row. This module is the loop that makes reality match it, one tick
at a time, from the gateway's reconciler:

  1. desired   — every org's rows (service role, one query, 60 s cache; a failed
                 read serves the last-known rows, never an empty set — a database
                 blink must not look like every placement being revoked).
  2. processes — a `dedicated`, unexpired row wants an `org::persona` process:
                 spawn it (in the background, the provisioner's caps apply and a
                 refusal is reported, not retried every tick), pin it past the idle
                 reaper when `always_on`. A live dedicated process with no such row
                 (deleted, expired, demoted) is consolidated and stopped.
  3. pods      — a row with `pod: standalone` wants a pod of its own (keyed by the
                 process), `pod: org` one pod shared by the org's dedicated instances
                 (keyed by the org). Each is a RunPodManager with its own pod name so
                 it is rediscovered after a redeploy. It wakes on the consumers'
                 demand, is held while they get output, and is paused after the pool's
                 grace period — the same demand/use contract as pool pod 0, per pod.
                 Two ceilings sit above that: the org's `gpu_daily_usd_budget` (0 =
                 no pods at all; spent = pods sleep until UTC rollover) and the
                 platform backstop BRAIN_MAX_STANDALONE_PODS. Anything that stops a
                 pod from serving leaves its consumers on the POOL (`fallback_pool`)
                 rather than off.
  4. metering  — one additive `gpu_usage` row per held pod per tick (an org pod's
                 tick is split evenly across its consumers), plus an in-memory
                 accrual so the budget check does not wait for the read cache.
  5. publish   — the ready pods land in the pool file's `standalone` map (consumer
                 key → host), which brain/pod_pool.resolve_pool_host prefers over the
                 pool assignment; the pool reconciler drops those consumers from its
                 own assignment and pressure. Consumers whose pod is booting, failed,
                 or budget-paused stay on the pool and appear in `fallback`.

Nothing here decides *whether* a persona deserves a placement (the owner API did,
with the caps) and nothing here touches learning state: the process it spawns
serves the org-canonical persona path, exactly as the shared instance would.

Kill switch: BRAIN_MULTI_PERSONA=0 disables both this loop and the header routing
that reaches dedicated instances, so the two can never disagree (a dedicated
process nobody can route to is a stranded persona: the shared instance refuses it
with 409 and the header is ignored).

Pure enough to test: the provisioner, the pool, the manager factory, the clock and
the Supabase client are all parameters.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brain import pod_pool, pod_pressure
from brain.pod_pool import PodSample

logger = logging.getLogger(__name__)

DESIRED_TTL_S = 60.0
SPEND_READ_TTL_S = 300.0
CAPS_TTL_S = 300.0
# A pod above the pool with no consumer for this long is paused even without the
# demand/use contract having a say (its instance was stopped or re-placed).
ORPHAN_GRACE_S = 120.0


def enabled() -> bool:
    """BRAIN_MULTI_PERSONA is the one switch for the premium tier: routing AND this
    loop. Default ON — the header can only reach a promoted persona and nothing
    spawns without a placement row, so an org with no rows is byte-identical to a
    deployment with the flag off. `0`/`false`/`no` is the kill switch."""
    return os.environ.get("BRAIN_MULTI_PERSONA", "1").strip().lower() not in ("0", "false", "no")


def max_standalone_pods() -> int:
    """Platform backstop on dedicated pods of every kind (0 = uncapped)."""
    try:
        return int(os.environ.get("BRAIN_MAX_STANDALONE_PODS", "4") or 0)
    except ValueError:
        return 4


def pod_name_for(kind: str, key: str) -> str:
    """Deterministic RunPod pod name for a dedicated pod, so a redeployed gateway
    adopts the same pod by name (RunPodManager discovers by exact name)."""
    h = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"ollama-{'sa' if kind == 'standalone' else 'org'}-{h}"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def gateway_client():
    """The service-role Supabase client for every read and write in this module.
    The gateway is not pinned to an org, so the tenant-side helpers' get_org_id()
    path is never used here — the org is always passed explicitly. None when
    storage is local (every read then answers 'nothing', every write is skipped)."""
    try:
        from brain.gateway.fleet_orgs import _client

        return _client()
    except Exception as e:
        logger.debug("[placement] no service-role client: %s", e)
        return None


@dataclass
class DedicatedPod:
    """One pod above the pool: a RunPodManager plus the controller's view of it."""

    key: str  # proc key (standalone) or org id (org)
    kind: str  # standalone | org
    org: str
    manager: object
    gpu_type: str | None = None
    consumers: set[str] = field(default_factory=set)
    up_since: float | None = None
    last_bill: float | None = None
    discovered: bool = False
    waking: bool = False
    fallback_reason: str = ""  # why consumers are on the pool right now ("" = serving)
    no_consumer_since: float | None = None
    last_use_at: float | None = None  # youngest consumer output seen (for the pause deadline)
    rate_seen: float = 0.0  # $/hr at the last billing tick (for the budget-exhaustion ETA)

    @property
    def pod_id(self) -> str | None:
        return getattr(self.manager, "_pod_id", None)

    @property
    def state(self) -> str:
        return str(getattr(self.manager, "_status", "off") or "off")

    @property
    def host(self) -> str | None:
        pid = self.pod_id
        if not pid:
            return None
        fn = getattr(self.manager, "_pod_host", None)
        try:
            return fn(pid) if callable(fn) else getattr(self.manager, "host", None)
        except Exception:
            return None

    @property
    def held(self) -> bool:
        return self.pod_id is not None and self.state not in ("off", "failed")

    @property
    def serving(self) -> bool:
        return self.held and self.state == "ready" and bool(self.host)

    @property
    def cost_per_hr(self) -> float | None:
        v = getattr(self.manager, "_cost_per_hr", None)
        return float(v) if v else None


@dataclass
class PlacementState:
    """Cross-tick memory. One per gateway process."""

    pods: dict[str, DedicatedPod] = field(default_factory=dict)
    desired: dict[str, list[dict]] | None = None  # org → rows (last successful read)
    desired_read_at: float = 0.0
    desired_failed_logged: bool = False
    spawning: set[str] = field(default_factory=set)
    refused: dict[str, str] = field(default_factory=dict)
    spent: dict[str, tuple[str, float]] = field(default_factory=dict)  # org → (day, usd)
    spend_read_at: dict[str, float] = field(default_factory=dict)
    caps_read_at: dict[str, float] = field(default_factory=dict)
    budget_paused: set[str] = field(default_factory=set)
    logged: set[str] = field(default_factory=set)  # once-per-condition log keys
    last_tick: float = field(default_factory=time.time)

    def log_once(self, key: str, level: int, msg: str, *args) -> None:
        if key in self.logged:
            return
        self.logged.add(key)
        logger.log(level, msg, *args)

    def clear_log(self, key: str) -> None:
        self.logged.discard(key)


# ── desired state ─────────────────────────────────────────────────────────────


def _read_desired(state: PlacementState, now: float, client=None) -> dict[str, list[dict]] | None:
    """{org: rows} from the registry, cached DESIRED_TTL_S; the last good read on
    failure; None only when nothing has ever been read."""
    if state.desired is not None and now - state.desired_read_at < DESIRED_TTL_S:
        return state.desired
    from brain import persona_placement

    rows = persona_placement.list_all(client=client)
    if rows is None:
        if state.desired is None and not state.desired_failed_logged:
            state.desired_failed_logged = True
            logger.warning(
                "[placement] registry unreadable and nothing cached — no instance is "
                "started or stopped until it answers (apply migration 038?)"
            )
        return state.desired
    state.desired = rows
    state.desired_read_at = now
    state.desired_failed_logged = False
    return rows


def desired_instances(rows_by_org: dict[str, list[dict]], now: float) -> dict[str, dict]:
    """proc key → row for every unexpired dedicated placement."""
    from brain.persona_placement import is_expired

    out: dict[str, dict] = {}
    for org, rows in (rows_by_org or {}).items():
        for r in rows:
            if str(r.get("mode") or "") != "dedicated" or is_expired(r, now):
                continue
            slug = str(r.get("persona") or "").strip()
            if not slug:
                continue
            out[f"{org}::{slug}"] = {**r, "org": org}
    return out


# ── processes ─────────────────────────────────────────────────────────────────


async def _spawn_bg(state: PlacementState, provisioner, org: str, persona: str, key: str) -> None:
    try:
        await provisioner.ensure(org, persona)
        state.refused.pop(key, None)
        state.clear_log(f"refused:{key}")
        logger.info("[placement] dedicated instance up: %s", key[:40])
    except Exception as e:  # CapacityError or a failed boot — reported, retried next tick
        state.refused[key] = str(e)
        state.log_once(
            f"refused:{key}", logging.WARNING, "[placement] %s not started: %s", key[:40], e
        )
    finally:
        state.spawning.discard(key)


def _spawn_task(coro) -> None:
    """Schedule without awaiting so a cold start never stalls the reconcile tick.
    Tests without a running loop call the coroutine directly."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    asyncio.ensure_future(coro)


async def reconcile_processes(
    state: PlacementState,
    provisioner,
    desired: dict[str, dict],
    *,
    stop_instance,
    report: dict,
) -> None:
    """Spawn what is wanted and missing; pin per always_on; consolidate and stop
    what is live and unwanted."""
    live = {k for k in provisioner.keys_for_all() if "::" in k}
    for key, row in desired.items():
        org, _, persona = key.partition("::")
        pinned = bool(row.get("always_on", True))
        if key in live:
            setter = getattr(provisioner, "set_pinned", None)
            if callable(setter):
                setter(org, persona, pinned)
            continue
        if key in state.spawning:
            continue
        state.spawning.add(key)
        report["actions"].append(f"spawn:{persona}")
        _spawn_task(_spawn_bg(state, provisioner, org, persona, key))
    for key in sorted(live - set(desired)):
        org, _, persona = key.partition("::")
        report["actions"].append(f"stop:{persona}")
        logger.info("[placement] %s has no live placement — consolidating and stopping", key[:40])
        try:
            await stop_instance(org, persona)
        except Exception as e:
            logger.warning("[placement] stop %s failed: %s", key[:40], e)


# ── budgets ───────────────────────────────────────────────────────────────────


def _org_budget(state: PlacementState, org: str, now: float, client=None) -> float:
    """The org's gpu_daily_usd_budget, refreshed off the loop every CAPS_TTL_S."""
    from brain import org_settings

    if now - state.caps_read_at.get(org, 0.0) >= CAPS_TTL_S:
        state.caps_read_at[org] = now
        try:
            org_settings.refresh_org_caps(org, client=client)
        except Exception as e:
            logger.debug("[placement] caps refresh for %s failed: %s", org[:8], e)
    caps = org_settings.cached_org_caps(org) or {}
    try:
        return float(caps.get("gpu_daily_usd_budget") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _org_spent(state: PlacementState, org: str, now: float, client=None) -> float:
    """Today's dedicated-pod spend for the org: the ledger (read every
    SPEND_READ_TTL_S) or this process's own accrual, whichever is higher."""
    from brain import gpu_usage_store

    day = _today()
    cur_day, accrued = state.spent.get(org, (day, 0.0))
    if cur_day != day:
        accrued = 0.0
        state.spent[org] = (day, 0.0)
    if now - state.spend_read_at.get(org, 0.0) >= SPEND_READ_TTL_S:
        state.spend_read_at[org] = now
        try:
            ledger = float(gpu_usage_store.usd_today_for(org, client=client))
            if ledger > accrued:
                state.spent[org] = (day, ledger)
                accrued = ledger
        except Exception as e:
            logger.debug("[placement] spend read for %s failed: %s", org[:8], e)
    return accrued


def _accrue(state: PlacementState, org: str, usd: float) -> None:
    day = _today()
    cur_day, accrued = state.spent.get(org, (day, 0.0))
    state.spent[org] = (day, (accrued if cur_day == day else 0.0) + usd)


# ── pods ──────────────────────────────────────────────────────────────────────


def wanted_pods(desired: dict[str, dict]) -> dict[str, dict]:
    """pod key → {kind, org, gpu_type, consumers} for rows that ask for a pod."""
    out: dict[str, dict] = {}
    for key, row in desired.items():
        kind = str(row.get("pod") or "pool")
        if kind not in ("standalone", "org"):
            continue
        org = row["org"]
        pod_key = key if kind == "standalone" else org
        ent = out.setdefault(
            pod_key, {"kind": kind, "org": org, "gpu_type": row.get("gpu_type"), "consumers": set()}
        )
        ent["consumers"].add(key)
        if not ent.get("gpu_type") and row.get("gpu_type"):
            ent["gpu_type"] = row.get("gpu_type")
    return out


def default_manager_factory(kind: str, key: str, gpu_type: str | None):
    """A RunPodManager that owns one dedicated pod: its own name (rediscoverable),
    never the legacy host channel, the premium card when one is named, and either
    the standalone models volume (RUNPOD_STANDALONE_VOLUME_ID) or an ephemeral disk
    — never pod 0's volume."""
    from brain.runpod_manager import RunPodManager

    volume = os.environ.get("RUNPOD_STANDALONE_VOLUME_ID", "").strip()
    return RunPodManager(
        pod_name=pod_name_for(kind, key),
        publish_host=False,
        gpu_type_id=gpu_type,
        ephemeral_disk=not volume,
        network_volume_id=volume or None,
    )


async def _wake_bg(pod: DedicatedPod, state: PlacementState) -> None:
    try:
        ok = await pod.manager.ensure_running()
        if ok:
            pod.up_since = pod.up_since or time.time()
            pod.fallback_reason = ""
            state.clear_log(f"wake-failed:{pod.key}")
            logger.info("[placement] %s pod %s ready for %s", pod.kind, pod.pod_id, pod.key[:40])
        else:
            pod.fallback_reason = "fallback_pool"
            state.log_once(
                f"wake-failed:{pod.key}",
                logging.WARNING,
                "[placement] %s pod for %s could not be created — instance stays on the "
                "pool (fallback_pool), retrying each tick",
                pod.kind,
                pod.key[:40],
            )
    except Exception as e:
        pod.fallback_reason = "fallback_pool"
        logger.warning("[placement] wake of %s pod for %s failed: %s", pod.kind, pod.key[:40], e)
    finally:
        pod.waking = False


async def _pause(pod: DedicatedPod, reason: str, report: dict) -> None:
    if pod.held or pod.pod_id:
        try:
            await pod.manager.pause()
        except Exception as e:
            logger.warning("[placement] pause of %s pod %s failed: %s", pod.kind, pod.key[:40], e)
    pod.up_since = None
    pod.last_bill = None
    pod.fallback_reason = reason
    report["actions"].append(f"pause:{pod.kind}:{pod.key[-12:]}")


async def reconcile_pods(
    state: PlacementState,
    desired: dict[str, dict],
    *,
    now: float,
    grace_s: float,
    parallel: int,
    pressure_dir: Path | None,
    manager_factory,
    client,
    report: dict,
) -> None:
    """Create, wake, hold, meter and pause the pods above the pool."""
    from brain import gpu_usage_store, pod_budget

    wanted = wanted_pods(desired)
    samples = pod_pressure.read_all(pressure_dir)

    # Pods nobody wants any more: pause and forget (after a short grace so a
    # re-placement within a tick does not churn a card).
    for key, pod in list(state.pods.items()):
        if key in wanted:
            pod.no_consumer_since = None
            continue
        pod.no_consumer_since = pod.no_consumer_since or now
        if now - pod.no_consumer_since >= ORPHAN_GRACE_S or not pod.held:
            if pod.held:
                await _pause(pod, "unplaced", report)
            state.pods.pop(key, None)
            report["actions"].append(f"forget:{pod.kind}:{key[-12:]}")

    held_total = sum(1 for p in state.pods.values() if p.held)
    cap = max_standalone_pods()

    for key, want in wanted.items():
        org = want["org"]
        pod = state.pods.get(key)
        if pod is None:
            pod = DedicatedPod(
                key=key,
                kind=want["kind"],
                org=org,
                manager=manager_factory(want["kind"], key, want.get("gpu_type")),
                gpu_type=want.get("gpu_type"),
            )
            state.pods[key] = pod
        pod.consumers = set(want["consumers"])
        if not pod.discovered:
            pod.discovered = True
            try:
                await pod.manager.discover_and_publish_host()
                if pod.pod_id:
                    pod.up_since = pod.up_since or now
            except Exception as e:
                logger.debug("[placement] discover %s failed: %s", key[:40], e)

        # Budget gates (org, then platform). Spent or absent → pause and fall back.
        budget = _org_budget(state, org, now, client=client)
        if budget <= 0.0:
            state.log_once(
                f"nobudget:{org}",
                logging.WARNING,
                "[placement] org %s has gpu_daily_usd_budget=0 — its %s pods stay off, "
                "instances use the pool",
                org[:8],
                pod.kind,
            )
            if pod.held:
                await _pause(pod, "no_budget", report)
            pod.fallback_reason = "no_budget"
            continue
        state.clear_log(f"nobudget:{org}")
        spent = _org_spent(state, org, now, client=client)
        if spent >= budget:
            if org not in state.budget_paused:
                state.budget_paused.add(org)
                logger.warning(
                    "[placement] org %s GPU budget spent ($%.2f/$%.2f) — pods sleep and "
                    "instances fall back to the pool until UTC rollover",
                    org[:8],
                    spent,
                    budget,
                )
            if pod.held:
                await _pause(pod, "budget_spent", report)
            pod.fallback_reason = "budget_spent"
            continue
        if org in state.budget_paused:
            state.budget_paused.discard(org)
            logger.info("[placement] org %s GPU budget available again", org[:8])

        # Demand/use for this pod = the youngest across its consumers' pressure files.
        own = dict.fromkeys(pod.consumers, key)
        own_samples = {c: s for c, s in samples.items() if c in pod.consumers}
        agg = pod_pool.aggregate_pressure(own_samples, own, now, parallel)
        demand_age = agg.demand_age_s
        use_age = agg.use_age_s
        up_for = (now - pod.up_since) if pod.up_since else None
        if use_age is not None:
            pod.last_use_at = now - use_age

        if pod.held or pod.waking:
            # Bill first (per tick, per held pod), then decide.
            if pod.held and pod.state in ("ready", "warming", "resuming", "pulling"):
                if pod.last_bill is not None:
                    seconds = max(0.0, now - pod.last_bill)
                    rate = pod.cost_per_hr or pod_budget.rate_per_hr()
                    pod.rate_seen = float(rate)
                    usd = seconds / 3600.0 * float(rate)
                    if seconds > 0:
                        n = max(1, len(pod.consumers))
                        rows = [
                            {
                                "persona": c.partition("::")[2],
                                "pod_kind": pod.kind,
                                "pod_id": pod.pod_id or "",
                                "seconds": seconds / n,
                                "usd": usd / n,
                            }
                            for c in sorted(pod.consumers)
                        ]
                        gpu_usage_store.record(org, rows, client=client)
                        _accrue(state, org, usd)
                pod.last_bill = now
            fresh_use = use_age is not None and use_age <= grace_s
            just_up = up_for is not None and up_for < grace_s
            if pod.held and not pod.waking and not fresh_use and not just_up:
                logger.info(
                    "[placement] %s pod for %s idle (use %s) — pausing",
                    pod.kind,
                    key[:40],
                    f"{use_age:.0f}s ago" if use_age is not None else "never",
                )
                await _pause(pod, "idle", report)
            elif pod.serving:
                pod.fallback_reason = ""
            continue

        # Not held: wake on fresh demand, within the platform backstop.
        if demand_age is not None and demand_age <= grace_s:
            if cap > 0 and held_total >= cap:
                state.log_once(
                    f"cap:{key}",
                    logging.WARNING,
                    "[placement] BRAIN_MAX_STANDALONE_PODS=%d reached — %s pod for %s stays "
                    "on the pool",
                    cap,
                    pod.kind,
                    key[:40],
                )
                pod.fallback_reason = "platform_cap"
                continue
            state.clear_log(f"cap:{key}")
            pod.waking = True
            pod.last_bill = now
            held_total += 1
            report["actions"].append(f"wake:{pod.kind}:{key[-12:]}")
            _spawn_task(_wake_bg(pod, state))
        else:
            pod.fallback_reason = pod.fallback_reason or "idle"


# ── publication ───────────────────────────────────────────────────────────────


def publish_view(state: PlacementState) -> tuple[dict[str, dict], dict[str, dict], list[PodSample]]:
    """(standalone, fallback, extra_pods) for the pool file. `standalone` holds only
    consumers whose pod is READY — everyone else stays on the pool."""
    standalone: dict[str, dict] = {}
    fallback: dict[str, dict] = {}
    extra: list[PodSample] = []
    for i, pod in enumerate(sorted(state.pods.values(), key=lambda p: p.key)):
        pid = pod.pod_id
        if pid:
            extra.append(
                PodSample(
                    pod_id=pid,
                    index=1000 + i,
                    state=pod.state,
                    host=pod.host,
                    name=pod_name_for(pod.kind, pod.key),
                    kind=pod.kind,
                    gpu=pod.gpu_type,
                    cost_per_hr=pod.cost_per_hr,
                    consumers=len(pod.consumers),
                    up_since=pod.up_since,
                )
            )
        for c in pod.consumers:
            if pod.serving:
                standalone[c] = {
                    "pod_id": pid,
                    "host": pod.host,
                    "state": "ready",
                    "kind": pod.kind,
                }
            else:
                fallback[c] = {
                    "kind": pod.kind,
                    "pod_id": pid,
                    "state": pod.state if pod.held or pod.waking else "off",
                    "reason": pod.fallback_reason or ("booting" if pod.waking else "idle"),
                }
    return standalone, fallback, extra


# ── the tick ──────────────────────────────────────────────────────────────────


async def placement_tick(
    provisioner,
    state: PlacementState,
    *,
    pool=None,
    stop_instance,
    now: float | None = None,
    client=None,
    manager_factory=None,
    pressure_dir: Path | None = None,
    grace_s: float | None = None,
    parallel: int | None = None,
) -> dict:
    """One tick. `pool` None = process placement only (the single-pod reconciler,
    BRAIN_POD_POOL=0): dedicated instances still get their own process, every pod
    request reads as `fallback_pool`."""
    ts = time.time() if now is None else now
    state.last_tick = ts
    report: dict = {"actions": [], "desired": 0, "pods": 0, "serving": 0}
    if not enabled():
        return {**report, "disabled": True}
    if client is None:
        client = gateway_client()

    rows = _read_desired(state, ts, client=client)
    if rows is None:
        return {**report, "unavailable": True}
    desired = desired_instances(rows, ts)
    report["desired"] = len(desired)

    await reconcile_processes(
        state, provisioner, desired, stop_instance=stop_instance, report=report
    )

    if pool is not None:
        cfg = getattr(pool, "cfg", None)
        await reconcile_pods(
            state,
            desired,
            now=ts,
            grace_s=float(grace_s if grace_s is not None else getattr(cfg, "grace_s", 600.0)),
            parallel=int(parallel if parallel is not None else getattr(cfg, "parallel", 2)),
            pressure_dir=pressure_dir,
            manager_factory=manager_factory or default_manager_factory,
            client=client,
            report=report,
        )
        standalone, fallback, extra = publish_view(state)
        pool.standalone = standalone
        pool.fallback = fallback
        pool.extra_pods = extra
        report["pods"] = len(state.pods)
        report["serving"] = len(standalone)
        report["fallback"] = len(fallback)
    elif wanted_pods(desired):
        state.log_once(
            "no-pool",
            logging.WARNING,
            "[placement] standalone/org pods need the pod pool (BRAIN_POD_POOL) — "
            "dedicated instances use the shared pod",
        )
    return report


def invalidate(state: PlacementState) -> None:
    """A placement or budget changed: re-read the registry and caps on the next tick."""
    state.desired_read_at = 0.0
    state.caps_read_at.clear()
    state.spend_read_at.clear()


def next_deadline(state: PlacementState, now: float, grace_s: float) -> float | None:
    """The earliest moment this loop needs a tick with no event arriving: a
    paid_until expiry, a held pod's pause-after-grace, an orphan grace, an org's
    projected budget exhaustion at the current burn rate, the UTC rollover that
    refills a spent budget. None when nothing is pending."""
    from datetime import UTC, datetime

    from brain.gateway.reconciler import earliest, next_utc_midnight

    dls: list[float | None] = []
    for rows in (state.desired or {}).values():
        for r in rows:
            pu = r.get("paid_until")
            if not pu or r.get("mode") != "dedicated":
                continue
            try:
                ts = datetime.fromisoformat(str(pu).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts.timestamp() > now:
                    dls.append(ts.timestamp())
            except ValueError:
                continue
    budget_left: dict[str, float] = {}
    for pod in state.pods.values():
        if pod.no_consumer_since is not None:
            dls.append(pod.no_consumer_since + ORPHAN_GRACE_S)
        if pod.held and not pod.waking:
            anchor = max(pod.last_use_at or 0.0, pod.up_since or 0.0)
            if anchor > 0:
                dls.append(anchor + grace_s)
            if pod.rate_seen > 0:
                budget_left.setdefault(pod.org, 0.0)
                budget_left[pod.org] += pod.rate_seen
    for org, rate in budget_left.items():
        from brain import org_settings

        caps = org_settings.cached_org_caps(org) or {}
        try:
            budget = float(caps.get("gpu_daily_usd_budget") or 0.0)
        except (TypeError, ValueError):
            budget = 0.0
        day, spent = state.spent.get(org, ("", 0.0))
        if budget > spent and rate > 0:
            dls.append(now + (budget - spent) / rate * 3600.0)
    if state.budget_paused:
        dls.append(next_utc_midnight(now))
    return earliest(*dls)


async def pause_org(state: PlacementState, org: str) -> int:
    """Sleep sweep: pause every dedicated pod of one org now (its instances were
    just consolidated and stopped). Returns how many were paused."""
    n = 0
    for pod in list(state.pods.values()):
        if pod.org == org and (pod.held or pod.pod_id):
            await _pause(pod, "slept", {"actions": []})
            n += 1
    return n


async def pause_all(state: PlacementState) -> int:
    n = 0
    for pod in list(state.pods.values()):
        if pod.held or pod.pod_id:
            await _pause(pod, "slept", {"actions": []})
            n += 1
    return n


def summary(state: PlacementState) -> dict:
    """For /__pod_status and the Fleet page."""
    return {
        "enabled": enabled(),
        "pods": [
            {
                "key": p.key,
                "kind": p.kind,
                "org": p.org,
                "pod_id": p.pod_id,
                "state": p.state,
                "host": p.host,
                "gpu": p.gpu_type,
                "cost_per_hr": p.cost_per_hr,
                "consumers": sorted(p.consumers),
                "serving": p.serving,
                "fallback": p.fallback_reason,
                "up_since": p.up_since,
            }
            for p in sorted(state.pods.values(), key=lambda p: p.key)
        ],
        "refused": dict(state.refused),
        "budget_paused": sorted(state.budget_paused),
        "spent_today": {o: round(v[1], 4) for o, v in state.spent.items()},
        "max_standalone_pods": max_standalone_pods(),
    }


__all__ = [
    "DedicatedPod",
    "PlacementState",
    "default_manager_factory",
    "desired_instances",
    "enabled",
    "gateway_client",
    "invalidate",
    "next_deadline",
    "max_standalone_pods",
    "pause_all",
    "pause_org",
    "placement_tick",
    "pod_name_for",
    "publish_view",
    "reconcile_pods",
    "reconcile_processes",
    "summary",
    "wanted_pods",
]
