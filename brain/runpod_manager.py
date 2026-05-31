"""
RunPodManager — lifecycle manager for the brain's RunPod Ollama pod.

Startup:  query available GPUs, resume a stopped pod or create a new one on
          the best available GPU, poll until ready, then write the pod's
          proxy URL into settings.runpod_host so all runpod-* cells use it.

Shutdown: stop (not terminate) the pod so the attached volume (and all
          downloaded models) persists for the next session.

Watcher:  background task that periodically checks for cheaper/better GPU
          options and logs them. Does not auto-migrate — restart the app to
          switch. This avoids mid-session disruption.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

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
_VRAM_FLOOR_GB = 24          # minimum acceptable VRAM
_PRICE_CEILING = 0.50        # max $/hr — hard cutoff
_MIGRATE_THRESHOLD = 0.20    # log a suggestion if new option is 20%+ cheaper

_POLL_S = 5.0
_READY_TIMEOUT_S = 300.0      # 5 min max for pod to come up
_WATCHER_INTERVAL_S = 1800.0  # check for better options every 30 min (pod running)
_CAPACITY_POLL_S = 300.0      # check for capacity every 5 min (no pod, on local fallback)


class RunPodManager:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        self._pod_id: str | None = None
        self._current_price: float | None = None
        self._watcher_task: asyncio.Task | None = None
        self._http: "httpx.AsyncClient | None" = None

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
        """Return GPU types sorted best-first: most VRAM then lowest price, under $0.50/hr."""
        candidates = []
        for g in gpu_types:
            price = (g.get("lowestPrice") or {}).get("uninterruptablePrice")
            vram = g.get("memoryInGb", 0)
            if price is None or vram < _VRAM_FLOOR_GB or price > _PRICE_CEILING:
                continue
            candidates.append({**g, "_price": price})
        # Most VRAM first, ties broken by lowest price
        candidates.sort(key=lambda g: (-g["memoryInGb"], g["_price"]))
        return candidates

    # ── Pod lifecycle ─────────────────────────────────────────────────────────

    async def _find_existing_pod(self) -> dict | None:
        data = await self._gql("""{ myself { pods {
            id name desiredStatus
            runtime { uptimeInSeconds }
        } } }""")
        for pod in data["myself"]["pods"]:
            if pod["name"] == _POD_NAME:
                return pod
        return None

    async def _resume_pod(self, pod_id: str) -> None:
        await self._gql("""mutation($id: String!) {
            podResume(input: {podId: $id, gpuCount: 1}) { id desiredStatus }
        }""", {"id": pod_id})
        logger.info("[RunPod] Resuming existing pod %s", pod_id)

    async def _create_pod(self, gpu_id: str) -> str | None:
        try:
            data = await self._gql("""mutation($gpuId: String!) {
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
            }""", {"gpuId": gpu_id})
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
                data = await self._gql("""query($id: String!) {
                    pod(input: {podId: $id}) {
                        desiredStatus runtime { uptimeInSeconds }
                    }
                }""", {"id": pod_id})
                pod = data.get("pod") or {}
                if pod.get("runtime"):
                    logger.info("[RunPod] Pod %s ready (%.0fs)", pod_id, elapsed)
                    return True
                logger.debug("[RunPod] Waiting for pod %s — %s (%.0fs)",
                             pod_id, pod.get("desiredStatus"), elapsed)
            except Exception as e:
                logger.debug("[RunPod] Poll error: %s", e)
        logger.warning("[RunPod] Pod %s not ready after %.0fs", pod_id, _READY_TIMEOUT_S)
        return False

    def _apply_host(self, pod_id: str) -> None:
        from brain.settings import settings
        host = f"https://{pod_id}-{_PORT}.proxy.runpod.net"
        settings.update({"runpod_host": host})
        logger.info("[RunPod] runpod_host → %s", host)

    def _clear_host(self) -> None:
        """Point runpod_host at local Ollama so cells don't hit a dead pod URL."""
        from brain.settings import settings
        local_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        settings.update({"runpod_host": local_host})
        logger.info("[RunPod] runpod_host → local Ollama (%s)", local_host)

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """
        Start or resume the Ollama pod and wire its URL into settings.
        Returns True if the pod is ready; False means runpod-* cells will
        fall back to OLLAMA_HOST (local Ollama).
        """
        if not self._api_key:
            logger.info("[RunPod] No API key — skipping pod management")
            self._clear_host()
            return False

        try:
            # Reuse a stopped pod if one exists — preserves downloaded models
            existing = await self._find_existing_pod()
            if existing:
                pod_id = existing["id"]
                if existing.get("runtime"):
                    logger.info("[RunPod] Pod %s already running", pod_id)
                    self._pod_id = pod_id
                    self._apply_host(pod_id)
                    self._watcher_task = asyncio.create_task(self._watch())
                    return True
                if existing.get("desiredStatus") in ("EXITED", "STOPPED"):
                    try:
                        await self._resume_pod(pod_id)
                        self._pod_id = pod_id
                        if await self._wait_until_ready(pod_id):
                            self._apply_host(pod_id)
                            self._watcher_task = asyncio.create_task(self._watch())
                            return True
                    except Exception as e:
                        logger.warning("[RunPod] Resume failed (%s) — trying best available GPU", e)

            # No existing pod or resume failed — pick best available GPU and create one
            gpu_types = await self._fetch_gpu_types()
            ranked = self._rank_gpus(gpu_types)
            if not ranked:
                logger.warning("[RunPod] No suitable GPU available — using local Ollama")
                return False

            for gpu in ranked:
                logger.info("[RunPod] Trying %s (%dGB, $%.2f/hr)",
                            gpu["displayName"], gpu["memoryInGb"], gpu["_price"])
                pod_id = await self._create_pod(gpu["id"])
                if pod_id:
                    self._pod_id = pod_id
                    self._current_price = gpu["_price"]
                    if await self._wait_until_ready(pod_id):
                        self._apply_host(pod_id)
                        self._spawn_watchdog(pod_id)
                        self._watcher_task = asyncio.create_task(self._watch())
                        return True
                    # Pod created but never came up — try next GPU type
                    await self._stop_pod(pod_id)
                    self._pod_id = None

            logger.warning("[RunPod] All GPU options failed — using local Ollama, will retry every %.0fs", _CAPACITY_POLL_S)
            self._clear_host()
            self._watcher_task = asyncio.create_task(self._watch())
            return False

        except Exception as e:
            logger.warning("[RunPod] Startup failed — using local Ollama, will retry every %.0fs: %s", _CAPACITY_POLL_S, e)
            self._clear_host()
            self._watcher_task = asyncio.create_task(self._watch())
            return False

    async def _stop_pod(self, pod_id: str) -> None:
        try:
            await self._gql("""mutation($id: String!) {
                podStop(input: {podId: $id}) { id desiredStatus }
            }""", {"id": pod_id})
            logger.info("[RunPod] Pod %s stopped", pod_id)
        except Exception as e:
            logger.warning("[RunPod] Failed to stop pod %s: %s", pod_id, e)

    async def stop(self) -> None:
        """Stop the pod (preserves volume) and cancel the watcher."""
        if self._watcher_task:
            self._watcher_task.cancel()
            self._watcher_task = None

        if self._pod_id:
            await self._stop_pod(self._pod_id)
            self._pod_id = None

    # ── Background watcher ────────────────────────────────────────────────────

    async def _watch(self) -> None:
        """
        When no pod is running: poll every 5 min for available capacity and
        spin one up as soon as a GPU comes free.
        When a pod is running: log if a meaningfully cheaper option appears.
        """
        while True:
            interval = _WATCHER_INTERVAL_S if self._pod_id else _CAPACITY_POLL_S
            await asyncio.sleep(interval)
            try:
                gpu_types = await self._fetch_gpu_types()
                ranked = self._rank_gpus(gpu_types)
                if not ranked:
                    continue

                if self._pod_id is None:
                    # No pod running — try to claim the best available GPU
                    logger.info("[RunPod] Capacity check — trying %s (%dGB, $%.2f/hr)",
                                ranked[0]["displayName"], ranked[0]["memoryInGb"], ranked[0]["_price"])
                    for gpu in ranked:
                        pod_id = await self._create_pod(gpu["id"])
                        if pod_id:
                            self._pod_id = pod_id
                            self._current_price = gpu["_price"]
                            if await self._wait_until_ready(pod_id):
                                self._apply_host(pod_id)
                                logger.info("[RunPod] Pod acquired mid-session — cells switching from local to RunPod")
                                return  # stop looking
                            await self._stop_pod(pod_id)
                            self._pod_id = None
                else:
                    # Pod running — log if something meaningfully cheaper appeared
                    best = ranked[0]
                    if (self._current_price and
                            best["_price"] < self._current_price * (1 - _MIGRATE_THRESHOLD)):
                        logger.info(
                            "[RunPod] Better GPU available: %s (%dGB, $%.2f/hr vs current $%.2f/hr)"
                            " — restart the app to switch.",
                            best["displayName"], best["memoryInGb"],
                            best["_price"], self._current_price,
                        )
            except Exception as e:
                logger.debug("[RunPod] Watcher error: %s", e)
