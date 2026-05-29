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
import contextlib
import logging
import os
from collections.abc import Callable

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
        on_user_interrupt: Callable[[], None] | None = None,
    ) -> None:
        self._bus = bus
        self._is_speaking_fn = is_speaking_fn
        self._on_user_interrupt = on_user_interrupt

        self.utterances: asyncio.Queue = asyncio.Queue()

        self._pcm_in: asyncio.Queue = asyncio.Queue(maxsize=200)  # mic → pumper
        self._pcm_buffer = _RollingPCM(SAMPLE_RATE, max_seconds=60.0)
        self._pending_words: list[dict] = []
        self._utterance_start_s: float | None = None

        self._stream = None             # sounddevice InputStream
        self._socket = None             # Deepgram AsyncV1SocketClient
        self._socket_cm = None          # async context manager
        self._pumper_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._device_monitor_task: asyncio.Task | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._muted = True              # start muted; user must explicitly unmute
        # Push-to-talk release grace: on key-up we keep the feed live briefly so
        # Deepgram can deliver trailing final Results before we finalize + mute.
        self._ptt_release_grace_s = float(os.environ.get("BRAIN_PTT_RELEASE_GRACE_MS", "350")) / 1000.0
        self._current_device_id = None  # track active device for change detection
        self._device_listener_active = False  # CoreAudio listener status

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._main_loop = asyncio.get_running_loop()

        # 1. Open Deepgram websocket (initial)
        await self._open_deepgram()

        # 2. Start mic input stream (sounddevice callback runs in PortAudio thread)
        import sounddevice as sd

        self._current_device_id = sd.default.device[0]  # track which device we're using

        def _audio_callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                logger.debug("[StreamingMic] PortAudio status: %s", status)
            # indata is a numpy int16 array, shape (frames, channels)
            chunk = bytes(indata)
            # Thread-safe handoff: schedule put_nowait on the asyncio loop
            with contextlib.suppress(RuntimeError):
                self._main_loop.call_soon_threadsafe(self._enqueue_chunk, chunk)

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCKSIZE,
            callback=_audio_callback,
        )
        self._stream.start()

        # 3. Register for macOS CoreAudio device change notifications
        self._setup_coreaudio_notifications()

        # 4. Launch pumper + reader supervisor + keepalive + device monitor
        self._running = True
        self._pumper_task = asyncio.create_task(self._pump_loop())
        self._reader_task = asyncio.create_task(self._reader_supervisor())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._device_monitor_task = asyncio.create_task(self._monitor_device_changes())

    async def _open_deepgram(self) -> None:
        """Open a fresh Deepgram WebSocket session.

        Tunables (via env vars) for handling noisy environments:
          BRAIN_STT_ENDPOINTING_MS   — silence (ms) before utterance ends.
                                       Default 800 (was 300; 300 cut sentences
                                       mid-pause for natural-paced speakers).
          BRAIN_STT_UTTERANCE_END_MS — additional grace period after endpointing
                                       before emitting UtteranceEnd. Default 1500.
          BRAIN_STT_LANGUAGE         — language hint (default 'en'). Without this
                                       Deepgram auto-detects, which is less
                                       accurate with background noise.
          BRAIN_STT_KEYWORDS         — comma-separated word:boost pairs to bias
                                       transcription toward expected vocabulary,
                                       e.g. 'claude:5,chloé:3,ableton:5'.
        """
        from deepgram import AsyncDeepgramClient
        client = AsyncDeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

        # Trade-off: high endpointing = catches whole sentences (no fragments)
        # but adds latency before each utterance reaches the brain. Voice bridge
        # joins queued utterances during TTS, so under-endpointing during TTS is
        # recoverable. 500ms is a compromise between 300 (fragments) and 800
        # (laggy turn-around). Tune per user via env.
        endpointing_ms = int(os.environ.get("BRAIN_STT_ENDPOINTING_MS", "500"))
        utterance_end_ms = int(os.environ.get("BRAIN_STT_UTTERANCE_END_MS", "1200"))
        language = os.environ.get("BRAIN_STT_LANGUAGE", "en").strip() or "en"
        keywords_raw = os.environ.get(
            "BRAIN_STT_KEYWORDS",
            "claude:5,chloé:3,ableton:5,imessage:3,github:3,ollama:3,deepgram:3,elevenlabs:3",
        )
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        connect_kwargs = {
            "model": "nova-3",
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "interim_results": True,
            "vad_events": True,
            "utterance_end_ms": utterance_end_ms,
            "endpointing": endpointing_ms,
            "punctuate": True,
            "smart_format": True,
            "diarize": True,
            "language": language,
            "numerals": True,        # "five" → "5"
        }
        # nova-3 uses `keyterm` (whole phrases, no boost number);
        # older models use `keywords` (word:boost pairs). Try keyterm first
        # for nova-3 — strip any :boost suffix.
        if keywords:
            keyterms = [k.split(":")[0] for k in keywords if k]
            connect_kwargs["keyterm"] = keyterms

        self._socket_cm = client.listen.v1._raw_client.connect(**connect_kwargs)
        self._socket = await self._socket_cm.__aenter__()
        logger.info(
            "[StreamingMic] Deepgram session open (nova-3, lang=%s, endpointing=%dms, "
            "utterance_end=%dms, %d keyword boosts)",
            language, endpointing_ms, utterance_end_ms, len(keywords),
        )

    async def _close_deepgram(self) -> None:
        """Close the current Deepgram session (best-effort)."""
        if self._socket_cm is not None:
            with contextlib.suppress(Exception):
                await self._socket_cm.__aexit__(None, None, None)
        self._socket = None
        self._socket_cm = None

    async def _reader_supervisor(self) -> None:
        """Run _read_loop in a reconnect loop. If Deepgram drops the WS
        (1011 timeout, network blip, etc.), close cleanly and re-open.

        Key invariant: _read_loop is ONLY called once self._socket is open.
        Keeping open/retry in its own inner loop prevents calling _read_loop
        with a None socket (which would otherwise cause a silent TypeError
        and a misleading "reconnecting" log storm when the API is unreachable).
        """
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

            # Inner reconnect loop: keep retrying _open_deepgram until the
            # socket is up before re-entering _read_loop.
            await self._close_deepgram()
            while self._running:
                try:
                    await self._open_deepgram()
                    # Fresh session — discard any in-progress utterance from the
                    # previous connection so stale words/timestamps don't bleed through.
                    self._utterance_start_s = None
                    self._pending_words = []
                    backoff = 0.5  # reset after a successful open
                    break          # connected — fall through to _read_loop
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error("[StreamingMic] Deepgram reconnect failed (%.1fs backoff): %s",
                                 backoff, e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)

    async def _keepalive_loop(self) -> None:
        """Send Deepgram KeepAlive JSON every 8 s to prevent 12-s inactivity timeout.

        Deepgram closes the WS with 1011 if it receives no audio data AND no text
        messages for ~12 s.  Pure-silence PCM (all-zero bytes sent when muted or
        the noise gate fires) does NOT satisfy this check on nova-3 — only real
        audio frames or an explicit KeepAlive text message do.  We send one every
        8 s as a belt-and-suspenders guard; the pump loop continues to send silence
        so VAD state is preserved across quiet gaps.
        """
        INTERVAL = 8.0
        try:
            while self._running:
                await asyncio.sleep(INTERVAL)
                if self._socket is not None:
                    try:
                        await self._socket.send_keep_alive()
                        logger.debug("[StreamingMic] KeepAlive sent")
                    except Exception as e:
                        logger.warning("[StreamingMic] KeepAlive failed — connection may drop: %s", e)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        for t in (self._pumper_task, self._reader_task, self._keepalive_task, self._device_monitor_task):
            if t is not None:
                t.cancel()
        for t in (self._pumper_task, self._reader_task, self._keepalive_task, self._device_monitor_task):
            if t is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self._close_deepgram()
        logger.info("[StreamingMic] session closed")

    def _setup_coreaudio_notifications(self) -> None:
        """Setup device change detection (no-op; handled by _monitor_device_changes polling)."""
        # CoreAudio callback mechanism is complex with PyObjC, so we use polling instead.
        # This is a pragmatic approach that works reliably.
        self._device_listener_active = True
        logger.debug("[StreamingMic] Device monitoring enabled (polling-based)")

    def _restart_stream(self) -> None:
        """Restart the mic stream (called when device changes)."""
        if self._stream is None:
            return
        try:
            logger.info("[StreamingMic] Restarting mic stream due to device change")
            self._stream.stop()
            self._stream.close()
            self._stream = None
        except Exception as e:
            logger.warning("[StreamingMic] Error closing old stream: %s", e)

        try:
            import sounddevice as sd

            def _audio_callback(indata, frames, time_info, status):  # noqa: ANN001
                if status:
                    logger.debug("[StreamingMic] PortAudio status: %s", status)
                chunk = bytes(indata)
                with contextlib.suppress(RuntimeError):
                    self._main_loop.call_soon_threadsafe(self._enqueue_chunk, chunk)

            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=BLOCKSIZE,
                callback=_audio_callback,
            )
            self._stream.start()
            logger.info("[StreamingMic] Mic stream restarted")
        except Exception as e:
            logger.error("[StreamingMic] Failed to restart stream: %s", e)

    async def _monitor_device_changes(self) -> None:
        """Poll for device changes and restart stream if device changed."""
        try:
            import sounddevice as sd

            while self._running:
                try:
                    current_device = sd.default.device[0]
                    if current_device != self._current_device_id:
                        logger.info(
                            "[StreamingMic] Input device changed: %s -> %s",
                            self._current_device_id,
                            current_device,
                        )
                        self._current_device_id = current_device
                        self._restart_stream()
                except Exception as e:
                    logger.debug("[StreamingMic] Device poll error: %s", e)
                await asyncio.sleep(1.0)  # Check every second for device changes
        except asyncio.CancelledError:
            pass

    @property
    def is_muted(self) -> bool:
        return self._muted

    @property
    def is_user_speaking(self) -> bool:
        """True while an utterance window is open — Deepgram has fired
        SpeechStarted but not yet UtteranceEnd. Used by the DMN speak gate
        to avoid interrupting the user mid-sentence. False when muted (we
        won't be receiving real audio anyway) and when no utterance has
        started yet."""
        if self._muted:
            return False
        return self._utterance_start_s is not None or bool(self._pending_words)

    def mute(self) -> None:
        """Discard mic audio until unmute(). Socket stays warm — no reconnect needed."""
        if not self._muted:
            self._muted = True
            # Reset any in-progress utterance so stale timestamps / words don't
            # corrupt the first real utterance after unmute.
            self._utterance_start_s = None
            self._pending_words = []
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

    async def _finalize_pending(self, last_end: float | None = None) -> None:
        """Build an utterance from the accumulated words + sliced PCM, publish it
        to the auditory bus, and hand it to next_utterance(). Resets utterance
        state. No-op when there's nothing pending. Shared by Deepgram's
        UtteranceEnd and the push-to-talk flush() path."""
        if not self._pending_words and self._utterance_start_s is None:
            return
        if last_end is None:
            last_end = self._pending_words[-1]["end"] if self._pending_words else (
                self._utterance_start_s or 0.0)
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
            return

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

    async def flush(self) -> None:
        """Push-to-talk release: keep the feed live for a short grace so trailing
        final Results arrive, finalize the held utterance immediately (don't wait
        for Deepgram's ~1.2s UtteranceEnd), then mute. Safe to call when already
        muted (no-op)."""
        if self._muted:
            return
        if self._ptt_release_grace_s > 0:
            await asyncio.sleep(self._ptt_release_grace_s)
        with contextlib.suppress(Exception):
            await self._finalize_pending()
        self.mute()

    # ── internal: mic side ─────────────────────────────────────────────────

    # Noise gate — drop chunks whose RMS energy is below this threshold.
    # Helps keep background noise (HVAC hum, distant chatter, keyboard clicks)
    # from confusing Deepgram's VAD. 0 disables. Tunable per-mic via
    # BRAIN_NOISE_GATE_RMS env var. For a Scarlett 2i2 with headphone mic at
    # normal gain, ambient noise is typically ~30–80 RMS; speech peaks 2000+.
    NOISE_GATE_RMS = float(os.environ.get("BRAIN_NOISE_GATE_RMS", "120"))

    def _enqueue_chunk(self, chunk: bytes) -> None:
        # Called on the main asyncio loop via call_soon_threadsafe.
        # When muted we still enqueue *silence* so Deepgram's WS doesn't time
        # out — Deepgram drops the connection after ~10s of no audio, and we
        # have no graceful way to recover mid-conversation.
        if self._muted:
            chunk = b"\x00" * len(chunk)
        elif self.NOISE_GATE_RMS > 0:
            # Cheap RMS check — sum of squares over int16 samples.
            # If the chunk is below the gate, replace with silence so the WS
            # stays warm but background noise doesn't reach Deepgram.
            import struct
            n = len(chunk) // 2
            if n > 0:
                samples = struct.unpack(f"<{n}h", chunk)
                # Sum of squares / n → variance; sqrt → RMS
                sq_sum = 0
                for s in samples:
                    sq_sum += s * s
                rms = (sq_sum / n) ** 0.5
                if rms < self.NOISE_GATE_RMS:
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
        if self._socket is None:
            return
        try:
            async for message in self._socket:
                mtype = getattr(message, "type", None)

                if mtype == "SpeechStarted":
                    if self._muted:
                        continue
                    ts = float(getattr(message, "timestamp", 0.0))
                    # Only record the timestamp of the FIRST burst in this utterance.
                    # Deepgram fires SpeechStarted again when the user resumes after
                    # a mid-sentence pause — overwriting _utterance_start_s and clearing
                    # _pending_words here would drop the first fragment, making the brain
                    # respond only to the tail of what was said.
                    if self._utterance_start_s is None:
                        self._utterance_start_s = ts
                    logger.debug("[StreamingMic] SpeechStarted @ %.2fs", ts)
                    # Note: barge-in is no longer triggered here on raw
                    # SpeechStarted. The mic picks up its own playback
                    # bleed-through and was killing TTS every reply. Barge-in
                    # is now keyword-driven and checked in the voice bridge
                    # in run.py against the final transcript.

                elif mtype == "Results":
                    if not getattr(message, "is_final", False):
                        continue
                    if self._muted:
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
                    if self._muted:
                        # Deepgram fires UtteranceEnd for silence while we're muted.
                        # Reset state so the next real utterance starts clean.
                        self._utterance_start_s = None
                        self._pending_words = []
                        continue
                    last_end = float(getattr(message, "last_word_end", 0.0))
                    await self._finalize_pending(last_end)

                elif mtype == "Metadata":
                    logger.debug("[StreamingMic] Metadata: %s",
                                 getattr(message, "request_id", "?"))
        except asyncio.CancelledError:
            raise  # propagate so _reader_supervisor handles shutdown cleanly
        except Exception as e:
            logger.error("[StreamingMic] read loop crashed — voice input is offline: %s", e)
