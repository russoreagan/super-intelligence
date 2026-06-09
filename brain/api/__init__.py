"""
Engine API — the public turn surface that lets another app use this brain as the
"brain" for its agents.

This is the seam that gives the per-(persona, end_user) machinery a live caller:
a partner's backend opens a session bound to one of their customers (end_user_id)
and runs turns against it. Each turn binds that customer's chemistry (via
BrainSession.api_turn → process_turn(end_user_id)) so their mood evolves in
isolation while the persona's learned competence stays shared.

Standalone server (own FastAPI app + port) so it is independent of the UI app's
cookie-auth and static routes. Off by default — only runs when an API key is
configured. Auth is a runtime bearer key; the router itself is decoupled from the
brain via a turn-runner callable so it can be tested without a live session.
"""

from brain.api.server import ApiServer, build_api_router
from brain.api.sessions import ApiSession, ApiSessionRegistry

__all__ = ["ApiServer", "build_api_router", "ApiSession", "ApiSessionRegistry"]
