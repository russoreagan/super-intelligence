"""
Active-agent binding for the current async context.

Mirrors the ``Bus.bind`` chemistry pattern (brain/bus.py): a contextvar holds the
agent in scope for one reactive dispatch, so the motor enforcement layer can
resolve PER-AGENT permissions without threading an argument through every call
site. Nothing bound (companion/local mode, or a self-directed job) → ``None`` →
the motor layer uses its global/baked config exactly as before.

The bound value carries the agent's stored permission overrides (a narrowing of
the org ceiling), fetched once at bind time so dispatch never hits the database.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# {"agent_id": str, "permissions": dict} or None.
_active_agent: ContextVar[dict | None] = ContextVar("brain_active_agent", default=None)


def current_agent() -> dict | None:
    """The agent bound for this async context, or None (use global config)."""
    return _active_agent.get()


@contextlib.contextmanager
def bind_agent(agent_id: str | None, permissions: dict | None = None):
    """Bind ``agent_id`` (and its permission overrides) for the duration of the
    block. A falsy agent_id is a no-op so companion turns stay on global config.
    Permissions are fetched once here unless passed in."""
    if not agent_id:
        yield None
        return
    if permissions is None:
        try:
            from brain import agents

            permissions = agents.permissions(agent_id)
        except Exception as e:
            logger.debug("[agent_ctx] permission fetch failed for %s: %s", agent_id, e)
            permissions = {}
    token = _active_agent.set({"agent_id": agent_id, "permissions": permissions or {}})
    try:
        yield agent_id
    finally:
        _active_agent.reset(token)
