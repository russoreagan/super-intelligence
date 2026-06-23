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

import brain.provisioner as pv
import brain.runpod_manager as rm


def _mgr(api_key="key", consumer=False):
    m = rm.RunPodManager(api_key=api_key)
    m._consumer = consumer
    return m


# ── ensure_running: never spends without reason ────────────────────────────


def test_ensure_running_no_key_is_noop(monkeypatch):
    # Hermetic: the manager falls back to RUNPOD_API_KEY when given an empty key,
    # so clear any ambient value (leaked from .env or another test) to isolate the
    # genuine no-key path.
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    m = _mgr(api_key="")
    assert asyncio.run(m.ensure_running()) is False
    assert m._pod_id is None


def test_ensure_running_consumer_is_noop():
    m = _mgr(consumer=True)
    assert asyncio.run(m.ensure_running()) is False
    assert m._pod_id is None


def test_multitenant_start_forces_consumer_even_without_host(monkeypatch):
    """A tenant brain (BRAIN_MULTITENANT) must ALWAYS enter consumer mode and never
    touch pod lifecycle — even when RUNPOD_HOST is empty/missing. Previously an empty
    host dropped the tenant into owner mode, where start() would resume/create GPU
    pods on boot — a blocking poll that wedged the tenant's /health (the hosted-load
    freeze). Owner-mode entry would call _find_existing_pods; assert it never does."""
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.delenv("RUNPOD_HOST", raising=False)  # the trap: no host published yet
    m = _mgr(api_key="key", consumer=False)

    async def _boom():
        raise AssertionError("tenant entered OWNER mode — tried to discover/create pods")

    m._find_existing_pods = _boom  # type: ignore[assignment]
    assert asyncio.run(m.start()) is True
    assert m._consumer is True
    assert m._pod_id is None


def test_multitenant_start_consumer_uses_published_host(monkeypatch):
    """With a shared host published, the tenant consumes it (no lifecycle)."""
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("RUNPOD_HOST", "https://pod-abc-11434.proxy.runpod.net")
    m = _mgr(api_key="key", consumer=False)

    async def _boom():
        raise AssertionError("tenant entered OWNER mode despite a published host")

    m._find_existing_pods = _boom  # type: ignore[assignment]
    assert asyncio.run(m.start()) is True
    assert m._consumer is True


def test_consumer_host_refresh_adopts_new_host_without_respawn(tmp_path, monkeypatch):
    """A running consumer must pick up a new pod host the gateway publishes to the
    shared file (BRAIN_RUNPOD_HOST_FILE) WITHOUT a respawn — closing the gap where
    the reconciler's os.environ sync only reached new spawns. The model_router
    re-reads settings.runpod_host each call, so updating settings is sufficient."""
    import brain.settings as settings_mod

    captured: dict = {}
    monkeypatch.setattr(
        settings_mod.settings, "get", lambda k, d=None: captured.get(k, "" if d is None else d)
    )
    monkeypatch.setattr(settings_mod.settings, "update", captured.update)

    host_file = tmp_path / ".runpod_host"
    host_file.write_text("https://newpod-11434.proxy.runpod.net", encoding="utf-8")
    monkeypatch.setenv("BRAIN_RUNPOD_HOST_FILE", str(host_file))
    monkeypatch.setenv("BRAIN_RUNPOD_HOST_POLL_S", "0.02")

    m = _mgr(consumer=True)

    async def _run():
        task = asyncio.create_task(m._consumer_host_refresh())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert captured.get("runpod_host") == "https://newpod-11434.proxy.runpod.net"

    # A localhost/empty host must NEVER be adopted (would dead-end inference).
    captured.clear()
    host_file.write_text("http://localhost:11434", encoding="utf-8")
    asyncio.run(_run())
    assert "runpod_host" not in captured


def test_ensure_running_idempotent_when_model_on_gpu():
    m = _mgr()
    m._pod_id = "podX"

    async def on_gpu(_host):
        return True

    m._model_on_gpu = on_gpu  # type: ignore[assignment]
    assert asyncio.run(m.ensure_running()) is True


def test_ensure_running_retires_cpu_only_pod():
    """A held pod that serves /api/tags but runs on CPU must be terminated and
    replaced, not treated as ready (that was the silent 'no idle thoughts' bug)."""
    m = _mgr()
    m._pod_id = "podCPU"
    m._known_pod_id = "podCPU"
    terminated: list[str] = []

    async def not_on_gpu(_host):
        return False

    async def alive(_pid):
        return True

    async def fake_terminate(pid):
        terminated.append(pid)

    async def no_pods():
        return []

    async def no_gpus():
        return []

    m._model_on_gpu = not_on_gpu  # type: ignore[assignment]
    m._probe_alive = alive  # type: ignore[assignment]
    m._terminate_pod = fake_terminate  # type: ignore[assignment]
    m._find_existing_pods = no_pods  # type: ignore[assignment]
    m._fetch_gpu_types = no_gpus  # type: ignore[assignment]

    assert asyncio.run(m.ensure_running()) is False
    assert "podCPU" in terminated, "CPU-only pod must be terminated"
    assert "podCPU" in m._unhealthy
    assert m._pod_id is None and m._known_pod_id is None
    assert m.status()["state"] == "failed"


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


# ── network volume: persist models across pod create/destroy ────────────────


def test_create_pod_attaches_network_volume(monkeypatch):
    """With RUNPOD_NETWORK_VOLUME_ID set, the create mutation attaches the network
    volume (volumeInGb=0, networkVolumeId + dataCenterId) so the ~20GB model persists
    on it instead of a fresh per-pod disk that would re-download on every cold start."""
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol123")
    monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "EU-RO-1")
    m = _mgr()
    captured: dict = {}

    async def fake_gql(query, variables=None):
        captured["query"] = query
        captured["variables"] = variables
        return {"podFindAndDeployOnDemand": {"id": "podNEW"}}

    m._gql = fake_gql  # type: ignore[assignment]
    assert asyncio.run(m._create_pod("NVIDIA RTX 4090")) == "podNEW"
    v = captured["variables"]
    assert v["networkVolumeId"] == "vol123"
    assert v["dataCenterId"] == "EU-RO-1"
    assert v["volumeInGb"] == 0, "must request no separate per-pod disk"
    assert "$networkVolumeId" in captured["query"]


def test_create_pod_without_network_volume_uses_per_pod_disk(monkeypatch):
    """Default (no volume id) is byte-for-byte the original behavior: a 50GB per-pod
    disk and no network volume / datacenter pin."""
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)
    monkeypatch.delenv("RUNPOD_DATA_CENTER_ID", raising=False)
    m = _mgr()
    captured: dict = {}

    async def fake_gql(query, variables=None):
        captured["variables"] = variables
        return {"podFindAndDeployOnDemand": {"id": "podDISK"}}

    m._gql = fake_gql  # type: ignore[assignment]
    assert asyncio.run(m._create_pod("gpu")) == "podDISK"
    v = captured["variables"]
    assert v["networkVolumeId"] is None
    assert v["dataCenterId"] is None
    assert v["volumeInGb"] == rm._VOLUME_GB == 50


def test_pause_terminates_pod_in_network_volume_mode(monkeypatch):
    """In network-volume mode pause TERMINATES the pod (not stop): a stopped pod's
    volume bills at the idle rate for nothing, since the models live on the network
    volume. The known id is cleared so the next ensure_running creates a fresh pod
    that re-attaches the volume warm."""
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol123")
    m = _mgr()
    m._pod_id = "podX"
    m._known_pod_id = "podX"
    terminated: list[str] = []
    stopped: list[str] = []

    async def fake_terminate(pid):
        terminated.append(pid)

    async def fake_stop(pid):  # pragma: no cover - must not be called
        stopped.append(pid)

    m._terminate_pod = fake_terminate  # type: ignore[assignment]
    m._stop_pod = fake_stop  # type: ignore[assignment]
    asyncio.run(m.pause())
    assert terminated == ["podX"]
    assert stopped == [], "must terminate, never stop, in network-volume mode"
    assert m._pod_id is None
    assert m._known_pod_id is None, "no pod to resume — create fresh next time"
    assert m.status()["state"] == "off"


def test_pause_stops_pod_without_network_volume(monkeypatch):
    """Without a network volume pause STOPS the pod (preserving its disk) and keeps
    the known id — the original resume-the-same-pod behavior is untouched."""
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)
    m = _mgr()
    m._pod_id = "podX"
    m._known_pod_id = "podX"
    stopped: list[str] = []
    terminated: list[str] = []

    async def fake_stop(pid):
        stopped.append(pid)

    async def fake_terminate(pid):  # pragma: no cover - must not be called
        terminated.append(pid)

    m._stop_pod = fake_stop  # type: ignore[assignment]
    m._terminate_pod = fake_terminate  # type: ignore[assignment]
    asyncio.run(m.pause())
    assert stopped == ["podX"]
    assert terminated == []
    assert m._pod_id is None
    assert m._known_pod_id == "podX", "known id retained for resume"


def test_ensure_running_terminates_unresumable_pod_in_netvol_mode(monkeypatch):
    """In network-volume mode a known pod that won't resume must be terminated to
    release the volume — otherwise a fresh create can't re-attach it (a wedge that
    dead-ends on permanent cloud fallback)."""
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol123")
    m = _mgr()
    m._known_pod_id = "podStuck"
    terminated: list[str] = []

    async def boom_resume(_pid):
        raise RuntimeError("DC out of GPUs")

    async def fake_terminate(pid):
        terminated.append(pid)

    async def no_gpus():
        return []

    m._resume_pod = boom_resume  # type: ignore[assignment]
    m._terminate_pod = fake_terminate  # type: ignore[assignment]
    m._fetch_gpu_types = no_gpus  # type: ignore[assignment]

    assert asyncio.run(m.ensure_running()) is False
    assert "podStuck" in terminated, "un-resumable pod must be terminated to free the volume"
    assert "podStuck" in m._unhealthy


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


def test_full_count_counts_only_live_full_tier_brains():
    prov = pv.Provisioner()
    full = pv._Proc(_FakeProc(True), 9001)  # default tier 'full'
    lite = pv._Proc(_FakeProc(True), 9002)
    lite.tier = "lite"  # alive but never uses the pod
    dead_full = pv._Proc(_FakeProc(False), 9003)  # full but died, not yet reaped
    prov._procs = {"full": full, "lite": lite, "dead": dead_full}
    assert prov.live_count() == 2  # full + lite are alive
    assert prov.full_count() == 1  # only the live full brain drives the pod


def test_published_host_tracks_pod_identity():
    m = _mgr()
    assert m.published_host() is None
    m._known_pod_id = "abc"
    assert m.published_host() == "https://abc-11434.proxy.runpod.net"
    m._pod_id = "live"  # held pod wins over known
    assert m.published_host() == "https://live-11434.proxy.runpod.net"


def test_is_running_tracks_per_tenant_liveness():
    prov = pv.Provisioner()
    prov._procs = {
        "a": pv._Proc(_FakeProc(True), 9001),
        "b": pv._Proc(_FakeProc(False), 9002),  # exited (e.g. graceful sleep)
    }
    assert prov.is_running("a") is True
    assert prov.is_running("b") is False
    assert prov.is_running("missing") is False


# ── boot-status surface (UI messaging) ──────────────────────────────────────


def test_status_starts_off():
    m = _mgr()
    s = m.status()
    assert s["state"] == "off"
    assert "elapsed_s" in s and "detail" in s


def test_set_status_resets_elapsed_only_on_change():
    m = _mgr()
    m._set_status("resuming", "starting GPU pod")
    assert m.status()["state"] == "resuming"
    assert m.status()["detail"] == "starting GPU pod"
    # same state again must NOT reset the phase clock (elapsed keeps growing)
    e1 = m._status_since
    m._set_status("resuming", "starting GPU pod")
    assert m._status_since == e1
    # a real transition resets it
    m._set_status("warming")
    assert m._status_since != e1


def test_pause_sets_status_off():
    m = _mgr()
    m._pod_id = "podX"
    m._set_status("ready")

    async def fake_stop(_pid):
        pass

    m._stop_pod = fake_stop  # type: ignore[assignment]
    asyncio.run(m.pause())
    assert m.status()["state"] == "off"


def test_ensure_running_failure_reports_failed_state():
    m = _mgr()

    async def no_pods():
        return []

    async def no_gpus():
        return []

    m._find_existing_pods = no_pods  # type: ignore[assignment]
    m._fetch_gpu_types = no_gpus  # type: ignore[assignment]
    assert asyncio.run(m.ensure_running()) is False
    assert m.status()["state"] == "failed"
