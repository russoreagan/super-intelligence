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

TurnRunner = Callable[[str, str, "str | None"], Awaitable[tuple[str, dict]]]


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
        mandate_id = (body or {}).get("mandate_id")
        if mandate_id is not None and not isinstance(mandate_id, str):
            raise HTTPException(status_code=400, detail="mandate_id must be a string")
        agent_id = (body or {}).get("agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise HTTPException(status_code=400, detail="agent_id must be a string")
        # An agent IS a (persona, role) pairing. Resolving agent_id picks the role
        # (mandate) for this process's persona — the single handle a partner passes
        # instead of juggling persona + mandate_id. Cross-persona agents live in a
        # different process (the gateway routes there) → 409.
        if agent_id:
            from brain.agents import AgentNotFound, AgentPersonaMismatch, resolve
            from brain.mandates import MandateError

            try:
                _persona, mandate_id = resolve(agent_id)
            except AgentPersonaMismatch as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            except AgentNotFound as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except MandateError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        s = registry.create(end_user_id.strip(), agent_id, mandate_id)
        return {
            "session_id": s.session_id,
            "end_user_id": s.end_user_id,
            "agent_id": s.agent_id,
            "mandate_id": s.mandate_id,
        }

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
        text, affect = await turn_runner(message, s.end_user_id, s.mandate_id)
        return {
            "session_id": session_id,
            "end_user_id": s.end_user_id,
            "response": text,
            "mood": _mood_from_affect(affect),
        }

    # ── Mandate management (the partner's role library + persona assignments) ──
    # The bearer key is the partner's backend credential: the same caller that can
    # open a session naming any mandate_id already controls which role text applies,
    # so these reuse it rather than inventing a separate admin scope.
    #
    # conduct_rules / reward_weights are accepted and stored (so a partner whose
    # source-of-truth lives in their own app can sync full rows) but the brain does
    # not consume them yet.

    def _guard():
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            raise HTTPException(status_code=503, detail="mandates require the Supabase storage backend")

    def _run(fn):
        from brain.mandates import MandateError

        try:
            return fn()
        except MandateError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/mandates")
    async def list_mandates_route(
        include_inactive: bool = False, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        _guard()
        from brain import mandates

        return {"mandates": _run(lambda: mandates.list_mandates(include_inactive))}

    @router.put("/mandates/{mandate_id}")
    async def upsert_mandate_route(
        mandate_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        _guard()
        from brain import mandates

        body = body or {}
        role_text = body.get("role_text")
        if not isinstance(role_text, str):
            raise HTTPException(status_code=400, detail="role_text (string) is required")
        return _run(
            lambda: mandates.upsert_mandate(
                mandate_id,
                role_text,
                body.get("conduct_rules"),
                body.get("reward_weights"),
            )
        )

    @router.delete("/mandates/{mandate_id}")
    async def deactivate_mandate_route(
        mandate_id: str, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        _guard()
        from brain import mandates

        ok = _run(lambda: mandates.deactivate_mandate(mandate_id))
        if not ok:
            raise HTTPException(status_code=404, detail="unknown mandate id")
        return {"ok": True, "mandate_id": mandate_id, "active": False}

    @router.get("/personas/{persona}/mandates")
    async def list_assignments_route(
        persona: str, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        _guard()
        from brain import mandates

        return {"persona": persona, "assignments": _run(lambda: mandates.list_assignments(persona))}

    @router.put("/personas/{persona}/mandates/{mandate_id}")
    async def assign_route(
        persona: str,
        mandate_id: str,
        body: dict | None = None,
        authorization: str | None = Header(default=None),
    ):
        _require(authorization)
        _guard()
        from brain import mandates

        sort_order = int((body or {}).get("sort_order", 0) or 0)
        return _run(lambda: mandates.assign(persona, mandate_id, sort_order))

    @router.delete("/personas/{persona}/mandates/{mandate_id}")
    async def unassign_route(
        persona: str, mandate_id: str, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        _guard()
        from brain import mandates

        ok = _run(lambda: mandates.unassign(persona, mandate_id))
        if not ok:
            raise HTTPException(status_code=404, detail="no such assignment")
        return {"ok": True, "persona": persona, "mandate_id": mandate_id}

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
