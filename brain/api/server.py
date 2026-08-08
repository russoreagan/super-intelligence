"""
Engine API router + standalone server.

``build_api_router`` is decoupled from the brain: it takes a ``turn_runner`` async
callable ``(message, end_user_id) -> (text, affect)`` so the routes can be tested
with a fake. ``ApiServer`` wraps the router in its own FastAPI app on its own port
(so it never inherits the UI app's cookie auth or static catch-all).

Endpoints (all bearer-key authed, fail-closed):
  POST /v1/sessions                 {end_user_id, agent_id?} -> {session_id, ...}
  POST /v1/sessions/{id}/turns      {message | audio_input} -> {response, affect, mood, turn_id, transcript?}
  POST /v1/sessions/{id}/turns/stream  {message | audio_input, audio?} -> SSE inner-life + done [+ audio]
  POST /v1/sessions/{id}/turns/{turn_id}/grade  {grade, source?} -> {ok, grade, applied_live}
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
import json
import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Header, HTTPException, WebSocket

from brain.api import end_users as _eu
from brain.api import limits as _limits
from brain.api.auth import AuthBackendError, check_bearer
from brain.api.sessions import ApiSessionRegistry
from brain.turn_ctx import bind_turn

logger = logging.getLogger(__name__)

TurnRunner = Callable[[str, str, "str | None", "str | None"], Awaitable[tuple[str, dict]]]


def _session_persona(s) -> str | None:
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
# (input_text, json_schema, instructions, tool_name) -> extracted object. Sessionless
# structured extraction: one forced-tool model call, no persona/memory/motor/DMN.
ExtractRunner = Callable[[str, dict, str, str], Awaitable[dict]]
# (end_user_id) -> deletion summary
PurgeRunner = Callable[[str], Awaitable[dict]]
# (turn_id, grade, end_user_id, persona, source, api_session_id) ->
# {ok, grade, applied_live}. External grade write path: resolves the turn ONLY within
# api_session_id (a turn from any other session is refused — {denied: true}, which the
# route maps to 404) and binds the graded end-user's chemistry so the DA nudge lands on
# that customer's mood (see session_loops.api_grade_turn_engine). Sync — no model call.
GradeRunner = Callable[[str, object, str, str, str, str], dict]
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
    jobs_list_runner: Callable[[int, str | None], list] | None = None,
    job_get_runner: Callable[[str], dict | None] | None = None,
    grade_runner: GradeRunner | None = None,
    learning_runner: Callable[..., dict] | None = None,
    purge_runner: PurgeRunner | None = None,
    extract_runner: ExtractRunner | None = None,
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
    # Whether the caller injected fakes decides which gate _require uses. Capture it
    # BEFORE defaulting `resolver`, or the production default looks like injection.
    _test_owner_fallback = auth is not check_bearer and resolver is None
    if resolver is None:
        from brain.api.auth import resolve_partner

        resolver = resolve_partner

    def _require(authorization: str | None) -> dict:
        """Gate on the key and return the caller's partner context.

        Resolves EXACTLY ONCE. The previous version called auth() and then the
        resolver as two independent Supabase queries, and mapped a None second
        result to org owner — so a transient database error between the two
        promoted any partner to full owner (key minting, purge, DMN control).
        A resolver that cannot reach its backend now raises, and that is a 503:
        "I cannot tell who you are" must never resolve to "you are the owner".

        The bool-fake branch exists only for tests that inject `auth` without a
        `resolver`. It is statically unreachable in production because ApiServer
        injects neither — do not make it a runtime condition."""
        if _test_owner_fallback:
            if not auth(authorization):
                raise HTTPException(status_code=401, detail="invalid or missing API key")
            return {"partner_id": None, "owner": True}
        try:
            ctx = resolver(authorization)
        except AuthBackendError:
            raise HTTPException(status_code=503, detail="auth backend unavailable") from None
        if ctx is None:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return ctx

    def _owns(ctx: dict, s) -> bool:
        # The org owner sees everything; a partner only its own sessions. Legacy
        # sessions with no partner_id are owner-scoped.
        return (
            bool(ctx.get("owner")) or s.partner_id is None or s.partner_id == ctx.get("partner_id")
        )

    def _resolve_event_source():
        """The event source for streaming, or None if this deployment has none.

        Defined once because SSE, WebSocket and /v1/capabilities must agree. They
        used to derive it independently, so capabilities would have reported
        streaming as unavailable whenever `event_source` was not injected — which is
        the normal production case, since ApiServer never passes one and the real
        source is the importable process singleton."""
        if event_source is not None:
            return event_source
        try:
            from brain.ui.emitter import emitter

            return emitter
        except Exception:
            return None

    def _checked_end_user_id(value: object) -> str:
        """Validated end_user_id, or 400.

        The only identifier in the API supplied wholesale by an outside caller, and
        until now the least validated one — every sibling id (persona, mandate,
        skill) was regex-checked while this went straight into an LLM prompt, a
        vault name, a SQL predicate and a filename."""
        from brain.ids import valid_end_user_id

        try:
            return valid_end_user_id(value)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _require_owner(authorization: str | None) -> dict:
        """Gate on an OWNER credential. Defined once, up here, because it guards three
        unrelated blocks further down (skills admin, key management, org config) and
        previously existed as two separate definitions where the second silently
        shadowed the first."""
        ctx = _require(authorization)
        if not ctx.get("owner"):
            raise HTTPException(status_code=403, detail="owner credential required")
        return ctx

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

    # ── Capabilities: what this deployment actually has ───────────────────────
    # Many subsystems are optional and return 501 when their runner was never wired
    # (grading, consolidation, approvals, extraction, job history, learning, audio,
    # streaming). The errors table told integrators to "feature-detect at startup"
    # and gave them no way to do it — the only method was to call each endpoint and
    # see whether it 501'd, several of which cost money or have side effects.
    @router.get("/capabilities")
    async def capabilities(authorization: str | None = Header(default=None)):
        """What this deployment supports and the limits it enforces.

        Call this once at startup instead of probing endpoints. Every flag is read
        from the same wiring the routes themselves check, so it cannot claim a
        capability the API does not have."""
        ctx = _require(authorization)
        from brain.second_brain import supabase_client

        try:
            has_supabase = bool(supabase_client.is_enabled())
        except Exception:
            has_supabase = False
        streaming = _resolve_event_source() is not None

        # Audio is the one pair where a wired runner is not enough: the routes return
        # 503 (not 501) when the provider key is missing, so a capability that only
        # checked the runner would promise a call that cannot succeed.
        def _audio_ready(runner, env_names: tuple[str, ...]) -> bool:
            if runner is None:
                return False
            return any(os.environ.get(n) for n in env_names)

        caps = {
            "turns": True,
            "streaming_sse": streaming,
            "streaming_ws": streaming,
            "grading": grade_runner is not None,
            "consolidation": consolidate_runner is not None,
            "confirmations": confirm_runner is not None,
            "approvals": approvals_list_runner is not None and approval_resolve_runner is not None,
            "extraction": extract_runner is not None,
            "job_history": job_get_runner is not None,
            "learning": learning_runner is not None,
            "erasure": purge_runner is not None,
            "tts": _audio_ready(tts_runner, ("ELEVENLABS_API_KEY", "OPENAI_API_KEY")),
            "stt": _audio_ready(stt_runner, ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY")),
            "stt_live": stt_live_runner is not None,
            "skills_screening": skill_screener is not None,
            # These all sit behind the same Supabase requirement _guard() enforces.
            "org_config": has_supabase,
            "mcp_tokens": has_supabase,
            "partner_keys": has_supabase,
        }

        limits: dict = dict(_limits.as_dict())
        if audio_quota is not None:
            pid = ctx.get("partner_id")
            with contextlib.suppress(Exception):
                from brain.api import audio_quota as _aq

                limits["audio"] = {
                    "metered": not ctx.get("owner"),
                    "window_s": audio_quota._window_s(),
                    "tts_chars_per_window": audio_quota._cap(_aq.TTS_CHARS),
                    "stt_seconds_per_window": audio_quota._cap(_aq.STT_SECONDS),
                    "tts_chars_used": audio_quota.window_total(pid, _aq.TTS_CHARS),
                    "stt_seconds_used": audio_quota.window_total(pid, _aq.STT_SECONDS),
                }
        with contextlib.suppress(Exception):
            from brain.settings import settings as _s

            org_cap = float(_s.get("cloud_daily_usd_budget") or 0)
            partner_cap = float(_s.get("partner_cloud_daily_usd_budget") or 0)
            if ctx.get("owner"):
                # The owner lane on a full brain reroutes to local over budget rather
                # than failing — quality changes, not the call. That is an owner-only
                # affordance, so only owners are told about it.
                limits["cloud"] = {
                    "daily_usd_budget": org_cap,
                    "over_budget_falls_back_to_local": True,
                }
            else:
                # A partner is metered against its own cap (tighter of partner/org) and
                # always gets 402 over budget — never a silent reroute.
                _partner_caps = [c for c in (partner_cap, org_cap) if c > 0]
                limits["cloud"] = {
                    "daily_usd_budget": min(_partner_caps) if _partner_caps else 0,
                    "over_budget_falls_back_to_local": False,
                }

        return {"api_version": "v1", "capabilities": caps, "limits": limits}

    @router.post("/sessions")
    async def create_session(body: dict, authorization: str | None = Header(default=None)):
        """Start a session for an end-user on an agent. Optionally pin app-provided
        skills into every turn of the session. Pass answer_only=true to declare the
        whole session synchronous Q&A: turns draft an answer and nothing else — no
        tool/motor work and no background follow-up jobs (a turn body can override
        per turn)."""
        ctx = _require(authorization)
        end_user_id = _checked_end_user_id((body or {}).get("end_user_id"))
        # Opening a session is how nearly every end user first appears, so this is the
        # main population path for the ownership registry. First-writer-wins: if the
        # id already belongs to another partner, that partner keeps it and this caller
        # is refused rather than silently sharing the customer's memory and chemistry.
        _eu.claim(end_user_id, ctx.get("partner_id"))
        if not _eu.is_allowed(ctx, end_user_id, unregistered_ok=True):
            raise HTTPException(status_code=403, detail="end_user belongs to another partner")
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
        if pinned_skills is not None and len(pinned_skills) > _limits.MAX_PINNED_SKILLS:
            raise HTTPException(
                status_code=400,
                detail=f"skills exceeds {_limits.MAX_PINNED_SKILLS} entries",
            )
        answer_only = (body or {}).get("answer_only", False)
        if not isinstance(answer_only, bool):
            raise HTTPException(status_code=400, detail="answer_only must be a boolean")
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
            answer_only=answer_only,
        )
        return {
            "session_id": s.session_id,
            "end_user_id": s.end_user_id,
            "agent_id": s.agent_id,
            "mandate_id": s.mandate_id,
            "skills": s.pinned_skills,
            "answer_only": s.answer_only,
        }

    @router.post("/extract")
    async def extract(body: dict, authorization: str | None = Header(default=None)):
        """Sessionless structured extraction: force ONE cheap model call to return JSON
        matching `schema`, with no session, persona, memory, motor, or DMN. Built for
        high-volume utility classification (e.g. pulling a tradeable signal out of an
        article) that must never pay for — or be unreliably answered by — a full
        conversational turn. Metered + bounded by the daily USD ceiling (over budget on
        a lite brain → 402). Body: {input, schema, instructions?, name?}."""
        _require(authorization)
        if extract_runner is None:
            raise HTTPException(status_code=501, detail="structured extraction not available")
        body = body or {}
        input_text = body.get("input")
        schema = body.get("schema")
        instructions = body.get("instructions") or ""
        name = body.get("name") or "extract"
        if not isinstance(input_text, str) or not input_text.strip():
            raise HTTPException(status_code=400, detail="input (non-empty string) is required")
        if not isinstance(schema, dict) or not schema:
            raise HTTPException(status_code=400, detail="schema (JSON Schema object) is required")
        if not isinstance(instructions, str) or not isinstance(name, str):
            raise HTTPException(status_code=400, detail="instructions and name must be strings")
        # Only the OUTPUT was bounded (max_tokens), so an unbounded document and an
        # unbounded schema went straight to a metered cloud model.
        if len(input_text) > _limits.EXTRACT_MAX_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"input exceeds {_limits.EXTRACT_MAX_INPUT_CHARS} characters",
            )
        if len(json.dumps(schema)) > _limits.EXTRACT_MAX_SCHEMA_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"schema exceeds {_limits.EXTRACT_MAX_SCHEMA_BYTES} bytes",
            )
        data = await extract_runner(input_text, schema, instructions, name)
        return {"data": data if isinstance(data, dict) else {}}

    def _resolve_answer_only(body: dict, s) -> bool:
        """Effective answer-only flag for one turn: the body's boolean when given,
        else the session's sticky value. (The agent-permission path is resolved
        inside the turn itself — see session_turn — so it needs no plumbing here.)"""
        v = (body or {}).get("answer_only")
        if v is None:
            return bool(getattr(s, "answer_only", False))
        if not isinstance(v, bool):
            raise HTTPException(status_code=400, detail="answer_only must be a boolean")
        return v

    @router.post("/sessions/{session_id}/turns")
    async def run_turn(
        session_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        """Run one turn. Returns clean display text, the structured affect block +
        mood, and a confirmation block when a cloud write is pending. Send message
        OR audio_input for voice-in. answer_only=true makes this turn pure Q&A —
        no tool/motor work, no background follow-up jobs (defaults to the
        session's answer_only setting)."""
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
            answer_only=_resolve_answer_only(body, s),
            partner_id=s.partner_id or "",
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
        # The handle a partner needs to grade this turn later (POST
        # /sessions/{id}/turns/{turn_id}/grade). Read from the raw affect, not the
        # curated block. The SSE path already emits turn_id in its events.
        _tid = affect.get("turn_id") if isinstance(affect, dict) else None
        if _tid:
            resp["turn_id"] = _tid
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

    @router.post("/sessions/{session_id}/turns/{turn_id}/grade")
    async def grade_turn(
        session_id: str,
        turn_id: str,
        body: dict,
        authorization: str | None = Header(default=None),
    ):
        """Submit an EXTERNAL grade for a turn — the one reward signal grounded outside
        the agent's own appraisal (a thumbs verdict, a rating, an automated grade). Body
        is {grade, source?}: grade is +1/-1/bool (thumbs) or any number normalized to
        [-1, +1]; source defaults to "api". turn_id comes from the turn response
        (POST /turns returns it as turn_id) or the SSE done event.

        The grade re-weights this turn's learning at the next consolidation and, with the
        DA nudge enabled, moves the end-user's own chemistry (bound for this write, so a
        partner's grade lands on that customer's mood, never the process resting pair).

        Contract: turn_id must belong to THIS session — grading a turn from any other
        session is 404, indistinguishable from a turn that never existed. Chemistry
        moves at most once per turn_id (a re-grade applies only the bounded difference
        from the previous grade). A grade that arrives after the turn left the live
        buffer (consolidation/restart) returns ok with applied_live=false and
        reason="turn_not_live": it is recorded for audit but no longer reaches learning
        or chemistry, so an async grader can detect it missed the window."""
        ctx = _require(authorization)
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        if grade_runner is None:
            raise HTTPException(status_code=501, detail="grading is not available on this server")
        grade = (body or {}).get("grade")
        if grade is None:
            raise HTTPException(status_code=400, detail="missing grade")
        # Partner-supplied provenance string ends up in the eval log / decision
        # stream verbatim — clamp to something log-safe (printable, bounded).
        # ASCII-printable, not str.isprintable(): the latter admits Unicode bidi
        # overrides (U+202E) and full-width homoglyphs, so a partner could author a
        # source that RENDERS in a log viewer as something it is not.
        source = str((body or {}).get("source", "api"))
        source = "".join(ch for ch in source if " " <= ch <= "~")[
            : _limits.GRADE_SOURCE_MAX
        ].strip()
        source = source or "api"
        result = grade_runner(
            turn_id, grade, s.end_user_id, _session_persona(s) or "", source, session_id
        )
        if isinstance(result, dict) and result.get("denied"):
            raise HTTPException(status_code=404, detail="unknown turn_id for this session")
        return result

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
            raise HTTPException(
                status_code=501, detail="consolidation is not available on this server"
            )
        reason = (body or {}).get("reason") or "api"
        # Bind the SESSION's persona for the consolidation so the Hebbian wiring update lands on
        # THAT persona's graph (wiring resolves the active persona from this contextvar). Without
        # this, every agent's learning collapses onto the boot persona. The await runs inside the
        # binding (same task → contextvar propagates).
        from brain.second_brain.store import bind_persona

        with bind_persona(_session_persona(s) or ""):
            result = await consolidate_runner(str(reason))
        return {"session_id": session_id, "consolidation": result}

    @router.post("/sessions/{session_id}/turns/stream")
    async def run_turn_stream(
        session_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        """Stream the turn over SSE — an open event, inner-thought + mood-delta
        events, a final done event, then optional audio_chunk frames when
        audio.enabled. answer_only behaves as on POST /turns."""
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
        # Validate before the stream opens — inside _gen a 400 can't surface.
        answer_only_flag = _resolve_answer_only(body, s)

        source = _resolve_event_source()
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
                answer_only=answer_only_flag,
                partner_id=s.partner_id or "",
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
        """Realtime duplex WebSocket for a session: stream PCM16 audio in (live
        STT), receive inner-life events and TTS chunks back.

        Auth is checked on the Authorization header of the upgrade request
        BEFORE accept(); unknown or unauthorised connections are closed 1008."""
        # Full frame vocabulary: brain/api/ws.py (docstring stays user-facing —
        # it renders verbatim as this route's Reference-page description).
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

        source = _resolve_event_source()

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
        """Approve or discard the cloud-write action a turn parked for sign-off."""
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
            raise HTTPException(
                status_code=501, detail="approvals are not available on this server"
            )
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not _owns(ctx, s):
            raise HTTPException(status_code=403, detail="session belongs to another partner")
        approve = bool((body or {}).get("approve", True))
        # Owner-key callers may also resolve autonomous/owner-lane items (see GET above).
        res = (
            approval_resolve_runner(approval_id, s.end_user_id, approve, bool(ctx.get("owner")))
            or {}
        )
        return {"session_id": session_id, "end_user_id": s.end_user_id, "approved": approve, **res}

    # ── Autonomous job history (durable; gate-independent) ─────────────────────
    # Job outcomes are pollable here regardless of any WS/voice gate — a client that
    # was disconnected while a job ran still reads its state + results. Backed by the
    # agent_jobs table (falls back to the JSON JobStore in local/companion mode).
    @router.get("/jobs")
    async def list_jobs(
        limit: int = 20,
        state: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        """Recent autonomous job outcomes (state, reason, summary) — durable and
        pollable, so a client that was disconnected while a job ran still reads
        its result. Filters: ?limit= and ?state=. A partner key sees only its own
        jobs; the owner sees all."""
        ctx = _require(authorization)
        if jobs_list_runner is None:
            return {"jobs": []}
        jobs = jobs_list_runner(int(limit or 20), state) or []
        if not ctx.get("owner"):
            pid = ctx.get("partner_id")
            jobs = [j for j in jobs if (j.get("partner_id") or "") == (pid or "")]
        return {"jobs": jobs}

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str, authorization: str | None = Header(default=None)):
        """Full record for one job — steps, results, source links, summary. 404
        for an unknown job id (or another partner's job); 501 when job history
        isn't available on this server."""
        ctx = _require(authorization)
        if job_get_runner is None:
            raise HTTPException(
                status_code=501, detail="job history is not available on this server"
            )
        rec = job_get_runner(job_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        # Another partner's job is indistinguishable from one that never existed.
        if not ctx.get("owner") and (rec.get("partner_id") or "") != (ctx.get("partner_id") or ""):
            raise HTTPException(status_code=404, detail="unknown job_id")
        return rec

    # ── Webhooks: signed job-outcome delivery (migration 032) ─────────────────
    # Register an endpoint and the engine POSTs there when a job finishes, so a
    # partner needn't hold a WebSocket open or poll /v1/jobs. Deliveries are HMAC
    # signed; the gateway (always up) retries. Owner-or-self scoped, like skills.
    @router.post("/webhooks")
    async def create_webhook(body: dict, authorization: str | None = Header(default=None)):
        """Register a webhook. Body: {url, events?}. Returns {id, url, events, secret}
        — the signing secret is shown ONCE. `url` must be a public https endpoint."""
        ctx = _require(authorization)
        _guard()
        from brain import net_guard
        from brain.api import webhooks

        url = (body or {}).get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=400, detail="url (non-empty string) is required")
        try:
            net_guard.validate_url(url.strip())
        except net_guard.UnsafeUrlError as e:
            raise HTTPException(status_code=400, detail=f"unsafe url: {e}") from e
        events = (body or {}).get("events")
        if events is not None and (
            not isinstance(events, list) or any(not isinstance(x, str) for x in events)
        ):
            raise HTTPException(status_code=400, detail="events must be a list of strings")
        try:
            return webhooks.register(ctx, url.strip(), events)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @router.get("/webhooks")
    async def list_webhooks(authorization: str | None = Header(default=None)):
        """Your registered webhooks (metadata only — never the secret)."""
        ctx = _require(authorization)
        _guard()
        from brain.api import webhooks

        return {"webhooks": webhooks.list_for(ctx)}

    @router.post("/webhooks/{webhook_id}/rotate")
    async def rotate_webhook(webhook_id: str, authorization: str | None = Header(default=None)):
        """Mint a new signing secret (shown once); the old one stops verifying."""
        ctx = _require(authorization)
        _guard()
        from brain.api import webhooks

        out = webhooks.rotate(ctx, webhook_id)
        if out is None:
            raise HTTPException(status_code=404, detail="unknown webhook id")
        return out

    @router.get("/webhooks/{webhook_id}/deliveries")
    async def webhook_deliveries(
        webhook_id: str, limit: int = 50, authorization: str | None = Header(default=None)
    ):
        """Recent delivery attempts for one webhook (state, status, error) — the
        failure-visibility surface. 404 for another partner's webhook."""
        ctx = _require(authorization)
        _guard()
        from brain.api import webhooks

        out = webhooks.list_deliveries(ctx, webhook_id, limit)
        if out is None:
            raise HTTPException(status_code=404, detail="unknown webhook id")
        return {"deliveries": out}

    @router.delete("/webhooks/{webhook_id}")
    async def delete_webhook(webhook_id: str, authorization: str | None = Header(default=None)):
        """Delete a webhook and its secret. 404 for another partner's webhook."""
        ctx = _require(authorization)
        _guard()
        from brain.api import webhooks

        if not webhooks.delete(ctx, webhook_id):
            raise HTTPException(status_code=404, detail="unknown webhook id")
        return {"ok": True, "id": webhook_id}

    # ── Learning surface (read-only views over the learning subsystems) ───────
    # Owner keys may name a persona; partner keys are pinned to the org's home
    # persona (partners are org-scoped, not persona-scoped — mirrors mandates).

    def _learning_persona(ctx: dict, persona: str) -> str:
        return persona if ctx.get("owner") else ""

    @router.get("/learning/stories")
    async def learning_stories(
        persona: str = "",
        limit: int = 50,
        authorization: str | None = Header(default=None),
    ):
        """Plain-language stories of what the brain learned (per session, with
        structured evidence citations). Owner keys may pass ?persona=; partner
        keys read the org's home persona. ?limit= pages."""
        ctx = _require(authorization)
        if learning_runner is None:
            raise HTTPException(status_code=501, detail="learning surface not available")
        return learning_runner(
            "stories", persona=_learning_persona(ctx, persona), limit=int(limit or 50)
        )

    @router.get("/learning/wiring")
    async def learning_wiring(
        persona: str = "",
        edge: str = "",
        authorization: str | None = Header(default=None),
    ):
        """Top learned routing edges + this session's weight deltas. ?edge=src→tgt
        adds that edge's drift series across consolidation snapshots and its
        recent update records."""
        ctx = _require(authorization)
        if learning_runner is None:
            raise HTTPException(status_code=501, detail="learning surface not available")
        return learning_runner("wiring", persona=_learning_persona(ctx, persona), edge=edge)

    @router.get("/learning/summary")
    async def learning_summary(
        persona: str = "",
        authorization: str | None = Header(default=None),
    ):
        """Learning vitals: plasticity per session, reward-source mix (self-graded
        vs external %), switch efficacy within safety bands, motor chunks,
        thought-sequence predictor stats."""
        ctx = _require(authorization)
        if learning_runner is None:
            raise HTTPException(status_code=501, detail="learning surface not available")
        return learning_runner("summary", persona=_learning_persona(ctx, persona))

    # ── DMN (idle-thought) runtime switch — owner only ────────────────────────
    # Durable kill-switch for the idle inner-life loop, settable while the brain
    # runs: the loop checks settings['dmn_enabled'] each cycle, so a PUT takes
    # effect on the next tick and re-enabling needs no restart. Distinct from the
    # BRAIN_DMN env gate (which decides whether the loop exists at all) — this
    # switch can only stop a running loop, never start one env has disabled.
    # Persisted via settings.save() so it survives a process restart.
    @router.get("/dmn")
    async def get_dmn_route(authorization: str | None = Header(default=None)):
        """Owner: read the DMN idle-thought switch."""
        _require_owner(authorization)
        from brain.settings import settings as brain_settings

        return {"enabled": bool(brain_settings.get("dmn_enabled", 1))}

    @router.put("/dmn")
    async def set_dmn_route(body: dict, authorization: str | None = Header(default=None)):
        """Owner: flip the DMN idle-thought switch ({enabled: bool})."""
        _require_owner(authorization)
        enabled = (body or {}).get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled (boolean) is required")
        from brain.settings import settings as brain_settings

        brain_settings.save({"dmn_enabled": 1 if enabled else 0})
        return {"enabled": enabled}

    # ── Audio (optional, partner-gated) ───────────────────────────────────────
    # Stateless: no session needed. TTS exposes the affect→voice mapping (the
    # differentiated half — a partner can't replicate mood-driven prosody client
    # side); STT is the commodity convenience path. Both 501 when no runner is
    # wired and 503 (via AudioError) when the provider key isn't configured.
    @router.post("/tts")
    async def tts_route(body: dict, authorization: str | None = Header(default=None)):
        """Text-to-speech with the affect→voice mapping: pass affect to drive
        mood-aware prosody. Stateless; 503 when no provider key is configured."""
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
        """Speech-to-text (base64 audio in) — the commodity transcription path,
        with optional diarisation. Stateless."""
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
        """List the org role library. Pass ?include_inactive=true to include
        deactivated roles."""
        _require(authorization)
        _guard()
        from brain import mandates

        return {"mandates": _run(lambda: mandates.list_mandates(include_inactive))}

    @router.put("/mandates/{mandate_id}")
    async def upsert_mandate_route(
        mandate_id: str, body: dict, authorization: str | None = Header(default=None)
    ):
        """Create or update a role (mandate). conduct_rules / reward_weights are
        stored for partner sync. Owner credential required."""
        _require_owner(authorization)
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
        """Deactivate a role. Existing persona assignments stop resolving it; the
        record is kept (soft-delete) so ?include_inactive=true can still list it.
        Owner credential required."""
        _require_owner(authorization)
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
        """List the roles assigned to a persona, in order."""
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
        """Assign a role to a persona (idempotent). Optional sort_order. Owner
        credential required."""
        _require_owner(authorization)
        _guard()
        from brain import mandates

        sort_order = int((body or {}).get("sort_order", 0) or 0)
        return _run(lambda: mandates.assign(persona, mandate_id, sort_order))

    @router.delete("/personas/{persona}/mandates/{mandate_id}")
    async def unassign_route(
        persona: str, mandate_id: str, authorization: str | None = Header(default=None)
    ):
        """Unassign a role from a persona. Owner credential required."""
        _require_owner(authorization)
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
        """List the org's agents and the account permission ceilings."""
        _require(authorization)
        _guard()
        from brain import agents as _ag
        from brain.settings import settings as _s

        agents = _run(lambda: _ag.list_agents())
        ceilings = {k: _s.get(k) for k in _ag.PERMISSION_KEYS}
        return {"agents": agents, "ceilings": ceilings}

    @router.get("/agents/{agent_id}")
    async def get_agent_route(agent_id: str, authorization: str | None = Header(default=None)):
        """Fetch one agent (persona×role) — its name, permissions, and model tier."""
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
        """Create-or-update an agent: set its display name, per-agent permission
        narrowing, and model tier. Owner credential required."""
        _require_owner(authorization)
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
        """Delete an agent by unassigning the persona×role pairing — the id stops
        resolving for new sessions. The underlying role stays in the library.
        Owner credential required."""
        _require_owner(authorization)
        _guard()
        from brain import mandates

        def _do():
            persona, _, mid = str(agent_id).partition(".")
            return mandates.unassign(persona, mid)

        ok = _run(_do)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown agent")
        return {"ok": True, "agent_id": agent_id}

    # ── Personas (authored identities: disposition text + emotional baseline) ──
    # A persona is the durable-identity half of an agent. Built-ins ship with the
    # engine; a partner (e.g. a story engine casting book characters) authors
    # CUSTOM personas here at runtime, then pairs one with a role via
    # PUT /v1/agents/{persona}.{mandate_id}. The gateway spawns any persona slug
    # on demand (X-Brain-Persona header → dedicated process, 503 while booting),
    # so a created persona is immediately resolvable — subject to the per-org
    # dedicated-instance cap surfaced by GET /v1/personas. Same bearer auth as
    # mandates: the caller already controls which persona a session runs as.

    def _run_persona(fn):
        from brain.personas import PersonaError

        try:
            return fn()
        except PersonaError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/personas")
    async def list_personas_route(authorization: str | None = Header(default=None)):
        """List every persona this org can run — the built-in roster plus custom
        (runtime-authored) specs — with the capacity limits that govern how many
        personas can run as dedicated brain processes at once (beyond
        max_dedicated_instances, extra personas are refused, so plan concurrent
        multi-persona scenes within the cap)."""
        _require(authorization)
        from brain import personas as _p

        return {"personas": _p.list_all(), "limits": _p.capacity_limits()}

    @router.get("/personas/{persona}")
    async def get_persona_route(persona: str, authorization: str | None = Header(default=None)):
        """Fetch one persona: a custom persona's stored spec (display name,
        disposition, resting chemistry baseline) or a built-in's canonical
        profile."""
        _require(authorization)
        from brain import personas as _p

        row = _p.get(persona)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown persona")
        return row

    @router.put("/personas/{persona}")
    async def upsert_persona_route(
        persona: str, body: dict | None = None, authorization: str | None = Header(default=None)
    ):
        """Create or update a CUSTOM persona (idempotent; built-in slugs are
        refused). Body fields are all optional and merge over the stored spec:
        display_name; disposition / personality / speaking (identity text,
        written as the persona in first person — becomes its self-model);
        baseline (chemistry channels DA/ACh/GABA/Glu/NE/5HT/CORT/OXT/AEA in
        [0,1]; unset channels default to a neutral profile). The baseline is the
        temperament the persona's brain boots with and relaxes toward.
        Owner credential required."""
        _require_owner(authorization)
        from brain import personas as _p

        return _run_persona(lambda: _p.upsert(persona, body or {}))

    @router.delete("/personas/{persona}")
    async def delete_persona_route(persona: str, authorization: str | None = Header(default=None)):
        """Delete a custom persona's spec, chemistry and identity document
        (built-ins can't be deleted). Its learned state stays keyed under the
        slug and simply goes dormant; delete its agents separately via
        DELETE /v1/agents/{agent_id}. Owner credential required."""
        _require_owner(authorization)
        from brain import personas as _p

        ok = _run_persona(lambda: _p.delete(persona))
        if not ok:
            raise HTTPException(status_code=404, detail="unknown persona")
        return {"ok": True, "persona": persona}

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

    @router.get("/skills")
    async def list_skills_route(
        include_inactive: bool = False,
        status: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        """List app-provided skills. Filters: ?status= and ?include_inactive=.
        A non-owner partner sees only its own submissions."""
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
        """Fetch one skill — the full row including body text, status (enabled /
        flagged / rejected), and screener notes. Use it to poll whether a
        submission cleared review."""
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
        """Submit or update an app-provided skill. Runs the admission screener:
        obviously-safe → enabled, anything in question → flagged for review."""
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
                    screen_notes={
                        "judge": {"verdict": None, "reasons": ["screener_not_configured"]}
                    },
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
        """Remove a skill — it leaves the live index on the next rewarm and stops
        being injected into turns."""
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
        """List skills the screener flagged, awaiting superadmin review."""
        _require_owner(authorization)
        _guard()
        from brain import skills_registry as sr

        return {"skills": _run_skill(lambda: sr.list_flagged())}

    @router.post("/admin/skills/{skill_id}/approve")
    async def approve_skill_route(skill_id: str, authorization: str | None = Header(default=None)):
        """Approve a flagged skill — it goes live."""
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
        """Reject a flagged skill, with an optional reason recorded in its
        screen notes. It never goes live."""
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
        """Erase one end-user's memory + state across every per-user table and
        in-memory cache (GDPR right-to-erasure).

        A partner may erase its OWN customers. It is the data controller for them, and
        gating this on the owner left partners unable to discharge their own erasure
        obligations through the API at all."""
        ctx = _require(authorization)
        end_user_id = _checked_end_user_id(end_user_id)
        # Capability before ownership: whether this deployment can erase at all is a
        # property of the SERVER, not of the target, so it must be reachable without
        # already owning an end user — otherwise GET /v1/capabilities cannot be
        # verified against the real endpoint.
        if purge_runner is None:
            raise HTTPException(
                status_code=501, detail="end-user purge is not available on this server"
            )
        if not _eu.is_allowed(ctx, end_user_id, unregistered_ok=False):
            raise HTTPException(status_code=404, detail="unknown end_user_id")
        # Drop any cached sessions for this end_user so a later turn can't run as a
        # half-erased customer.
        registry.forget_end_user(end_user_id)
        result = await purge_runner(end_user_id)
        # Ownership row goes last: while it exists the customer is still owned and so
        # still re-purgeable, so a partial failure leaves retryable work rather than
        # rows nobody can reach.
        _eu.forget(end_user_id)
        return result

    # ── Per-partner key management (owner-only) ───────────────────────────────
    @router.get("/partner_keys")
    async def list_partner_keys_route(authorization: str | None = Header(default=None)):
        """List the org's partner keys (metadata only — never the token)."""
        _require_owner(authorization)
        _guard()
        from brain.api import auth as _a

        return {"keys": _a.list_partner_keys()}

    @router.post("/partner_keys")
    async def mint_partner_key_route(body: dict, authorization: str | None = Header(default=None)):
        """Mint a partner key — the token is returned once, at creation. Optional
        role: "partner" (default) or "owner". An owner-grade key can call owner-gated
        routes and, unlike the env owner key, resolves through the hosted gateway."""
        _require_owner(authorization)
        _guard()
        from brain.api import auth as _a

        partner_id = (body or {}).get("partner_id")
        if not isinstance(partner_id, str) or not partner_id.strip():
            raise HTTPException(status_code=400, detail="partner_id (non-empty string) is required")
        try:
            return _a.mint_partner_key(
                partner_id.strip(),
                (body or {}).get("label"),
                role=(body or {}).get("role") or "partner",
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/partner_keys/{key_id}")
    async def revoke_partner_key_route(
        key_id: str, authorization: str | None = Header(default=None)
    ):
        """Revoke a partner key — requests bearing it are rejected immediately;
        the metadata row is kept for audit."""
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
        """Store a per-end-user MCP access token (vault-encrypted at rest) after
        the partner completes the OAuth flow, so managed agents can build
        per-user Vaults. Scoped to the calling partner's own end users."""
        ctx = _require(authorization)
        body = body or {}
        end_user_id = _checked_end_user_id(body.get("end_user_id"))
        server_name = body.get("server_name")
        server_url = body.get("server_url")
        access_token = body.get("access_token")
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
        # Claim-then-check. Storing a token is the first thing a partner does for a new
        # customer, so this is where most ownership rows come from; the check then stops
        # a partner writing a connector onto SOMEONE ELSE'S customer, which is the
        # hijack path (attacker-controlled server_url + token, picked up on the next
        # vault build). Enforced here rather than in the RPCs: migration 026 warns that
        # re-signaturing those security-definer functions must re-issue their
        # `revoke ... from anon, public`, and ownership is an API-layer concept anyway.
        _eu.claim(end_user_id, ctx.get("partner_id"))
        if not _eu.is_allowed(ctx, end_user_id, unregistered_ok=True):
            raise HTTPException(status_code=403, detail="end_user belongs to another partner")
        try:
            from brain.second_brain import supabase_client

            _sb_client().rpc(
                "set_end_user_mcp_token",
                {
                    "p_end_user_id": end_user_id.strip(),
                    "p_server_name": server_name.strip(),
                    "p_server_url": server_url.strip(),
                    "p_token": access_token.strip(),
                    "p_expires_at": expires_at,
                    # Explicit org for the service-key fallback mode (asymmetric JWT
                    # signing → no auth.uid()); ignored when a real org JWT is
                    # attached, so it can't be used to name another org.
                    "p_org_id": supabase_client.get_org_id(),
                },
            ).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to store token: {e}") from e
        return {"ok": True, "end_user_id": end_user_id.strip(), "server_name": server_name.strip()}

    @router.get("/mcp/tokens/{end_user_id}")
    async def list_mcp_tokens(end_user_id: str, authorization: str | None = Header(default=None)):
        """List an end-user's connected MCP servers (metadata only — never the
        token). Scoped to the calling partner's own end users."""
        ctx = _require(authorization)
        end_user_id = _checked_end_user_id(end_user_id)
        # 404, not 403, for an id this caller may not see: a 403 would confirm that
        # the id exists, which is itself the customer-graph leak being closed.
        if not _eu.is_allowed(ctx, end_user_id, unregistered_ok=False):
            raise HTTPException(status_code=404, detail="unknown end_user_id")
        # end_user_id is partner-chosen free text and NOT globally unique — the PK
        # is (org_id, end_user_id, server_name), so the same id ("user_1", an email)
        # legitimately exists in other orgs. The org filter is what keeps this from
        # returning their rows; without it, a guessed id leaks which third-party
        # services another org's end-users have connected.
        #
        # This reads PostgREST directly rather than via a SECURITY DEFINER RPC like
        # its store/delete siblings, because those RPCs derive `v_org := auth.uid()`
        # — which is NULL under the service-role key — and would fail closed in the
        # mode production actually runs in.
        try:
            from brain.second_brain import supabase_client

            resp = (
                _sb_client()
                .table("end_user_mcp_tokens")
                .select("server_name, server_url, expires_at")
                # Explicit org filter: this direct PostgREST read is not RLS-scoped
                # under the service-key fallback (asymmetric JWT signing), so scope
                # it in-query like every other tenant read. Redundant but harmless
                # under a real org JWT (RLS would already scope it).
                .eq("org_id", supabase_client.get_org_id())
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
        """Delete one stored MCP token connection — the disconnect path when an
        end-user unlinks a service; agents lose access on their next Vault
        build. Scoped to the calling partner's own end users."""
        ctx = _require(authorization)
        end_user_id = _checked_end_user_id(end_user_id)
        if not _eu.is_allowed(ctx, end_user_id, unregistered_ok=False):
            raise HTTPException(status_code=404, detail="unknown end_user_id")
        try:
            from brain.second_brain import supabase_client

            resp = (
                _sb_client()
                .rpc(
                    "delete_end_user_mcp_token",
                    {
                        "p_end_user_id": end_user_id,
                        "p_server_name": server_name,
                        # See store_mcp_token: explicit org for the service-key mode.
                        "p_org_id": supabase_client.get_org_id(),
                    },
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
        jobs_list_runner: Callable[[int, str | None], list] | None = None,
        job_get_runner: Callable[[str], dict | None] | None = None,
        grade_runner: GradeRunner | None = None,
        learning_runner: Callable[..., dict] | None = None,
        purge_runner: PurgeRunner | None = None,
        extract_runner: ExtractRunner | None = None,
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

        # A cloud-budget block surfaces as 402 Payment Required (not an opaque 500), so
        # a partner can distinguish "you're over your daily ceiling" from a real fault.
        from fastapi.responses import JSONResponse

        from brain.model_router import CloudBudgetExceeded

        async def _budget_handler(_request, exc):  # noqa: ANN001
            return JSONResponse(status_code=402, content={"detail": str(exc)})

        self._app.add_exception_handler(CloudBudgetExceeded, _budget_handler)

        # Body cap, as a backstop behind the gateway's. The engine binds 127.0.0.1
        # behind the gateway in the hosted shape, but it also runs standalone for
        # direct/self-hosted callers, where nothing else bounds a request.
        #
        # Also stamps a request id. Without one there is no shared handle between what
        # a partner saw and what our logs recorded, so every support conversation
        # starts by trying to correlate on timestamps.
        @self._app.middleware("http")
        async def _cap_body(request, call_next):  # noqa: ANN001
            import secrets as _secrets

            rid = (request.headers.get("x-request-id") or "").strip()
            # Echo the caller's id when it is sane, so their trace and ours agree;
            # otherwise mint one rather than propagating arbitrary text into logs.
            if not rid or len(rid) > 64 or not all(" " <= c <= "~" for c in rid):
                rid = _secrets.token_hex(8)
            request.state.request_id = rid

            cap = _limits.MAX_BODY_BYTES
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                resp = JSONResponse(
                    status_code=413, content={"detail": f"request body exceeds {cap} bytes"}
                )
            else:
                resp = await call_next(request)
            resp.headers["X-Request-Id"] = rid
            return resp

        self._app.include_router(
            build_api_router(
                turn_runner,
                self._registry,
                consolidate_runner=consolidate_runner,
                confirm_runner=confirm_runner,
                approvals_list_runner=approvals_list_runner,
                approval_resolve_runner=approval_resolve_runner,
                jobs_list_runner=jobs_list_runner,
                job_get_runner=job_get_runner,
                grade_runner=grade_runner,
                learning_runner=learning_runner,
                purge_runner=purge_runner,
                extract_runner=extract_runner,
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
            self._app,
            host=_host,
            port=_port,
            log_level="warning",
            access_log=False,
            # Bound the header block too. The body cap above is a middleware and so
            # runs only once headers are parsed; without this, oversized headers are
            # buffered before any of our code sees the request.
            h11_max_incomplete_event_size=64 * 1024,
        )
        logger.info("Engine API starting at http://%s:%d/v1", _host, _port)
        await uvicorn.Server(config).serve()
