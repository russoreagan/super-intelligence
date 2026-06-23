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
    # Network volumes attach only to Secure Cloud — the create must request SECURE.
    assert "cloudType: SECURE" in captured["query"]


def test_fetch_gpu_types_prices_secure_in_network_volume_mode(monkeypatch):
    """In network-volume mode GPU pricing must be queried against SECURE availability
    (and the pinned DC) so the $0.50/hr ceiling reflects the secure pod we'll deploy."""
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol123")
    monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "EU-RO-1")
    m = _mgr()
    captured: dict = {}

    async def fake_gql(query, variables=None):
        captured["variables"] = variables
        return {"gpuTypes": []}

    m._gql = fake_gql  # type: ignore[assignment]
    asyncio.run(m._fetch_gpu_types())
    pi = captured["variables"]["input"]
    assert pi["secureCloud"] is True
    assert pi["dataCenterId"] == "EU-RO-1"


def test_fetch_gpu_types_prices_any_cloud_without_volume(monkeypatch):
    """Default (no volume): price across all clouds (no secureCloud filter), unchanged."""
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)
    m = _mgr()
    captured: dict = {}

    async def fake_gql(query, variables=None):
        captured["variables"] = variables
        return {"gpuTypes": []}

    m._gql = fake_gql  # type: ignore[assignment]
    asyncio.run(m._fetch_gpu_types())
    pi = captured["variables"]["input"]
    assert "secureCloud" not in pi
    assert pi == {"gpuCount": 1}


def _gpu(id_, vram, price):
    return {"id": id_, "displayName": id_, "memoryInGb": vram,
            "lowestPrice": {"uninterruptablePrice": price}}


def test_create_candidates_secure_first_then_community_in_netvol_mode(monkeypatch):
    """With a network volume, create attempts are SECURE+volume first (warm), then a
    COMMUNITY+no-volume fallback (cold) so an affordable community GPU is still tried
    when no secure GPU is under the ceiling."""
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol123")
    monkeypatch.setenv("RUNPOD_MIN_VRAM_GB", "16")  # isolate sequencing from the model floor
    m = _mgr()

    async def fake_fetch(*, secure=None, data_center=None):
        return [_gpu("A40", 48, 0.40)] if secure else [_gpu("3090", 24, 0.22)]

    m._fetch_gpu_types = fake_fetch  # type: ignore[assignment]
    cands = asyncio.run(m._create_candidates())
    assert [(c["gpu"]["id"], c["cloud_type"], c["attach_volume"]) for c in cands] == [
        ("A40", "SECURE", True),
        ("3090", "COMMUNITY", False),
    ]


def test_create_candidates_community_only_without_volume(monkeypatch):
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)
    monkeypatch.setenv("RUNPOD_MIN_VRAM_GB", "16")  # isolate sequencing from the model floor
    m = _mgr()

    async def fake_fetch(*, secure=None, data_center=None):
        assert not secure, "no network volume → never query secure-only pricing"
        return [_gpu("3090", 24, 0.22)]

    m._fetch_gpu_types = fake_fetch  # type: ignore[assignment]
    cands = asyncio.run(m._create_candidates())
    assert [(c["cloud_type"], c["attach_volume"]) for c in cands] == [("COMMUNITY", False)]


def test_create_pod_community_fallback_does_not_attach_volume(monkeypatch):
    """The COMMUNITY fallback leg must NOT attach the network volume (community pods
    can't), even with RUNPOD_NETWORK_VOLUME_ID set: it deploys a plain per-pod-disk pod
    that re-downloads the model."""
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol123")
    m = _mgr()
    captured: dict = {}

    async def fake_gql(query, variables=None):
        captured["query"] = query
        captured["variables"] = variables
        return {"podFindAndDeployOnDemand": {"id": "podCOMM"}}

    m._gql = fake_gql  # type: ignore[assignment]
    assert asyncio.run(
        m._create_pod("3090", cloud_type="COMMUNITY", attach_volume=False)
    ) == "podCOMM"
    v = captured["variables"]
    assert v["networkVolumeId"] is None
    assert v["volumeInGb"] == rm._VOLUME_GB
    assert "cloudType: COMMUNITY" in captured["query"]


# ── model-aware GPU VRAM floor (don't deploy a model onto a too-small card) ──


def test_min_vram_floor_scales_with_model(monkeypatch):
    """The floor is derived from the model size so a 32B isn't deployed onto a 24GB
    card (where it spills to CPU and fails the residency gate)."""
    monkeypatch.delenv("RUNPOD_MIN_VRAM_GB", raising=False)
    m = _mgr()
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:32b")
    assert m._min_vram_gb() == 40  # needs an A40/A6000-class card
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:14b")
    assert m._min_vram_gb() == 18  # a 24GB card is fine
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:7b")
    assert m._min_vram_gb() == 12


def test_min_vram_floor_env_override(monkeypatch):
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:32b")
    monkeypatch.setenv("RUNPOD_MIN_VRAM_GB", "48")
    assert _mgr()._min_vram_gb() == 48


def test_min_vram_floor_falls_back_when_unparseable(monkeypatch):
    monkeypatch.delenv("RUNPOD_MIN_VRAM_GB", raising=False)
    monkeypatch.setenv("RUNPOD_MODEL", "nomic-embed-text")
    assert _mgr()._min_vram_gb() == rm._VRAM_FLOOR_GB  # conservative default


def test_rank_gpus_excludes_too_small_for_32b(monkeypatch):
    """With 32B configured, 24GB cards must be filtered out (they'd run on CPU); the
    48GB A40/A6000 under the ceiling are kept and preferred (most VRAM first)."""
    monkeypatch.delenv("RUNPOD_MIN_VRAM_GB", raising=False)
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:32b")
    m = _mgr()
    ranked = m._rank_gpus([
        _gpu("A6000", 48, 0.33),
        _gpu("A40", 48, 0.35),
        _gpu("4090", 24, 0.34),   # too small for 32B
        _gpu("L4", 24, 0.44),     # too small for 32B
        _gpu("L40S", 48, 0.79),   # over the $0.50 ceiling
    ])
    ids = [g["id"] for g in ranked]
    assert ids == ["A6000", "A40"], "only affordable 48GB cards qualify for 32B"


def test_rank_gpus_allows_24gb_for_14b(monkeypatch):
    monkeypatch.delenv("RUNPOD_MIN_VRAM_GB", raising=False)
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:14b")
    m = _mgr()
    ranked = m._rank_gpus([_gpu("3090", 24, 0.22), _gpu("A5000", 24, 0.16)])
    assert {g["id"] for g in ranked} == {"3090", "A5000"}, "24GB cards run 14B fine"


# ── keep searching for good inventory vs. cool down on churn ─────────────────


def test_ensure_running_supply_shortage_does_not_cooldown(monkeypatch):
    """A right-sized GPU exists but every create fails on supply (nothing created):
    DON'T cool down — keep hunting each reconcile tick rather than settling for a worse
    pod or going quiet for 30min. (Operator preference: patient + picky.)"""
    monkeypatch.delenv("RUNPOD_MIN_VRAM_GB", raising=False)
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:32b")
    m = _mgr()

    async def no_pods():
        return []

    async def some_gpus(*, secure=None, data_center=None):
        return [_gpu("A40", 48, 0.35)]  # valid, but create will fail on supply

    async def fail_create(*a, **k):
        return None  # SUPPLY_CONSTRAINT → no pod created

    m._find_existing_pods = no_pods  # type: ignore[assignment]
    m._fetch_gpu_types = some_gpus  # type: ignore[assignment]
    m._create_pod = fail_create  # type: ignore[assignment]

    assert asyncio.run(m.ensure_running()) is False
    assert m._cooldown_until == 0.0, "supply shortage must NOT trigger a cooldown"
    assert m.status()["state"] == "failed"


def test_ensure_running_churn_triggers_cooldown(monkeypatch):
    """A pod that comes UP but fails health (churn) DOES cool down — otherwise it would
    create→terminate every tick on a bad image/GPU."""
    monkeypatch.delenv("RUNPOD_MIN_VRAM_GB", raising=False)
    monkeypatch.setenv("RUNPOD_MODEL", "qwen2.5:32b")
    m = _mgr()

    async def no_pods():
        return []

    async def some_gpus(*, secure=None, data_center=None):
        return [_gpu("A40", 48, 0.35)]

    async def make_pod(*a, **k):
        return "podBad"

    async def bad_activate(_pid):
        return False  # came up but unhealthy

    async def fake_retire(_pid):
        pass

    m._find_existing_pods = no_pods  # type: ignore[assignment]
    m._fetch_gpu_types = some_gpus  # type: ignore[assignment]
    m._create_pod = make_pod  # type: ignore[assignment]
    m._activate_pod = bad_activate  # type: ignore[assignment]
    m._retire_unhealthy = fake_retire  # type: ignore[assignment]

    assert asyncio.run(m.ensure_running()) is False
    assert m._cooldown_until > 0.0, "churn (created-but-unhealthy) must cool down"


def test_create_pod_without_network_volume_uses_per_pod_disk(monkeypatch):
    """Default (no volume id) is byte-for-byte the original behavior: a 50GB per-pod
    disk and no network volume / datacenter pin."""
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)
    monkeypatch.delenv("RUNPOD_DATA_CENTER_ID", raising=False)
    m = _mgr()
    captured: dict = {}

    async def fake_gql(query, variables=None):
        captured["query"] = query
        captured["variables"] = variables
        return {"podFindAndDeployOnDemand": {"id": "podDISK"}}

    m._gql = fake_gql  # type: ignore[assignment]
    assert asyncio.run(m._create_pod("gpu")) == "podDISK"
    v = captured["variables"]
    assert v["networkVolumeId"] is None
    assert v["dataCenterId"] is None
    assert v["volumeInGb"] == rm._VOLUME_GB == 50
    assert "cloudType: COMMUNITY" in captured["query"]


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
