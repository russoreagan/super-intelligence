"""RunPodPool — N RunPodManagers behind the single-pod surface the gateway already uses.

One manager per pool slot. Slot 0 is the legacy "ollama-brain" pod and keeps every
legacy channel (settings.runpod_host, the readiness flags, tenants/.runpod_host): a
brain spawned before the pool existed — or one whose gateway runs with the pool
disabled — sees exactly what it saw before. Slots 1..N-1 are "ollama-brain-p2".. and
are published ONLY through tenants/.runpod_pool.json (plan §10.2), which consumer
brains read first (brain/runpod_manager._consumer_host).

The pool holds no policy. Which pod a process uses, when to add or remove one, and what
the budget allows are decided by brain/pod_pool (pure) and driven by the gateway
reconciler (brain/gateway/pod_reconcile). This class only owns the managers, the
assignment table, the drain timers and the two files.

Duck-typed compatibility: `ensure_running`, `pause`, `status`, `published_host`,
`_pod_id`, `_cost_per_hr`, `_cancel_watcher` behave as the single manager's did (for
pod 0 / the whole pool), so every gateway call site and test that held a RunPodManager
can hold a RunPodPool instead.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from brain.pod_pool import (
    PodSample,
    PoolConfig,
    ScaleHistory,
    pod_name_for,
    pool_file_body,
)
from brain.runpod_manager import RunPodManager

logger = logging.getLogger(__name__)

POOL_FILE_NAME = ".runpod_pool.json"


def pool_file_path(tenants_dir: Path | None = None) -> Path:
    base = tenants_dir or Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve()
    return base / POOL_FILE_NAME


class RunPodPool:
    def __init__(
        self,
        api_key: str | None = None,
        cfg: PoolConfig | None = None,
        *,
        tenants_dir: Path | None = None,
        manager_factory: Callable[[int, str], RunPodManager] | None = None,
    ) -> None:
        self.cfg = cfg or PoolConfig.from_env()
        self._api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        factory = manager_factory or self._default_factory
        # One manager per slot, built up front: discovery is by name, and a slot with
        # no pod costs nothing but an object.
        self._managers: list[RunPodManager] = [
            factory(i, pod_name_for(i)) for i in range(max(1, self.cfg.max_pods))
        ]
        self.assignments: dict[str, str] = {}  # proc_key → pod_id
        self.history = ScaleHistory()
        self.cooldown_until = 0.0
        self._drain_since: dict[str, float] = {}  # pod_id → when its consumers were moved
        self._up_since: dict[str, float] = {}  # pod_id → first tick seen held
        self.pool_file = pool_file_path(tenants_dir)

    def _default_factory(self, index: int, name: str) -> RunPodManager:
        # Only slot 0 speaks on the legacy channels.
        return RunPodManager(self._api_key, pod_name=name, publish_host=(index == 0))

    # ── identity helpers ──

    @property
    def managers(self) -> list[RunPodManager]:
        return list(self._managers)

    def manager_for(self, pod_id: str) -> RunPodManager | None:
        for m in self._managers:
            if m._pod_id == pod_id:
                return m
        return None

    def index_of(self, pod_id: str) -> int | None:
        for i, m in enumerate(self._managers):
            if m._pod_id == pod_id:
                return i
        return None

    def held_pod_ids(self) -> list[str]:
        return [m._pod_id for m in self._managers if m._pod_id]

    # ── the single-pod surface the gateway already calls ──

    @property
    def _pod_id(self) -> str | None:
        return self._managers[0]._pod_id

    @property
    def _cost_per_hr(self) -> float | None:
        """The HIGHEST rate among held pods — what the budget must convert at."""
        rates = [m._cost_per_hr for m in self._managers if m._pod_id and m._cost_per_hr]
        return max(rates) if rates else None

    def published_host(self) -> str | None:
        return self._managers[0].published_host()

    def _cancel_watcher(self) -> None:
        for m in self._managers:
            m._cancel_watcher()

    async def ensure_running(self) -> bool:
        """Demand-driven pod 0 (what _kick_pod and the old reconciler call)."""
        return await self.ensure_min()

    async def pause(self) -> None:
        """Sleep the whole pool, largest slot first; pod 0 last so the legacy host file
        is blanked by its own manager exactly as before."""
        for m in reversed(self._managers):
            if m._pod_id:
                pid = m._pod_id
                await m.pause()
                self._forget(pid)
        self.assignments.clear()
        self.publish()

    def status(self) -> dict:
        """Pod 0's boot-phase status (the in-app banner reads state/detail/elapsed_s)
        plus the pool summary (`pods`, `ready`, `assignments`)."""
        out = dict(self._managers[0].status())
        out.update(self.summary())
        return out

    # ── lifecycle ──

    async def discover(self) -> str | None:
        """Adopt whatever pods already exist by name (a gateway redeploy must not
        orphan running pods) and rebuild assignments from the pool file, keeping only
        those that still point at a held pod. Returns pod 0's host, if any."""
        host0: str | None = None
        for i, m in enumerate(self._managers):
            try:
                h = await m.discover_and_publish_host()
                if i == 0:
                    host0 = h
            except Exception as e:
                logger.warning("[pool] discover of slot %d failed: %s", i, e)
        held = set(self.held_pod_ids())
        try:
            data = json.loads(self.pool_file.read_text(encoding="utf-8"))
            prior = data.get("assignments") or {}
            self.assignments = {k: v for k, v in prior.items() if v in held}
            if prior and not self.assignments:
                logger.info("[pool] prior assignments discarded — none of their pods are held")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("[pool] pool file unreadable: %s", e)
        for pid in held:
            self._up_since.setdefault(pid, time.time())
        return host0

    async def ensure_min(self) -> bool:
        """Pod 0 up, plus any slots below min_pods. Returns whether pod 0 is serving."""
        ok = await self._managers[0].ensure_running()
        self._note_up(self._managers[0])
        for i in range(1, min(self.cfg.min_pods, len(self._managers))):
            m = self._managers[i]
            if not m._pod_id:
                await m.ensure_running()
                self._note_up(m)
        return ok

    async def scale_up(self) -> str | None:
        """Bring up the lowest empty slot above 0. Arms the scale cooldown either way —
        a failed create must not be retried every tick."""
        self.cooldown_until = time.time() + self.cfg.cooldown_s
        for m in self._managers[1:]:
            if m._pod_id or m._pod_name in self._draining_names():
                continue
            ok = await m.ensure_running()
            self._note_up(m)
            if ok and m._pod_id:
                logger.info("[pool] scaled up: %s → %s", m._pod_name, m._pod_id)
                return m._pod_id
            logger.warning("[pool] scale-up of %s failed (%s)", m._pod_name, m.status()["detail"])
            return None
        return None

    def drain(self, pod_id: str) -> None:
        """Mark a pod draining: it stays up for cfg.drain_s so calls already in flight
        finish and consumers re-poll onto their new pod, then terminate() runs."""
        if pod_id not in self._drain_since:
            self._drain_since[pod_id] = time.time()
            logger.info("[pool] draining pod %s", pod_id)

    def draining_due(self, now: float | None = None) -> list[str]:
        ts = time.time() if now is None else now
        return [pid for pid, since in self._drain_since.items() if ts - since >= self.cfg.drain_s]

    def is_draining(self, pod_id: str) -> bool:
        return pod_id in self._drain_since

    async def terminate(self, pod_id: str) -> None:
        """Take a pod down (the manager terminates under a network volume, stops
        otherwise — either way it is rediscovered by name and reused warm next time)."""
        m = self.manager_for(pod_id)
        if m is not None:
            await m.pause()
        self._forget(pod_id)
        logger.info("[pool] pod %s released", pod_id)

    async def probe_all(self) -> dict[str, bool]:
        """Liveness per held pod. A dead pod is released here (its manager's own
        watcher would eventually do the same) so the reconciler can reassign its
        consumers this tick rather than two minutes later."""
        out: dict[str, bool] = {}
        for m in self._managers:
            pid = m._pod_id
            if not pid:
                continue
            alive = await m._probe_alive(pid)
            out[pid] = alive
            if not alive:
                logger.warning("[pool] pod %s (%s) not responding — releasing", pid, m._pod_name)
                m._cancel_watcher()
                m._pod_id = None
                m._set_status("off", "pod died")
                self._forget(pid)
        return out

    # ── views ──

    def pods(self, now: float | None = None) -> list[PodSample]:
        """Every HELD slot as a PodSample (state from the manager, 'draining' when the
        pool has marked it). Pressure fields are filled by the reconciler."""
        ts = time.time() if now is None else now
        out: list[PodSample] = []
        for i, m in enumerate(self._managers):
            pid = m._pod_id
            if not pid:
                continue
            state = "draining" if pid in self._drain_since else m._status
            self._up_since.setdefault(pid, ts)
            out.append(
                PodSample(
                    pod_id=pid,
                    index=i,
                    state=state,
                    host=m._pod_host(pid),
                    name=m._pod_name,
                    kind="pool",
                    gpu=None,
                    cost_per_hr=m._cost_per_hr,
                    parallel=self.cfg.parallel,
                    up_since=self._up_since.get(pid),
                    drain_since=self._drain_since.get(pid),
                )
            )
        return out

    def summary(self) -> dict:
        pods = self.pods()
        return {
            "pods": [p.as_dict() for p in pods],
            "ready": sum(1 for p in pods if p.ready),
            "assignments": len(self.assignments),
            "max_pods": self.cfg.max_pods,
        }

    # ── publication ──

    def publish(self, now: float | None = None) -> dict:
        """Write tenants/.runpod_pool.json (atomic) AND the legacy tenants/.runpod_host
        with pod 0's host while pod 0 is READY, empty otherwise. Brains that predate the
        pool (or run without BRAIN_RUNPOD_POOL_FILE) keep working off the legacy file;
        empty flips them to the fail-fast 'off' sentinel the moment pod 0 is not serving,
        instead of timing out against a booting or stopped pod."""
        ts = time.time() if now is None else now
        pods = self.pods(ts)
        body = pool_file_body(pods, self.assignments, {}, now=ts)
        try:
            self.pool_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pool_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body), encoding="utf-8")
            os.replace(tmp, self.pool_file)
        except Exception as e:
            logger.warning("[pool] pool file write failed: %s", e)
        pod0 = next((p for p in pods if p.index == 0), None)
        legacy = pod0.host if pod0 is not None and pod0.ready and pod0.host else ""
        try:
            from brain.provisioner import publish_runpod_host

            publish_runpod_host(legacy)
        except Exception as e:
            logger.debug("[pool] legacy host publish skipped: %s", e)
        return body

    # ── internals ──

    def _note_up(self, m: RunPodManager) -> None:
        if m._pod_id:
            self._up_since.setdefault(m._pod_id, time.time())

    def _forget(self, pod_id: str) -> None:
        self._drain_since.pop(pod_id, None)
        self._up_since.pop(pod_id, None)
        self.history.cold_since.pop(pod_id, None)
        for k in [k for k, v in self.assignments.items() if v == pod_id]:
            del self.assignments[k]

    def _draining_names(self) -> set[str]:
        return {m._pod_name for m in self._managers if m._pod_id and m._pod_id in self._drain_since}
