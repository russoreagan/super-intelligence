"""
Engine API router + standalone server.

``build_api_router`` is decoupled from the brain: it takes a ``turn_runner`` async
callable ``(message, end_user_id) -> (text, affect)`` so the routes can be tested
with a fake. ``ApiServer`` wraps the router in its own FastAPI app on its own port
(so it never inherits the UI app's cookie auth or static catch-all).

Endpoints (all bearer-key authed, fail-closed):
  POST /v1/sessions                 {end_user_id, agent_id?} -> {session_id, ...}
  POST /v1/sessions/{id}/turns      {message | audio_input} -> {response, affect, mood, transcript?}
  POST /v1/sessions/{id}/turns/stream  {message | audio_input, audio?} -> SSE inner-life + done [+ audio]
  POST /v1/tts                      {text, ...}      -> {format, data, segments, ...}
  POST /v1/stt                      {audio, ...}     -> {transcript, words, segments}

The turn response surfaces the persona's mood + a structured ``affect`` block
(clean display text, per-segment mood/tag) — the differentiator a raw LLM API
can't offer, and the handle a partner needs to drive their own TTS while ours
(POST /v1/tts) stays strictly better via the affect→voice mapping. Audio is
optional and self-gates on provider keys; see brain/api/audio.py.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Header, HTTPException, WebSocket

from brain.api.auth import check_bearer
from brain.api.sessions import ApiSessionRegistry
from brain.turn_ctx import bind_turn

logger = logging.getLogger(__name__)

TurnRunner = Callable[[str, str, "str | None", "str | None"], Awaitable[tuple[str, dict]]]


def _session_persona(s) -> "str | None":
    """The persona a session's agent_id names (multi-persona Path B), e.g.
    'the_visionary.trading_bull' → 'the_visionary'. None when the session has no
    agent or an unscoped id → the process persona, unchanged."""
    aid = getattr(s, "agent_id", None)
    if not aid or "." not in aid:
        return None
    return aid.split(".", 1)[0]


def _log_agent_turn(s, prompt: str, response: str, turn_id: str = "") -> None:
    """Record a completed agent turn to the durable activity log, off the hot path
    (fire-and-forget; best-effort). Surfaces in the owner's Agents view, never the
    main chat feed."""
    import asyncio

    from brain import agent_log

    with contextlib.suppress(Exception):
        asyncio.create_task(
            asyncio.to_thread(
                agent_log.record,
                agent_id=getattr(s, "agent_id", None),
                end_user_id=getattr(s, "end_user_id", None),
                session_id=getattr(s, "session_id", None),
                turn_id=turn_id,
                persona=_session_persona(s),
                prompt=prompt,
                response=response,
            )
        )
# (reason) -> consolidation status dict. Runs the session-end Hebbian/sleep pass on
# demand (checkpoint; does not tear down). Used by the debate consolidation barrier
# and by long-running agents to persist learning before a crash can lose it.
ConsolidateRunner = Callable[[str], Awaitable[dict]]
# (pending_action, end_user_id, mandate_id, approve) -> (text, affect)
ConfirmRunner = Callable[[dict, str, "str | None", bool], Awaitable[tuple[str, dict]]]
# (end_user_id) -> deletion summary
PurgeRunner = Callable[[str], Awaitable[dict]]
# (text, **opts) -> audio result dict ; (audio_bytes, **opts) -> transcript dict
TtsRunner = Callable[..., Awaitable[dict]]
SttRunner = Callable[..., Awaitable[dict]]
# (text, **opts) -> async iterator of ("meta"|"chunk"|"end", payload) tuples
TtsStreamRunner = Callable[..., object]
# () -> DeepgramLiveSession (factory so each WS connection gets a fresh instance)
SttLiveRunner = Callable[[], object]


# Event types forwarded over the SSE stream (the brain's per-turn inner life).
# audio_meta/audio_chunk/audio_end are reserved for the streamed-audio path
# (emitted only when a turn requests audio); listing them here keeps the
# transport-neutral event vocabulary in one place.
_STREAMED_TYPES = frozenset(
    {
        "turn_start",
        "activation",
        "stream_thought",
        # Chemistry (neuromod/hormonal) is deliberately NOT streamed to partners — only
        # the mood OUTPUT (emotion) crosses the API, keeping the affect model opaque.
        "emotion",
        "user_emotion",
        "turn_end",
        "audio_meta",
        "audio_chunk",
        "audio_end",
    }
)


def _sse(name: str, obj: dict) -> str:
    import json

    return f"event: {name}\ndata: {json.dumps(obj)}\n\n"


async def _stream_audio(
    tts_stream_runner, text, affect, audio_opt, turn_id, *, partner_id=None, quota=None
):
    """Yield SSE audio frames (audio_meta / audio_chunk* / audio_end) for a turn
    that requested audio. Self-contained so a synth failure degrades to an
    audio_error frame without aborting the already-sent text. Every frame carries
    ``turn_id`` (reserved for a future realtime transport that multiplexes turns
    over one socket).

    Metered against the partner's TTS-char quota like POST /v1/tts: refuse up
    front when already over, record the actual characters synthesised after."""
    if tts_stream_runner is None:
        yield _sse(
            "audio_error",
            {
                "type": "audio_error",
                "turn_id": turn_id,
                "detail": "audio is not available on this server",
            },
        )
        return
    from brain.api.audio_quota import TTS_CHARS

    if quota is not None and partner_id:
        reason = quota.check(partner_id, TTS_CHARS)
        if reason:
            yield _sse("audio_error", {"type": "audio_error", "turn_id": turn_id, "detail": reason})
            return
    _names = {"meta": "audio_meta", "chunk": "audio_chunk", "end": "audio_end"}
    chars = 0
    try:
        from brain.api.audio import AudioError

        async for kind, payload in tts_stream_runner(
            text,
            affect=affect,
            voice_id=audio_opt.get("voice_id"),
            model=audio_opt.get("model"),
            fmt=audio_opt.get("format"),
            provider=audio_opt.get("provider"),
        ):
            if kind == "end":
                chars = payload.get("chars") or 0
            name = _names.get(kind)
            if name:
                yield _sse(name, {"type": name, "turn_id": turn_id, **payload})
    except AudioError as ae:
        yield _sse("audio_error", {"type": "audio_error", "turn_id": turn_id, "detail": ae.detail})
    except Exception as ae:  # noqa: BLE001 — audio is best-effort; text already sent
        logger.warning("audio stream failed: %s", ae, exc_info=True)
        yield _sse("audio_error", {"type": "audio_error", "turn_id": turn_id, "detail": str(ae)})
    else:
        if quota is not None and partner_id and chars:
            quota.record(partner_id, TTS_CHARS, chars)


# Curated public affect/mood views live in brain.api._affect — one definition shared
# with the WS transport so the chemistry-not-exposed contract can't drift between them.
from brain.api._affect import affect_view as _affect_view  # noqa: E402
from brain.api._affect import mood_from_affect as _mood_from_affect  # noqa: E402


def build_api_router(
    turn_runner: TurnRunner,
    registry: ApiSessionRegistry | None = None,
    *,
    auth: Callable[[str | None], bool] = check_bearer,
    consolidate_runner: ConsolidateRunner | None = None,
    confirm_runner: ConfirmRunner | None = None,
    approvals_list_runner: Callable[[str, bool], list] | None = None,
    approval_resolve_runner: Callable[[str, str, bool, bool], dict] | None = None,
    purge_runner: PurgeRunner | None = None,
    tts_runner: TtsRunner | None = None,
    stt_runner: SttRunner | None = None,
    tts_stream_runner: TtsStreamRunner | None = None,
    stt_live_runner: SttLiveRunner | None = None,
    audio_quota=None,
    resolver: Callable[[str | None], dict | None] | None = None,
    event_source=None,
    skill_screener: Callable[[str, str, str], Awaitable[dict]] | None = None,
    skill_rewarm: Callable[[], Awaitable[None]] | None = None,
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
        return (
            bool(ctx.get("owner")) or s.partner_id is None or s.partner_id == ctx.get("partner_id")
        )

    async def _resolve_input(body: dict) -> tuple[str, str | None]:
        """Resolve a turn's text input. Returns ``(message, transcript)`` —
        ``transcript`` is non-None (and echoed in the response) when the caller
        sent ``audio_input`` instead of ``message``. The two are mutually
        exclusive; audio_input is transcribed via the STT runner (the same
        commodity path as POST /v1/stt), keeping voice-in a distinct channel."""
        body = body or {}
        message = body.get("message")
        audio_input = body.get("audio_input")
        if audio_input is not None and message is not None:
            raise HTTPException(
                status_code=400, detail="provide either message or audio_input, not both"
            )
        if audio_input is None:
            if not isinstance(message, str) or not message.strip():
                raise HTTPException(
                    status_code=400, detail="message (non-empty string) is required"
                )
            return message, None
        # ── voice-in: transcribe, then run the turn on the transcript ──
        if stt_runner is None:
            raise HTTPException(
                status_code=501, detail="speech-to-text is not available on this server"
            )
        if not isinstance(audio_input, dict):
            raise HTTPException(status_code=400, detail="audio_input must be an object")
        data_b64 = audio_input.get("data")
        if not isinstance(data_b64, str) or not data_b64.strip():
            raise HTTPException(
                status_code=400, detail="audio_input.data (base64 string) is required"
            )
        import base64
        import binascii

        from brain.api.audio import AudioError

        try:
            audio = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(
                status_code=400, detail="audio_input.data must be valid base64"
            ) from e
        try:
            result = await stt_runner(
                audio,
                mimetype=audio_input.get("mimetype") or "audio/wav",
                diarize=False,
                model=audio_input.get("model"),
            )
        except AudioError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e
        transcript = ((result or {}).get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=422, detail="no speech detected in audio_input")
        return transcript, transcript

    def _enforce_quota(ctx: dict, meter: str) -> None:
        """Refuse (429) when the partner is already at/over the meter's window cap.
        Owner keys and an unconfigured quota are never metered."""
        if audio_quota is None or ctx.get("owner"):
            return
        reason = audio_quota.check(ctx.get("partner_id"), meter)
        if reason:
            raise HTTPException(status_code=429, detail=reason)

    def _record_quota(ctx: dict, meter: str, amount) -> None:
        """Log actual usage after a successful call (best-effort; never raises)."""
        if audio_quota is None or ctx.get("owner") or not amount:
            return
        with contextlib.suppress(TypeError, ValueError):
            audio_quota.record(ctx.get("partner_id"), meter, float(amount))

    @router.post("/sessions")
    async def create_session(body: dict, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        end_user_id = (body or {}).get("end_user_id")
        if not isinstance(end_user_id, str) or not end_user_id.strip():
            raise HTTPException(
                status_code=400, detail="end_user_id (non-empty string) is required"
            )
        mandate_id = (body or {}).get("mandate_id")
        if mandate_id is not None and not isinstance(mandate_id, str):
            raise HTTPException(status_code=400, detail="mandate_id must be a string")
        agent_id = (body or {}).get("agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise HTTPException(status_code=400, detail="agent_id must be a string")
        # Optional pin: app-provided skill ids forced into every turn of this session.
        # Unknown/!enabled ids are silently ignored at turn time (a pin can't conjure an
        # unscreened skill), so accept any list of strings here.
        pinned_skills = (body or {}).get("skills")
        if pinned_skills is not None and (
            not isinstance(pinned_skills, list)
            or any(not isinstance(x, str) for x in pinned_skills)
        ):
            raise HTTPException(status_code=400, detail="skills must be a list of strings")
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
        s = registry.create(
            end_user_id.strip(),
            agent_id,
            mandate_id,
            partner_id=ctx.get("partner_id"),
            pinned_skills=pinned_skills,
        )
        return {
            "session_id": s.session_id,
            "end_user_id": s.end_user_id,
            "agent_id": s.agent_id,
            "mandate_id": s.mandate_id,
            "skills": s.pinned_skills,
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
        message, transcript = await _resolve_input(body)
        # Tag every event this turn emits with the agent lane so it never lands in
        # the owner's main feed (and can't bleed into another partner's stream).
        with bind_turn(
            "agent",
            session_id=s.session_id,
            agent_id=s.agent_id,
            end_user_id=s.end_user_id,
            pinned_skills=s.pinned_skills,
        ):
            text, affect = await turn_runner(
                message, s.end_user_id, s.mandate_id, _session_persona(s)
            )
        # The turn returns TTS-ready text (still carrying [mood:X] markup + bare
        # reaction tags). Hand partners clean display text + the structured affect
        # that drives prosody — never the raw markup as the response.
        display, affect_block = _affect_view(text, affect)
        resp = {
            "session_id": session_id,
            "end_user_id": s.end_user_id,
            "response": display,
            "affect": affect_block,
            "mood": _mood_from_affect(affect),
        }
        if transcript is not None:
            resp["transcript"] = transcript  # echo what we heard (voice-in)
        _log_agent_turn(s, message, display)
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

    @router.post("/sessions/{session_id}/consolidate")
    async def consolidate_session(
        session_id: str, body: dict | None = None, authorization: str | None = Header(default=None)
    ):
        """Run the session-end Hebbian/sleep consolidation now and persist learning,
        without tearing the brain down. A checkpoint: idempotent and single-flight.
        The orchestrator calls this for every participant at debate end; long-running
        agents call it to durably commit learning between sessions."""
        ctx = _require(authorization)
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        if consolidate_runner is None:
            raise HTTPException(status_code=501, detail="consolidation is not available on this server")
        reason = (body or {}).get("reason") or "api"
        result = await consolidate_runner(str(reason))
        return {"session_id": session_id, "consolidation": result}

    @router.post("/sessions/{session_id}/turns/stream")
    async def run_turn_stream(
        session_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        ctx = _require(authorization)
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        message, transcript = await _resolve_input(body)
        audio_opt = (body or {}).get("audio")
        if audio_opt is not None and not isinstance(audio_opt, dict):
            raise HTTPException(status_code=400, detail="audio must be an object")

        source = event_source
        if source is None:
            try:
                from brain.ui.emitter import emitter as source  # process singleton
            except Exception:
                source = None
        if source is None:
            raise HTTPException(
                status_code=501, detail="event streaming is not available on this server"
            )

        import asyncio

        from fastapi.responses import StreamingResponse

        async def _gen():
            tap: asyncio.Queue = asyncio.Queue(maxsize=512)
            source.add_tap(tap)
            # Tap is live BEFORE the turn starts so turn_start isn't missed.
            # bind_turn is active across create_task so the copied context tags the
            # turn task's events with this session's lane (route_sid == session_id).
            with bind_turn(
                "agent",
                session_id=session_id,
                agent_id=s.agent_id,
                end_user_id=s.end_user_id,
                pinned_skills=s.pinned_skills,
            ):
                turn_task = asyncio.create_task(
                    turn_runner(message, s.end_user_id, s.mandate_id, _session_persona(s))
                )
            try:
                _open = {"session_id": session_id, "end_user_id": s.end_user_id}
                if transcript is not None:
                    _open["transcript"] = transcript  # echo what we heard (voice-in)
                yield _sse("open", _open)
                saw_end = False
                turn_id = None
                while not saw_end:
                    try:
                        ev = await asyncio.wait_for(tap.get(), timeout=0.5)
                    except TimeoutError:
                        if turn_task.done():
                            break
                        yield ": keep-alive\n\n"
                        continue
                    etype = ev.get("type")
                    if etype in ("turn_start", "turn_end"):
                        turn_id = ev.get("turn_id", turn_id)
                    # Only this session's events — never another partner's, and
                    # never the owner's idle inner life (which has no route_sid).
                    if ev.get("route_sid") != session_id:
                        continue
                    if etype in _STREAMED_TYPES:
                        yield _sse(etype, ev)
                    if etype == "turn_end":
                        saw_end = True
                # The turn's authoritative result (curated mood + any pending write).
                text, affect = await turn_task
                display, affect_block = _affect_view(text, affect)
                final = {
                    "response": display,
                    "affect": affect_block,
                    "mood": _mood_from_affect(affect),
                }
                pending = affect.get("pending") if isinstance(affect, dict) else None
                if pending:
                    s.pending = pending
                    registry.update(s)
                    final["confirmation"] = {
                        "required": True,
                        "description": pending.get("description") or pending.get("task"),
                    }
                # Text first (done) so the client renders without waiting on audio;
                # audio chunks follow and stream in as each segment synthesises. An
                # audio client keeps reading until audio_end. TTS uses the RAW text
                # (markup intact) so mood spans drive per-chunk prosody.
                yield _sse("done", final)
                _log_agent_turn(s, message, display, turn_id or "")
                if isinstance(audio_opt, dict) and audio_opt.get("enabled"):
                    _pid = None if ctx.get("owner") else ctx.get("partner_id")
                    # Default the voice to the session persona's configured voice
                    # (persona_voice_<slug>) when the caller didn't pin one — the
                    # engine owns the persona→voice mapping, so an agent session
                    # speaks in its persona's voice instead of the provider default.
                    _audio = audio_opt
                    if not _audio.get("voice_id"):
                        from brain.persona_chem import voice_id_for

                        _pv = voice_id_for(_session_persona(s))
                        if _pv:
                            _audio = {**_audio, "voice_id": _pv}
                    async for frame in _stream_audio(
                        tts_stream_runner,
                        text,
                        affect,
                        _audio,
                        turn_id,
                        partner_id=_pid,
                        quota=audio_quota,
                    ):
                        yield frame
            except Exception as e:  # noqa: BLE001 — surface as a stream error frame
                yield _sse("error", {"detail": str(e)})
            finally:
                source.remove_tap(tap)
                if not turn_task.done():
                    turn_task.cancel()

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @router.websocket("/sessions/{session_id}/stream")
    async def ws_turn_stream(session_id: str, websocket: WebSocket):
        """Realtime WebSocket — persistent duplex connection for a session.

        Supports streaming audio in (PCM16 → Deepgram live STT), inner-life
        event forwarding, and TTS chunk streaming back. See brain/api/ws.py and
        the message-protocol reference for the full frame vocabulary.

        Auth is checked via the ``Authorization`` header of the upgrade request
        BEFORE accept(); unknown or unauthorised connections are closed 1008."""
        authorization = websocket.headers.get("authorization")
        try:
            ctx = _require(authorization)
        except HTTPException:
            await websocket.close(code=1008)
            return

        s = registry.get(session_id)
        if s is None or not _owns(ctx, s):
            await websocket.close(code=1008)
            return

        source = event_source
        if source is None:
            with contextlib.suppress(Exception):
                from brain.ui.emitter import emitter as _em  # noqa: F841

                source = _em  # type: ignore[assignment]

        from brain.api.ws import WsSession

        await WsSession(
            websocket,
            s,
            ctx,
            turn_runner=turn_runner,
            registry=registry,
            tts_stream_runner=tts_stream_runner,
            audio_quota=audio_quota,
            event_source=source,
            stt_live_factory=stt_live_runner,
        ).run()

    @router.post("/sessions/{session_id}/confirm")
    async def confirm_action(
        session_id: str, body: dict | None = None, authorization: str | None = Header(default=None)
    ):
        ctx = _require(authorization)
        if confirm_runner is None:
            raise HTTPException(
                status_code=501, detail="confirmation is not available on this server"
            )
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

    @router.get("/sessions/{session_id}/approvals")
    async def list_approvals(session_id: str, authorization: str | None = Header(default=None)):
        """Pending tool-action approvals for this session's end-user — the sensitive
        actions the brain skipped and is waiting on a yes/no for."""
        ctx = _require(authorization)
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        if approvals_list_runner is None:
            return {"session_id": session_id, "approvals": []}
        # Owner-key callers (this deployment's single-tenant apps) also see the
        # autonomous/owner lane — actions the brain queued while unattended — so
        # "approve from when I was away" works from a tenant app, not just the owner UI.
        return {
            "session_id": session_id,
            "end_user_id": s.end_user_id,
            "approvals": approvals_list_runner(s.end_user_id, bool(ctx.get("owner"))) or [],
        }

    @router.post("/sessions/{session_id}/approvals/{approval_id}/resolve")
    async def resolve_approval(
        session_id: str,
        approval_id: str,
        body: dict | None = None,
        authorization: str | None = Header(default=None),
    ):
        """Approve (run it) or skip a pending action, on behalf of this end-user."""
        ctx = _require(authorization)
        if approval_resolve_runner is None:
            raise HTTPException(status_code=501, detail="approvals are not available on this server")
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        approve = bool((body or {}).get("approve", True))
        # Owner-key callers may also resolve autonomous/owner-lane items (see GET above).
        res = approval_resolve_runner(approval_id, s.end_user_id, approve, bool(ctx.get("owner"))) or {}
        return {"session_id": session_id, "end_user_id": s.end_user_id, "approved": approve, **res}

    # ── Audio (optional, partner-gated) ───────────────────────────────────────
    # Stateless: no session needed. TTS exposes the affect→voice mapping (the
    # differentiated half — a partner can't replicate mood-driven prosody client
    # side); STT is the commodity convenience path. Both 501 when no runner is
    # wired and 503 (via AudioError) when the provider key isn't configured.
    @router.post("/tts")
    async def tts_route(body: dict, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        if tts_runner is None:
            raise HTTPException(
                status_code=501, detail="text-to-speech is not available on this server"
            )
        from brain.api.audio import AudioError
        from brain.api.audio_quota import TTS_CHARS

        body = body or {}
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text (non-empty string) is required")
        affect = body.get("affect")
        if affect is not None and not isinstance(affect, dict):
            raise HTTPException(status_code=400, detail="affect must be an object")
        _enforce_quota(ctx, TTS_CHARS)
        try:
            result = await tts_runner(
                text,
                affect=affect,
                voice_id=body.get("voice_id"),
                model=body.get("model"),
                fmt=body.get("format"),
                provider=body.get("provider"),
            )
        except AudioError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e
        _record_quota(ctx, TTS_CHARS, (result or {}).get("chars"))
        return result

    @router.post("/stt")
    async def stt_route(body: dict, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        if stt_runner is None:
            raise HTTPException(
                status_code=501, detail="speech-to-text is not available on this server"
            )
        import base64
        import binascii

        from brain.api.audio import AudioError
        from brain.api.audio_quota import STT_SECONDS

        body = body or {}
        audio_b64 = body.get("audio")
        if not isinstance(audio_b64, str) or not audio_b64.strip():
            raise HTTPException(status_code=400, detail="audio (base64 string) is required")
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(status_code=400, detail="audio must be valid base64") from e
        _enforce_quota(ctx, STT_SECONDS)
        try:
            result = await stt_runner(
                audio,
                mimetype=body.get("mimetype") or "audio/wav",
                diarize=bool(body.get("diarize", False)),
                model=body.get("model"),
            )
        except AudioError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e
        _record_quota(ctx, STT_SECONDS, (result or {}).get("duration_s"))
        return result

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
            raise HTTPException(
                status_code=503, detail="mandates require the Supabase storage backend"
            )

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
            if "tier" in body:
                _ag.set_tier(agent_id, str(body.get("tier")))
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

    # ── App-provided skills (the partner's skill library + admission review) ────
    # A skill is partner-supplied content injected into the agent's prompt, so every
    # submission is SCREENED before it can go live (brain/skills_screener.py): obviously
    # safe → auto-enabled; anything in question → flagged for the superadmin. Submit/
    # list/delete use the same bearer as sessions, with a non-owner partner scoped to
    # its own submissions. Approve/reject is OWNER-only — the platform superadmin acts
    # with the owner credential and the control plane fans the queue across orgs.

    def _run_skill(fn):
        from brain.skills_registry import SkillError

        try:
            return fn()
        except SkillError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _skill_owned(ctx: dict, row: dict | None) -> bool:
        # New id (no row) → anyone authenticated may create it. Existing → owner, or the
        # partner that submitted it.
        if row is None:
            return True
        return bool(ctx.get("owner")) or row.get("submitted_by") == ctx.get("partner_id")

    def _require_owner(authorization: str | None) -> dict:
        ctx = _require(authorization)
        if not ctx.get("owner"):
            raise HTTPException(status_code=403, detail="owner credential required")
        return ctx

    @router.get("/skills")
    async def list_skills_route(
        include_inactive: bool = False,
        status: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        ctx = _require(authorization)
        _guard()
        from brain import skills_registry as sr

        rows = _run_skill(lambda: sr.list_skills(include_inactive, status))
        if not ctx.get("owner"):
            pid = ctx.get("partner_id")
            rows = [r for r in rows if r.get("submitted_by") == pid]
        return {"skills": rows}

    @router.get("/skills/{skill_id}")
    async def get_skill_route(skill_id: str, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        _guard()
        from brain import skills_registry as sr

        row = _run_skill(lambda: sr.get_skill(skill_id))
        if row is None:
            raise HTTPException(status_code=404, detail="unknown skill id")
        if not _skill_owned(ctx, row):
            raise HTTPException(status_code=403, detail="skill belongs to another partner")
        return row

    @router.put("/skills/{skill_id}")
    async def upsert_skill_route(
        skill_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        ctx = _require(authorization)
        _guard()
        from brain import skills_registry as sr

        body = body or {}
        skill_body = body.get("body")
        if not isinstance(skill_body, str):
            raise HTTPException(status_code=400, detail="body (string) is required")
        description = body.get("description") or ""
        if not isinstance(description, str):
            raise HTTPException(status_code=400, detail="description must be a string")
        existing = _run_skill(lambda: sr.get_skill(skill_id))
        if not _skill_owned(ctx, existing):
            raise HTTPException(status_code=403, detail="skill belongs to another partner")
        submitted_by = None if ctx.get("owner") else ctx.get("partner_id")
        staged = _run_skill(
            lambda: sr.stage_skill(
                skill_id,
                skill_body,
                description,
                display_name=body.get("display_name"),
                keywords=body.get("keywords"),
                allowed_tools=body.get("allowed_tools"),
                tier=int(body.get("tier", 2) or 2),
                submitted_by=submitted_by,
            )
        )
        # Run the admission screener, record the verdict, and re-warm if it went live.
        if skill_screener is not None:
            verdict = await skill_screener(skill_id, skill_body, description)
            result = _run_skill(
                lambda: sr.set_status(
                    skill_id, verdict.get("status", "flagged"), screen_notes=verdict.get("notes")
                )
            )
        else:
            # No screener wired → fail safe to human review (never auto-enable).
            result = _run_skill(
                lambda: sr.set_status(
                    skill_id,
                    "flagged",
                    screen_notes={"judge": {"verdict": None, "reasons": ["screener_not_configured"]}},
                )
            )
        st = result.get("status")
        if st == "enabled" and skill_rewarm is not None:
            await skill_rewarm()
        return {
            "id": skill_id,
            "status": st,
            "version": staged.get("version"),
            "screen_notes": result.get("screen_notes"),
        }

    @router.delete("/skills/{skill_id}")
    async def delete_skill_route(skill_id: str, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        _guard()
        from brain import skills_registry as sr

        existing = _run_skill(lambda: sr.get_skill(skill_id))
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown skill id")
        if not _skill_owned(ctx, existing):
            raise HTTPException(status_code=403, detail="skill belongs to another partner")
        _run_skill(lambda: sr.delete_skill(skill_id))
        if skill_rewarm is not None:
            await skill_rewarm()
        return {"ok": True, "skill_id": skill_id, "active": False}

    @router.get("/admin/skills/flagged")
    async def list_flagged_skills_route(authorization: str | None = Header(default=None)):
        _require_owner(authorization)
        _guard()
        from brain import skills_registry as sr

        return {"skills": _run_skill(lambda: sr.list_flagged())}

    @router.post("/admin/skills/{skill_id}/approve")
    async def approve_skill_route(skill_id: str, authorization: str | None = Header(default=None)):
        _require_owner(authorization)
        _guard()
        from brain import skills_registry as sr

        existing = _run_skill(lambda: sr.get_skill(skill_id))
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown skill id")
        result = _run_skill(lambda: sr.set_status(skill_id, "enabled", reviewed_by="owner"))
        if skill_rewarm is not None:
            await skill_rewarm()
        return {"id": skill_id, "status": result.get("status")}

    @router.post("/admin/skills/{skill_id}/reject")
    async def reject_skill_route(
        skill_id: str, body: dict | None = None, authorization: str | None = Header(default=None)
    ):
        _require_owner(authorization)
        _guard()
        from brain import skills_registry as sr

        existing = _run_skill(lambda: sr.get_skill(skill_id))
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown skill id")
        notes = dict(existing.get("screen_notes") or {})
        notes["review"] = {"action": "rejected", "reason": str((body or {}).get("reason") or "")}
        result = _run_skill(
            lambda: sr.set_status(skill_id, "rejected", screen_notes=notes, reviewed_by="owner")
        )
        return {"id": skill_id, "status": result.get("status")}

    # ── End-user lifecycle ────────────────────────────────────────────────────
    # Erase one customer's footprint across every per-end-user table + the
    # process's in-memory caches (ops / GDPR right-to-erasure).
    @router.delete("/end_users/{end_user_id}")
    async def purge_end_user(end_user_id: str, authorization: str | None = Header(default=None)):
        ctx = _require(authorization)
        if not ctx.get("owner"):
            raise HTTPException(status_code=403, detail="owner key required")
        if purge_runner is None:
            raise HTTPException(
                status_code=501, detail="end-user purge is not available on this server"
            )
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
    async def mint_partner_key_route(body: dict, authorization: str | None = Header(default=None)):
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
    async def revoke_partner_key_route(
        key_id: str, authorization: str | None = Header(default=None)
    ):
        _require_owner(authorization)
        _guard()
        from brain.api import auth as _a

        if not _a.revoke_partner_key(key_id):
            raise HTTPException(status_code=404, detail="unknown key id")
        return {"ok": True, "id": key_id, "active": False}

    # ── Per-end-user MCP tokens ───────────────────────────────────────────────
    # Partners call these after completing an OAuth flow for their end-users so
    # CMAExecutor can create per-user Anthropic Vaults with the right credentials.
    # Tokens are vault-encrypted at rest; GET endpoints return metadata only.

    def _sb_client():
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            raise HTTPException(
                status_code=503, detail="MCP token storage requires the Supabase backend"
            )
        return supabase_client.get_client()

    @router.post("/mcp/tokens")
    async def store_mcp_token(body: dict, authorization: str | None = Header(default=None)):
        _require(authorization)
        body = body or {}
        end_user_id = body.get("end_user_id")
        server_name = body.get("server_name")
        server_url = body.get("server_url")
        access_token = body.get("access_token")
        if not isinstance(end_user_id, str) or not end_user_id.strip():
            raise HTTPException(
                status_code=400, detail="end_user_id (non-empty string) is required"
            )
        if not isinstance(server_name, str) or not server_name.strip():
            raise HTTPException(
                status_code=400, detail="server_name (non-empty string) is required"
            )
        if not isinstance(server_url, str) or not server_url.strip():
            raise HTTPException(status_code=400, detail="server_url (non-empty string) is required")
        if not isinstance(access_token, str) or not access_token.strip():
            raise HTTPException(
                status_code=400, detail="access_token (non-empty string) is required"
            )
        expires_at = body.get("expires_at")
        try:
            _sb_client().rpc(
                "set_end_user_mcp_token",
                {
                    "p_end_user_id": end_user_id.strip(),
                    "p_server_name": server_name.strip(),
                    "p_server_url": server_url.strip(),
                    "p_token": access_token.strip(),
                    "p_expires_at": expires_at,
                },
            ).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to store token: {e}") from e
        return {"ok": True, "end_user_id": end_user_id.strip(), "server_name": server_name.strip()}

    @router.get("/mcp/tokens/{end_user_id}")
    async def list_mcp_tokens(end_user_id: str, authorization: str | None = Header(default=None)):
        _require(authorization)
        try:
            resp = (
                _sb_client()
                .table("end_user_mcp_tokens")
                .select("server_name, server_url, expires_at")
                .eq("end_user_id", end_user_id)
                .execute()
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to list tokens: {e}") from e
        return {"end_user_id": end_user_id, "connections": resp.data or []}

    @router.delete("/mcp/tokens/{end_user_id}/{server_name}")
    async def delete_mcp_token(
        end_user_id: str, server_name: str, authorization: str | None = Header(default=None)
    ):
        _require(authorization)
        try:
            resp = (
                _sb_client()
                .rpc(
                    "delete_end_user_mcp_token",
                    {"p_end_user_id": end_user_id, "p_server_name": server_name},
                )
                .execute()
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to delete token: {e}") from e
        if not resp.data:
            raise HTTPException(status_code=404, detail="token not found")
        return {"ok": True, "end_user_id": end_user_id, "server_name": server_name}

    return router


class ApiServer:
    """Standalone uvicorn server exposing the engine API on its own port. Off
    unless started; the brain only starts it when an API key is configured."""

    def __init__(
        self,
        turn_runner: TurnRunner,
        *,
        registry: ApiSessionRegistry | None = None,
        consolidate_runner: ConsolidateRunner | None = None,
        confirm_runner: ConfirmRunner | None = None,
        approvals_list_runner: Callable[[str, bool], list] | None = None,
        approval_resolve_runner: Callable[[str, str, bool, bool], dict] | None = None,
        purge_runner: PurgeRunner | None = None,
        tts_runner: TtsRunner | None = None,
        stt_runner: SttRunner | None = None,
        tts_stream_runner: TtsStreamRunner | None = None,
        stt_live_runner: SttLiveRunner | None = None,
        audio_quota=None,
        skill_screener: Callable[[str, str, str], Awaitable[dict]] | None = None,
        skill_rewarm: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry or ApiSessionRegistry()
        # Default the audio runners to the stateless synth/transcribe helpers.
        # They self-gate (503) when ELEVENLABS/OPENAI/DEEPGRAM keys aren't set,
        # so wiring them unconditionally is safe — the routes just report 503.
        if tts_runner is None or stt_runner is None or tts_stream_runner is None:
            from brain.api import audio as _audio

            tts_runner = tts_runner or _audio.synthesize
            stt_runner = stt_runner or _audio.transcribe
            tts_stream_runner = tts_stream_runner or _audio.synthesize_stream
        if stt_live_runner is None:
            from brain.api.stt_live import DeepgramLiveSession

            stt_live_runner = DeepgramLiveSession
        if audio_quota is None:
            from brain.api.audio_quota import AudioQuota

            audio_quota = AudioQuota()  # inert until settings caps are set
        self._app = FastAPI(docs_url="/v1/docs", redoc_url=None)
        self._app.include_router(
            build_api_router(
                turn_runner,
                self._registry,
                consolidate_runner=consolidate_runner,
                confirm_runner=confirm_runner,
                approvals_list_runner=approvals_list_runner,
                approval_resolve_runner=approval_resolve_runner,
                purge_runner=purge_runner,
                tts_runner=tts_runner,
                stt_runner=stt_runner,
                tts_stream_runner=tts_stream_runner,
                stt_live_runner=stt_live_runner,
                audio_quota=audio_quota,
                skill_screener=skill_screener,
                skill_rewarm=skill_rewarm,
            )
        )

    @property
    def app(self) -> FastAPI:
        return self._app

    async def start(self, host: str | None = None, port: int | None = None) -> None:
        import uvicorn

        _port = port or int(os.environ.get("BRAIN_API_PORT", "8780"))
        _host = host or ("0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1")  # nosec B104
        config = uvicorn.Config(
            self._app, host=_host, port=_port, log_level="warning", access_log=False
        )
        logger.info("Engine API starting at http://%s:%d/v1", _host, _port)
        await uvicorn.Server(config).serve()
