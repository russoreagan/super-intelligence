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
# (pending_action, end_user_id, mandate_id, approve) -> (text, affect)
ConfirmRunner = Callable[[dict, str, "str | None", bool], Awaitable[tuple[str, dict]]]
# (end_user_id) -> deletion summary
PurgeRunner = Callable[[str], Awaitable[dict]]


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
    confirm_runner: ConfirmRunner | None = None,
    purge_runner: PurgeRunner | None = None,
    resolver: "Callable[[str | None], dict | None] | None" = None,
) -> APIRouter:
    registry = registry or ApiSessionRegistry()
    router = APIRouter(prefix="/v1")
    if resolver is None:
        from brain.api.auth import resolve_partner

        resolver = resolve_partner

    def _require(authorization: str | None) -> dict:
        """Gate on the key, return the caller's partner context. A fake bool ``auth``
        (tests) that passes while the real resolver finds nothing → org owner."""
        if not auth(authorization):
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return resolver(authorization) or {"partner_id": None, "owner": True}

    def _owns(ctx: dict, s) -> bool:
        # The org owner sees everything; a partner only its own sessions. Legacy
        # sessions with no partner_id are owner-scoped.
        return bool(ctx.get("owner")) or s.partner_id is None or s.partner_id == ctx.get("partner_id")

    @router.post("/sessions")
    async def create_session(body: dict, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
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
        s = registry.create(end_user_id.strip(), agent_id, mandate_id, partner_id=ctx.get("partner_id"))
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
        ctx = _require(authorization)
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        message = (body or {}).get("message")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message (non-empty string) is required")
        text, affect = await turn_runner(message, s.end_user_id, s.mandate_id)
        resp = {
            "session_id": session_id,
            "end_user_id": s.end_user_id,
            "response": text,
            "mood": _mood_from_affect(affect),
        }
        # A cloud write awaiting sign-off — park it on the session and tell the
        # partner. They approve via POST /sessions/{id}/confirm. (Auto-confirmed
        # agents never reach here; the write already ran.)
        pending = affect.get("pending") if isinstance(affect, dict) else None
        if pending:
            s.pending = pending
            registry.update(s)
            resp["confirmation"] = {
                "required": True,
                "description": pending.get("description") or pending.get("task"),
            }
        return resp

    @router.post("/sessions/{session_id}/confirm")
    async def confirm_action(
        session_id: str, body: dict | None = None, authorization: str | None = Header(default=None)
    ):
        ctx = _require(authorization)
        if confirm_runner is None:
            raise HTTPException(status_code=501, detail="confirmation is not available on this server")
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        if not s.pending:
            raise HTTPException(status_code=409, detail="no action awaiting confirmation")
        approve = bool((body or {}).get("approve", True))
        pending = s.pending
        text, affect = await confirm_runner(pending, s.end_user_id, s.mandate_id, approve)
        s.pending = None
        registry.update(s)
        return {
            "session_id": session_id,
            "end_user_id": s.end_user_id,
            "approved": approve,
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

    # ── Agents (the persona×role pairing as a first-class resource) ────────────
    # The `/personas/{persona}/mandates` routes above are the low-level assignment
    # primitive; these speak "agent": list by derived agent_id, and set the name +
    # per-agent permission narrowing a partner syncs from their own app. Same
    # bearer auth; agents are org-level data so management spans all personas (the
    # process's own persona only matters for runtime resolve()).

    @router.get("/agents")
    async def list_agents_route(authorization: str | None = Header(default=None)):
        _require(authorization)
        _guard()
        from brain import agents as _ag
        from brain.settings import settings as _s

        agents = _run(lambda: _ag.list_agents())
        ceilings = {k: _s.get(k) for k in _ag.PERMISSION_KEYS}
        return {"agents": agents, "ceilings": ceilings}

    @router.get("/agents/{agent_id}")
    async def get_agent_route(agent_id: str, authorization: str | None = Header(default=None)):
        _require(authorization)
        _guard()
        from brain import agents as _ag

        row = _run(lambda: _ag.get(agent_id))
        if not row:
            raise HTTPException(status_code=404, detail="unknown agent")
        return row

    @router.put("/agents/{agent_id}")
    async def upsert_agent_route(
        agent_id: str, body: dict | None = None, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        _guard()
        from brain import agents as _ag
        from brain import mandates

        body = body or {}

        def _do():
            persona, _, mid = str(agent_id).partition(".")
            mandates.assign(persona, mid)  # create-or-enable the pairing (idempotent)
            if "name" in body:
                _ag.set_name(agent_id, body.get("name"))
            if "permissions" in body:
                _ag.set_permissions(agent_id, body.get("permissions") or {})
            return _ag.get(agent_id)

        return _run(_do)

    @router.delete("/agents/{agent_id}")
    async def delete_agent_route(agent_id: str, authorization: str | None = Header(default=None)):
        _require(authorization)
        _guard()
        from brain import mandates

        def _do():
            persona, _, mid = str(agent_id).partition(".")
            return mandates.unassign(persona, mid)

        ok = _run(_do)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown agent")
        return {"ok": True, "agent_id": agent_id}

    # ── End-user lifecycle ────────────────────────────────────────────────────
    # Erase one customer's footprint across every per-end-user table + the
    # process's in-memory caches (ops / GDPR right-to-erasure).
    @router.delete("/end_users/{end_user_id}")
    async def purge_end_user(end_user_id: str, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        if not ctx.get("owner"):
            raise HTTPException(status_code=403, detail="owner key required")
        if purge_runner is None:
            raise HTTPException(status_code=501, detail="end-user purge is not available on this server")
        if not end_user_id.strip():
            raise HTTPException(status_code=400, detail="end_user_id required")
        # Drop any cached sessions for this end_user so a later turn can't run as a
        # half-erased customer.
        registry.forget_end_user(end_user_id)
        return await purge_runner(end_user_id)

    # ── Per-partner key management (owner-only) ───────────────────────────────
    def _require_owner(authorization: str | None) -> dict:
        ctx = _require(authorization)
        if not ctx.get("owner"):
            raise HTTPException(status_code=403, detail="owner key required")
        return ctx

    @router.get("/partner_keys")
    async def list_partner_keys_route(authorization: str | None = Header(default=None)):
        _require_owner(authorization)
        _guard()
        from brain.api import auth as _a

        return {"keys": _a.list_partner_keys()}

    @router.post("/partner_keys")
    async def mint_partner_key_route(
        body: dict, authorization: str | None = Header(default=None)
    ):
        _require_owner(authorization)
        _guard()
        from brain.api import auth as _a

        partner_id = (body or {}).get("partner_id")
        if not isinstance(partner_id, str) or not partner_id.strip():
            raise HTTPException(status_code=400, detail="partner_id (non-empty string) is required")
        try:
            return _a.mint_partner_key(partner_id.strip(), (body or {}).get("label"))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/partner_keys/{key_id}")
    async def revoke_partner_key_route(key_id: str, authorization: str | None = Header(default=None)):
        _require_owner(authorization)
        _guard()
        from brain.api import auth as _a

        if not _a.revoke_partner_key(key_id):
            raise HTTPException(status_code=404, detail="unknown key id")
        return {"ok": True, "id": key_id, "active": False}

    return router


class ApiServer:
    """Standalone uvicorn server exposing the engine API on its own port. Off
    unless started; the brain only starts it when an API key is configured."""

    def __init__(
        self,
        turn_runner: TurnRunner,
        *,
        registry: ApiSessionRegistry | None = None,
        confirm_runner: ConfirmRunner | None = None,
        purge_runner: PurgeRunner | None = None,
    ) -> None:
        self._registry = registry or ApiSessionRegistry()
        self._app = FastAPI(docs_url="/v1/docs", redoc_url=None)
        self._app.include_router(
            build_api_router(
                turn_runner, self._registry,
                confirm_runner=confirm_runner, purge_runner=purge_runner,
            )
        )

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
