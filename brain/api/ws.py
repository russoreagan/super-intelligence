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
import os
import time

from brain.turn_ctx import bind_turn

logger = logging.getLogger(__name__)

from brain.log_scope import lane_text  # noqa: E402

# How long after playback stops that the words we just spoke still count as
# echo. Flux delivers EndOfTurn after its endpointing pause, so the tail of a
# bleed-through utterance arrives slightly late.
_ECHO_TAIL_S = 2.0

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
        on_speech_interrupted=None,
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
        # Brain hook: (full_text, heard_text) -> rewrite the cut-off reply in the
        # next turn's conversation history (BrainSession.note_speech_interrupted).
        self._on_speech_interrupted = on_speech_interrupted

        self._turn_lock: asyncio.Lock = asyncio.Lock()
        # Set by barge-in; passed into the synthesis loop so an interrupt aborts
        # the in-flight segment, not just the gap between segments.
        self._tts_cancel: asyncio.Event = asyncio.Event()
        self._active_turn_id: str | None = None
        self._dg_session = None  # DeepgramLiveSession | None
        self._audio_opts: dict = {}  # last audio config from client
        self._transcript_seq: int = 0  # monotonic counter for transcript frames
        # What this session is currently speaking ('' when idle) — input to the
        # echo guard, mirroring PNS.speaking_text on the server-mic path. The
        # client streams its mic through playback, so on open speakers Flux
        # transcribes our own output; without this the reply interrupts itself
        # and the echo is dispatched as a user turn.
        self._speaking_text: str = ""
        # Flux delivers EndOfTurn after its endpointing pause, so the tail of an
        # echo can land a beat AFTER playback stopped, when _speaking_text is
        # already back to ''. Keep what we just said alive for a short window so
        # that trailing echo is still recognised instead of becoming a turn.
        self._echo_tail_text: str = ""
        self._echo_tail_until: float = 0.0
        # Playback position of the reply being heard (brain.spoken_cursor). Audio
        # is generated several times faster than it plays, so "speaking" means
        # "still playing on the client", not "still synthesising". None until a
        # reply's first chunk goes out.
        self._cursor = None
        self._cursor_text: str = ""  # display text of the reply the cursor tracks
        self._cursor_turn_id: str | None = None
        self._last_heard: str = ""
        from brain.voice_bridge import parse_barge_words

        self._barge_words = parse_barge_words(os.environ.get("BRAIN_BARGE_IN_WORDS"))

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
        await self._barge_in()
        asyncio.create_task(self._run_turn(message, transcript=None))

    # ── STT transcript callback ───────────────────────────────────────────────

    def _playing(self, now: float | None = None) -> bool:
        """The client is (by estimate) still playing our reply."""
        c = self._cursor
        return c is not None and c.started_at is not None and not c.finished(now)

    def _echo_reference(self) -> str:
        """What our own playback sounds like right now: the text spoken in the
        last few seconds while the client plays it, the tail of it just after,
        or — while synthesis runs ahead of any playback — the whole reply."""
        now = time.monotonic()
        c = self._cursor
        if c is not None and c.started_at is not None:
            if c.elapsed_ms(now) < c.total_ms + _ECHO_TAIL_S * 1000:
                return c.window_text(now) or self._cursor_text
            return ""
        if self._speaking_text:
            return self._speaking_text
        if now < self._echo_tail_until:
            return self._echo_tail_text
        return ""

    def _is_tts_echo(self, text: str) -> bool:
        """True when transcribed speech is mostly the words we just spoke — the
        mic hearing our own playback on open speakers. Covers the window just
        after playback ends as well as during it."""
        reference = self._echo_reference()
        if not reference:
            return False
        from brain.voice_bridge import echo_containment, echo_containment_max

        return echo_containment(text, reference) >= echo_containment_max()

    async def _on_transcript(self, text: str, is_final: bool, duration_s: float) -> None:
        """Called from the DeepgramLiveSession reader task on each result."""
        seq = self._transcript_seq
        self._transcript_seq += 1
        payload: dict = {"type": "transcript", "text": text, "is_final": is_final, "seq": seq}
        if is_final and duration_s > 0:
            payload["duration_s"] = duration_s
        await self._send(payload)

        if not text:
            return

        if not is_final:
            # Interim result. Same barge policy the server-mic path runs
            # (brain/session_loops.py:_on_live_speech) — one implementation, so
            # the two transports can't drift. Interims land within ~300ms, so
            # the interrupt cuts mid-utterance instead of waiting for Flux's
            # EndOfTurn. The utterance still dispatches normally on the final;
            # this only stops the playback.
            from brain.voice_bridge import barge_in_mode, should_voice_interrupt

            speaking = self._playing() or bool(self._speaking_text)
            if (
                speaking
                and barge_in_mode() != "off"
                and should_voice_interrupt(
                    text, self._echo_reference(), barge_words=self._barge_words
                )
            ):
                logger.debug("[WsSession] voice barge-in — cancelling TTS: %r", lane_text(text, 60))
                await self._barge_in()
            return

        # Final. Drop our own playback rather than answering it: without this,
        # open speakers put the session in a loop where every reply transcribes
        # itself into another turn.
        if self._is_tts_echo(text):
            logger.debug("[WsSession] dropped TTS echo: %r", text[:60])
            return

        # Record STT quota on the final result (duration_s is populated).
        if duration_s > 0 and self._audio_quota and not self._ctx.get("owner"):
            from brain.api.audio_quota import STT_SECONDS

            with contextlib.suppress(Exception):
                self._audio_quota.record(self._ctx.get("partner_id"), STT_SECONDS, duration_s)

        # The Flux session stays open across turns (turn_index just increments),
        # so there is nothing to close here — tearing it down per utterance cost
        # a handshake in the gap and swallowed the first words of a fast
        # follow-up. _handle_audio_end / disconnect own the close.

        # Barge-in: cancel any in-flight TTS, then kick off a new turn.
        await self._barge_in()
        asyncio.create_task(self._run_turn(text, transcript=text))

    # ── barge-in ──────────────────────────────────────────────────────────────

    async def _barge_in(self) -> None:
        """Stop our reply: cancel synthesis, and if the client is still playing
        audio we already sent, tell it to stop and record what was heard."""
        self._tts_cancel.set()
        if self._playing():
            await self._record_interruption(notify_client=True)

    async def _record_interruption(self, *, notify_client: bool) -> str:
        """Close out the reply the cursor tracks as interrupted. Returns the text
        the user heard (by playback estimate) and hands it to the brain so the
        next turn's history holds what landed, not the whole reply."""
        c = self._cursor
        if c is None:
            return self._last_heard
        heard = c.heard_text()
        full, turn_id = self._cursor_text, self._cursor_turn_id
        self._cursor = None
        self._last_heard = heard
        # The client stops on our signal (or its own); echo of the last words
        # can still trail in.
        self._echo_tail_text = heard[-400:]
        self._echo_tail_until = time.monotonic() + _ECHO_TAIL_S
        if notify_client:
            await self._send({"type": "audio_interrupted", "turn_id": turn_id, "heard": heard})
        if self._on_speech_interrupted is not None and full:
            s = self._session
            try:
                # History is per conversation lane: amend this session's, which
                # the transcript callback (unbound) would not otherwise reach.
                with bind_turn(
                    "agent",
                    session_id=s.session_id,
                    agent_id=s.agent_id,
                    end_user_id=s.end_user_id,
                    partner_id=getattr(s, "partner_id", "") or "",
                ):
                    self._on_speech_interrupted(full, heard)
            except Exception as e:  # noqa: BLE001 — history repair is best-effort
                logger.debug("[WsSession] speech-interrupted hook failed: %s", e)
        return heard

    # ── emitter forwarding ────────────────────────────────────────────────────

    def _answer_only(self) -> bool:
        """Answer-only for this session: the org-wide switch (settings `answer_only`)
        OR the session's sticky flag. The agent-permission path is folded inside the
        turn (session_turn) and surfaces on affect["answer_only"]."""
        try:
            from brain.settings import settings as _s

            if bool(int(_s.get("answer_only", 0) or 0)):
                return True
        except Exception:
            pass
        return bool(getattr(self._session, "answer_only", False))

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
                # Guard 3 (WS): answer-only promised no background work, but a job
                # minted by an EARLIER normal turn on this session re-binds this
                # session's lane when it runs (session_turn._run_task →
                # bind_turn(session_id=origin)), so its proactive_speech /
                # task_outcome / stream_thought{from_job} carry our route_sid. Drop
                # them under the flag.
                if self._answer_only() and (
                    etype in ("proactive_speech", "task_outcome")
                    or (etype == "stream_thought" and ev.get("from_job"))
                ):
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
            self._prewarm_tts()
            try:
                await self._run_turn_locked(message, transcript=transcript)
            finally:
                # A turn that errored, answered silently or was refused audio
                # must not leave its socket open until the TTL.
                from brain.api.audio import release_prewarmed

                await release_prewarmed()

    def _prewarm_tts(self) -> None:
        """Open the reply's dialogue socket now so its handshake overlaps the
        brain turn. Only for our own synthesizer (an injected runner has its own
        transport) and only when audio is on."""
        if not (isinstance(self._audio_opts, dict) and self._audio_opts.get("enabled")):
            return
        from brain.api import audio as _audio

        if self._tts_stream_runner is not _audio.synthesize_stream:
            return
        with contextlib.suppress(Exception):
            _audio.prewarm_for(self._audio_opts, self._voice_id())

    def _voice_id(self) -> str | None:
        """The client's pinned voice, else the session persona's configured voice
        (same persona→voice ownership as the SSE transport)."""
        _voice_id = (self._audio_opts or {}).get("voice_id")
        if not _voice_id:
            from brain.persona_chem import voice_id_for

            s = self._session
            _persona = s.agent_id.split(".", 1)[0] if s.agent_id and "." in s.agent_id else None
            _voice_id = voice_id_for(_persona)
        return _voice_id

    async def _run_turn_locked(self, message: str, *, transcript: str | None) -> None:
        """The turn body (under _turn_lock, with the TTS socket prewarmed)."""
        s = self._session
        # Multi-persona Path B: bind the session agent's persona for the turn.
        _persona = s.agent_id.split(".", 1)[0] if s.agent_id and "." in s.agent_id else None
        try:
            with bind_turn(
                "agent",
                session_id=s.session_id,
                agent_id=s.agent_id,
                end_user_id=s.end_user_id,
                answer_only=self._answer_only(),
                partner_id=getattr(s, "partner_id", "") or "",
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
        # Same {elapsed_s, llm_calls} the SSE done frame and POST /turns carry.
        from brain.api._affect import turn_stats as _turn_stats

        if isinstance(affect, dict):
            final.update(_turn_stats(affect))
        if transcript is not None:
            final["transcript"] = transcript
        pending = (affect or {}).get("pending") if isinstance(affect, dict) else None
        # Guard 2 (WS): no confirmation on an answer-only session/turn.
        _ao = self._answer_only() or bool(isinstance(affect, dict) and affect.get("answer_only"))
        if pending and not _ao:
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
        """Stream TTS chunks over the WebSocket. _tts_cancel is both polled
        between segments here AND passed into the synthesis loop, so a barge-in
        aborts the segment being generated instead of paying for the rest of it.

        Publishes `text` as _speaking_text for the duration so the echo guard in
        _on_transcript knows what our own playback sounds like."""
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
        # pin one, so an agent session speaks in its persona's voice.
        _voice_id = self._voice_id()
        chars = 0
        # Markup stripped: the echo guard compares against what is SPOKEN, so
        # [mood:X] tag words must not join the comparison set.
        with contextlib.suppress(Exception):
            from brain.pns import PNS

            self._speaking_text = PNS._strip_all_tags(text)
        from brain.spoken_cursor import SpokenCursor

        cursor = SpokenCursor()
        self._cursor, self._cursor_text, self._cursor_turn_id = (
            cursor,
            self._speaking_text,
            turn_id,
        )
        sample_rate = None
        try:
            from brain.api.audio import AudioError

            async for kind, payload in self._tts_stream_runner(
                text,
                affect=affect,
                voice_id=_voice_id,
                model=opts.get("model"),
                fmt=opts.get("format"),
                provider=opts.get("provider"),
                cancel=self._tts_cancel,
            ):
                # Barge-in: stop streaming if new speech started.
                if self._tts_cancel.is_set():
                    heard = (
                        await self._record_interruption(notify_client=False)
                        if self._cursor is cursor
                        else self._last_heard
                    )
                    await self._send(
                        {
                            "type": "audio_end",
                            "turn_id": turn_id,
                            "chunks": 0,
                            "cancelled": True,
                            "heard": heard,
                        }
                    )
                    return
                if kind == "end":
                    chars = payload.get("chars") or 0
                    await self._send({"type": "audio_end", "turn_id": turn_id, **payload})
                elif kind == "meta":
                    sample_rate = payload.get("sample_rate")
                    await self._send({"type": "audio_meta", "turn_id": turn_id, **payload})
                elif kind == "chunk":
                    await self._send({"type": "audio_chunk", "turn_id": turn_id, **payload})
                    cursor.add_chunk(
                        payload.get("text") or "",
                        _chunk_ms(payload, sample_rate),
                        payload.get("alignment"),
                    )
        except AudioError as ae:
            await self._send({"type": "audio_error", "turn_id": turn_id, "detail": ae.detail})
        except Exception as e:  # noqa: BLE001 — audio is best-effort; done already sent
            logger.warning("[WsSession] TTS stream error: %s", e, exc_info=True)
            await self._send({"type": "audio_error", "turn_id": turn_id, "detail": str(e)})
        else:
            if self._audio_quota and partner_id and chars:
                with contextlib.suppress(Exception):
                    self._audio_quota.record(partner_id, TTS_CHARS, chars)
        finally:
            self._echo_tail_text = self._speaking_text
            self._echo_tail_until = time.monotonic() + _ECHO_TAIL_S
            self._speaking_text = ""

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> None:
        """Send a JSON message; best-effort (swallow disconnect errors)."""
        with contextlib.suppress(Exception):
            await self._ws.send_json(payload)


# ── module-level helpers (no server.py import) ────────────────────────────────


def _chunk_ms(payload: dict, sample_rate: int | None) -> float:
    """Playback length of one audio_chunk: exact for PCM, else estimated from
    the text it speaks (~65 ms a character)."""
    data = payload.get("data") or ""
    if sample_rate and data:
        return (len(data) * 3 // 4) / (2 * sample_rate) * 1000
    return len(payload.get("text") or "") * 65.0


# Curated public affect/mood views live in brain.api._affect — one definition shared
# with the SSE transport (server.py) so the chemistry-not-exposed contract can't drift.
from brain.api._affect import affect_view as _affect_view  # noqa: E402
from brain.api._affect import mood_from_affect as _mood_from_affect  # noqa: E402
