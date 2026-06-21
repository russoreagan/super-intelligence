"""
Pod scheduler — the capacity allocator for local-inference pods.

This layer does NOT decide cloud-vs-local. That truth lives in exactly one place —
ModelRouter + cell config ("swap providers here, nowhere else") — and the per-brain
`tier` gate (a lite brain never goes local). The scheduler is consulted ONLY after
something upstream has already determined "this needs local/RunPod capacity," and its
sole job is: which pod serves this consumer's model, and is there room (else a new pod
must be brought up). It never sees motor/cloud work, so it carries no allow/deny list.

Model:
  - A pod serves ONE model and up to `capacity` consumers (pods are spec'd per brain
    by default, capacity=1; capacity>1 lets several share one pod). A consumer needing
    a different model can't share a pod serving another model.
  - allocate() reuses a pod serving the right model with room; otherwise returns
    needs_pod so the caller provisions one, registers it, then confirm_pod() binds it.
  - release()/remove_pod() free slots; reapable_pods() lists pods idle beyond min_warm
    so the pool can scale back down (to zero by default).

Pure and synchronous: all I/O (provisioning, host discovery, teardown) lives in the
caller, so this is fully unit-testable and deterministic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_MODEL = os.environ.get("BRAIN_LOCAL_MODEL", "qwen")
DEFAULT_CAPACITY = max(1, int(os.environ.get("BRAIN_POD_CAPACITY", "1")))


@dataclass
class Pod:
    pod_id: str
    model: str
    capacity: int
    host: str | None = None  # published inference endpoint, once the pod is up
    assignments: set[str] = field(default_factory=set)

    @property
    def load(self) -> int:
        return len(self.assignments)

    @property
    def has_room(self) -> bool:
        return self.host is not None and self.load < self.capacity


@dataclass
class Placement:
    """The allocator's answer for one consumer.
    mode: 'assigned'  → use the pod at `host` (pod_id set).
          'needs_pod' → caller must provision a pod for `model`, register it, then
                        call confirm_pod(consumer, pod_id) to bind this consumer.
    """

    mode: str
    model: str | None = None
    pod_id: str | None = None
    host: str | None = None


class PodScheduler:
    def __init__(self, *, default_capacity: int | None = None, min_warm: int = 0) -> None:
        self._pods: dict[str, Pod] = {}
        self._assignment: dict[str, str] = {}  # consumer -> pod_id
        self._default_capacity = default_capacity or DEFAULT_CAPACITY
        self._min_warm = max(0, min_warm)

    # ── pool registration (called by the executor after real provisioning) ──
    def register_pod(
        self, pod_id: str, *, model: str = DEFAULT_MODEL, host: str | None = None, capacity: int | None = None
    ) -> Pod:
        pod = self._pods.get(pod_id)
        if pod is None:
            pod = self._pods[pod_id] = Pod(pod_id, model, capacity or self._default_capacity)
        if host is not None:
            pod.host = host
        return pod

    def remove_pod(self, pod_id: str) -> None:
        pod = self._pods.pop(pod_id, None)
        if pod:
            for c in pod.assignments:
                self._assignment.pop(c, None)

    # ── allocation (only ever called once local capacity is known to be needed) ──
    def allocate(self, consumer: str, *, model: str = DEFAULT_MODEL) -> Placement:
        """Find a pod serving `model` for this consumer, or signal a new one is needed.
        Idempotent: a consumer already on a live pod for the same model keeps it."""
        cur = self._assignment.get(consumer)
        if cur and cur in self._pods and self._pods[cur].host and self._pods[cur].model == model:
            pod = self._pods[cur]
            return Placement(mode="assigned", model=model, pod_id=pod.pod_id, host=pod.host)

        # Reuse the most-loaded pod with room (pack tight so idle pods free up sooner).
        candidates = [p for p in self._pods.values() if p.model == model and p.has_room]
        if candidates:
            pod = max(candidates, key=lambda p: p.load)
            self._bind(consumer, pod)
            return Placement(mode="assigned", model=model, pod_id=pod.pod_id, host=pod.host)

        return Placement(mode="needs_pod", model=model)

    def confirm_pod(self, consumer: str, pod_id: str) -> Placement:
        """Bind a consumer to a freshly provisioned+registered pod (after needs_pod)."""
        pod = self._pods.get(pod_id)
        if pod is None or not pod.host:
            raise ValueError(f"pod '{pod_id}' is not registered with a host")
        if not pod.has_room and consumer not in pod.assignments:
            raise ValueError(f"pod '{pod_id}' is at capacity")
        self._bind(consumer, pod)
        return Placement(mode="assigned", model=pod.model, pod_id=pod.pod_id, host=pod.host)

    def _bind(self, consumer: str, pod: Pod) -> None:
        prev = self._assignment.get(consumer)
        if prev and prev in self._pods:
            self._pods[prev].assignments.discard(consumer)
        pod.assignments.add(consumer)
        self._assignment[consumer] = pod.pod_id

    def release(self, consumer: str) -> str | None:
        """Free this consumer's slot. Returns the pod_id it was on, if any."""
        pod_id = self._assignment.pop(consumer, None)
        if pod_id and pod_id in self._pods:
            self._pods[pod_id].assignments.discard(consumer)
        return pod_id

    # ── scale-down ──
    def reapable_pods(self) -> list[str]:
        """Idle pods (zero load) the pool may stop, keeping `min_warm` warm."""
        idle = [p.pod_id for p in self._pods.values() if p.load == 0]
        keep = max(0, self._min_warm - (len(self._pods) - len(idle)))
        return idle[keep:] if keep else idle

    def host_for(self, consumer: str) -> str | None:
        pod_id = self._assignment.get(consumer)
        return self._pods[pod_id].host if pod_id and pod_id in self._pods else None

    def stats(self) -> dict:
        return {
            "pods": len(self._pods),
            "assignments": len(self._assignment),
            "by_pod": {pid: p.load for pid, p in self._pods.items()},
        }
