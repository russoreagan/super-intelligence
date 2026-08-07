"""
Realtime WebSocket session handler for the engine API.

``WsSession`` manages a single persistent WebSocket connection covering the full
lifetime of an API session. It replaces the per-turn SSE round-trip with a
bidirectional transport that supports:

  - Streaming audio IN (PCM16 chunks) → Deepgram live STT → interim transcripts
    forwarded to the client → full utterance triggers a brain turn
  - Brain inner-life events (thoughts, mood OUTPUT) forwarded over the socket,
    filtered to the active turn_id — raw chemistry (neuromod/hormonal) is withheld
  - Streaming audio OUT (TTS chunks via synthesize_stream) on the same connection
  - Barge-in: audio arriving while TTS is playing cancels the in-flight synthesis
    and opens a fresh STT session for the new utterance

Protocol: see the plan / API reference. All messages are JSON. Audio data is
base64-encoded in the JSON payload (consistent with the existing audio_chunk
SSE shape).

This module never imports from brain.api.server to avoid circular imports.
The curated affect/mood views (_affect_view, _mood_from_affect) are shared with the
SSE transport via brain.api._affect — neither transport depends on the other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from brain.turn_ctx import bind_turn

logger = logging.getLogger(__name__)

# Emitter event types forwarded to WS clients (subset of _STREAMED_TYPES).
# audio_* types are produced locally by _ws_stream_audio, not from the emitter.
_FORWARD_TYPES = frozenset(
    {
        "turn_start",
        "stream_thought",
        # Chemistry (neuromod/hormonal) is deliberately NOT forwarded to partners — only
        # the mood OUTPUT (emotion) crosses the boundary, so the affect model can't be
        # reverse-engineered from the raw signal. It stays visible in the owner's own UI.
        "emotion",
        "user_emotion",
        # Out-of-band: a backgrounded/always-on job's result. Fires after turn_end,
        # so it's exempt from the active-turn filter below.
        "proactive_speech",
        # Terminal job outcome (state + reason + summary) for every autonomous job —
        # completed / deferred / failed / stopped_budget / awaiting_approval. Out-of-band
        # like proactive_speech; gate-independent so a client sees terminal state live
        # even when the spoken-delivery gates suppress TTS.
        "task_outcome",
    }
)


class WsSession:
    """Manages one WebSocket connection for a realtime API session.

    Instantiated per connection by the @router.websocket handler in server.py;
    ``run()`` owns the socket lifetime."""

    def __init__(
        self,
        websocket,
        session,
        ctx: dict,
        *,
        turn_runner,
        registry,
        tts_stream_runner=None,
        audio_quota=None,
        event_source=None,
        stt_live_factory=None,
    ) -> None:
        self._ws = websocket
        self._session = session
        self._registry = registry
        self._ctx = ctx
        self._turn_runner = turn_runner
        self._tts_stream_runner = tts_stream_runner
        self._audio_quota = audio_quota
        self._event_source = event_source
        self._stt_live_factory = stt_live_factory

        self._turn_lock: asyncio.Lock = asyncio.Lock()
        # Set by barge-in; _ws_stream_audio polls this between chunks.
        self._tts_cancel: asyncio.Event = asyncio.Event()
        self._active_turn_id: str | None = None
        self._dg_session = None  # DeepgramLiveSession | None
        self._audio_opts: dict = {}  # last audio config from client
        self._transcript_seq: int = 0  # monotonic counter for transcript frames

    async def run(self) -> None:
        """Accept the connection, send ready, run until disconnect."""
        await self._ws.accept()
        await self._ws.send_json(
            {
                "type": "ready",
                "session_id": self._session.session_id,
                "expects": "pcm_16000",
            }
        )

        source = self._event_source
        if source is None:
            with contextlib.suppress(Exception):
                from brain.ui.emitter import emitter as source  # type: ignore[assignment]

        tap: asyncio.Queue = asyncio.Queue(maxsize=512)
        if source is not None:
            source.add_tap(tap)

        tasks = [
            asyncio.create_task(self._receive_loop()),
            asyncio.create_task(self._emitter_loop(tap)),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        finally:
            if source is not None:
                source.remove_tap(tap)
            if self._dg_session is not None:
                await self._dg_session.close()

    # ── receive loop ──────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        try:
            while True:
                try:
                    raw = await self._ws.receive_json()
                except Exception:
                    break
                mtype = raw.get("type")
                if mtype == "audio":
                    await self._handle_audio(raw)
                elif mtype == "audio_end":
                    await self._handle_audio_end()
                elif mtype == "text":
                    await self._handle_text(raw)
                elif mtype == "ping":
                    await self._ws.send_json({"type": "pong"})
        except asyncio.CancelledError:
            pass

    async def _handle_audio(self, msg: dict) -> None:
        import base64

        from brain.api.audio_quota import STT_SECONDS

        data_b64 = msg.get("data") or ""
        try:
            pcm_bytes = base64.b64decode(data_b64)
        except Exception:
            await self._send(
                {"type": "error", "detail": "audio.data must be valid base64", "code": 400}
            )
            return

        # Open a Deepgram session on the first audio chunk; check quota once here.
        if self._dg_session is None:
            if self._stt_live_factory is None:
                await self._send(
                    {
                        "type": "error",
                        "detail": "live STT is not available on this server",
                        "code": 501,
                    }
                )
                return
            if self._audio_quota and not self._ctx.get("owner"):
                reason = self._audio_quota.check(self._ctx.get("partner_id"), STT_SECONDS)
                if reason:
                    await self._send({"type": "error", "detail": reason, "code": 429})
                    return
            from brain.api.audio import AudioError

            session = self._stt_live_factory()
            try:
                await session.open(self._on_transcript)
            except AudioError as e:
                await self._send({"type": "error", "detail": e.detail, "code": e.status})
                return
            self._dg_session = session

        await self._dg_session.send(pcm_bytes)

    async def _handle_audio_end(self) -> None:
        if self._dg_session is not None:
            await self._dg_session.close()
            self._dg_session = None

    async def _handle_text(self, msg: dict) -> None:
        message = str(msg.get("message") or "").strip()
        if not message:
            await self._send(
                {"type": "error", "detail": "message (non-empty string) is required", "code": 400}
            )
            return
        audio = msg.get("audio")
        if audio is not None and isinstance(audio, dict):
            self._audio_opts = audio
        # Signal barge-in (cancels any in-flight TTS before acquiring the lock).
        self._tts_cancel.set()
        asyncio.create_task(self._run_turn(message, transcript=None))

    # ── STT transcript callback ───────────────────────────────────────────────

    async def _on_transcript(self, text: str, is_final: bool, duration_s: float) -> None:
        """Called from the DeepgramLiveSession reader task on each result."""
        seq = self._transcript_seq
        self._transcript_seq += 1
        payload: dict = {"type": "transcript", "text": text, "is_final": is_final, "seq": seq}
        if is_final and duration_s > 0:
            payload["duration_s"] = duration_s
        await self._send(payload)

        if not is_final or not text:
            return

        # Record STT quota on the final result (duration_s is populated).
        if duration_s > 0 and self._audio_quota and not self._ctx.get("owner"):
            from brain.api.audio_quota import STT_SECONDS

            with contextlib.suppress(Exception):
                self._audio_quota.record(self._ctx.get("partner_id"), STT_SECONDS, duration_s)

        # Close the completed utterance session before the turn starts.
        if self._dg_session is not None:
            await self._dg_session.close()
            self._dg_session = None

        # Barge-in: cancel any in-flight TTS, then kick off a new turn.
        self._tts_cancel.set()
        asyncio.create_task(self._run_turn(text, transcript=text))

    # ── emitter forwarding ────────────────────────────────────────────────────

    async def _emitter_loop(self, tap: asyncio.Queue) -> None:
        """Forward per-turn brain events to the client, filtered by active turn_id."""
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(tap.get(), timeout=30.0)
                except TimeoutError:
                    continue
                etype = ev.get("type")
                turn_id = ev.get("turn_id")
                if etype == "turn_start":
                    self._active_turn_id = turn_id
                # Only this session's lane — never another partner's turn, and
                # never the owner's idle inner life (no route_sid).
                if ev.get("route_sid") != self._session.session_id:
                    continue
                if etype not in _FORWARD_TYPES:
                    continue
                # Proactive results are intentionally out-of-band (they fire under a
                # bg_<turn_id> after turn_end), so they bypass the active-turn filter.
                out_of_band = etype == "proactive_speech"
                # Belt-and-suspenders: stick to the active turn within this session.
                if not out_of_band and turn_id is not None and turn_id != self._active_turn_id:
                    continue
                out_type = (
                    "proactive"
                    if etype == "proactive_speech"
                    else "thought"
                    if etype == "stream_thought"
                    else etype
                )
                await self._send({"type": out_type, **{k: v for k, v in ev.items() if k != "type"}})
                # Push path: also VOICE a proactive result when the client opted into
                # audio — out-of-band (it fires after turn_end), mirroring the turn-reply
                # audio in _run_turn. Mood comes from the [mood:X] markup the proactive
                # text still carries, so no separate affect is needed. A client can keep
                # reply audio but mute proactive audio with audio.proactive=false.
                if (
                    out_of_band
                    and isinstance(self._audio_opts, dict)
                    and self._audio_opts.get("enabled")
                    and self._audio_opts.get("proactive", True)
                ):
                    proactive_text = (ev.get("text") or "").strip()
                    if proactive_text:
                        # Clear any stale barge-in flag from an earlier turn so the
                        # out-of-band synth isn't cancelled before it starts.
                        self._tts_cancel.clear()
                        # Carry the mood the brain attached so the spoken result has the
                        # same prosody as an interactive reply, not a flat default.
                        await self._ws_stream_audio(proactive_text, ev.get("affect"), turn_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[WsSession] emitter loop error: %s", e)

    # ── turn execution ────────────────────────────────────────────────────────

    async def _run_turn(self, message: str, *, transcript: str | None) -> None:
        """Run a brain turn under the serialisation lock, then stream TTS if
        audio is enabled. Multiple concurrent callers queue behind the lock."""
        async with self._turn_lock:
            self._tts_cancel.clear()
            s = self._session
            # Multi-persona Path B: bind the session agent's persona for the turn.
            _persona = s.agent_id.split(".", 1)[0] if s.agent_id and "." in s.agent_id else None
            try:
                with bind_turn(
                    "agent",
                    session_id=s.session_id,
                    agent_id=s.agent_id,
                    end_user_id=s.end_user_id,
                    answer_only=getattr(s, "answer_only", False),
                ):
                    # This is the one engine transport that survives turn_end, so it
                    # keeps the non-blocking defer→proactive loop: a reactive tool's
                    # result arrives out-of-band as a `proactive` event (see
                    # _emitter_loop / _FORWARD_TYPES), not inline in this reply. The
                    # request/response transports (server.py) default to inline.
                    text, affect = await self._turn_runner(
                        message, s.end_user_id, s.mandate_id, _persona, inline_tools=False
                    )
            except Exception as e:
                logger.warning("[WsSession] turn error: %s", e)
                await self._send({"type": "error", "detail": str(e), "code": 500})
                return

            display, affect_block = _affect_view(text, affect)
            turn_id = self._active_turn_id
            final: dict = {
                "type": "done",
                "response": display,
                "affect": affect_block,
                "mood": _mood_from_affect(affect),
            }
            if transcript is not None:
                final["transcript"] = transcript
            pending = (affect or {}).get("pending") if isinstance(affect, dict) else None
            if pending:
                s.pending = pending
                if self._registry is not None:
                    with contextlib.suppress(Exception):
                        self._registry.update(s)
                final["confirmation"] = {
                    "required": True,
                    "description": pending.get("description") or pending.get("task"),
                }
            await self._send(final)
            self._active_turn_id = None

            if isinstance(self._audio_opts, dict) and self._audio_opts.get("enabled"):
                await self._ws_stream_audio(text, affect, turn_id)

    async def _ws_stream_audio(self, text: str, affect: dict | None, turn_id: str | None) -> None:
        """Stream TTS chunks over the WebSocket. Polls _tts_cancel between
        chunks so a barge-in can abort mid-stream without waiting for the full
        synthesis to complete."""
        if self._tts_stream_runner is None:
            await self._send(
                {
                    "type": "audio_error",
                    "turn_id": turn_id,
                    "detail": "audio is not available on this server",
                }
            )
            return

        from brain.api.audio_quota import TTS_CHARS

        partner_id = None if self._ctx.get("owner") else self._ctx.get("partner_id")
        if self._audio_quota and partner_id:
            reason = self._audio_quota.check(partner_id, TTS_CHARS)
            if reason:
                await self._send({"type": "audio_error", "turn_id": turn_id, "detail": reason})
                return

        opts = self._audio_opts
        # Default to the session persona's configured voice when the client didn't
        # pin one — same persona→voice ownership as the SSE transport, so an agent
        # session speaks in its persona's voice rather than the provider default.
        _voice_id = opts.get("voice_id")
        if not _voice_id:
            from brain.persona_chem import voice_id_for

            s = self._session
            _persona = s.agent_id.split(".", 1)[0] if s.agent_id and "." in s.agent_id else None
            _voice_id = voice_id_for(_persona)
        chars = 0
        try:
            from brain.api.audio import AudioError

            async for kind, payload in self._tts_stream_runner(
                text,
                affect=affect,
                voice_id=_voice_id,
                model=opts.get("model"),
                fmt=opts.get("format"),
                provider=opts.get("provider"),
            ):
                # Barge-in: stop streaming if new speech started.
                if self._tts_cancel.is_set():
                    await self._send(
                        {"type": "audio_end", "turn_id": turn_id, "chunks": 0, "cancelled": True}
                    )
                    return
                if kind == "end":
                    chars = payload.get("chars") or 0
                    await self._send({"type": "audio_end", "turn_id": turn_id, **payload})
                elif kind == "meta":
                    await self._send({"type": "audio_meta", "turn_id": turn_id, **payload})
                elif kind == "chunk":
                    await self._send({"type": "audio_chunk", "turn_id": turn_id, **payload})
        except AudioError as ae:
            await self._send({"type": "audio_error", "turn_id": turn_id, "detail": ae.detail})
        except Exception as e:  # noqa: BLE001 — audio is best-effort; done already sent
            logger.warning("[WsSession] TTS stream error: %s", e, exc_info=True)
            await self._send({"type": "audio_error", "turn_id": turn_id, "detail": str(e)})
        else:
            if self._audio_quota and partner_id and chars:
                with contextlib.suppress(Exception):
                    self._audio_quota.record(partner_id, TTS_CHARS, chars)

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> None:
        """Send a JSON message; best-effort (swallow disconnect errors)."""
        with contextlib.suppress(Exception):
            await self._ws.send_json(payload)


# ── module-level helpers (no server.py import) ────────────────────────────────


# Curated public affect/mood views live in brain.api._affect — one definition shared
# with the SSE transport (server.py) so the chemistry-not-exposed contract can't drift.
from brain.api._affect import affect_view as _affect_view  # noqa: E402
from brain.api._affect import mood_from_affect as _mood_from_affect  # noqa: E402
