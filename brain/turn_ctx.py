"""
Routing identity for the current turn's emitted events.

Mirrors brain/agent_ctx.py (a contextvar bound for the duration of a turn), but
where agent_ctx carries *permission* scope, this carries *event-routing* scope:
which lane every event a turn emits belongs to. The ActivationEmitter is a
process-global singleton — one UI feed queue plus N engine-API stream taps all
drain the same events. Without a lane, a partner turn's events (its prompt, its
"trading layer" tables/charts, its inner life) leak into the owner's main feed
and into other partners' streams.

Binding is ambient: set once at a turn's entry point, the emitter reads it deep
inside the turn without threading an argument through every emit call site
(exactly how store.bind_persona threads the persona). Contextvars are per-async
task and ``asyncio.create_task`` copies the current context, so the identity
follows the turn coroutine and any task spawned to run it — even when turns run
concurrently.

Nothing bound → the owner lane (the interactive UI conversation + the brain's
own idle inner life). Engine-API turns bind the ``agent`` lane with the session
that owns them.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar

# {"channel": "owner"|"agent", "session_id": str, "agent_id": str, "end_user_id": str,
#  "pinned_skills": list[str], "answer_only": bool}
_OWNER: dict = {
    "channel": "owner",
    "session_id": "",
    "agent_id": "",
    "end_user_id": "",
    "pinned_skills": [],
    "answer_only": False,
}

_current: ContextVar[dict] = ContextVar("brain_turn_ctx", default=_OWNER)


def current_turn() -> dict:
    """The routing identity bound for this async context. Defaults to the owner
    lane (interactive UI + idle inner life) when no turn is bound."""
    return _current.get()


@contextlib.contextmanager
def bind_turn(
    channel: str,
    session_id: str = "",
    agent_id: str | None = None,
    end_user_id: str = "",
    pinned_skills: list[str] | None = None,
    answer_only: bool = False,
):
    """Bind the routing lane for the duration of a turn. ``channel`` is "agent"
    for engine-API turns (the partner-/agent-driven path) or "owner" for the
    interactive UI. The owner path can leave this unbound; only the agent path
    must bind so its events are tagged and filtered out of the main feed.

    ``pinned_skills`` are app-provided skill ids the session forces into every turn's
    bundle (read by frontal._apply_pinned_skills), on top of relevance selection.

    ``answer_only`` marks the turn as synchronous Q&A: the caller wants a spoken
    answer and NOTHING else — no motor dispatch (and therefore no muscle-memory
    open-loop execution) and no FollowThrough task enqueue. Declared by the API
    caller per session/turn; the same enforcement also fires when the session's
    agent carries the ``answer_only`` permission (see session_turn)."""
    token = _current.set(
        {
            "channel": channel or "owner",
            "session_id": session_id or "",
            "agent_id": agent_id or "",
            "end_user_id": end_user_id or "",
            "pinned_skills": list(pinned_skills or []),
            "answer_only": bool(answer_only),
        }
    )
    try:
        yield
    finally:
        _current.reset(token)
