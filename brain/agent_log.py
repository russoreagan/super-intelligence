"""
Durable activity log for engine-API "agent" turns (the partner-/agent-driven path,
e.g. the trading app).

The interactive UI keeps its recent turns in process memory and replays them on
reconnect; agent turns are now treated the same way, but the recent buffer is
backed by the ``agent_turns`` table (migration 015) so the owner's Agents view
survives a restart and — when an org runs a dedicated agent-worker process —
aggregates across processes.

Best-effort and Supabase-backed, exactly like brain/api/sessions.py: a write never
fails a turn, and companion/local mode (no Supabase) is a silent no-op (the live
in-memory buffer in the UI server still works).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _sb():
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def record(
    *,
    agent_id: str | None,
    end_user_id: str | None,
    session_id: str | None,
    turn_id: str | None,
    persona: str | None,
    prompt: str,
    response: str,
) -> None:
    """Append one completed agent turn. Best-effort: any failure is swallowed."""
    sb = _sb()
    if sb is None:
        return
    try:
        client, org = sb
        client.table("agent_turns").insert(
            {
                "org_id": org,
                "persona": persona or "",
                "agent_id": agent_id or "",
                "end_user_id": end_user_id or "",
                "session_id": session_id or "",
                "turn_id": turn_id or "",
                "prompt": prompt or "",
                "response": response or "",
            }
        ).execute()
    except Exception as e:
        logger.debug("[agent_log] record skipped: %s", e)


def recent(limit: int = 50, agent_id: str | None = None) -> list[dict]:
    """Most recent agent turns for this org (oldest-first, ready to render), across
    all agents or filtered to one ``agent_id``. Empty on any error or local mode."""
    sb = _sb()
    if sb is None:
        return []
    try:
        client, org = sb
        q = (
            client.table("agent_turns")
            .select("agent_id, end_user_id, session_id, turn_id, prompt, response, ts")
            .eq("org_id", org)
        )
        if agent_id:
            q = q.eq("agent_id", agent_id)
        rows = q.order("ts", desc=True).limit(max(1, min(limit, 200))).execute().data or []
        rows.reverse()  # oldest-first for append-style rendering
        return rows
    except Exception as e:
        logger.debug("[agent_log] recent skipped: %s", e)
        return []
