"""
API session registry — maps a logical conversation handle to one end_user.

An API ``session`` is a partner-facing conversation id bound to a single customer
(end_user_id). The partner opens one per customer-conversation; every turn against
it runs as that customer, so the brain binds that customer's chemistry and keys
their relationship/memory.

Durable: sessions persist to the ``api_sessions`` Supabase table (write-through on
create/update, read-through on a memory miss) so they survive process restarts.
The in-memory dict stays as a hot cache. Companion/local mode (no Supabase) keeps
everything in memory exactly as before — persistence is best-effort and never
fails a request.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ApiSession:
    session_id: str
    end_user_id: str
    agent_id: str | None = None
    # The role for this conversation (persona contract MANDATE), selected by id from
    # the persona's cached assignment catalog. The partner's routing maps a customer
    # to a mandate_id (billing queue vs technical queue) and passes it here. An
    # unknown id silently falls back to no assignment. Subordinate to identity +
    # safety when it reaches the prompt.
    mandate_id: str | None = None
    # The partner identity that opened this session (per-partner key scoping); None
    # for the org owner's own key.
    partner_id: str | None = None
    # A cloud-write action awaiting confirmation (the confirmation flow). None when
    # nothing is pending. Stored per-session so concurrent sessions don't collide on
    # the executor's process-global pending slot.
    pending: dict | None = None
    created_ts: float = 0.0


class ApiSessionRegistry:
    def __init__(self, now_fn=time.time, id_fn=None) -> None:
        self._sessions: dict[str, ApiSession] = {}
        self._now = now_fn
        self._id_fn = id_fn or (lambda: uuid.uuid4().hex[:16])

    def create(
        self,
        end_user_id: str,
        agent_id: str | None = None,
        mandate_id: str | None = None,
        partner_id: str | None = None,
    ) -> ApiSession:
        sid = self._id_fn()
        s = ApiSession(
            session_id=sid,
            end_user_id=end_user_id,
            agent_id=agent_id,
            mandate_id=mandate_id,
            partner_id=partner_id,
            created_ts=self._now(),
        )
        self._sessions[sid] = s
        self._persist(s)
        return s

    def get(self, session_id: str) -> ApiSession | None:
        s = self._sessions.get(session_id)
        if s is not None:
            return s
        s = self._load(session_id)
        if s is not None:
            self._sessions[session_id] = s
        return s

    def update(self, session: ApiSession) -> None:
        """Persist a mutated session (e.g. after setting/clearing ``pending``)."""
        self._sessions[session.session_id] = session
        self._persist(session)

    def forget_end_user(self, end_user_id: str) -> None:
        """Drop in-memory sessions bound to this end_user (lifecycle purge). The
        durable api_sessions rows are removed by the purge's table sweep."""
        for sid in [k for k, s in self._sessions.items() if s.end_user_id == end_user_id]:
            self._sessions.pop(sid, None)

    # ── persistence (best-effort, Supabase-backed) ────────────────────────────

    def _persist(self, s: ApiSession) -> None:
        sb = self._sb()
        if sb is None:
            return
        try:
            client, org = sb
            client.table("api_sessions").upsert(
                {
                    "org_id": org,
                    "session_id": s.session_id,
                    "end_user_id": s.end_user_id,
                    "agent_id": s.agent_id,
                    "mandate_id": s.mandate_id,
                    "partner_id": s.partner_id,
                    "pending": s.pending,
                },
                on_conflict="org_id,session_id",
            ).execute()
        except Exception as e:
            logger.debug("[ApiSessions] persist skipped: %s", e)

    def _load(self, session_id: str) -> ApiSession | None:
        sb = self._sb()
        if sb is None:
            return None
        try:
            client, org = sb
            res = (
                client.table("api_sessions")
                .select("session_id, end_user_id, agent_id, mandate_id, partner_id, pending, created_ts")
                .eq("org_id", org)
                .eq("session_id", session_id)
                .execute()
            )
            rows = res.data or []
            if not rows:
                return None
            r = rows[0]
            return ApiSession(
                session_id=r["session_id"],
                end_user_id=r["end_user_id"],
                agent_id=r.get("agent_id"),
                mandate_id=r.get("mandate_id"),
                partner_id=r.get("partner_id"),
                pending=r.get("pending"),
                created_ts=0.0,
            )
        except Exception as e:
            logger.debug("[ApiSessions] load skipped: %s", e)
            return None

    @staticmethod
    def _sb():
        try:
            from brain.second_brain import supabase_client

            if not supabase_client.is_enabled():
                return None
            return supabase_client.get_client(), supabase_client.get_org_id()
        except Exception:
            return None
