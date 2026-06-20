"""
Deepgram live-transcription session for the engine API WebSocket path.

Decoupled from sounddevice (streaming_mic.py handles the server mic). This
module owns the STT side of the realtime WS transport: open a Deepgram live
session, pipe raw PCM16 16 kHz audio in, fire a callback on each result
(interim for UX and final for turn dispatch).

One session per utterance: open → stream audio → UtteranceEnd fires callback
with is_final=True → caller closes. The WsSession reopens a fresh session when
the next utterance begins (natural boundary or barge-in).

Configuration uses the same env vars as streaming_mic.py:
  DEEPGRAM_API_KEY            (required; 503 if absent)
  BRAIN_STT_ENDPOINTING_MS    (silence before utterance ends; default 500)
  BRAIN_STT_UTTERANCE_END_MS  (grace after endpointing; default 1200)
  BRAIN_STT_LANGUAGE          (hint; default 'en')
  BRAIN_STT_KEYWORDS          (comma-separated word:boost pairs)
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
    """One Deepgram live session covering a single utterance.

    Usage::

        session = DeepgramLiveSession()
        await session.open(on_transcript)   # spawns internal reader task
        await session.send(pcm_bytes)       # call as audio arrives from client
        await session.close()              # on barge-in or after final transcript

    ``on_transcript(text, is_final, duration_s)`` is always called from an
    asyncio task — callers may safely await coroutines inside it.

    Interim results (``is_final=False``) are forwarded immediately so the client
    can show a live "hearing…" display. The final, authoritative transcript is
    sent on ``UtteranceEnd`` with ``is_final=True`` and ``duration_s`` populated
    (the STT quota meter). Callers should only dispatch a brain turn on
    ``is_final=True``."""

    def __init__(self) -> None:
        self._socket_cm = None
        self._socket = None
        self._reader_task: asyncio.Task | None = None
        self._on_transcript: TranscriptCallback | None = None
        self._pending_words: list[dict] = []
        self._utterance_start_s: float | None = None
        self._closed = False

    async def open(self, on_transcript: TranscriptCallback) -> None:
        """Open the Deepgram live session and start the internal reader task.

        Raises ``AudioError(status=503)`` if DEEPGRAM_API_KEY is not set."""
        from brain.api.audio import AudioError

        if not os.environ.get("DEEPGRAM_API_KEY"):
            raise AudioError("DEEPGRAM_API_KEY is not configured", status=503)

        self._on_transcript = on_transcript
        self._closed = False

        from deepgram import AsyncDeepgramClient

        client = AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

        endpointing_ms = int(os.environ.get("BRAIN_STT_ENDPOINTING_MS", "500"))
        utterance_end_ms = int(os.environ.get("BRAIN_STT_UTTERANCE_END_MS", "1200"))
        language = (os.environ.get("BRAIN_STT_LANGUAGE") or "en").strip() or "en"
        keywords_raw = os.environ.get("BRAIN_STT_KEYWORDS", "")
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        connect_kwargs: dict = {
            "model": "nova-3",
            "encoding": "linear16",
            "sample_rate": EXPECTED_SAMPLE_RATE,
            "channels": 1,
            "interim_results": True,
            "vad_events": True,
            "utterance_end_ms": utterance_end_ms,
            "endpointing": endpointing_ms,
            "punctuate": True,
            "smart_format": True,
            "language": language,
        }
        if keywords:
            connect_kwargs["keyterm"] = [k.split(":")[0] for k in keywords]

        self._socket_cm = client.listen.v1._raw_client.connect(**connect_kwargs)
        self._socket = await self._socket_cm.__aenter__()
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.debug(
            "[SttLive] session open (nova-3 lang=%s endpointing=%dms utterance_end=%dms)",
            language,
            endpointing_ms,
            utterance_end_ms,
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
        """Consume Deepgram events and fire on_transcript."""
        if self._socket is None:
            return
        try:
            async for message in self._socket:
                if self._closed:
                    break
                mtype = getattr(message, "type", None)

                if mtype == "Results":
                    await self._handle_results(message)

                elif mtype == "UtteranceEnd":
                    await self._handle_utterance_end(message)

                elif mtype == "SpeechStarted":
                    ts = float(getattr(message, "timestamp", 0.0))
                    if self._utterance_start_s is None:
                        self._utterance_start_s = ts

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[SttLive] read loop error: %s", e)

    async def _handle_results(self, message) -> None:
        is_final = bool(getattr(message, "is_final", False))
        try:
            channel = getattr(message, "channel", None)
            alts = (getattr(channel, "alternatives", None) or []) if channel else []
            if not alts:
                return
            alt = alts[0]
            text = (getattr(alt, "transcript", "") or "").strip()
            if not text:
                return
            if is_final:
                # Accumulate words for the final UtteranceEnd assembly.
                for w in getattr(alt, "words", None) or []:
                    word = getattr(w, "word", "") or getattr(w, "punctuated_word", "") or ""
                    self._pending_words.append(
                        {
                            "word": word,
                            "start": float(getattr(w, "start", 0.0)),
                            "end": float(getattr(w, "end", 0.0)),
                        }
                    )
            # Forward all results (interim and final segment) as non-final UX
            # hints. The authoritative is_final=True fires on UtteranceEnd.
            if self._on_transcript:
                await self._on_transcript(text, False, 0.0)
        except Exception as e:
            logger.debug("[SttLive] results parse error: %s", e)

    async def _handle_utterance_end(self, message) -> None:
        words = self._pending_words
        if not words:
            return
        last_end = float(getattr(message, "last_word_end", 0.0))
        start = (
            self._utterance_start_s
            if self._utterance_start_s is not None
            else words[0].get("start", 0.0)
        )
        text = " ".join(w["word"] for w in words if w.get("word")).strip()
        duration_s = round(max(0.0, last_end - float(start)), 3)
        self._pending_words = []
        self._utterance_start_s = None
        if text and self._on_transcript:
            await self._on_transcript(text, True, duration_s)
