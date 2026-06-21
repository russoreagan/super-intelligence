"""
Provisioner — LocalProcessSpawner-style tenant manager (JupyterHub pattern).

The gateway calls ensure(user_id) on each authenticated request; this spawns the
identical brain code as ONE OS subprocess per user, bound to a private localhost
port, with per-user env (settings.json, second_brain dir, BRAIN_USER_ID; the
brain fetches its own API keys from the Vault at boot). The gateway then
reverse-proxies that user's traffic to their port.

Same shape as JupyterHub's spawner + culler:
  ensure()  → resume-or-spawn the user's process, wait for /health, return port
  touch()   → mark activity (called by the gateway proxy)
  reaper    → safety backstop only: reap a process whose user hasn't connected
              in BRAIN_SESSION_IDLE_TIMEOUT_S (default 24h). A brain stays awake
              and keeps thinking (DMN) while its user is away — it only stops when
              explicitly slept (Sleep button → /shutdown), not on logout/idle.
  stop()    → stop one user's process

Subprocesses are children of the gateway, so if the gateway dies they die too
(no orphans to reconcile); a fresh gateway lazily respawns on next login. State
that must survive lives in Supabase + the per-user volume, not in the process.

Scale path (unchanged callers): swap _spawn() for a Docker/pod backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Safety backstop, NOT an aggressive idle culler. A tenant brain is meant to stay
# awake and keep thinking (its DMN runs idle thoughts) the whole time its user is
# away — it should only stop when the user explicitly sleeps it (Sleep button →
# /shutdown). So this is set to a long window (24h) and reaps only a session whose
# user hasn't connected at all in that span — i.e. truly abandoned. The brain
# self-consolidates periodically while awake (sleep_periodic_*), so a backstop
# reap doesn't lose memory. Override with BRAIN_SESSION_IDLE_TIMEOUT_S if needed.
IDLE_TIMEOUT_S = float(os.environ.get("BRAIN_SESSION_IDLE_TIMEOUT_S", "86400"))
READY_TIMEOUT_S = float(os.environ.get("BRAIN_TENANT_READY_TIMEOUT_S", "180"))
PORT_RANGE_START = int(os.environ.get("BRAIN_TENANT_PORT_START", "9000"))
PORT_RANGE_END = int(os.environ.get("BRAIN_TENANT_PORT_END", "9999"))
# Per-user data root on the app host. Each user: <root>/<user_id>/{settings.json,second_brain}
TENANTS_DIR = Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve()
# brain.run flags for tenant processes. Mirrors the shared deploy's set; override
# via BRAIN_TENANT_ARGS (e.g. drop --ears to cut per-process RAM).
# --ears enables server-side mic DSP (fingerprinting/speaker-ID/prosody via
# AuditoryCluster). On hosted Railway, voice input arrives from the browser and
# goes straight to Deepgram — there is no server-side mic, so --ears adds RAM
# overhead with no benefit. Re-enable via BRAIN_TENANT_ARGS if needed.
TENANT_ARGS = os.environ.get(
    "BRAIN_TENANT_ARGS", "--ui --dmn --metacognition --voice --motor"
).split()
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_SETTINGS = Path(__file__).resolve().parent / "settings.json"


def _start_log_relay(proc, user_id: str) -> None:
    """Relay brain subprocess stdout+stderr through the gateway's logger.

    Without this the child's output lands on inherited FDs that Railway's CLI
    batches and drops for subprocess output. Prefixing with the tenant id makes
    boot errors and tracebacks identifiable in the dashboard."""
    prefix = f"[tenant:{user_id[:8]}]"

    def _read():
        try:
            for line in proc.stdout:
                logger.info("%s %s", prefix, line.rstrip())
        except Exception:
            pass

    threading.Thread(target=_read, daemon=True, name=f"log-relay-{user_id[:8]}").start()


class _Proc:
    def __init__(self, proc, port: int, api_port: int | None = None) -> None:
        self.proc = proc
        self.port = port
        self.api_port = api_port  # tenant's engine-API port (for /v1 gateway routing)
        self.last_active: float = time.time()
        self.booting: bool = True


def _free_port() -> int:
    """Grab an OS-assigned free port in our range by trying binds."""
    for _ in range(200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                # 0 lets the OS pick; we then check it's in range, else retry.
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            except OSError:
                continue
        if PORT_RANGE_START <= port <= PORT_RANGE_END:
            return port
    # Fall back to scanning the range explicitly.
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port available for tenant process")


class Provisioner:
    """Owns the lifecycle of all tenant brain subprocesses on this host."""

    def __init__(self, cmd_builder=None) -> None:
        self._procs: dict[str, _Proc] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None
        # cmd_builder(port, env) -> list[str]. Defaults to the brain.run command;
        # a test or a future container backend can inject its own.
        self._cmd_builder = cmd_builder or self._default_cmd

    @staticmethod
    def _key(user_id: str, persona: str | None = None) -> str:
        """Process key. Without a persona it's just the tenant id (the original
        single-persona-per-tenant behavior, byte-for-byte). With a persona it's a
        composite so one tenant can run several personas concurrently, each its own
        process — Path A multi-persona. The gateway passes the target persona; older
        callers that pass none keep the tenant-only key."""
        return user_id if not persona else f"{user_id}::{persona}"

    @staticmethod
    def _default_cmd(port: int, env: dict) -> list[str]:
        # BRAIN_TENANT_CMD overrides the spawn command (shlex-split; "{port}" is
        # substituted). Used by integration tests and as an ops escape hatch;
        # default is the real brain.
        override = os.environ.get("BRAIN_TENANT_CMD", "").strip()
        if override:
            import shlex

            return [part.replace("{port}", str(port)) for part in shlex.split(override)]
        return [sys.executable, "-m", "brain.run", *TENANT_ARGS]

    async def start(self) -> None:
        TENANTS_DIR.mkdir(parents=True, exist_ok=True)
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="tenant_reaper")

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
        for key in list(self._procs):
            await self._stop_key(key)

    def _lock_for(self, user_id: str, persona: str | None = None) -> asyncio.Lock:
        key = self._key(user_id, persona)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def touch(self, user_id: str, persona: str | None = None) -> None:
        p = self._procs.get(self._key(user_id, persona))
        if p:
            p.last_active = time.time()

    def status(self, user_id: str, persona: str | None = None) -> dict | None:
        p = self._procs.get(self._key(user_id, persona))
        if not p:
            return None
        return {"port": p.port, "api_port": p.api_port, "booting": p.booting, "pid": p.proc.pid}

    def is_running(self, user_id: str, persona: str | None = None) -> bool:
        """True if this (user, persona) brain process exists and is still alive. Used
        by the gateway's sleep flow to wait for a graceful self-shutdown
        (consolidation) before force-reaping."""
        p = self._procs.get(self._key(user_id, persona))
        return bool(p and p.proc.poll() is None)

    def live_count(self) -> int:
        """Number of tenant brain processes currently alive (booting or serving).

        The gateway uses this to drive the shared RunPod pod's lifecycle: the pod
        only needs to run while ≥1 brain is alive to use it. A brain that died
        (proc.poll() set) but hasn't been reaped yet does NOT count — it has no
        DMN running, so keeping the pod up for it is pure waste."""
        return sum(1 for p in self._procs.values() if p.proc.poll() is None)

    async def ensure(self, user_id: str, persona: str | None = None) -> int:
        """Resume-or-spawn this (user, persona) brain process; return its localhost
        port. With no persona this is the original tenant-only process; with one, a
        per-persona process so a tenant can run several personas at once.

        Idempotent and concurrency-safe: simultaneous callers for the same key
        await one spawn. Reuses a live process; replaces a dead one."""
        key = self._key(user_id, persona)
        async with self._lock_for(user_id, persona):
            p = self._procs.get(key)
            if p and p.proc.poll() is None:
                p.last_active = time.time()
                return p.port
            if p and p.proc.poll() is not None:
                logger.warning(
                    "[provisioner] %s process exited (code %s) — respawning",
                    key[:16],
                    p.proc.poll(),
                )
                self._procs.pop(key, None)
            return await self._spawn(user_id, persona)

    async def _spawn(self, user_id: str, persona: str | None = None) -> int:
        """Launch one brain.run subprocess for (user_id, persona) and wait for
        /health.

        Retries once on a failed boot with a fresh port: between _free_port()'s
        probe and the child binding, another process can steal the port
        (concurrent logins), which shows up as a health-check timeout."""
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                return await self._spawn_once(user_id, persona)
            except RuntimeError as e:
                last_err = e
                logger.warning(
                    "[provisioner] spawn attempt %d for %s failed: %s",
                    attempt + 1,
                    self._key(user_id, persona)[:16],
                    e,
                )
        raise last_err if last_err else RuntimeError("tenant spawn failed")

    async def _spawn_once(self, user_id: str, persona: str | None = None) -> int:
        port = _free_port()
        api_port = _free_port()  # engine API on its own port (distinct from the UI port)
        while api_port == port:
            api_port = _free_port()
        # An explicit persona gets its own data dir so its settings/local caches
        # (and local-mode wiring.json) never collide with a sibling persona under
        # the same tenant. The default (no persona) keeps the original path.
        root = TENANTS_DIR / user_id / "personas" / persona if persona else TENANTS_DIR / user_id
        (root / "second_brain").mkdir(parents=True, exist_ok=True)
        settings_path = root / "settings.json"
        # Seed a fresh tenant's settings.json from the bundled defaults so they
        # start with sane chemistry; thereafter it's theirs and never overwritten.
        # Copy via temp + atomic rename: the persona_name read just below (and a
        # racing concurrent spawn) must never see a partially-written file.
        if not settings_path.exists() and _BUNDLED_SETTINGS.exists():
            tmp = settings_path.with_suffix(".json.tmp")
            shutil.copy(_BUNDLED_SETTINGS, tmp)
            os.replace(tmp, settings_path)

        # Pin the persona name so memory/chemistry key off the right slug from the
        # first boot (run.py prefers settings.json's persona_name over the env var
        # and falls back to "default" if empty). For an explicit persona we FORCE it
        # into the seeded settings so settings + env agree; for the default we read
        # it from settings as before.
        persona_name = ""
        if persona:
            persona_name = persona
            with contextlib.suppress(Exception):
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                if data.get("persona_name") != persona:
                    data["persona_name"] = persona
                    tmp = settings_path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(data), encoding="utf-8")
                    os.replace(tmp, settings_path)
        else:
            try:
                persona_name = str(
                    json.loads(settings_path.read_text(encoding="utf-8")).get("persona_name", "")
                )
            except Exception:
                persona_name = ""

        env = os.environ.copy()
        env.update(
            {
                "BRAIN_MULTITENANT": "1",
                # user_id is the TENANT key = org id (the gateway resolves it). For a
                # personal org it equals the user's own uid, so data keyed by
                # BRAIN_USER_ID is unchanged. BRAIN_ORG_ID is the explicit name the
                # brain's membership-aware auth uses.
                "BRAIN_USER_ID": user_id,
                "BRAIN_ORG_ID": user_id,
                "BRAIN_STORAGE_BACKEND": "supabase",
                "BRAIN_SETTINGS_PATH": str(settings_path),
                "SECOND_BRAIN_PATH": str(root / "second_brain"),
                # Hosted tenants render/capture audio in the browser, not via a
                # server-side sound device. Without this, BROWSER_AUDIO_MODE is
                # false, attach_tts_queue never runs, and TTS audio is never
                # streamed to the client (voice silently no-ops on hosted).
                "BRAIN_AUDIO_OUTPUT_DEVICE": "browser",
                "PORT": str(port),
                # Engine API binds here; the gateway proxies /v1 partner traffic to it
                # (the brain only starts the API server if the org has partner keys).
                "BRAIN_API_PORT": str(api_port),
            }
        )
        if persona_name:
            env["BRAIN_PERSONA_NAME"] = persona_name
        else:
            # run.py now hard-fails a multitenant boot without a resolvable persona
            # (the persona='default' fallback cross-buckets tenants). Catch it here
            # before burning a port + health-check timeout on a doomed spawn.
            raise RuntimeError(
                f"tenant {user_id[:8]}: settings.json at {settings_path} has no "
                "persona_name — cannot spawn without BRAIN_PERSONA_NAME"
            )
        # Bind the child to 127.0.0.1 (reachable only via the gateway). server.py
        # binds 0.0.0.0 when RAILWAY_ENVIRONMENT is set, so clear it for children;
        # the gateway keeps it to serve publicly.
        env.pop("RAILWAY_ENVIRONMENT", None)
        # Auth stays ON in the child; it re-verifies the forwarded cookie and pins
        # to BRAIN_USER_ID. Never disable it for tenants.
        env.pop("BRAIN_AUTH_DISABLED", None)
        # Scoped DB credential: mint an org JWT (sub = org_id) so the tenant's
        # storage layer runs under RLS instead of the service role. The vault
        # fetch happens HERE (the gateway holds the service key) and the user's
        # BYO keys are injected directly — the tenant never needs service-role
        # access for anything. Key changes are picked up on respawn.
        from brain.gateway.org_token import mint_org_token

        # mint_org_token may do a (cached) JWKS probe over the network; run it off the
        # event loop so a spawn never blocks the gateway from serving other requests.
        org_jwt = await asyncio.to_thread(mint_org_token, user_id)
        if org_jwt:
            env["BRAIN_SUPABASE_JWT"] = org_jwt
        else:
            # No mintable token — either SUPABASE_JWT_SECRET is unset (local dev) or
            # the project signs JWTs asymmetrically, which makes any HS256 token we
            # could mint inert (Supabase would 401 it). The tenant keeps the
            # service-role key; isolation rests on the storage layer's in-query
            # org scoping. See brain/gateway/org_token.py.
            logger.info(
                "[provisioner] tenant %s uses the service-role key "
                "(no org JWT mintable under asymmetric signing / no secret).",
                user_id[:8],
            )
        # Gateway-only secrets never belong in a tenant process: the service-role
        # key (the tenant uses the org JWT above), pod lifecycle (RUNPOD_API_KEY),
        # admission/reset mail (RESEND_API_KEY), the gateway's engine-API keys
        # (BRAIN_API_KEYS), and the platform Anthropic key — tenants are BYO-key.
        # Redact BEFORE the vault injection below so the user's own keys survive;
        # the service key is only kept when no JWT could be minted (dev fallback).
        secrets = [
            "RUNPOD_API_KEY",
            "RESEND_API_KEY",
            "BRAIN_API_KEYS",
            "BRAIN_API_KEY",
            "ANTHROPIC_API_KEY",
        ]
        if org_jwt:
            secrets.append("SUPABASE_SERVICE_KEY")
        for secret in secrets:
            env.pop(secret, None)

        # The tenant's own BYO keys, fetched here because only the gateway holds
        # the service role. Key changes are picked up on respawn.
        try:
            from brain.vault import PROVIDER_ENV, fetch_user_keys

            # Synchronous Supabase RPC (+ decrypt) — offload so it doesn't block the loop.
            user_keys = await asyncio.to_thread(fetch_user_keys, user_id)
            for provider, value in (user_keys or {}).items():
                env_name = PROVIDER_ENV.get(provider)
                if env_name and value:
                    env[env_name] = value
        except Exception as e:
            logger.warning("[provisioner] vault key fetch for %s failed: %s", user_id[:8], e)

        cmd = self._cmd_builder(port, env)
        logger.info("[provisioner] spawning %s on :%d (%s)", user_id[:8], port, " ".join(cmd[:4]))
        proc = subprocess.Popen(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # Relay the brain's stdout/stderr through the gateway's logger so boot
        # errors and tracebacks are visible in Railway dashboard logs (the CLI
        # doesn't surface child-process output reliably).
        _start_log_relay(proc, user_id)
        key = self._key(user_id, persona)
        entry = _Proc(proc, port, api_port=api_port)
        self._procs[key] = entry

        ok = await self._wait_health(port, proc)
        entry.booting = False
        if not ok:
            with contextlib.suppress(Exception):
                proc.terminate()
            self._procs.pop(key, None)
            raise RuntimeError(
                f"tenant {key[:16]} failed to become healthy on :{port} "
                f"(exit code: {proc.poll()})"
            )
        logger.info("[provisioner] %s healthy on :%d", key[:16], port)
        return port

    async def _wait_health(self, port: int, proc) -> bool:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + READY_TIMEOUT_S
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    logger.error("[provisioner] process exited during boot (code %s)", proc.poll())
                    return False
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        return False

    async def stop_user(self, user_id: str, persona: str | None = None) -> None:
        await self._stop_key(self._key(user_id, persona))

    async def _stop_key(self, key: str) -> None:
        p = self._procs.pop(key, None)
        if not p:
            return
        with contextlib.suppress(Exception):
            p.proc.terminate()
        # Give it a moment, then hard-kill if needed.
        for _ in range(10):
            if p.proc.poll() is not None:
                break
            await asyncio.sleep(0.3)
        if p.proc.poll() is None:
            with contextlib.suppress(Exception):
                p.proc.kill()
        logger.info("[provisioner] stopped %s", key[:16])

    async def _reaper_loop(self) -> None:
        # Backstop only: a brain stays awake (and keeps thinking) until the user
        # sleeps it. We reap solely to reclaim truly-abandoned sessions — a user
        # who hasn't connected at all in IDLE_TIMEOUT_S (default 24h). last_active
        # tracks client connections, not the brain's own DMN activity, so "no
        # connection for 24h" is the right abandoned signal.
        # IDLE_TIMEOUT_S <= 0 disables reaping entirely (always-on): every tenant
        # brain runs until it's explicitly slept. Used now while there's a small
        # user count; revisit with real capacity management before scaling up.
        if IDLE_TIMEOUT_S <= 0:
            logger.info("[provisioner] idle reaping disabled — tenant brains run until slept")
            return
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for key, p in list(self._procs.items()):
                if not p.booting and (now - p.last_active) > IDLE_TIMEOUT_S:
                    logger.info(
                        "[provisioner] reaping abandoned tenant %s (no connection in %.0fh)",
                        key[:16],
                        (now - p.last_active) / 3600,
                    )
                    await self._stop_key(key)
