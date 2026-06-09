"""
API session registry — maps a logical conversation handle to one end_user.

An API ``session`` is a partner-facing conversation id bound to a single customer
(end_user_id). The partner opens one per customer-conversation; every turn against
it runs as that customer, so the brain binds that customer's chemistry and keys
their relationship/memory. Process-local and in-memory for v1 (durable session
storage is part of the engine-layer build).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


@dataclass
class ApiSession:
    session_id: str
    end_user_id: str
    agent_id: str | None = None
    created_ts: float = 0.0


class ApiSessionRegistry:
    def __init__(self, now_fn=time.time, id_fn=None) -> None:
        self._sessions: dict[str, ApiSession] = {}
        self._now = now_fn
        self._id_fn = id_fn or (lambda: uuid.uuid4().hex[:16])

    def create(self, end_user_id: str, agent_id: str | None = None) -> ApiSession:
        sid = self._id_fn()
        s = ApiSession(
            session_id=sid,
            end_user_id=end_user_id,
            agent_id=agent_id,
            created_ts=self._now(),
        )
        self._sessions[sid] = s
        return s

    def get(self, session_id: str) -> ApiSession | None:
        return self._sessions.get(session_id)
