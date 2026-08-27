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

One session per utterance: open → stream audio → EndOfTurn fires callback
with is_final=True → caller closes. The WsSession reopens a fresh session when
the next utterance begins (natural boundary or barge-in).

Configuration:
  DEEPGRAM_API_KEY          (required; 503 if absent)
  BRAIN_STT_EOT_THRESHOLD   (end-of-turn confidence, 0.5–0.9; default 0.7)
  BRAIN_STT_EOT_TIMEOUT_MS  (max silence in ms before a turn is force-ended
                             regardless of confidence; default 5000)
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
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# PCM16 sample rate the client must send.
EXPECTED_SAMPLE_RATE = 16_000

# Callback type: (text, is_final, duration_s) -> None
TranscriptCallback = Callable[[str, bool, float], Awaitable[None]]


class DeepgramLiveSession:
    """One Deepgram Flux session covering a single utterance.

    Usage::

        session = DeepgramLiveSession()
        await session.open(on_transcript)   # spawns internal reader task
        await session.send(pcm_bytes)       # call as audio arrives from client
        await session.close()              # on barge-in or after final transcript

    ``on_transcript(text, is_final, duration_s)`` is always called from an
    asyncio task — callers may safely await coroutines inside it.

    In-progress turn updates (``is_final=False``) are forwarded immediately so
    the client can show a live "hearing…" display. The final, authoritative
    transcript is sent on Flux's ``EndOfTurn`` with ``is_final=True`` and
    ``duration_s`` populated (the STT quota meter). Callers should only
    dispatch a brain turn on ``is_final=True``."""

    def __init__(self) -> None:
        self._socket_cm = None
        self._socket = None
        self._reader_task: asyncio.Task | None = None
        self._on_transcript: TranscriptCallback | None = None
        self._closed = False

    async def open(self, on_transcript: TranscriptCallback) -> None:
        """Open the Deepgram Flux session and start the internal reader task.

        Raises ``AudioError(status=503)`` if DEEPGRAM_API_KEY is not set."""
        from brain.api.audio import AudioError

        if not os.environ.get("DEEPGRAM_API_KEY"):
            raise AudioError("DEEPGRAM_API_KEY is not configured", status=503)

        self._on_transcript = on_transcript
        self._closed = False

        from deepgram import AsyncDeepgramClient

        from brain.stt_config import stt_keyterms

        client = AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

        eot_threshold = float(os.environ.get("BRAIN_STT_EOT_THRESHOLD", "0.7"))
        eot_timeout_ms = int(os.environ.get("BRAIN_STT_EOT_TIMEOUT_MS", "5000"))
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

        self._socket_cm = client.listen.v2.connect(**connect_kwargs)
        self._socket = await self._socket_cm.__aenter__()
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.debug(
            "[SttLive] session open (%s lang=%s eot_threshold=%.2f eot_timeout=%dms)",
            connect_kwargs["model"],
            language,
            eot_threshold,
            eot_timeout_ms,
        )

    async def send(self, pcm_bytes: bytes) -> None:
        """Send a raw PCM16 chunk to Deepgram. No-op after close()."""
        if self._socket is None or self._closed:
            return
        with contextlib.suppress(Exception):
            await self._socket.send_media(pcm_bytes)

    async def close(self) -> None:
        """Close the session gracefully. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if self._socket_cm is not None:
            with contextlib.suppress(Exception):
                await self._socket_cm.__aexit__(None, None, None)
        self._socket = None
        self._socket_cm = None
        logger.debug("[SttLive] session closed")

    # ── internal ──────────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Consume Flux TurnInfo events and fire on_transcript."""
        if self._socket is None:
            return
        try:
            async for message in self._socket:
                if self._closed:
                    break
                mtype = getattr(message, "type", None)

                if mtype == "TurnInfo":
                    await self._handle_turn_info(message)

                elif mtype == "Error":
                    logger.warning(
                        "[SttLive] Flux error: %s",
                        getattr(message, "description", None) or message,
                    )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[SttLive] read loop error: %s", e)

    async def _handle_turn_info(self, message) -> None:
        try:
            event = getattr(message, "event", "") or ""
            text = (getattr(message, "transcript", "") or "").strip()

            if event == "EndOfTurn":
                # eot_timeout_ms can force-end a speechless turn — nothing to
                # dispatch then.
                if not text or not self._on_transcript:
                    return
                words = getattr(message, "words", None) or []
                if words:
                    start = float(getattr(words[0], "start", 0.0) or 0.0)
                    end = float(getattr(words[-1], "end", 0.0) or 0.0)
                else:
                    start = float(getattr(message, "audio_window_start", 0.0) or 0.0)
                    end = float(getattr(message, "audio_window_end", 0.0) or 0.0)
                duration_s = round(max(0.0, end - start), 3)
                await self._on_transcript(text, True, duration_s)
                return

            # StartOfTurn / Update / EagerEndOfTurn / TurnResumed — forward the
            # in-progress transcript as a non-final UX hint.
            if text and self._on_transcript:
                await self._on_transcript(text, False, 0.0)
        except Exception as e:
            logger.debug("[SttLive] turn info parse error: %s", e)
