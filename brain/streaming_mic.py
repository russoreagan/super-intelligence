"""
StreamingMicSession — continuous mic capture + Deepgram websocket streaming.

Replaces the press-to-talk mic_listen() with an always-on listener that:
  - streams 16 kHz PCM from sounddevice to Deepgram's live API
  - uses Deepgram VAD (SpeechStarted + UtteranceEnd) to segment utterances
  - on each completed utterance: publishes auditory.raw_audio + auditory.diarized_audio
    (so the auditory cortex pipeline runs unchanged) and pushes the transcript into
    a queue the run loop awaits
  - on SpeechStarted *while the entity is speaking*: calls on_user_interrupt()
    so the TTS playback aborts immediately ("barge-in")

The PCM corresponding to each utterance is sliced from a rolling buffer using
Deepgram's absolute timestamps, so the audio sent to the cortex exactly matches
the words Deepgram returned.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2  # int16
BLOCKSIZE = 1600       # 100 ms per callback at 16 kHz


class _RollingPCM:
    """Rolling int16 PCM buffer with absolute timestamps for stream-time slicing."""

    def __init__(self, sample_rate: int, max_seconds: float = 60.0) -> None:
        self._sr = sample_rate
        self._max_bytes = int(sample_rate * BYTES_PER_SAMPLE * max_seconds)
        self._buf = bytearray()
        self._discarded = 0   # bytes dropped off the front

    def append(self, chunk: bytes) -> None:
        self._buf.extend(chunk)
        if len(self._buf) > self._max_bytes:
            cut = len(self._buf) - self._max_bytes
            del self._buf[:cut]
            self._discarded += cut

    def slice(self, start_s: float, end_s: float) -> bytes:
        start_byte = int(start_s * self._sr * BYTES_PER_SAMPLE)
        end_byte = int(end_s * self._sr * BYTES_PER_SAMPLE)
        rel_start = max(0, start_byte - self._discarded)
        rel_end = max(0, end_byte - self._discarded)
        if rel_end <= rel_start:
            return b""
        return bytes(self._buf[rel_start:rel_end])


class StreamingMicSession:
    """Continuous mic → Deepgram → utterance queue, with barge-in detection."""

    def __init__(
        self,
        bus,
        is_speaking_fn: Callable[[], bool],
        on_user_interrupt: Optional[Callable[[], None]] = None,
    ) -> None:
        self._bus = bus
        self._is_speaking_fn = is_speaking_fn
        self._on_user_interrupt = on_user_interrupt

        self.utterances: asyncio.Queue = asyncio.Queue()

        self._pcm_in: asyncio.Queue = asyncio.Queue(maxsize=200)  # mic → pumper
        self._pcm_buffer = _RollingPCM(SAMPLE_RATE, max_seconds=60.0)
        self._pending_words: list[dict] = []
        self._utterance_start_s: Optional[float] = None

        self._stream = None             # sounddevice InputStream
        self._socket = None             # Deepgram AsyncV1SocketClient
        self._socket_cm = None          # async context manager
        self._pumper_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._muted = False             # when True, mic audio is discarded

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._main_loop = asyncio.get_running_loop()

        # 1. Open Deepgram websocket (initial)
        await self._open_deepgram()

        # 2. Start mic input stream (sounddevice callback runs in PortAudio thread)
        import sounddevice as sd

        def _audio_callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                logger.debug("[StreamingMic] PortAudio status: %s", status)
            # indata is a numpy int16 array, shape (frames, channels)
            chunk = bytes(indata)
            # Thread-safe handoff: schedule put_nowait on the asyncio loop
            try:
                self._main_loop.call_soon_threadsafe(self._enqueue_chunk, chunk)
            except RuntimeError:
                pass  # loop closed during shutdown

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCKSIZE,
            callback=_audio_callback,
        )
        self._stream.start()

        # 3. Launch pumper + reader supervisor
        self._running = True
        self._pumper_task = asyncio.create_task(self._pump_loop())
        self._reader_task = asyncio.create_task(self._reader_supervisor())

    async def _open_deepgram(self) -> None:
        """Open a fresh Deepgram WebSocket session."""
        # AsyncDeepgramClient.listen.v1._raw_client.connect is @asynccontextmanager.
        # The sync DeepgramClient.listen.v1.connect is sync-only — wrong for asyncio.
        from deepgram import AsyncDeepgramClient
        client = AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
        self._socket_cm = client.listen.v1._raw_client.connect(
            model="nova-3",
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            interim_results=True,
            vad_events=True,
            utterance_end_ms=1000,
            endpointing=300,
            punctuate=True,
            smart_format=True,
            diarize=True,
        )
        self._socket = await self._socket_cm.__aenter__()
        logger.info("[StreamingMic] Deepgram live session open (nova-3, VAD on)")

    async def _close_deepgram(self) -> None:
        """Close the current Deepgram session (best-effort)."""
        if self._socket_cm is not None:
            try:
                await self._socket_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._socket = None
        self._socket_cm = None

    async def _reader_supervisor(self) -> None:
        """Run _read_loop in a reconnect loop. If Deepgram drops the WS
        (1011 timeout, network blip, etc.), close cleanly and re-open."""
        backoff = 0.5
        while self._running:
            try:
                await self._read_loop()
                # Read loop returned cleanly (socket closed). If we're still
                # running, that means Deepgram disconnected — reconnect.
                if not self._running:
                    return
                logger.warning("[StreamingMic] Deepgram session ended — reconnecting...")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("[StreamingMic] read loop crashed — reconnecting in %.1fs: %s",
                             backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)

            await self._close_deepgram()
            try:
                await self._open_deepgram()
                backoff = 0.5  # reset after a successful open
            except Exception as e:
                logger.error("[StreamingMic] Deepgram reconnect failed (will retry): %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)

    async def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        for t in (self._pumper_task, self._reader_task):
            if t is not None:
                t.cancel()
        for t in (self._pumper_task, self._reader_task):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        await self._close_deepgram()
        logger.info("[StreamingMic] session closed")

    @property
    def is_muted(self) -> bool:
        return self._muted

    def mute(self) -> None:
        """Discard mic audio until unmute(). Socket stays warm — no reconnect needed."""
        if not self._muted:
            self._muted = True
            logger.info("[StreamingMic] Muted")

    def unmute(self) -> None:
        if self._muted:
            self._muted = False
            logger.info("[StreamingMic] Unmuted")

    def toggle_mute(self) -> bool:
        """Toggle mute state. Returns new is_muted value."""
        if self._muted:
            self.unmute()
        else:
            self.mute()
        return self._muted

    async def next_utterance(self) -> dict:
        """Await the next completed utterance. Returns dict with transcript/audio/words."""
        return await self.utterances.get()

    # ── internal: mic side ─────────────────────────────────────────────────

    def _enqueue_chunk(self, chunk: bytes) -> None:
        # Called on the main asyncio loop via call_soon_threadsafe.
        # When muted we still enqueue *silence* so Deepgram's WS doesn't time
        # out — Deepgram drops the connection after ~10s of no audio, and we
        # have no graceful way to recover mid-conversation.
        if self._muted:
            chunk = b"\x00" * len(chunk)
        try:
            self._pcm_in.put_nowait(chunk)
        except asyncio.QueueFull:
            # Drop oldest if the pumper is falling behind (network blip)
            try:
                self._pcm_in.get_nowait()
                self._pcm_in.put_nowait(chunk)
            except Exception:
                pass

    async def _pump_loop(self) -> None:
        """Pull PCM from mic queue → send to Deepgram + append to rolling buffer.
        Drops chunks silently when the WS is being recycled by the supervisor
        (avoids flooding logs during a reconnect window)."""
        last_warn_ts = 0.0
        consecutive_failures = 0
        try:
            while self._running:
                chunk = await self._pcm_in.get()
                self._pcm_buffer.append(chunk)
                if self._socket is not None:
                    try:
                        await self._socket.send_media(chunk)
                        consecutive_failures = 0
                    except Exception as e:
                        consecutive_failures += 1
                        # Throttle: warn at most once per 5s during a reconnect storm
                        import time as _time
                        now = _time.time()
                        if now - last_warn_ts > 5.0:
                            logger.warning("[StreamingMic] send_media failed "
                                           "(%d consecutive): %s",
                                           consecutive_failures, e)
                            last_warn_ts = now
                        await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    # ── internal: Deepgram side ────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Consume Deepgram events: SpeechStarted, Results, UtteranceEnd."""
        try:
            async for message in self._socket:
                mtype = getattr(message, "type", None)

                if mtype == "SpeechStarted":
                    ts = float(getattr(message, "timestamp", 0.0))
                    self._utterance_start_s = ts
                    self._pending_words = []
                    logger.debug("[StreamingMic] SpeechStarted @ %.2fs", ts)
                    # Barge-in: if the entity is mid-speech, cut it off
                    if self._is_speaking_fn() and self._on_user_interrupt is not None:
                        try:
                            self._on_user_interrupt()
                        except Exception as e:
                            logger.warning("[StreamingMic] interrupt callback raised: %s", e)

                elif mtype == "Results":
                    if not getattr(message, "is_final", False):
                        continue
                    # Accumulate words from the final alternative for this segment
                    try:
                        channel = getattr(message, "channel", None)
                        alts = getattr(channel, "alternatives", []) if channel else []
                        if alts:
                            alt = alts[0]
                            words = getattr(alt, "words", []) or []
                            for w in words:
                                self._pending_words.append({
                                    "word": getattr(w, "word", "") or getattr(w, "punctuated_word", ""),
                                    "start": float(getattr(w, "start", 0.0)),
                                    "end": float(getattr(w, "end", 0.0)),
                                    "speaker": int(getattr(w, "speaker", 0) or 0),
                                    "speaker_confidence": float(getattr(w, "speaker_confidence", 1.0) or 1.0),
                                })
                    except Exception as e:
                        logger.debug("[StreamingMic] results parse error: %s", e)

                elif mtype == "UtteranceEnd":
                    last_end = float(getattr(message, "last_word_end", 0.0))
                    start = self._utterance_start_s if self._utterance_start_s is not None else (
                        self._pending_words[0]["start"] if self._pending_words else last_end
                    )
                    transcript = " ".join(w["word"] for w in self._pending_words if w["word"]).strip()
                    audio_bytes = self._pcm_buffer.slice(start, last_end)
                    diarized = list(self._pending_words)

                    # Reset for next utterance
                    self._utterance_start_s = None
                    self._pending_words = []

                    if not transcript and not audio_bytes:
                        continue

                    logger.info("[StreamingMic] utterance: %r (%.2fs, %d words, %d bytes)",
                                transcript[:60], last_end - start, len(diarized), len(audio_bytes))

                    # Feed the auditory cortex pipeline (prosody + speaker ID + dynamics)
                    if audio_bytes:
                        await self._bus.publish_dict(
                            "auditory.raw_audio",
                            {
                                "audio_bytes": audio_bytes,
                                "sample_rate": SAMPLE_RATE,
                                "duration_s": float(last_end - start),
                                "channels": CHANNELS,
                                "dtype": "int16",
                            },
                            source="pns",
                        )
                        await self._bus.publish_dict(
                            "auditory.diarized_audio",
                            {
                                "audio_bytes": audio_bytes,
                                "sample_rate": SAMPLE_RATE,
                                "duration_s": float(last_end - start),
                                "dtype": "int16",
                                "diarized_words": diarized,
                                "transcript": transcript,
                            },
                            source="pns",
                        )

                    # Hand the utterance to whoever's awaiting next_utterance()
                    await self.utterances.put({
                        "transcript": transcript,
                        "audio_bytes": audio_bytes,
                        "sample_rate": SAMPLE_RATE,
                        "diarized_words": diarized,
                        "duration_s": float(last_end - start),
                    })

                elif mtype == "Metadata":
                    logger.debug("[StreamingMic] Metadata: %s",
                                 getattr(message, "request_id", "?"))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[StreamingMic] read loop crashed — voice input is offline: %s", e)
