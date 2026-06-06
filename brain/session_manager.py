"""
SessionManager — manages one BrainSession per authenticated user.

In multi-tenant mode the FastAPI server creates one SessionManager and calls
get_or_create(user_id) on each new WebSocket connection. Sessions stay alive
for SESSION_IDLE_TIMEOUT_S after the last client disconnects so the DMN keeps
thinking; they are garbage-collected after that.

Single-user local mode: the existing run.py path creates BrainSession directly
and bypasses this entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.brain_session import BrainSession

logger = logging.getLogger(__name__)

SESSION_IDLE_TIMEOUT_S = float(os.environ.get("BRAIN_SESSION_IDLE_TIMEOUT_S", "600"))


class _SessionEntry:
    def __init__(self, session: BrainSession, task: asyncio.Task) -> None:
        self.session = session
        self.task = task
        self.client_count: int = 0
        self.last_active: float = time.time()


class SessionManager:
    """Owns the lifecycle of all active BrainSessions."""

    def __init__(self, args, shared_ui_server=None) -> None:
        self._args = args
        self._shared_ui_server = shared_ui_server
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="session_reaper")

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
        for entry in list(self._sessions.values()):
            entry.task.cancel()

    async def get_or_create(self, user_id: str) -> BrainSession:
        """Return the existing session for user_id, or boot a new one."""
        async with self._lock:
            if user_id in self._sessions:
                entry = self._sessions[user_id]
                entry.client_count += 1
                entry.last_active = time.time()
                logger.info(
                    "[SessionManager] Existing session for user %s (%d clients)",
                    user_id[:8],
                    entry.client_count,
                )
                return entry.session

            logger.info("[SessionManager] Booting new session for user %s", user_id[:8])
            session = await self._boot_session(user_id)
            return session

    def client_disconnected(self, user_id: str) -> None:
        entry = self._sessions.get(user_id)
        if entry:
            entry.client_count = max(0, entry.client_count - 1)
            entry.last_active = time.time()

    async def _boot_session(self, user_id: str) -> BrainSession:
        """Create and start a BrainSession. Called inside _lock."""
        # Set up per-user storage context
        from brain.second_brain import supabase_client

        supabase_client.set_user_id(user_id)

        from brain.brain_session import BrainSession

        session = BrainSession(
            self._args,
            user_id=user_id,
            shared_ui_server=self._shared_ui_server,
        )

        # Run the session in a background task — it blocks on its own event loop
        # internals (brainstem loops) so it must run as a task, not be awaited.
        task = asyncio.create_task(
            self._run_session(session, user_id), name=f"session_{user_id[:8]}"
        )
        self._sessions[user_id] = _SessionEntry(session, task)
        # Give the session a moment to complete setup before returning
        await asyncio.sleep(0.1)
        return session

    async def _run_session(self, session: BrainSession, user_id: str) -> None:
        try:
            # Run setup phases only — not the blocking run() loop.
            # The session's loops run as tasks managed by brainstem.
            await session._setup_core()
            await session._setup_runpod()
            await session._setup_wiring()
            await session._setup_clusters()
            # Skip _setup_ui — we use the shared server
            await session._setup_motor()
            await session._setup_dmn()
            await session._setup_meta()
            await session._setup_auditory()
            await session._setup_streaming_mic()
            session._setup_speak_gate()
            session._setup_voice_bridge()
            session._setup_loops()
            logger.info("[SessionManager] Session %s fully booted", session.session_id)
            # Keep alive — brainstem manages its own tasks
            while not session.brainstem._stop_event.is_set():
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[SessionManager] Session %s crashed: %s", user_id[:8], e)
        finally:
            async with self._lock:
                self._sessions.pop(user_id, None)
            logger.info("[SessionManager] Session for user %s ended", user_id[:8])

    async def _reaper_loop(self) -> None:
        """Periodically shut down sessions with no connected clients."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            async with self._lock:
                for uid, entry in list(self._sessions.items()):
                    idle_s = now - entry.last_active
                    if entry.client_count == 0 and idle_s > SESSION_IDLE_TIMEOUT_S:
                        logger.info(
                            "[SessionManager] Reaping idle session for user %s (idle %.0fs)",
                            uid[:8],
                            idle_s,
                        )
                        entry.task.cancel()
                        self._sessions.pop(uid, None)
