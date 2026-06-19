"""RunPod pod-lifecycle regression tests.

Guards the money-leak fixes: the shared pod must run ONLY while a brain needs it,
must never speculatively resurrect itself, and must be paused when the last brain
sleeps or is reaped.

Background: a pod once ran ~5 days with no consumer (and no DMN) because nothing
tied its lifecycle to the live tenant-brain count, the watcher speculatively
re-created it, and the reaper killed brains without stopping the pod.
"""

from __future__ import annotations

import asyncio

import pytest

import brain.provisioner as pv
import brain.runpod_manager as rm


def _mgr(api_key="key", consumer=False):
    m = rm.RunPodManager(api_key=api_key)
    m._consumer = consumer
    return m


# ── ensure_running: never spends without reason ────────────────────────────


def test_ensure_running_no_key_is_noop():
    m = _mgr(api_key="")
    assert asyncio.run(m.ensure_running()) is False
    assert m._pod_id is None


def test_ensure_running_consumer_is_noop():
    m = _mgr(consumer=True)
    assert asyncio.run(m.ensure_running()) is False
    assert m._pod_id is None


def test_ensure_running_idempotent_when_alive():
    m = _mgr()
    m._pod_id = "podX"

    async def alive(_pid):
        return True

    m._probe_alive = alive  # type: ignore[assignment]
    assert asyncio.run(m.ensure_running()) is True


def test_ensure_running_resumes_known_pod_not_create():
    """With a known pod id, ensure_running resumes it — it must NOT create a new
    one (which would orphan the old and double-spend)."""
    m = _mgr()
    m._known_pod_id = "podKnown"
    created = []
    resumed = []

    async def fake_resume(pid):
        resumed.append(pid)

    async def fake_activate(pid):
        m._pod_id = pid
        return True

    async def fake_probe(_pid):
        return False  # not currently serving

    async def fake_create(_gpu):  # pragma: no cover - must not be called
        created.append(_gpu)
        return "podNEW"

    m._resume_pod = fake_resume  # type: ignore[assignment]
    m._activate_pod = fake_activate  # type: ignore[assignment]
    m._probe_alive = fake_probe  # type: ignore[assignment]
    m._create_pod = fake_create  # type: ignore[assignment]
    m._start_watcher = lambda: None  # type: ignore[assignment]

    assert asyncio.run(m.ensure_running()) is True
    assert resumed == ["podKnown"]
    assert created == [], "must resume the known pod, never create a new one"
    assert m._pod_id == "podKnown"


# ── pause: stops the pod and the watcher that could revive it ───────────────


def test_pause_stops_pod_and_cancels_watcher():
    m = _mgr()
    m._pod_id = "podX"
    stopped = []

    async def fake_stop(pid):
        stopped.append(pid)

    m._stop_pod = fake_stop  # type: ignore[assignment]

    async def run():
        # a real watcher task that pause() must cancel
        m._watcher_task = asyncio.create_task(asyncio.sleep(3600))
        await m.pause()

    asyncio.run(run())
    assert stopped == ["podX"]
    assert m._pod_id is None
    assert m._watcher_task is None
    # known id retained so a later ensure_running resumes the SAME pod
    # (here it was never set, but the field must not have been clobbered to a value)
    assert m._known_pod_id is None


def test_pause_consumer_is_noop():
    m = _mgr(consumer=True)
    m._pod_id = "shared"  # a consumer must never stop the shared pod

    async def boom(_pid):  # pragma: no cover
        raise AssertionError("consumer must not stop the shared pod")

    m._stop_pod = boom  # type: ignore[assignment]
    asyncio.run(m.pause())
    assert m._pod_id == "shared"


# ── watcher: liveness-only, never speculatively creates ─────────────────────


def test_watch_returns_immediately_when_nothing_held():
    m = _mgr()
    # _pod_id is None → watcher must exit at once, never create a pod.
    asyncio.run(asyncio.wait_for(m._watch(), timeout=1.0))


def test_watch_has_no_create_path(monkeypatch):
    """A held pod that dies unrecoverably is released — the watcher must NOT then
    create a replacement (that was the resurrection leak)."""
    m = _mgr()
    m._pod_id = "podDead"
    created = []

    async def never_alive(_pid):
        return False

    async def fail_resume(_pid):
        raise RuntimeError("gone")

    async def fake_create(_gpu):  # pragma: no cover
        created.append(_gpu)
        return "podNEW"

    m._probe_alive = never_alive  # type: ignore[assignment]
    m._resume_pod = fail_resume  # type: ignore[assignment]
    m._create_pod = fake_create  # type: ignore[assignment]
    monkeypatch.setattr(rm, "_LIVENESS_POLL_S", 0.01)

    asyncio.run(asyncio.wait_for(m._watch(), timeout=2.0))
    assert m._pod_id is None
    assert created == [], "watcher must never create a pod on its own"


# ── provisioner live_count drives the gateway reconciler ────────────────────


class _FakeProc:
    def __init__(self, alive):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def test_live_count_excludes_dead_unreaped_procs():
    prov = pv.Provisioner()
    prov._procs = {
        "a": pv._Proc(_FakeProc(True), 9001),
        "b": pv._Proc(_FakeProc(True), 9002),
        "c": pv._Proc(_FakeProc(False), 9003),  # died, not yet reaped
    }
    assert prov.live_count() == 2
    prov._procs = {}
    assert prov.live_count() == 0
