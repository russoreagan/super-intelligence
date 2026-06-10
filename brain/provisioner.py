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
import sys
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
TENANT_ARGS = os.environ.get(
    "BRAIN_TENANT_ARGS", "--ui --dmn --metacognition --ears --voice --motor"
).split()
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_SETTINGS = Path(__file__).resolve().parent / "settings.json"


class _Proc:
    def __init__(self, proc, port: int) -> None:
        self.proc = proc
        self.port = port
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
        for uid in list(self._procs):
            await self.stop_user(uid)

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = self._locks[user_id] = asyncio.Lock()
        return lock

    def touch(self, user_id: str) -> None:
        p = self._procs.get(user_id)
        if p:
            p.last_active = time.time()

    def status(self, user_id: str) -> dict | None:
        p = self._procs.get(user_id)
        if not p:
            return None
        return {"port": p.port, "booting": p.booting, "pid": p.proc.pid}

    async def ensure(self, user_id: str) -> int:
        """Resume-or-spawn this user's brain process; return its localhost port.

        Idempotent and concurrency-safe: simultaneous callers for the same user
        await one spawn. Reuses a live process; replaces a dead one."""
        async with self._lock_for(user_id):
            p = self._procs.get(user_id)
            if p and p.proc.poll() is None:
                p.last_active = time.time()
                return p.port
            if p and p.proc.poll() is not None:
                logger.warning("[provisioner] %s process exited (code %s) — respawning",
                               user_id[:8], p.proc.poll())
                self._procs.pop(user_id, None)
            return await self._spawn(user_id)

    async def _spawn(self, user_id: str) -> int:
        """Launch one brain.run subprocess for user_id and wait for /health.

        Retries once on a failed boot with a fresh port: between _free_port()'s
        probe and the child binding, another process can steal the port
        (concurrent logins), which shows up as a health-check timeout."""
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                return await self._spawn_once(user_id)
            except RuntimeError as e:
                last_err = e
                logger.warning(
                    "[provisioner] spawn attempt %d for %s failed: %s",
                    attempt + 1,
                    user_id[:8],
                    e,
                )
        raise last_err if last_err else RuntimeError("tenant spawn failed")

    async def _spawn_once(self, user_id: str) -> int:
        port = _free_port()
        root = TENANTS_DIR / user_id
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

        # Pin the tenant's persona name so its memory/chemistry key off the right
        # slug from the first boot (rather than relying on run.py re-deriving it
        # from settings.json, which falls back to "default" and cross-buckets if
        # persona_name is empty). Read it from the just-seeded settings.json.
        persona_name = ""
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

        org_jwt = mint_org_token(user_id)
        if org_jwt:
            env["BRAIN_SUPABASE_JWT"] = org_jwt
        else:
            logger.warning(
                "[provisioner] SUPABASE_JWT_SECRET unset — tenant %s falls back to "
                "the service-role key (RLS not enforced). Set the secret in prod.",
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

            for provider, value in (fetch_user_keys(user_id) or {}).items():
                env_name = PROVIDER_ENV.get(provider)
                if env_name and value:
                    env[env_name] = value
        except Exception as e:
            logger.warning("[provisioner] vault key fetch for %s failed: %s", user_id[:8], e)

        cmd = self._cmd_builder(port, env)
        logger.info("[provisioner] spawning %s on :%d (%s)", user_id[:8], port, " ".join(cmd[:4]))
        import subprocess

        proc = subprocess.Popen(cmd, cwd=str(_REPO_ROOT), env=env)
        entry = _Proc(proc, port)
        self._procs[user_id] = entry

        ok = await self._wait_health(port, proc)
        entry.booting = False
        if not ok:
            with contextlib.suppress(Exception):
                proc.terminate()
            self._procs.pop(user_id, None)
            raise RuntimeError(f"tenant {user_id[:8]} failed to become healthy on :{port}")
        logger.info("[provisioner] %s healthy on :%d", user_id[:8], port)
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

    async def stop_user(self, user_id: str) -> None:
        p = self._procs.pop(user_id, None)
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
        logger.info("[provisioner] stopped %s", user_id[:8])

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
            for uid, p in list(self._procs.items()):
                if not p.booting and (now - p.last_active) > IDLE_TIMEOUT_S:
                    logger.info("[provisioner] reaping abandoned tenant %s (no connection in %.0fh)",
                                uid[:8], (now - p.last_active) / 3600)
                    await self.stop_user(uid)
