"""
RunPodManager — lifecycle manager for the brain's RunPod Ollama pod.

Startup:  try to resume any stopped "ollama-brain" pod (multiple supported —
          one per GPU type, each with its own volume and pre-downloaded models).
          If none resume, create a new pod on the best available GPU and pull
          the required models automatically before marking ready.

Shutdown: stop (not terminate) the active pod — volume and models persist for
          the next session.

Watcher:  liveness-only. While a pod is HELD it's probed every couple minutes and
          resumed if it stopped responding. It NEVER speculatively creates a pod
          when none is held — that resurrected paused pods and burned money with no
          consumer. Acquisition is demand-driven via ensure_running().

Demand-driven (gateway): the gateway owns the shared pod and drives ensure_running()
          / pause() off the live tenant-brain count, so the pod runs only while a
          brain needs it and is paused the moment the last one sleeps or is reaped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

# Pod boot phases surfaced to the UI so a user can tell progress from a hang.
#   off      — no pod held (paused / no demand). Inference runs on cloud.
#   resuming — pod resume/create issued; waiting for it to come up.
#   pulling  — downloading a model onto the pod (only on a fresh pod).
#   warming  — pod is serving; loading the model into VRAM.
#   ready    — pod serving + model resident (cheap local inference live).
#   failed   — couldn't bring the pod up; staying on cloud fallback.
POD_STATES = ("off", "resuming", "pulling", "warming", "ready", "failed")

_API_URL = "https://api.runpod.io/graphql"
_POD_NAME = "ollama-brain"
_POD_IMAGE = "ollama/ollama"
_PORT = 11434
_VOLUME_MOUNT = "/root/.ollama"
_VOLUME_GB = 50
_CONTAINER_DISK_GB = 10
_MIN_VCPU = 2
_MIN_MEMORY_GB = 15

# GPU selection policy
_VRAM_FLOOR_GB = 24  # minimum acceptable VRAM
_PRICE_CEILING = 0.50  # max $/hr — hard cutoff

_POLL_S = 5.0
_READY_TIMEOUT_S = 300.0  # 5 min max for pod to come up
# Liveness of a HELD pod is probed every couple minutes — a dead pod must be
# detected and recovered fast, or idle DMN thinking (its cells are model="runpod")
# goes silent for the whole gap. 30 min of silence was the "idle thoughts freeze"
# symptom; 2 min keeps recovery responsive.
_LIVENESS_POLL_S = 120.0

# A pod can serve /api/tags (looks "alive") while running the model entirely on CPU
# (size_vram == 0) — e.g. when ollama can't see the GPU. That gives ~0.25 tok/s and
# every DMN/inference call times out (silent total failure). Require the model to be
# (almost) fully resident in VRAM before a pod is considered healthy/ready.
_MIN_GPU_FRACTION = 0.9
# After failing to bring up any healthy pod, wait this long before trying again so a
# systemic fault (e.g. a bad image) can't churn create→terminate every tick.
_UNHEALTHY_COOLDOWN_S = 1800.0

# Warmup runs in the background (never blocks boot). Loading a 32B model from disk
# routinely exceeds the RunPod proxy's request timeout, so we kick the load and then
# poll /api/ps — fast requests — until the model is resident.
_WARMUP_KICK_TIMEOUT_S = 30.0  # don't hold a long request open across the model load
_WARMUP_POLL_S = 5.0
_WARMUP_TIMEOUT_S = 300.0  # confirm residency within 5 min


def _required_models() -> list[str]:
    return [
        os.environ.get("RUNPOD_MODEL", "qwen2.5:32b"),
        os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
    ]


class RunPodManager:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        # _pod_id: the pod we are actively HOLDING and serving (None = not holding).
        # _known_pod_id: the persistent ollama-brain pod id, even while stopped — so
        # ensure_running() resumes the SAME pod (stable host) rather than creating a
        # new one each cycle. Set by discover_and_publish_host()/ensure_running().
        self._pod_id: str | None = None
        self._known_pod_id: str | None = None
        self._current_price: float | None = None
        self._watcher_task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None
        # Consumer-only: polls the gateway's shared host file to track the live pod
        # host without a respawn (set in start() consumer branch).
        self._consumer_sync_task: asyncio.Task | None = None
        self._watchdog_proc = None  # subprocess.Popen — stops pod if brain process dies
        self._lifecycle_lock = asyncio.Lock()  # serialize ensure_running/pause
        # Boot-progress state surfaced to the UI (see POD_STATES).
        self._status: str = "off"
        self._status_since: float = time.time()
        self._status_detail: str = ""
        self._http = None  # httpx.AsyncClient, created lazily in _get_http()
        # Pods we proved unhealthy (CPU-only) this session — never resume them again.
        self._unhealthy: set[str] = set()
        # After we fail to bring up ANY healthy pod, back off creating for a while so
        # a systemic problem doesn't churn create→terminate every reconciler tick.
        self._cooldown_until: float = 0.0
        # Consumer mode (multi-tenant tenant pointing at a shared pod): never owns
        # or stops the pod. Set in start() when BRAIN_MULTITENANT + RUNPOD_HOST.
        self._consumer = False

    # ── HTTP / GraphQL ────────────────────────────────────────────────────────

    def _get_http(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient()
        return self._http

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        r = await self._get_http().post(
            _API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"RunPod API: {data['errors']}")
        return data["data"]

    # ── GPU selection ─────────────────────────────────────────────────────────

    async def _fetch_gpu_types(self) -> list[dict]:
        data = await self._gql("""{ gpuTypes {
            id displayName memoryInGb
            lowestPrice(input: {gpuCount: 1}) { uninterruptablePrice }
        } }""")
        return data["gpuTypes"]

    def _rank_gpus(self, gpu_types: list[dict]) -> list[dict]:
        """Most VRAM first, ties by lowest price, hard ceiling $0.50/hr."""
        candidates = []
        for g in gpu_types:
            price = (g.get("lowestPrice") or {}).get("uninterruptablePrice")
            vram = g.get("memoryInGb", 0)
            if price is None or vram < _VRAM_FLOOR_GB or price > _PRICE_CEILING:
                continue
            candidates.append({**g, "_price": price})
        candidates.sort(key=lambda g: (-g["memoryInGb"], g["_price"]))
        return candidates

    # ── Pod lifecycle ─────────────────────────────────────────────────────────

    async def _find_existing_pods(self) -> list[dict]:
        """Return all stopped/running ollama-brain pods."""
        data = await self._gql("""{ myself { pods {
            id name desiredStatus
            runtime { uptimeInSeconds }
        } } }""")
        return [p for p in data["myself"]["pods"] if p["name"] == _POD_NAME]

    async def _resume_pod(self, pod_id: str) -> None:
        await self._gql(
            """mutation($id: String!) {
            podResume(input: {podId: $id, gpuCount: 1}) { id desiredStatus }
        }""",
            {"id": pod_id},
        )
        logger.info("[RunPod] Resuming pod %s", pod_id)

    async def _create_pod(self, gpu_id: str) -> str | None:
        try:
            data = await self._gql(
                """mutation($gpuId: String!) {
                podFindAndDeployOnDemand(input: {
                    cloudType: COMMUNITY,
                    gpuCount: 1,
                    volumeInGb: 50,
                    containerDiskInGb: 10,
                    minVcpuCount: 2,
                    minMemoryInGb: 15,
                    gpuTypeId: $gpuId,
                    name: "ollama-brain",
                    imageName: "ollama/ollama",
                    ports: "11434/http",
                    volumeMountPath: "/root/.ollama",
                    env: [{key: "OLLAMA_HOST", value: "0.0.0.0"}]
                }) { id }
            }""",
                {"gpuId": gpu_id},
            )
            pod_id = data["podFindAndDeployOnDemand"]["id"]
            logger.info("[RunPod] Created pod %s on %s", pod_id, gpu_id)
            return pod_id
        except Exception as e:
            logger.warning("[RunPod] Failed to create pod on %s: %s", gpu_id, e)
            return None

    async def _wait_until_ready(self, pod_id: str) -> bool:
        elapsed = 0.0
        while elapsed < _READY_TIMEOUT_S:
            await asyncio.sleep(_POLL_S)
            elapsed += _POLL_S
            try:
                data = await self._gql(
                    """query($id: String!) {
                    pod(input: {podId: $id}) {
                        desiredStatus runtime { uptimeInSeconds }
                    }
                }""",
                    {"id": pod_id},
                )
                pod = data.get("pod") or {}
                if pod.get("runtime"):
                    # GraphQL uptime ≠ Ollama serving. Probe the actual endpoint so we
                    # don't apply the host (and let cells start hitting it) before the
                    # server answers — the gap that made resumed pods fail their first
                    # inference calls.
                    host = self._pod_host(pod_id)
                    try:
                        r = await self._get_http().get(f"{host}/api/tags", timeout=10.0)
                        if r.status_code == 200:
                            logger.info(
                                "[RunPod] Pod %s ready + Ollama serving (%.0fs)", pod_id, elapsed
                            )
                            return True
                        logger.debug(
                            "[RunPod] Pod %s up; Ollama HTTP %s (%.0fs)",
                            pod_id,
                            r.status_code,
                            elapsed,
                        )
                    except Exception:
                        logger.debug(
                            "[RunPod] Pod %s up; Ollama not serving yet (%.0fs)", pod_id, elapsed
                        )
                else:
                    logger.debug(
                        "[RunPod] Waiting for pod %s — %s (%.0fs)",
                        pod_id,
                        pod.get("desiredStatus"),
                        elapsed,
                    )
            except Exception as e:
                logger.debug("[RunPod] Poll error: %s", e)
        logger.warning("[RunPod] Pod %s not ready after %.0fs", pod_id, _READY_TIMEOUT_S)
        return False

    async def _pull_models(self, host: str) -> bool:
        """Pull any required models not already present on the pod."""
        import json as _json

        models = _required_models()

        # Check what's already installed
        try:
            r = await self._get_http().get(f"{host}/api/tags", timeout=15.0)
            installed = {m["name"].split(":")[0] for m in r.json().get("models", [])}
        except Exception:
            installed = set()

        for model in models:
            base = model.split(":")[0]
            if base in installed:
                logger.info("[RunPod] Model %s already present", model)
                continue
            logger.info("[RunPod] Pulling %s (this may take a few minutes)...", model)
            self._set_status("pulling", f"downloading {base}")
            try:
                async with self._get_http().stream(
                    "POST",
                    f"{host}/api/pull",
                    json={"name": model, "stream": True},
                    timeout=600.0,
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            d = _json.loads(line)
                            if d.get("status") == "success":
                                logger.info("[RunPod] Pulled %s", model)
                                break
                            if d.get("total") and d.get("completed"):
                                pct = d["completed"] / d["total"] * 100
                                self._set_status("pulling", f"downloading {base} {pct:.0f}%")
                                logger.debug("[RunPod] %s: %.1f%%", model, pct)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("[RunPod] Failed to pull %s: %s", model, e)
                return False
        return True

    def _pod_host(self, pod_id: str) -> str:
        return f"https://{pod_id}-{_PORT}.proxy.runpod.net"

    def published_host(self) -> str | None:
        """The proxy host tenant brains should point at (RUNPOD_HOST). Reflects the
        pod we hold (or the known pod we'll resume — stable id → stable host), so the
        gateway can keep RUNPOD_HOST in sync as the pod identity changes and never
        leave tenants pointed at a dead pod."""
        pid = self._pod_id or self._known_pod_id
        return self._pod_host(pid) if pid else None

    async def _probe_alive(self, pod_id: str) -> bool:
        """Best-effort liveness probe — same check as readiness (/api/tags 200)."""
        host = self._pod_host(pod_id)
        try:
            r = await self._get_http().get(f"{host}/api/tags", timeout=10.0)
            return r.status_code == 200
        except Exception:
            return False

    async def _model_on_gpu(self, host: str) -> bool:
        """True only if the inference model is resident AND (almost) fully on the GPU.

        /api/ps reports a model as loaded even when it's running on CPU (size_vram==0);
        that pod is useless (~0.25 tok/s → every call times out). This is the real
        health signal, not /api/tags."""
        model = os.environ.get("RUNPOD_MODEL", "qwen2.5:32b")
        base = model.split(":")[0]
        try:
            r = await self._get_http().get(f"{host}/api/ps", timeout=10.0)
            if r.status_code != 200:
                return False
            for m in r.json().get("models", []):
                if m.get("name", "").split(":")[0] != base:
                    continue
                size = m.get("size", 0) or 0
                vram = m.get("size_vram", 0) or 0
                return size > 0 and (vram / size) >= _MIN_GPU_FRACTION
        except Exception:
            return False
        return False

    def _apply_host(self, pod_id: str) -> None:
        from brain.settings import settings

        host = self._pod_host(pod_id)
        settings.update({"runpod_host": host})
        logger.info("[RunPod] runpod_host → %s", host)

    def _clear_host(self) -> None:
        from brain.settings import settings

        local_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        settings.update({"runpod_host": local_host})
        logger.info("[RunPod] runpod_host → local Ollama (%s)", local_host)

    async def _consumer_host_refresh(self) -> None:
        """Consumer-only: poll the gateway's shared host file and keep
        settings.runpod_host current, so a running tenant recovers when the shared
        pod's host changes (a new pod after churn/crash, or the pod coming up after
        the brain spawned host-less) WITHOUT a respawn. The model_router re-reads
        settings.runpod_host on every call, so the next inference uses the new host.

        This completes the gateway's existing host-sync (which only reached NEW
        spawns via os.environ): the gateway now also writes the live host to
        BRAIN_RUNPOD_HOST_FILE, and this loop applies it in-process."""
        from pathlib import Path

        from brain.settings import settings

        path = os.environ.get("BRAIN_RUNPOD_HOST_FILE", "").strip()
        if not path:
            return
        host_file = Path(path)
        interval = float(os.environ.get("BRAIN_RUNPOD_HOST_POLL_S", "30"))
        while True:
            try:
                if host_file.exists():
                    host = host_file.read_text(encoding="utf-8").strip()
                    # Only adopt a real pod host; never clobber with empty/localhost.
                    if host and "localhost" not in host:
                        if str(settings.get("runpod_host") or "") != host:
                            settings.update({"runpod_host": host})
                            logger.info(
                                "[RunPod] consumer host refreshed → %s (gateway pod change)", host
                            )
            except Exception as e:
                logger.debug("[RunPod] consumer host refresh error: %s", e)
            await asyncio.sleep(interval)

    async def _warmup_model(self, host: str) -> bool:
        """Load the model into VRAM and confirm it's actually on the GPU.

        Returns True only when the model is resident with (almost) all weights in
        VRAM — the health signal that distinguishes a working pod from a CPU-only one
        (which serves /api/tags fine but runs at ~0.25 tok/s and times out every call).

        Loading a 32B model from disk routinely exceeds the RunPod proxy's request
        timeout (~100s): a blocking /api/generate gets killed with a 524 even though
        ollama keeps loading server-side. So we kick the load (tolerating the proxy
        timeout) and poll /api/ps — fast requests — until weights are on the GPU.
        """
        model = os.environ.get("RUNPOD_MODEL", "qwen2.5:32b")
        logger.info("[RunPod] Warming up %s into VRAM...", model)

        try:
            await self._get_http().post(
                f"{host}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": "30m"},
                timeout=_WARMUP_KICK_TIMEOUT_S,
            )
        except Exception as e:
            logger.debug("[RunPod] Warmup kick returned %s (load continues server-side)", e)

        elapsed = 0.0
        while elapsed < _WARMUP_TIMEOUT_S:
            if await self._model_on_gpu(host):
                logger.info("[RunPod] Model %s warm and on GPU (%.0fs)", model, elapsed)
                self._set_status("ready")
                return True
            await asyncio.sleep(_WARMUP_POLL_S)
            elapsed += _WARMUP_POLL_S

        logger.warning(
            "[RunPod] Model %s not resident on GPU after %.0fs — pod is unhealthy "
            "(likely running on CPU). Will replace it.",
            model,
            _WARMUP_TIMEOUT_S,
        )
        self._set_status("failed", "GPU not engaged — replacing pod")
        return False

    def _spawn_watchdog(self, pod_id: str) -> None:
        """Start a detached watchdog subprocess that stops the pod if this process dies."""
        import subprocess
        import sys

        max_hours = float(os.environ.get("RUNPOD_MAX_HOURS", "8"))
        watchdog_script = os.path.join(os.path.dirname(__file__), "runpod_watchdog.py")
        if not os.path.exists(watchdog_script):
            logger.warning("[RunPod] Watchdog script not found at %s — skipping", watchdog_script)
            return
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    watchdog_script,
                    pod_id,
                    str(os.getpid()),
                    self._api_key,
                    str(int(max_hours * 3600)),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach from parent's process group
            )
            self._watchdog_proc = proc
            logger.info("[RunPod] Watchdog started (pid=%d, max_hours=%.1f)", proc.pid, max_hours)
        except Exception as e:
            logger.warning("[RunPod] Failed to spawn watchdog: %s", e)

    async def _activate_pod(self, pod_id: str) -> bool:
        """Bring a pod into service: wait for ollama, pull models, warm the model ONTO
        THE GPU, then apply host + watchdog. Returns True only if the model is actually
        GPU-resident — a CPU-only pod fails here so the caller stops it and tries
        another (rather than silently serving ~0.25 tok/s)."""
        if not await self._wait_until_ready(pod_id):
            self._set_status("failed", "pod did not come up")
            return False
        host = self._pod_host(pod_id)
        if not await self._pull_models(host):
            self._set_status("failed", "model download failed")
            return False
        # Health gate: await warmup and confirm the model is on the GPU. This blocks
        # for up to the warmup timeout, which is fine — _activate_pod runs in a
        # background lifecycle task, never under an HTTP request. The UI shows
        # "warming" throughout (honest), and a CPU-only pod is rejected here.
        self._set_status("warming", "loading model into memory")
        if not await self._warmup_model(host):
            return False  # unhealthy (CPU-only / never resident) — caller replaces it
        self._apply_host(pod_id)
        self._spawn_watchdog(pod_id)
        return True

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """
        Try to resume any stopped ollama-brain pod, then fall back to creating
        a new one. Returns True if a pod is ready; False = using local Ollama.
        """
        # Multi-tenant CONSUMER mode: a tenant child must NEVER manage a pod — its
        # idle-reaper/watchdog would stop inference for every other tenant, and
        # (critically) owner-mode resume/create POLLS and BLOCKS, which on a tenant's
        # boot path stalls /health until the provisioner kills it and respawns — the
        # exact wedge that kept the hosted app from loading. The single gateway process
        # is the SOLE pod owner + recovery path (see brain/gateway/server).
        #
        # A tenant is ALWAYS a consumer, regardless of whether RUNPOD_HOST is set yet:
        # if a shared host is known, point inference at it; if not, fall back to cloud
        # until the gateway publishes one. NEVER provision a pod from a tenant — the
        # old `and shared_host` guard let an empty/stale/clobbered host drop the tenant
        # into owner mode, where it would churn GPU pods on boot.
        if os.environ.get("BRAIN_MULTITENANT", "").lower() in ("1", "true", "yes"):
            from brain.settings import settings

            self._consumer = True
            shared_host = os.environ.get("RUNPOD_HOST", "").strip()
            if shared_host:
                settings.update({"runpod_host": shared_host})
                logger.info(
                    "[RunPod] Consumer mode — using shared pod %s (no lifecycle)", shared_host
                )
            else:
                # No host published yet: use cloud fallback, never provision a pod.
                self._clear_host()
                logger.info(
                    "[RunPod] Consumer mode — no shared host yet; cloud fallback (no lifecycle)"
                )
            # Track the live pod host the gateway publishes (recover from a pod change
            # — churn/crash/late-boot — without a respawn). Idempotent.
            if self._consumer_sync_task is None or self._consumer_sync_task.done():
                self._consumer_sync_task = asyncio.create_task(self._consumer_host_refresh())
            return True

        if not self._api_key:
            logger.info("[RunPod] No API key — skipping pod management")
            self._clear_host()
            return False

        try:
            # Try all stopped pods — each may be on a different GPU type with
            # models already downloaded. First one that resumes and is ready wins.
            existing_pods = await self._find_existing_pods()
            running = [p for p in existing_pods if p.get("runtime")]
            stopped = [p for p in existing_pods if p.get("desiredStatus") in ("EXITED", "STOPPED")]

            if running:
                pod_id = running[0]["id"]
                logger.info("[RunPod] Pod %s already running", pod_id)
                self._pod_id = pod_id
                self._known_pod_id = pod_id
                # Health-gate even an already-running pod (it may be CPU-only).
                if await self._activate_pod(pod_id):
                    self._start_watcher()
                    return True
                await self._stop_pod(pod_id)
                self._pod_id = None  # fall through to resume/create a healthy one

            for pod in stopped:
                pod_id = pod["id"]
                try:
                    await self._resume_pod(pod_id)
                    self._pod_id = pod_id
                    if await self._activate_pod(pod_id):
                        self._known_pod_id = pod_id
                        self._start_watcher()
                        return True
                    self._pod_id = None
                except Exception as e:
                    logger.warning("[RunPod] Resume of %s failed (%s) — trying next", pod_id, e)

            # No existing pod worked — create on best available GPU
            gpu_types = await self._fetch_gpu_types()
            ranked = self._rank_gpus(gpu_types)
            if not ranked:
                logger.warning("[RunPod] No suitable GPU available — using local Ollama")
                self._clear_host()
                return False

            for gpu in ranked:
                logger.info(
                    "[RunPod] Trying %s (%dGB, $%.2f/hr)",
                    gpu["displayName"],
                    gpu["memoryInGb"],
                    gpu["_price"],
                )
                pod_id = await self._create_pod(gpu["id"])
                if pod_id:
                    self._pod_id = pod_id
                    self._current_price = gpu["_price"]
                    if await self._activate_pod(pod_id):
                        self._known_pod_id = pod_id
                        self._start_watcher()
                        return True
                    await self._stop_pod(pod_id)
                    self._pod_id = None

            logger.warning("[RunPod] All GPU options failed — using local Ollama")
            self._clear_host()
            return False

        except Exception as e:
            logger.warning("[RunPod] Startup failed — using local Ollama: %s", e)
            self._clear_host()
            return False

    # ── Demand-driven lifecycle (gateway-owned shared pod) ──────────────────────
    #
    # The gateway drives these off the live tenant-brain count (provisioner). The
    # pod runs ONLY while ≥1 brain needs it; it is paused the moment the last brain
    # is slept or reaped. This is the contract that prevents an orphaned pod from
    # burning money with no consumer (and no DMN) for days.

    async def discover_and_publish_host(self) -> str | None:
        """Find the persistent ollama-brain pod and publish its stable proxy host
        to settings/RUNPOD_HOST WITHOUT resuming it.

        The host is deterministic from the pod id and stable across stop/resume, so
        publishing it up front lets every tenant spawn inherit the right host even
        while the pod is paused — the model router fails over to cloud until the pod
        answers, then transparently uses it once ensure_running() brings it up.
        Returns the host, or None if there's no pod / no API key."""
        if not self._api_key:
            return None
        try:
            existing = await self._find_existing_pods()
        except Exception as e:
            logger.warning("[RunPod] discover failed: %s", e)
            return None
        if not existing:
            logger.info("[RunPod] No existing ollama-brain pod to discover")
            return None
        running = [p for p in existing if p.get("runtime")]
        pod = (running or existing)[0]
        self._known_pod_id = pod["id"]
        if running:
            self._pod_id = pod["id"]  # already serving — adopt it
        host = self._pod_host(self._known_pod_id)
        from brain.settings import settings

        settings.update({"runpod_host": host})
        logger.info(
            "[RunPod] Discovered pod %s (%s) — host published: %s",
            self._known_pod_id,
            "running" if running else "stopped",
            host,
        )
        return host

    async def ensure_running(self) -> bool:
        """Resume-or-create a HEALTHY shared pod and make it serve. Idempotent.

        Called by the gateway reconciler when ≥1 brain is alive. No-op (returns True)
        only when the held pod has the model resident ON THE GPU. A pod that serves
        /api/tags but runs the model on CPU is retired (terminated) and replaced —
        otherwise inference silently times out. After a full failure we cool down so a
        systemic fault doesn't churn create→terminate every tick."""
        if self._consumer or not self._api_key:
            return False
        async with self._lifecycle_lock:
            host = self._pod_host(self._pod_id) if self._pod_id else None
            # Fast-path: only a no-op if the model is actually on the GPU.
            if host and await self._model_on_gpu(host):
                self._set_status("ready")
                return True
            # Held pod is alive but CPU-only (or dead): retire it before replacing.
            if self._pod_id is not None:
                if await self._probe_alive(self._pod_id):
                    logger.warning("[RunPod] Held pod %s is CPU-only — retiring", self._pod_id)
                    await self._retire_unhealthy(self._pod_id)
                else:
                    self._cancel_watcher()
                    self._pod_id = None

            if time.time() < self._cooldown_until:
                self._set_status("failed", "GPU unavailable — using cloud (cooling down)")
                return False

            # Pick a pod to bring up: the known-good one, else a discovered one,
            # skipping any we've proven unhealthy this session.
            candidates: list[str] = []
            if self._known_pod_id and self._known_pod_id not in self._unhealthy:
                candidates.append(self._known_pod_id)
            else:
                existing = await self._find_existing_pods()
                ordered = [p for p in existing if p.get("runtime")] + [
                    p for p in existing if not p.get("runtime")
                ]
                for p in ordered:
                    if p["id"] not in self._unhealthy:
                        candidates.append(p["id"])
                        break

            for pod_id in candidates:
                try:
                    self._set_status("resuming", "starting GPU pod")
                    await self._resume_pod(pod_id)
                    self._pod_id = pod_id
                    self._known_pod_id = pod_id
                    if await self._activate_pod(pod_id):
                        self._start_watcher()
                        logger.info("[RunPod] Pod %s ensured running (demand>0)", pod_id)
                        return True
                    await self._retire_unhealthy(pod_id)  # CPU-only / never resident
                except Exception as e:
                    logger.warning("[RunPod] ensure_running resume of %s failed: %s", pod_id, e)
                    self._pod_id = None
                    # Don't chase a pod that won't resume (dead, or its host is out of
                    # GPUs): mark it unhealthy for this session so the next cycle skips it
                    # and falls through to discover/create instead of retrying the same
                    # dead pod every reconcile tick. Drop it as the known pod too so it
                    # isn't re-adopted as a candidate.
                    self._unhealthy.add(pod_id)
                    if self._known_pod_id == pod_id:
                        self._known_pod_id = None

            # Nothing healthy to resume — create on the best GPU.
            try:
                self._set_status("resuming", "provisioning a GPU")
                ranked = self._rank_gpus(await self._fetch_gpu_types())
            except Exception as e:
                logger.warning("[RunPod] ensure_running GPU fetch failed: %s", e)
                self._set_status("failed", "GPU lookup failed")
                return False
            for gpu in ranked:
                new_id = await self._create_pod(gpu["id"])
                if new_id:
                    self._pod_id = new_id
                    self._known_pod_id = new_id
                    self._current_price = gpu["_price"]
                    if await self._activate_pod(new_id):
                        self._start_watcher()
                        logger.info("[RunPod] Pod %s created + running (demand>0)", new_id)
                        return True
                    await self._retire_unhealthy(new_id)  # delete the dud, try next GPU

            logger.warning(
                "[RunPod] No healthy GPU pod could be brought up — cloud fallback, "
                "cooling down %.0fmin",
                _UNHEALTHY_COOLDOWN_S / 60,
            )
            self._cooldown_until = time.time() + _UNHEALTHY_COOLDOWN_S
            self._set_status("failed", "no healthy GPU pod — using cloud")
            return False

    async def pause(self) -> None:
        """Stop the shared pod (preserves volume) when no brain needs it.

        Cancels the liveness watcher FIRST so it can't resume the pod we're about
        to stop, then stops the pod and the crash-watchdog. _known_pod_id is kept
        so a later ensure_running() resumes the SAME pod (stable host)."""
        if self._consumer:
            return
        async with self._lifecycle_lock:
            self._cancel_watcher()
            if self._warmup_task:
                self._warmup_task.cancel()
                self._warmup_task = None
            self._stop_watchdog()
            if self._pod_id:
                await self._stop_pod(self._pod_id)
                logger.info("[RunPod] Pod %s paused (no live brains)", self._pod_id)
                self._pod_id = None
            self._set_status("off")

    def _set_status(self, state: str, detail: str = "") -> None:
        """Record a boot-phase transition (timestamped) for the UI poller."""
        if state != self._status:
            self._status = state
            self._status_since = time.time()
            logger.info("[RunPod] status → %s%s", state, f" ({detail})" if detail else "")
        self._status_detail = detail

    def status(self) -> dict:
        """Current pod boot phase + seconds in this phase. Consumed by the gateway
        /__pod_status endpoint and rendered in the UI."""
        return {
            "state": self._status,
            "detail": self._status_detail,
            "elapsed_s": round(max(0.0, time.time() - self._status_since), 1),
        }

    def _start_watcher(self) -> None:
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(self._watch())

    def _cancel_watcher(self) -> None:
        if self._watcher_task:
            self._watcher_task.cancel()
            self._watcher_task = None

    def _stop_watchdog(self) -> None:
        # SIGTERM = "exit without stopping the pod" — we (or the gateway) own the
        # podStop, so the watchdog must not race us.
        if self._watchdog_proc is not None:
            try:
                import signal as _signal

                self._watchdog_proc.send_signal(_signal.SIGTERM)
            except Exception:
                pass
            self._watchdog_proc = None

    async def _stop_pod(self, pod_id: str) -> None:
        try:
            await self._gql(
                """mutation($id: String!) {
                podStop(input: {podId: $id}) { id desiredStatus }
            }""",
                {"id": pod_id},
            )
            logger.info("[RunPod] Pod %s stopped", pod_id)
        except Exception as e:
            logger.warning("[RunPod] Failed to stop pod %s: %s", pod_id, e)

    async def _terminate_pod(self, pod_id: str) -> None:
        """Permanently delete a pod (frees its volume → stops storage charges). Used
        for confirmed-unhealthy pods so they aren't rediscovered or billed."""
        try:
            await self._gql(
                "mutation($id: String!) { podTerminate(input: {podId: $id}) }",
                {"id": pod_id},
            )
            logger.info("[RunPod] Pod %s terminated", pod_id)
        except Exception as e:
            logger.warning("[RunPod] Failed to terminate pod %s: %s", pod_id, e)

    async def _retire_unhealthy(self, pod_id: str) -> None:
        """Mark a pod unhealthy and delete it so we never resume it again."""
        self._unhealthy.add(pod_id)
        await self._terminate_pod(pod_id)
        if self._pod_id == pod_id:
            self._pod_id = None
        if self._known_pod_id == pod_id:
            self._known_pod_id = None

    async def stop(self) -> None:
        """Stop the active pod (preserves volume) and cancel the watcher.

        Used by the single-brain (non-gateway) path on shutdown. The gateway uses
        pause() instead. Delegates to the same teardown."""
        # Consumer (tenant) never owns the shared pod — stopping it would kill
        # inference for every other tenant. Nothing to tear down.
        if self._consumer:
            return
        await self.pause()

    # ── Background watcher (liveness-only) ──────────────────────────────────────

    async def _watch(self) -> None:
        """Keep the HELD pod alive — recover it if it stops responding.

        Liveness-only by design: this NEVER speculatively creates a pod when none
        is held. Spinning one up on a timer (the old behavior) meant a paused pod
        resurrected itself within minutes and burned money with no consumer. When
        the held pod is unrecoverable we release it and exit; the gateway reconciler
        re-engages ensure_running() only when a brain actually needs it.
        """
        while True:
            if self._pod_id is None:
                return  # nothing held — gateway decides when to acquire
            await asyncio.sleep(_LIVENESS_POLL_S)
            if self._pod_id is None:
                return
            try:
                if await self._probe_alive(self._pod_id):
                    continue
                logger.warning(
                    "[RunPod] Held pod %s not responding — attempting recovery", self._pod_id
                )
                recovered = False
                try:
                    await self._resume_pod(self._pod_id)
                    recovered = await self._activate_pod(self._pod_id)
                except Exception as e:
                    logger.warning("[RunPod] Recovery of %s failed: %s", self._pod_id, e)
                if recovered:
                    logger.info(
                        "[RunPod] Pod %s recovered — cheap local inference restored", self._pod_id
                    )
                else:
                    logger.warning(
                        "[RunPod] Pod %s unrecoverable — releasing (cloud fallback). "
                        "Reconciler will retry on demand.",
                        self._pod_id,
                    )
                    self._pod_id = None
                    return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("[RunPod] Watcher error: %s", e)
