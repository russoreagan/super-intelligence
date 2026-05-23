"""
Peripheral Nervous System — input/output adapters.
Text stdin/stdout for v0.1. Voice (Deepgram + ElevenLabs) enabled via env flag.
"""
from __future__ import annotations

import asyncio
import logging
import os

from brain.bus import Bus
from brain.security import screen_input

logger = logging.getLogger(__name__)

VOICE_MODE = os.environ.get("BRAIN_VOICE_MODE", "false").lower() == "true"


def _resolve_output_device():
    """Pick the audio output device for TTS.

    BRAIN_AUDIO_OUTPUT_DEVICE env var: integer index OR case-insensitive
    substring of the device name (e.g. "Scarlett", "Mac mini", "DELL", "0").
    If unset, returns None and sounddevice uses the system default — which
    is correct for most setups (USB audio interface with headphones, etc.).
    Run `python -c "import sounddevice as sd; print(sd.query_devices())"`
    to list available devices.
    """
    try:
        import sounddevice as sd
    except Exception:
        return None

    raw = os.environ.get("BRAIN_AUDIO_OUTPUT_DEVICE", "").strip()
    devices = sd.query_devices()

    if raw:
        # Try int index first
        try:
            idx = int(raw)
            if 0 <= idx < len(devices) and devices[idx]["max_output_channels"] > 0:
                logger.info("[I/O] Audio output: [%d] %s (BRAIN_AUDIO_OUTPUT_DEVICE)",
                            idx, devices[idx]["name"])
                return idx
        except ValueError:
            pass
        # Substring match by name
        needle = raw.lower()
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0 and needle in d["name"].lower():
                logger.info("[I/O] Audio output: [%d] %s (matched %r)",
                            i, d["name"], raw)
                return i
        logger.warning("[I/O] BRAIN_AUDIO_OUTPUT_DEVICE=%r not found — using system default", raw)
        return None

    # Use system default — most user setups have it pointed at the right
    # device (headphones, monitors, etc.). Log it so the user can see.
    try:
        default_out = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        if isinstance(default_out, int) and 0 <= default_out < len(devices):
            logger.info("[I/O] Audio output: [%d] %s (system default)",
                        default_out, devices[default_out]["name"])
    except Exception:
        pass
    return None


class PNS:
    # Barge-in grace period: ignore interrupt requests for this many seconds
    # after TTS starts. With keyword-driven barge-in (handled in run.py voice
    # bridge) auto-interrupts from mic bleed are already prevented, so this
    # is just a small safety net for the first half-second of playback.
    # Override via BRAIN_BARGE_IN_GRACE_SECONDS.
    BARGE_IN_GRACE_SECONDS = float(os.environ.get("BRAIN_BARGE_IN_GRACE_SECONDS", "0.5"))

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._deepgram_client = None
        self._elevenlabs_client = None
        # interruption_switch (PLAN.md): set while TTS is streaming so a
        # concurrent mic detector can flip self._interrupt_event to cut it off.
        # Trigger side (continuous mic VAD during playback) is a TODO — the
        # current mic_listen() is press-to-talk, not streaming.
        self._speaking: bool = False
        self._speak_started_at: float = 0.0
        self._interrupt_event: asyncio.Event = asyncio.Event()

    async def receive_text(self, text: str, image_path: str | None = None) -> None:
        """Post user input to the bus."""
        payload = {"type": "raw", "text": text, "image_present": image_path is not None}
        if image_path:
            payload["image_path"] = image_path
        result = screen_input(text)
        if result.flagged:
            logger.warning("[I/O] [Security] Possible injection attempt in user input — message still processed but flagged (reason=%s)", result.reason)
            payload["risk"] = result.risk
            payload["screen_reason"] = result.reason
        await self._bus.publish_dict("sensory.text", payload, source="pns")

    async def emit(self, response: str, affect: dict | None = None) -> None:
        """Emit response to the user. `affect` (if present) drives voice modulation."""
        if VOICE_MODE:
            await self._speak(response, affect or {})
        else:
            print(f"\nBrain: {response}\n", flush=True)

    # Comprehensive emotion → v3 audio tag map. Covers every label that
    # name_emotion() can produce, plus context-driven emotions (embarrassed,
    # flirty, apologetic, grateful) that the metacognition cell or frontal
    # lobe can set on affect.emotion when situational appraisal warrants it.
    _V3_TAG_BY_EMOTION: dict[str, str] = {
        # — neuromod-derivable (from emotion_vocabulary.EMOTION_TABLE) —
        "joy":               "[happy]",
        "excitement":        "[excited]",
        "enthusiasm":        "[enthusiastic]",
        "curious":           "[curious]",
        "curious-uncertain": "[curious]",
        "content":           "",                    # natural voice
        "warm":              "[warmly]",
        "thoughtful":        "[thoughtfully]",
        "confident":         "[confidently]",
        "anxious":           "[nervously]",
        "inhibited":         "[softly]",
        "flat":              "[softly]",
        "restless":          "[urgently]",
        "cautious-agitated": "[urgently]",
        "agitated":          "[firmly]",
        "angry":             "[angrily]",
        "proud":             "[proudly]",
        "surprised":         "[gasps]",
        "defensive":         "[firmly]",
        "wistful":           "[softly]",
        "confused":          "[confused]",
        "neutral":           "",
        # — context-driven (no neuromod combo produces these on its own) —
        "amused":            "[laughs softly]",
        "playful":           "[playfully]",
        "joking":            "[laughs softly]",
        "sad":               "[sadly]",
        "somber":            "[softly]",
        "melancholy":        "[softly]",
        "frustrated":        "[firmly]",
        "irritated":         "[firmly]",
        "embarrassed":       "[bashfully]",
        "shy":               "[shyly]",
        "flirty":            "[playfully]",
        "tender":            "[gently]",
        "affectionate":      "[warmly]",
        "apologetic":        "[softly]",
        "grateful":          "[warmly]",
        "relieved":          "[sighs]",
        "disappointed":      "[softly]",
        "sympathetic":       "[gently]",
        "sarcastic":         "[sarcastically]",
        # — mid-tier defaults (feeling-wheel ancestors) for hierarchy fallback —
        # When a leaf emotion has no explicit entry, lookup walks leaf → mid → core
        # and lands on one of these. Lets new leaves inherit a sensible delivery
        # without forcing an entry per label.
        "playful":           "[playfully]",
        "loving":            "[warmly]",
        "peaceful":          "",
        "joyful":            "[happy]",
        "lonely":            "[softly]",
        "humiliated":        "[bashfully]",
        "mad":               "[firmly]",
        "frustrated":        "[firmly]",
        "anxious":           "[nervously]",
        "happy":             "[happy]",
        "sad":               "[sadly]",
        "anger":             "[firmly]",
        "fear":              "[nervously]",
        "surprise":          "[gasps]",
    }

    @staticmethod
    def _v3_audio_tag_from_affect(affect: dict) -> str | None:
        """eleven_v3 audio-tag selector: ONE leading tag that shapes the whole
        utterance's inflection. Resolution order:
          1. Leaf emotion entry in the explicit map.
          2. Mid-tier ancestor (feeling-wheel) → core ancestor.
          3. Neuromod-derived fallback for emotions outside the taxonomy.
          4. None for neutral so the model uses its natural voice.

        Stacking tags is unreliable in v3 — pick one and let it land.
        """
        from brain.emotion_hierarchy import lookup_with_inheritance

        emotion = (affect.get("emotion") or "").lower()
        # Honour explicit empty string (e.g. content="") as "natural voice".
        if emotion in PNS._V3_TAG_BY_EMOTION:
            tag = PNS._V3_TAG_BY_EMOTION[emotion]
            return tag or None
        inherited = lookup_with_inheritance(emotion, PNS._V3_TAG_BY_EMOTION)
        if inherited is not None:
            return inherited

        # Fallback: derive from neuromods for unclassified emotion labels.
        nm = affect.get("neuromod") or {}
        DA = float(nm.get("DA", 0.5))
        GABA = float(nm.get("GABA", 0.0))
        ACh = float(nm.get("ACh", 0.3))
        Glu = float(nm.get("Glu", 0.3))
        # Order matters: stressed-arousal before plain de-escalation.
        if Glu > 0.55 and GABA > 0.35:
            return "[urgently]"
        if GABA > 0.5:
            return "[gently]"
        if DA > 0.6 and Glu > 0.55:
            return "[excited]"
        if ACh > 0.55 and GABA < 0.35:
            return "[curious]"
        if DA < 0.3:
            return "[softly]"
        return None

    @staticmethod
    def _add_breath_pauses(text: str, count: int = 1) -> str:
        """Replace up to `count` mid-sentence ', ' with ' — ' (em-dash).
        Em-dashes produce a longer, more natural pause than commas in v3.
        No-op on text without commas. Skips the first comma if it's very early
        (within first 12 chars) — early commas usually punctuate an address."""
        if count <= 0 or ", " not in text:
            return text
        replaced = 0
        result_parts: list[str] = []
        i = 0
        while i < len(text):
            if replaced < count and text[i:i + 2] == ", " and i > 12:
                result_parts.append(" — ")
                replaced += 1
                i += 2
            else:
                result_parts.append(text[i])
                i += 1
        return "".join(result_parts)

    @staticmethod
    def _shape_for_v3(text: str, affect: dict) -> str:
        """Shape text for eleven_v3 delivery. Two complementary channels:
          - Audio tag (leading) — picks the emotional inflection.
          - Light punctuation shaping — adds breath pauses for grounded states
            so the tag has somewhere to actually slow down within the sentence.
        If the drafter pre-tagged the response, trust their phrasing fully
        and skip both passes.
        """
        stripped = text.lstrip()
        if stripped.startswith("[") and "]" in stripped[:40]:
            return text

        nm = affect.get("neuromod") or {}
        DA = float(nm.get("DA", 0.5))
        GABA = float(nm.get("GABA", 0.0))

        # Punctuation pass: insert breath pauses for grounded/de-escalation states.
        # Skip for brisk/aroused/neutral — em-dashes would fight the natural cadence.
        shaped = text
        if GABA > 0.5:
            shaped = PNS._add_breath_pauses(shaped, count=1)
        elif DA < 0.3:
            shaped = PNS._add_breath_pauses(shaped, count=2)

        # Tag pass: prepend an inflection cue (if warranted).
        tag = PNS._v3_audio_tag_from_affect(affect)
        if not tag:
            return shaped
        return f"{tag} {shaped}"

    @staticmethod
    def _voice_params_from_affect(affect: dict) -> dict:
        """voice_modulation_switch (PLAN.md): map entity state → ElevenLabs voice settings.
        Low DA → slower/lower; high arousal (Glu) + positive DA → faster/brighter;
        high GABA (threat/defuse) → calm/steady regardless."""
        nm = affect.get("neuromod") or {}
        DA = float(nm.get("DA", 0.5))
        GABA = float(nm.get("GABA", 0.0))
        Glu = float(nm.get("Glu", 0.3))

        # Defaults (current expressive baseline)
        stability, style, speed = 0.45, 0.40, 1.0

        if GABA > 0.5:
            # Threat / de-escalation: steadier, less dramatic, a touch slower
            stability, style, speed = 0.65, 0.25, 0.95
        elif Glu > 0.55 and DA > 0.55:
            # Bright + aroused: more expressive, more character, a touch faster
            stability, style, speed = 0.35, 0.55, 1.05
        elif DA < 0.35:
            # Low mood: slower, less style injection
            stability, style, speed = 0.55, 0.30, 0.93

        return {"stability": stability, "style": style, "speed": speed}

    def set_voice_id(self, voice_id: str) -> None:
        self._voice_id = voice_id
        logger.info("[I/O] ElevenLabs voice changed to %s", voice_id)

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def interrupt(self) -> None:
        """Signal in-progress TTS playback to stop ASAP. Safe to call any time.
        Ignores requests during the barge-in grace period (so TTS isn't killed
        by mic bleed in the first second of playback)."""
        if not self._speaking:
            return
        import time
        elapsed = time.time() - self._speak_started_at
        if elapsed < self.BARGE_IN_GRACE_SECONDS:
            logger.debug("[I/O] Ignoring barge-in (%.2fs < %.2fs grace period)",
                         elapsed, self.BARGE_IN_GRACE_SECONDS)
            return
        logger.info("[I/O] Interruption requested — cutting off TTS")
        self._interrupt_event.set()

    async def _speak(self, text: str, affect: dict | None = None) -> None:
        """
        Stream TTS audio to the system speakers via eleven_v3.

        v3 is slower/costlier than turbo but supports inline audio tags
        ([gently], [excited], [curious], [softly], [laughs]...) that let
        the affect signals actually shape delivery rather than just stability/style.

        Uses PCM output + sounddevice so playback starts on the first chunk.
        Falls back to buffered play() if sounddevice is unavailable.
        """
        api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not api_key:
            logger.warning("[I/O] ELEVENLABS_API_KEY not set — skipping TTS")
            return
        try:
            from elevenlabs import AsyncElevenLabs
            from elevenlabs.types import VoiceSettings
            client = AsyncElevenLabs(api_key=api_key)
            voice_id = getattr(self, "_voice_id", None) or os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

            params = self._voice_params_from_affect(affect or {})
            vs_kwargs = {
                "stability": params["stability"],
                "similarity_boost": 0.80,
                "style": params["style"],
                "use_speaker_boost": True,
            }
            try:
                voice_settings = VoiceSettings(**vs_kwargs, speed=params["speed"])
            except TypeError:
                voice_settings = VoiceSettings(**vs_kwargs)

            shaped_text = self._shape_for_v3(text, affect or {})
            logger.debug(
                "[I/O] TTS: model=eleven_v3 stability=%.2f style=%.2f speed=%.2f emotion=%s tag=%s",
                params["stability"], params["style"], params["speed"],
                (affect or {}).get("emotion"),
                shaped_text[: shaped_text.find("]") + 1] if shaped_text.startswith("[") else "—",
            )

            audio_iter = client.text_to_speech.convert(
                text=shaped_text,
                voice_id=voice_id,
                model_id="eleven_v3",
                output_format="pcm_22050",   # raw int16 PCM — no decode overhead
                voice_settings=voice_settings,
            )

            self._interrupt_event.clear()
            import time as _time
            self._speak_started_at = _time.time()
            self._speaking = True
            try:
                try:
                    import sounddevice as sd
                    SAMPLE_RATE = 22050
                    output_device = _resolve_output_device()
                    stream = sd.RawOutputStream(
                        samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        device=output_device,
                    )
                    stream.start()
                    try:
                        async for chunk in audio_iter:
                            if self._interrupt_event.is_set():
                                logger.debug("[I/O] TTS interrupted mid-stream")
                                break
                            if chunk:
                                await asyncio.get_event_loop().run_in_executor(
                                    None, stream.write, chunk
                                )
                    finally:
                        if self._interrupt_event.is_set():
                            stream.abort()  # drop the buffer immediately
                        else:
                            await asyncio.sleep(0.3)  # let buffer drain
                            stream.stop()
                        stream.close()
                except ImportError:
                    # sounddevice not installed — fall back to buffered play()
                    logger.debug("[I/O] sounddevice unavailable — falling back to buffered TTS playback")
                    from elevenlabs.play import play
                    audio_bytes = b"".join([chunk async for chunk in audio_iter])
                    if not self._interrupt_event.is_set():
                        await asyncio.get_event_loop().run_in_executor(None, play, audio_bytes)
            finally:
                self._speaking = False

        except Exception as e:
            logger.warning("[I/O] Text-to-speech failed — printing response as text instead: %s", e)
            print(f"\nBrain: {text}\n", flush=True)

    async def mic_listen(self) -> str:
        """Capture mic input via Deepgram and return transcript."""
        try:
            from deepgram import DeepgramClient, PrerecordedOptions
            import sounddevice as sd

            SAMPLE_RATE = 16000
            DURATION = 5  # seconds, adjust as needed

            print("Listening... (5s)", flush=True)
            audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                           channels=1, dtype="int16")
            sd.wait()

            audio_bytes = audio.tobytes()

            # Publish raw audio for auditory cortex (fire-and-forget; dropped if cortex inactive)
            asyncio.ensure_future(self._bus.publish_dict(
                "auditory.raw_audio",
                {
                    "audio_bytes": audio_bytes,
                    "sample_rate": SAMPLE_RATE,
                    "duration_s": float(DURATION),
                    "channels": 1,
                    "dtype": "int16",
                },
                source="pns",
            ))

            client = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])
            response = await client.listen.asyncprerecorded.v("1").transcribe_file(
                {"buffer": audio_bytes, "mimetype": "audio/raw"},
                PrerecordedOptions(
                    model="nova-3",
                    diarize_model="latest",  # per-word speaker labels from one mic
                    smart_format=True,
                    utterances=True,
                    punctuate=True,
                ),
            )
            alt = response.results.channels[0].alternatives[0]
            transcript = alt.transcript.strip()

            # Extract per-word speaker labels for the auditory cortex
            diarized_words: list[dict] = []
            try:
                if hasattr(alt, "words") and alt.words:
                    for w in alt.words:
                        diarized_words.append({
                            "word": getattr(w, "word", ""),
                            "start": float(getattr(w, "start", 0)),
                            "end": float(getattr(w, "end", 0)),
                            "speaker": int(getattr(w, "speaker", 0)),
                            "speaker_confidence": float(getattr(w, "speaker_confidence", 1.0)),
                        })
            except Exception as _e:
                logger.debug("PNS: diarized word extraction failed: %s", _e)

            # Publish diarized audio for speaker ID pipeline
            asyncio.ensure_future(self._bus.publish_dict(
                "auditory.diarized_audio",
                {
                    "audio_bytes": audio_bytes,
                    "sample_rate": SAMPLE_RATE,
                    "duration_s": float(DURATION),
                    "dtype": "int16",
                    "diarized_words": diarized_words,
                    "transcript": transcript,
                },
                source="pns",
            ))
            return transcript
        except Exception as e:
            logger.error("[I/O] Microphone capture failed. Check DEEPGRAM_API_KEY in .env and that 'sounddevice' is installed: %s", e)
            return ""
