"""
Realtime WebSocket session handler for the engine API.

``WsSession`` manages a single persistent WebSocket connection covering the full
lifetime of an API session. It replaces the per-turn SSE round-trip with a
bidirectional transport that supports:

  - Streaming audio IN (PCM16 chunks) → Deepgram live STT → interim transcripts
    forwarded to the client → full utterance triggers a brain turn
  - Brain inner-life events (thoughts, mood, neuromod) forwarded over the socket,
    filtered to the active turn_id
  - Streaming audio OUT (TTS chunks via synthesize_stream) on the same connection
  - Barge-in: audio arriving while TTS is playing cancels the in-flight synthesis
    and opens a fresh STT session for the new utterance

Protocol: see the plan / API reference. All messages are JSON. Audio data is
base64-encoded in the JSON payload (consistent with the existing audio_chunk
SSE shape).

This module never imports from brain.api.server to avoid circular imports.
Helper functions (_affect_view, _mood_from_affect) are inlined here.
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
    {"turn_start", "stream_thought", "neuromod", "hormonal", "emotion", "user_emotion"}
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
                # Belt-and-suspenders: stick to the active turn within this session.
                if turn_id is not None and turn_id != self._active_turn_id:
                    continue
                out_type = "thought" if etype == "stream_thought" else etype
                await self._send({"type": out_type, **{k: v for k, v in ev.items() if k != "type"}})
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
                ):
                    text, affect = await self._turn_runner(
                        message, s.end_user_id, s.mandate_id, _persona
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
        chars = 0
        try:
            from brain.api.audio import AudioError

            async for kind, payload in self._tts_stream_runner(
                text,
                affect=affect,
                voice_id=opts.get("voice_id"),
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


def _affect_view(text: str, affect: dict | None) -> tuple[str, dict]:
    try:
        from brain.api.audio import affect_view

        return affect_view(text, affect)
    except Exception:
        return text, {"base_tag": None, "segments": []}


def _mood_from_affect(affect: dict | None) -> dict:
    affect = affect or {}
    mood: dict = {"emotion": affect.get("emotion", "neutral")}
    if affect.get("user_emotion"):
        mood["user_emotion"] = affect["user_emotion"]
    if isinstance(affect.get("hormonal"), dict):
        mood["hormonal"] = affect["hormonal"]
    return mood
