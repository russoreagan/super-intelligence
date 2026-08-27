"""
Deepgram Flux live-transcription session for the engine API WebSocket path.

Uses Listen v2 (Flux) — Deepgram's conversational STT with model-based
end-of-turn detection. Flux replaces the hand-rolled Listen v1 turn logic
this module used to carry (endpointing + utterance_end_ms tuning, per-word
accumulation across Results, transcript assembly on UtteranceEnd): the model
decides when the turn is over and delivers the complete, punctuated
transcript in a single EndOfTurn event.

The server-mic path (brain/streaming_mic.py) intentionally stays on Listen
v1/nova-3: it needs per-word speaker diarization for the auditory-cortex
speaker-ID pipeline, and Listen v2 does not support diarization.

Decoupled from sounddevice (streaming_mic.py handles the server mic). This
module owns the STT side of the realtime WS transport: open a Deepgram live
session, pipe raw PCM16 16 kHz audio in, fire a callback on each result
(interim for UX and final for turn dispatch).

ONE SESSION, MANY TURNS. Flux's state machine is multi-turn on a single
connection — ``turn_index`` increments immediately after each ``EndOfTurn``
and the machine returns to its initial state, ready for the next turn. This
module used to close after every utterance and let the caller reopen; that
bought nothing and cost a full WSS handshake in the gap between turns, during
which ``send()`` silently discarded audio — so the first syllables of a fast
follow-up were dropped. The session now lives until ``close()``.

Because the session is long-lived, a dropped socket no longer self-heals via
the next utterance's reopen: ``_supervisor`` owns reconnection. While audio is
actively flowing it reconnects with backoff; if the client has gone quiet it
goes dormant instead of churning connections, and the next ``send()`` reopens
lazily.

Configuration:
  DEEPGRAM_API_KEY          (required; 503 if absent)
  BRAIN_STT_EOT_THRESHOLD   (end-of-turn confidence, 0.5–0.9; default 0.7)
  BRAIN_STT_EOT_TIMEOUT_MS  (max silence in ms before a turn is force-ended
                             regardless of confidence; default 3000. Deepgram's
                             own default is 5000, tuned for dictation; a
                             conversational agent wants to stop waiting sooner)
  BRAIN_STT_LANGUAGE        ('en' → flux-general-en; any other code →
                             flux-general-multi with that language_hint)
  BRAIN_STT_KEYWORDS        (comma-separated word:boost pairs; boosts are
                             stripped to plain keyterm phrases; default
                             shared with streaming_mic via brain/stt_config.py)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# PCM16 sample rate the client must send.
EXPECTED_SAMPLE_RATE = 16_000

# Audio must have arrived within this window for a dropped socket to be worth
# reconnecting eagerly. Past it the client is idle, so we go dormant and let the
# next send() reopen — otherwise a quiet client churns a connection every few
# seconds for as long as it stays connected.
_ACTIVE_WINDOW_S = 10.0

_RECONNECT_BACKOFF_START_S = 0.5
_RECONNECT_BACKOFF_MAX_S = 8.0

# Callback type: (text, is_final, duration_s) -> None
TranscriptCallback = Callable[[str, bool, float], Awaitable[None]]


class DeepgramLiveSession:
    """One Deepgram Flux connection covering a whole conversation.

    Usage::

        session = DeepgramLiveSession()
        await session.open(on_transcript)   # spawns the supervisor task
        await session.send(pcm_bytes)       # call as audio arrives from client
        await session.close()               # on client audio_end / disconnect

    ``on_transcript(text, is_final, duration_s)`` is always called from an
    asyncio task — callers may safely await coroutines inside it.

    In-progress turn updates (``is_final=False``) are forwarded immediately so
    the client can show a live "hearing…" display, and drive interim barge-in.
    The final, authoritative transcript is sent on Flux's ``EndOfTurn`` with
    ``is_final=True`` and ``duration_s`` populated (the STT quota meter).
    Callers should only dispatch a brain turn on ``is_final=True``."""

    def __init__(self) -> None:
        self._socket_cm = None
        self._socket = None
        self._supervisor_task: asyncio.Task | None = None
        self._on_transcript: TranscriptCallback | None = None
        self._closed = False
        # Guards concurrent (re)connects between send() and the supervisor.
        self._connect_lock = asyncio.Lock()
        self._last_media_ts: float = 0.0
        self._dormant = False
        # Bytes of audio fed since the last EndOfTurn. This is the STT quota
        # meter: audio length is the unit Deepgram bills, and the unit the batch
        # path meters (brain/api/audio.py reads metadata.duration). Measuring the
        # word span instead undercounted every turn's leading/trailing silence,
        # so the same clip billed differently depending on transport.
        self._turn_bytes: int = 0

    async def open(self, on_transcript: TranscriptCallback) -> None:
        """Connect to Deepgram Flux and start the supervisor task.

        Raises ``AudioError(status=503)`` if DEEPGRAM_API_KEY is not set."""
        from brain.api.audio import AudioError

        if not os.environ.get("DEEPGRAM_API_KEY"):
            raise AudioError("DEEPGRAM_API_KEY is not configured", status=503)

        self._on_transcript = on_transcript
        self._closed = False
        self._dormant = False
        await self._connect()
        self._supervisor_task = asyncio.create_task(self._supervisor())

    async def send(self, pcm_bytes: bytes) -> None:
        """Send a raw PCM16 chunk to Deepgram. No-op after close().

        Reopens the connection first if the supervisor parked it while the
        client was idle — this is the lazy half of the reconnect strategy."""
        if self._closed:
            return
        self._last_media_ts = time.monotonic()
        if self._socket is None and self._dormant:
            # Clear the flag FIRST: a second concurrent send must not race in and
            # spawn a second supervisor onto the same socket.
            self._dormant = False
            try:
                await self._connect()
                self._supervisor_task = asyncio.create_task(self._supervisor())
            except Exception as e:
                logger.warning("[SttLive] lazy reopen failed: %s", e)
                self._dormant = True  # try again on the next chunk
                return
        if self._socket is None:
            return
        self._turn_bytes += len(pcm_bytes)
        with contextlib.suppress(Exception):
            await self._socket.send_media(pcm_bytes)

    async def close(self) -> None:
        """Close the session gracefully. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._supervisor_task is not None and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor_task
        self._supervisor_task = None
        await self._disconnect()
        logger.debug("[SttLive] session closed")

    # ── internal: connection ──────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open the Deepgram Flux socket. Caller owns supervisor lifecycle."""
        async with self._connect_lock:
            if self._socket is not None or self._closed:
                return

            from deepgram import AsyncDeepgramClient

            from brain.stt_config import stt_keyterms

            client = AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

            eot_threshold = float(os.environ.get("BRAIN_STT_EOT_THRESHOLD", "0.7"))
            eot_timeout_ms = int(os.environ.get("BRAIN_STT_EOT_TIMEOUT_MS", "3000"))
            language = (os.environ.get("BRAIN_STT_LANGUAGE") or "en").strip() or "en"
            keyterms = stt_keyterms()

            connect_kwargs: dict = {
                "encoding": "linear16",
                "sample_rate": EXPECTED_SAMPLE_RATE,
                "eot_threshold": eot_threshold,
                "eot_timeout_ms": eot_timeout_ms,
                "numerals": "true",  # "five" → "5"
            }
            # flux-general-en is English-only; other languages go through the
            # multilingual model with the configured language as a hint.
            if language == "en":
                connect_kwargs["model"] = "flux-general-en"
            else:
                connect_kwargs["model"] = "flux-general-multi"
                connect_kwargs["language_hint"] = language
            if keyterms:
                connect_kwargs["keyterm"] = keyterms

            socket_cm = client.listen.v2.connect(**connect_kwargs)
            self._socket = await socket_cm.__aenter__()
            self._socket_cm = socket_cm
            logger.debug(
                "[SttLive] session open (%s lang=%s eot_threshold=%.2f eot_timeout=%dms)",
                connect_kwargs["model"],
                language,
                eot_threshold,
                eot_timeout_ms,
            )

    async def _disconnect(self) -> None:
        """Tear the socket down without ending the session."""
        socket_cm, self._socket_cm = self._socket_cm, None
        self._socket = None
        if socket_cm is not None:
            with contextlib.suppress(Exception):
                await socket_cm.__aexit__(None, None, None)

    # ── internal: supervisor ──────────────────────────────────────────────────

    async def _supervisor(self) -> None:
        """Run _read_loop, reconnecting when Deepgram drops the socket.

        Without this, a persistent session that lost its socket would go silent
        for the rest of the connection: send() suppresses its exceptions, so the
        caller would see no audio, no transcripts and no error. Goes dormant
        rather than reconnecting when the client has stopped sending audio."""
        backoff = _RECONNECT_BACKOFF_START_S
        while not self._closed:
            try:
                await self._read_loop()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[SttLive] read loop crashed: %s", e)

            if self._closed:
                return

            await self._disconnect()

            idle_for = time.monotonic() - self._last_media_ts
            if idle_for > _ACTIVE_WINDOW_S:
                # Client isn't speaking — park instead of churning connections.
                # send() reopens on the next chunk.
                self._dormant = True
                logger.debug(
                    "[SttLive] socket ended after %.0fs idle — dormant until next audio", idle_for
                )
                return

            logger.warning("[SttLive] Flux session ended mid-conversation — reconnecting...")
            while not self._closed:
                try:
                    await self._connect()
                    backoff = _RECONNECT_BACKOFF_START_S
                    break
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error("[SttLive] Flux reconnect failed (%.1fs backoff): %s", backoff, e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_S)

    async def _read_loop(self) -> None:
        """Consume Flux TurnInfo events and fire on_transcript. Returns when the
        socket ends; the supervisor decides whether to reconnect."""
        socket = self._socket
        if socket is None:
            return
        async for message in socket:
            if self._closed:
                return
            mtype = getattr(message, "type", None)

            if mtype == "TurnInfo":
                await self._handle_turn_info(message)

            elif mtype == "Error":
                logger.warning(
                    "[SttLive] Flux error: %s",
                    getattr(message, "description", None) or message,
                )

    async def _handle_turn_info(self, message) -> None:
        try:
            event = getattr(message, "event", "") or ""
            text = (getattr(message, "transcript", "") or "").strip()

            if event == "EndOfTurn":
                # Meter the audio fed for this turn regardless of whether there
                # is a transcript — eot_timeout_ms can force-end a speechless
                # turn, and that audio still cost us.
                duration_s = round(self._turn_bytes / (EXPECTED_SAMPLE_RATE * 2), 3)
                self._turn_bytes = 0
                if not text or not self._on_transcript:
                    return
                await self._on_transcript(text, True, duration_s)
                return

            # StartOfTurn / Update / EagerEndOfTurn / TurnResumed — forward the
            # in-progress transcript as a non-final hint. The caller uses it for
            # the live "hearing…" display and for interim barge-in.
            #
            # Not yet wired: eager_eot_threshold, which turns EagerEndOfTurn into
            # a speculative-drafting signal (cancelled by TurnResumed) and cuts
            # hundreds of ms of end-to-end latency. Deferred deliberately — it
            # raises LLM calls 50-70% per Deepgram, so it needs a cost baseline
            # first. See docs/V3_CONVERSATIONAL_SPIKE.md.
            if text and self._on_transcript:
                await self._on_transcript(text, False, 0.0)
        except Exception as e:
            logger.debug("[SttLive] turn info parse error: %s", e)
