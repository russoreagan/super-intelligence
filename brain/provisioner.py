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
  reaper    → stop processes idle past BRAIN_SESSION_IDLE_TIMEOUT_S
  stop()    → stop one user's process

Subprocesses are children of the gateway, so if the gateway dies they die too
(no orphans to reconcile); a fresh gateway lazily respawns on next login. State
that must survive lives in Supabase + the per-user volume, not in the process.

Scale path (unchanged callers): swap _spawn() for a Docker/pod backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_S = float(os.environ.get("BRAIN_SESSION_IDLE_TIMEOUT_S", "600"))
READY_TIMEOUT_S = float(os.environ.get("BRAIN_TENANT_READY_TIMEOUT_S", "180"))
PORT_RANGE_START = int(os.environ.get("BRAIN_TENANT_PORT_START", "9000"))
PORT_RANGE_END = int(os.environ.get("BRAIN_TENANT_PORT_END", "9999"))
# Per-user data root on the app host. Each user: <root>/<user_id>/{settings.json,second_brain}
TENANTS_DIR = Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve()
# brain.run flags for tenant processes. Mirrors the shared deploy's set; override
# via BRAIN_TENANT_ARGS (e.g. drop --ears to cut per-process RAM).
TENANT_ARGS = os.environ.get(
    "BRAIN_TENANT_ARGS", "--ui --dmn --metacognition --ears --voice"
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
        """Launch one brain.run subprocess for user_id and wait for /health."""
        port = _free_port()
        root = TENANTS_DIR / user_id
        (root / "second_brain").mkdir(parents=True, exist_ok=True)
        settings_path = root / "settings.json"
        # Seed a fresh tenant's settings.json from the bundled defaults so they
        # start with sane chemistry; thereafter it's theirs and never overwritten.
        if not settings_path.exists() and _BUNDLED_SETTINGS.exists():
            shutil.copy(_BUNDLED_SETTINGS, settings_path)

        env = os.environ.copy()
        env.update(
            {
                "BRAIN_MULTITENANT": "1",
                "BRAIN_USER_ID": user_id,
                "BRAIN_STORAGE_BACKEND": "supabase",
                "BRAIN_SETTINGS_PATH": str(settings_path),
                "SECOND_BRAIN_PATH": str(root / "second_brain"),
                "PORT": str(port),
            }
        )
        # Bind the child to 127.0.0.1 (reachable only via the gateway). server.py
        # binds 0.0.0.0 when RAILWAY_ENVIRONMENT is set, so clear it for children;
        # the gateway keeps it to serve publicly.
        env.pop("RAILWAY_ENVIRONMENT", None)
        # Auth stays ON in the child; it re-verifies the forwarded cookie and pins
        # to BRAIN_USER_ID. Never disable it for tenants.
        env.pop("BRAIN_AUTH_DISABLED", None)

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
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for uid, p in list(self._procs.items()):
                if not p.booting and (now - p.last_active) > IDLE_TIMEOUT_S:
                    logger.info("[provisioner] reaping idle tenant %s (idle %.0fs)",
                                uid[:8], now - p.last_active)
                    await self.stop_user(uid)
