"""
Engine API router + standalone server.

``build_api_router`` is decoupled from the brain: it takes a ``turn_runner`` async
callable ``(message, end_user_id) -> (text, affect)`` so the routes can be tested
with a fake. ``ApiServer`` wraps the router in its own FastAPI app on its own port
(so it never inherits the UI app's cookie auth or static catch-all).

Endpoints (all bearer-key authed, fail-closed):
  POST /v1/sessions                 {end_user_id, agent_id?} -> {session_id, ...}
  POST /v1/sessions/{id}/turns      {message}               -> {response, mood, ...}

The turn response surfaces the persona's mood — the differentiator a raw LLM API
can't offer. Token streaming (SSE) is a later enhancement: process_turn returns a
finished response, not a token stream, so v1 returns JSON.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Header, HTTPException

from brain.api.auth import check_bearer
from brain.api.sessions import ApiSessionRegistry

logger = logging.getLogger(__name__)

TurnRunner = Callable[[str, str], Awaitable[tuple[str, dict]]]


def _mood_from_affect(affect: dict | None) -> dict:
    """Curate the public mood view from the internal affect dict — emotion + the
    hormonal layer, never internal fields (enrollment, appraisal, etc.)."""
    affect = affect or {}
    mood: dict = {"emotion": affect.get("emotion", "neutral")}
    if affect.get("user_emotion"):
        mood["user_emotion"] = affect["user_emotion"]
    if isinstance(affect.get("hormonal"), dict):
        mood["hormonal"] = affect["hormonal"]
    return mood


def build_api_router(
    turn_runner: TurnRunner,
    registry: ApiSessionRegistry | None = None,
    *,
    auth: Callable[[str | None], bool] = check_bearer,
) -> APIRouter:
    registry = registry or ApiSessionRegistry()
    router = APIRouter(prefix="/v1")

    def _require(authorization: str | None) -> None:
        if not auth(authorization):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    @router.post("/sessions")
    async def create_session(body: dict, authorization: str | None = Header(default=None)):
        _require(authorization)
        end_user_id = (body or {}).get("end_user_id")
        if not isinstance(end_user_id, str) or not end_user_id.strip():
            raise HTTPException(status_code=400, detail="end_user_id (non-empty string) is required")
        s = registry.create(end_user_id.strip(), (body or {}).get("agent_id"))
        return {"session_id": s.session_id, "end_user_id": s.end_user_id, "agent_id": s.agent_id}

    @router.post("/sessions/{session_id}/turns")
    async def run_turn(
        session_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        message = (body or {}).get("message")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message (non-empty string) is required")
        text, affect = await turn_runner(message, s.end_user_id)
        return {
            "session_id": session_id,
            "end_user_id": s.end_user_id,
            "response": text,
            "mood": _mood_from_affect(affect),
        }

    return router


class ApiServer:
    """Standalone uvicorn server exposing the engine API on its own port. Off
    unless started; the brain only starts it when an API key is configured."""

    def __init__(self, turn_runner: TurnRunner, *, registry: ApiSessionRegistry | None = None) -> None:
        self._registry = registry or ApiSessionRegistry()
        self._app = FastAPI(docs_url="/v1/docs", redoc_url=None)
        self._app.include_router(build_api_router(turn_runner, self._registry))

    @property
    def app(self) -> FastAPI:
        return self._app

    async def start(self, host: str | None = None, port: int | None = None) -> None:
        import uvicorn

        _port = port or int(os.environ.get("BRAIN_API_PORT", "8780"))
        _host = host or ("0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1")  # nosec B104
        config = uvicorn.Config(self._app, host=_host, port=_port, log_level="warning", access_log=False)
        logger.info("Engine API starting at http://%s:%d/v1", _host, _port)
        await uvicorn.Server(config).serve()
