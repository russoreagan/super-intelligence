"""
Pod scheduler — the pure capacity allocator. It does NOT decide cloud-vs-local (that
lives in ModelRouter + the tier gate); it's only consulted once local capacity is
known to be needed, and answers "which pod / is there room / bring a new one up".
Deterministic, no RunPod API, so every branch is unit-tested.
"""

from __future__ import annotations

import pytest

from brain.pod_scheduler import PodScheduler


def test_first_consumer_asks_for_a_new_pod_then_binds():
    s = PodScheduler()
    p = s.allocate("bull", model="qwen")
    assert p.mode == "needs_pod" and p.model == "qwen"  # nothing to share yet
    s.register_pod("pod-1", model="qwen", host="http://pod-1:11434")
    bound = s.confirm_pod("bull", "pod-1")
    assert bound.mode == "assigned" and bound.host == "http://pod-1:11434"
    assert s.host_for("bull") == "http://pod-1:11434"


def test_capacity_one_forces_a_new_pod_per_consumer():
    s = PodScheduler(default_capacity=1)
    s.register_pod("pod-1", model="qwen", host="h1")
    assert s.confirm_pod("bull", "pod-1").mode == "assigned"
    assert s.allocate("bear", model="qwen").mode == "needs_pod"  # spec'd-for-one


def test_capacity_many_shares_one_pod_until_full():
    s = PodScheduler(default_capacity=2)
    s.register_pod("pod-1", model="qwen", host="h1")
    assert s.confirm_pod("bull", "pod-1").mode == "assigned"
    p2 = s.allocate("bear", model="qwen")
    assert p2.mode == "assigned" and p2.pod_id == "pod-1"  # shares
    assert s.allocate("risk", model="qwen").mode == "needs_pod"  # now full


def test_a_different_model_never_shares_a_pod():
    s = PodScheduler(default_capacity=4)
    s.register_pod("pod-1", model="qwen", host="h1")
    s.confirm_pod("bull", "pod-1")
    assert s.allocate("vis", model="llava").mode == "needs_pod"  # room, wrong model


def test_allocate_is_idempotent_for_a_bound_consumer():
    s = PodScheduler(default_capacity=2)
    s.register_pod("pod-1", model="qwen", host="h1")
    s.confirm_pod("bull", "pod-1")
    again = s.allocate("bull", model="qwen")
    assert again.mode == "assigned" and again.pod_id == "pod-1"
    assert s.stats()["by_pod"]["pod-1"] == 1  # not double-counted


def test_release_frees_capacity_for_the_next_consumer():
    s = PodScheduler(default_capacity=1)
    s.register_pod("pod-1", model="qwen", host="h1")
    s.confirm_pod("bull", "pod-1")
    assert s.allocate("bear", model="qwen").mode == "needs_pod"  # full
    s.release("bull")
    p = s.allocate("bear", model="qwen")
    assert p.mode == "assigned" and p.pod_id == "pod-1"


def test_reapable_pods_are_idle_ones_beyond_min_warm():
    s = PodScheduler(default_capacity=1, min_warm=1)
    s.register_pod("pod-1", model="qwen", host="h1")
    s.register_pod("pod-2", model="qwen", host="h2")
    s.confirm_pod("bull", "pod-1")
    s.release("bull")
    assert len(s.reapable_pods()) == 1  # min_warm keeps one alive
    s2 = PodScheduler(default_capacity=1, min_warm=0)
    s2.register_pod("p", model="qwen", host="h")
    assert s2.reapable_pods() == ["p"]  # scale to zero when nothing needs it


def test_confirm_pod_rejects_unknown_or_hostless_pod():
    s = PodScheduler()
    with pytest.raises(ValueError):
        s.confirm_pod("bull", "missing")
    s.register_pod("pod-1", model="qwen")  # no host yet (still booting)
    with pytest.raises(ValueError):
        s.confirm_pod("bull", "pod-1")


def test_remove_pod_unbinds_its_consumers():
    s = PodScheduler(default_capacity=2)
    s.register_pod("pod-1", model="qwen", host="h1")
    s.confirm_pod("bull", "pod-1")
    s.confirm_pod("bear", "pod-1")
    s.remove_pod("pod-1")
    assert s.host_for("bull") is None and s.host_for("bear") is None
    assert s.allocate("bull", model="qwen").mode == "needs_pod"
