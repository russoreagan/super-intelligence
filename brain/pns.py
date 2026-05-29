"""
Peripheral Nervous System — input/output adapters.
Text stdin/stdout for v0.1. Voice (Deepgram + ElevenLabs) enabled via env flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from brain.bus import Bus
from brain.emotion_presets import EMOTION_TAG_MAP, strip_reaction_tags
from brain.security import screen_input
from brain.settings import settings

logger = logging.getLogger(__name__)

VOICE_MODE = os.environ.get("BRAIN_VOICE_MODE", "false").lower() == "true"

# Watchdog: if no audio chunk arrives within this many seconds, the ElevenLabs
# call is considered hung and TTS is aborted so _speaking resets to False.
_TTS_CHUNK_TIMEOUT_S: float = float(os.environ.get("BRAIN_TTS_CHUNK_TIMEOUT", "30"))


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
                logger.info(
                    "[I/O] Audio output: [%d] %s (BRAIN_AUDIO_OUTPUT_DEVICE)",
                    idx,
                    devices[idx]["name"],
                )
                return idx
        except ValueError:
            pass
        # Substring match by name
        needle = raw.lower()
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0 and needle in d["name"].lower():
                logger.info("[I/O] Audio output: [%d] %s (matched %r)", i, d["name"], raw)
                return i
        logger.warning("[I/O] BRAIN_AUDIO_OUTPUT_DEVICE=%r not found — using system default", raw)
        return None

    # Use system default — most user setups have it pointed at the right
    # device (headphones, monitors, etc.). Log it so the user can see.
    try:
        default_out = (
            sd.default.device[1]
            if isinstance(sd.default.device, (list, tuple))
            else sd.default.device
        )
        if isinstance(default_out, int) and 0 <= default_out < len(devices):
            logger.info(
                "[I/O] Audio output: [%d] %s (system default)",
                default_out,
                devices[default_out]["name"],
            )
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

    def __init__(self, bus: Bus, on_speaking_change=None) -> None:
        self._bus = bus
        self._deepgram_client = None
        self._elevenlabs_client = None
        # interruption_switch (PLAN.md): set while TTS is streaming so a
        # concurrent mic detector can flip self._interrupt_event to cut it off.
        # Trigger side (continuous mic VAD during playback) is a TODO — the
        # current mic_listen() is press-to-talk, not streaming.
        self._speaking: bool = False
        self._speaking_text: str = ""  # what TTS is currently saying
        self._speak_started_at: float = 0.0
        self._interrupt_event: asyncio.Event = asyncio.Event()
        self._speak_lock: asyncio.Lock = asyncio.Lock()  # serializes TTS calls
        self._on_speaking_change = on_speaking_change  # Callable[[bool], None] | None
        # Deliberate mood: set_mood tool publishes here; consumed once per _speak() call.
        self._deliberate_emotion_inbox = bus.subscribe("meta.deliberate_emotion")

    async def receive_text(self, text: str, image_path: str | None = None) -> None:
        """Post user input to the bus."""
        payload = {"type": "raw", "text": text, "image_present": image_path is not None}
        if image_path:
            payload["image_path"] = image_path
        result = screen_input(text)
        if result.flagged:
            logger.warning(
                "[I/O] [Security] Possible injection attempt in user input — message still processed but flagged (reason=%s)",
                result.reason,
            )
            payload["risk"] = result.risk
            payload["screen_reason"] = result.reason
        await self._bus.publish_dict("sensory.text", payload, source="pns")

    async def emit(self, response: str, affect: dict | None = None) -> None:
        """Emit response to the user. `affect` (if present) drives voice modulation.

        Serialized via _speak_lock: concurrent callers (main turn, follow-through
        background task, proactive DMN) queue rather than overlap.
        """
        if VOICE_MODE:
            async with self._speak_lock:
                await self._speak(response, affect or {})
        else:
            print(f"\nBrain: {response}\n", flush=True)

    # Single source of truth for emotion → v3 audio tag lives in
    # emotion_presets.EMOTION_TAG_MAP, so the reactive affect path here and the
    # deliberate set_mood() / [mood:X] paths can never drift apart. Aliased for
    # the existing PNS._V3_TAG_BY_EMOTION references below.
    _V3_TAG_BY_EMOTION: dict[str, str] = EMOTION_TAG_MAP

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
        if (
            Glu > settings.get("glu_urgently_threshold")
            and settings.get("gaba_urgently_threshold") < GABA
        ):
            return "[urgently]"
        if settings.get("gaba_gently_threshold") < GABA:
            return "[gently]"
        # Pacing extremes recover the intent of the old `speed` slider the
        # v3-native way: v3 doesn't honour a speed param, so rate is conveyed by
        # tags. High positive arousal → rushed; very low arousal → drawn out.
        if (
            settings.get("da_excited_threshold") < DA
            and Glu > settings.get("glu_excited_threshold") + 0.15
        ):
            return "[rushed]"
        if settings.get("da_excited_threshold") < DA and Glu > settings.get(
            "glu_excited_threshold"
        ):
            return "[excited]"
        if (
            ACh > settings.get("ach_curious_threshold")
            and settings.get("gaba_curious_threshold") > GABA
        ):
            return "[curious]"
        if settings.get("da_softly_threshold") - 0.10 > DA:
            return "[drawn out]"
        if settings.get("da_softly_threshold") > DA:
            return "[softly]"
        return None

    @staticmethod
    def _split_sentences(text: str, min_len: int = 260, first_min_len: int = 120) -> list[str]:
        """Split text at natural boundaries for pipelined TTS generation.

        Graduated sizing for v3: the FIRST chunk uses a smaller target
        (`first_min_len`) so audio starts quickly (low time-to-first-sound),
        while every chunk after that uses the larger `min_len` so v3 has enough
        context to perform realistic, emotion-infused prosody. (v3 degrades on
        tiny fragments; request stitching in _producer carries prosody across
        the boundaries.)

        Paragraph breaks (\\n\\n) are hard stops — a paragraph >= half the active
        target is flushed as its own chunk rather than merged into the next.
        Within long paragraphs, sentence endings (.!?…) are used to sub-split.
        """
        import re

        chunks: list[str] = []
        buf = ""

        def _target() -> int:
            # Smaller threshold until the first chunk is emitted, then larger.
            return first_min_len if not chunks else min_len

        for para in re.split(r"\n\n+", text.strip()):
            para = para.strip()
            if not para:
                continue

            # Paragraph break: flush buffer if it's substantial enough to stand alone.
            if buf and len(buf) >= _target() // 2:
                chunks.append(buf)
                buf = ""

            # Accumulate sentences within the paragraph until we hit the target.
            for part in re.split(r"(?<=[.!?…])\s+", para):
                buf = (buf + " " + part).strip() if buf else part
                if len(buf) >= _target():
                    chunks.append(buf)
                    buf = ""

        # Remaining text: tiny tails (< 80 chars) merge into the previous chunk
        # to avoid a separate ElevenLabs call for "Right?" or "Pretty cool!".
        # Anything larger stands alone as its own chunk.
        if buf:
            if chunks and len(buf) < 80:
                chunks[-1] = chunks[-1] + " " + buf
            else:
                chunks.append(buf)

        return [c for c in chunks if c.strip()]

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
            if replaced < count and text[i : i + 2] == ", " and i > 12:
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
        if settings.get("gaba_single_pause_threshold") < GABA:
            shaped = PNS._add_breath_pauses(shaped, count=1)
        elif settings.get("da_double_pause_threshold") > DA:
            shaped = PNS._add_breath_pauses(
                shaped, count=int(settings.get("breath_pause_count_max"))
            )

        # Tag pass: prepend an inflection cue (if warranted).
        tag = PNS._v3_audio_tag_from_affect(affect)
        if not tag:
            return shaped
        return f"{tag} {shaped}"

    @staticmethod
    def _parse_mood_markup(text: str, base_tag: str | None = None) -> tuple[str, str]:
        """Parse [mood:X]...[/mood] inline markers in the AI's response text.

        Returns (display_text, tts_text):
          - display_text: markup stripped — clean for chat display.
          - tts_text: [mood:X] replaced with ElevenLabs v3 audio tag inline;
                      after each segment, base_tag (the reactive affect tag) is
                      restored so the rest of the sentence returns to normal voice.

        Segments without markup are unchanged.  Unknown emotion names are stripped
        silently (no tag injected) so the response still renders.

        Example:
          input:  "Sure. [mood:angry] This is unacceptable! [/mood] Anyway, let's fix it."
          display: "Sure. This is unacceptable! Anyway, let's fix it."
          tts v3: "Sure. [angrily] This is unacceptable! [base_tag] Anyway, let's fix it."
        """
        import re

        from brain.emotion_presets import get_tag

        pattern = re.compile(r"\[mood:([^\]]+)\](.*?)\[/mood\]", re.DOTALL | re.IGNORECASE)

        def replace_for_display(m: re.Match) -> str:
            return m.group(2).strip()

        def replace_for_tts(m: re.Match) -> str:
            emotion = m.group(1).strip().lower()
            segment = m.group(2).strip()
            el_tag = get_tag(emotion)
            reset = f" {base_tag}" if base_tag else ""
            if el_tag:
                return f"{el_tag} {segment}{reset}"
            return f"{segment}{reset}"

        display_text = pattern.sub(replace_for_display, text)
        tts_text = pattern.sub(replace_for_tts, text)
        return display_text, tts_text

    def _drain_deliberate_emotion(self) -> str | None:
        """Consume the most recent meta.deliberate_emotion bus message (if any)."""
        result: str | None = None
        while True:
            try:
                msg = self._deliberate_emotion_inbox.get_nowait()
                if not msg.expired:
                    result = msg.payload.get("emotion")  # None = cleared
            except asyncio.QueueEmpty:
                break
        return result

    @staticmethod
    def _voice_params_from_affect(affect: dict) -> dict:
        """voice_modulation_switch (PLAN.md): map entity state → ElevenLabs voice settings.
        Low DA → slower/lower; high arousal (Glu) + positive DA → faster/brighter;
        high GABA (threat/defuse) → calm/steady regardless."""
        nm = affect.get("neuromod") or {}
        DA = float(nm.get("DA", 0.5))
        GABA = float(nm.get("GABA", 0.0))
        Glu = float(nm.get("Glu", 0.3))

        stability = settings.get("voice_stability_default")
        style = settings.get("voice_style_default")
        speed = settings.get("voice_speed_default")

        if settings.get("gaba_gently_threshold") < GABA:
            stability = settings.get("voice_stability_threat")
            style = settings.get("voice_style_threat")
            speed = settings.get("voice_speed_threat")
        elif (
            Glu > settings.get("glu_excited_threshold")
            and settings.get("da_excited_threshold") < DA
        ):
            stability = settings.get("voice_stability_bright")
            style = settings.get("voice_style_bright")
            speed = settings.get("voice_speed_bright")
        elif DA < 0.35:
            stability = settings.get("voice_stability_low_mood")
            style = settings.get("voice_style_low_mood")
            speed = settings.get("voice_speed_low_mood")

        return {"stability": stability, "style": style, "speed": speed}

    @staticmethod
    def _snap_v3_stability(value: float) -> float:
        """v3 exposes three discrete stability modes: 0.0 (Creative — most
        expressive), 0.5 (Natural), 1.0 (Robust — most stable). Map the
        continuous, neuromod-derived stability onto them with threshold BANDS
        (not nearest-point) so the four voice buckets actually spread across all
        three modes instead of all collapsing to Natural:
          bright (0.35) → Creative · default/low-mood (0.45/0.55) → Natural ·
          threat (0.65) → Robust."""
        v = float(value)
        if v <= 0.40:
            return 0.0  # Creative — most expressive (bright/animated states)
        if v >= 0.60:
            return 1.0  # Robust — most stable (threat / de-escalation)
        return 0.5  # Natural

    def _emit_tts_error(self, detail: str) -> None:
        """Surface a TTS failure to the UI so it isn't silently disguised as a
        normal text reply. Best-effort — never raises."""
        try:
            from brain.ui.emitter import emitter

            asyncio.ensure_future(emitter.emit_event({"type": "tts_error", "detail": detail[:200]}))
        except Exception:
            pass

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
            logger.debug(
                "[I/O] Ignoring barge-in (%.2fs < %.2fs grace period)",
                elapsed,
                self.BARGE_IN_GRACE_SECONDS,
            )
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
            voice_id = getattr(self, "_voice_id", None) or os.environ.get(
                "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
            )

            params = self._voice_params_from_affect(affect or {})
            model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_v3").strip() or "eleven_v3"

            if model_id == "eleven_v3":
                # v3 honours stability (snapped to its discrete Creative/Natural/
                # Robust points) + similarity_boost + use_speaker_boost. It does NOT
                # act on `style` or `speed` — expressiveness comes from audio tags +
                # text, not the sliders — so sending them is at best ignored and at
                # worst 422-rejected (a silent "no audio" cause). Drop them for v3.
                sent_stability = self._snap_v3_stability(params["stability"])
                voice_settings = VoiceSettings(
                    stability=sent_stability,
                    similarity_boost=0.80,
                    use_speaker_boost=True,
                )
            else:
                sent_stability = params["stability"]
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

            # Check for a deliberate whole-turn emotion set via set_mood() tool.
            # Consumed once here so it doesn't bleed into subsequent turns.
            deliberate_emotion = self._drain_deliberate_emotion()
            has_inline_mood = False

            # Only shape the text for v3 (audio tags). Other models would
            # literally read "[gently]" out loud.
            if model_id == "eleven_v3":
                # Step 1: resolve the base (reactive) tag from affect.
                base_tag = self._v3_audio_tag_from_affect(affect or {})

                # Step 2: parse [mood:X]...[/mood] inline markup.
                # display_text has markup stripped; tts_text has ElevenLabs tags inline.
                display_text, tts_text = self._parse_mood_markup(text, base_tag)
                has_inline_mood = tts_text != display_text

                # Publish a meta.mood_expression event for each inline segment
                # so session_turn can collect them for the Langfuse trace.
                if has_inline_mood:
                    import re as _re

                    for _m in _re.finditer(
                        r"\[mood:([^\]]+)\](.*?)\[/mood\]",
                        text,
                        _re.DOTALL | _re.IGNORECASE,
                    ):
                        _emotion = _m.group(1).strip().lower()
                        _preview = _m.group(2).strip()[:80]
                        import asyncio as _asyncio

                        _asyncio.get_event_loop().call_soon(
                            lambda e=_emotion, p=_preview: _asyncio.ensure_future(
                                self._bus.publish_dict(
                                    "meta.mood_expression",
                                    {"emotion": e, "source": "inline", "preview": p},
                                    source="pns",
                                )
                            )
                        )

                # Step 3: if set_mood() was called, override the whole-turn tag
                # (only when there's no inline markup — inline markup takes precedence).
                if deliberate_emotion and not has_inline_mood:
                    from brain.emotion_presets import get_tag as _get_tag

                    override_tag = _get_tag(deliberate_emotion)
                    if override_tag:
                        tts_text = f"{override_tag} {tts_text}"
                elif not has_inline_mood:
                    # Standard reactive shaping: use affect-derived tag + breath pauses.
                    tts_text = self._shape_for_v3(tts_text, affect or {})

                shaped_text = tts_text
                tag_preview = (
                    shaped_text[: shaped_text.find("]") + 1] if shaped_text.startswith("[") else "—"
                )
            else:
                # Non-v3: strip [mood:X] markup AND bare reaction tags so none of
                # them get read aloud literally (those tags are v3-only).
                display_text, _ = self._parse_mood_markup(text, None)
                shaped_text = strip_reaction_tags(display_text)
                tag_preview = "—"

            if model_id == "eleven_v3":
                logger.info(
                    "[I/O] TTS: voice=%s model=%s stability=%.2f (snapped, style/speed dropped) "
                    "emotion=%s tag=%s deliberate=%s inline_mood=%s",
                    voice_id,
                    model_id,
                    sent_stability,
                    (affect or {}).get("emotion"),
                    tag_preview,
                    deliberate_emotion or "—",
                    has_inline_mood,
                )
            else:
                logger.info(
                    "[I/O] TTS: voice=%s model=%s stability=%.2f style=%.2f speed=%.2f emotion=%s tag=%s",
                    voice_id,
                    model_id,
                    params["stability"],
                    params["style"],
                    params["speed"],
                    (affect or {}).get("emotion"),
                    tag_preview,
                )

            # Split into sentence-sized chunks so ElevenLabs starts generating
            # audio for the first sentence immediately, while subsequent
            # sentences are fetched concurrently with playback.
            sentences = self._split_sentences(shaped_text)
            logger.debug("[I/O] TTS: %d sentence chunk(s) for streaming", len(sentences))

            self._interrupt_event.clear()
            import time as _time

            self._speak_started_at = _time.time()
            self._speaking = True
            self._speaking_text = text
            if self._on_speaking_change:
                self._on_speaking_change(True)
            first_chunk_ts: float | None = None
            try:
                try:
                    import sounddevice as sd

                    SAMPLE_RATE = 22050
                    output_device = _resolve_output_device()

                    def _open_stream(device):
                        s = sd.RawOutputStream(
                            samplerate=SAMPLE_RATE,
                            channels=1,
                            dtype="int16",
                            device=device,
                        )
                        s.start()
                        return s

                    try:
                        stream = _open_stream(output_device)
                    except Exception as dev_err:
                        # The configured device (e.g. a Scarlett that's asleep,
                        # unplugged, or a stale BRAIN_AUDIO_OUTPUT_DEVICE index)
                        # failed to open. Fall back to the system default rather
                        # than dropping the whole reply to silent text.
                        if output_device is not None:
                            logger.warning(
                                "[I/O] Output device %r failed to open (%s) — "
                                "falling back to system default output",
                                output_device,
                                dev_err,
                            )
                            self._emit_tts_error(
                                f"Audio device {output_device!r} unavailable — using default"
                            )
                            stream = _open_stream(None)
                        else:
                            raise

                    # Producer: fetch sentences from ElevenLabs one at a time,
                    # streaming each sentence's audio chunks into the queue.
                    # While the consumer plays sentence N, the producer is already
                    # fetching sentence N+1, eliminating the long wait for the
                    # full-response audio to be ready.
                    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

                    async def _producer() -> None:
                        try:
                            for i, sentence in enumerate(sentences):
                                if self._interrupt_event.is_set():
                                    break
                                # Request stitching: tell v3 what came right before
                                # and after this chunk so prosody/emotion carries
                                # across the chunk boundaries instead of resetting.
                                convert_kwargs = {
                                    "text": sentence,
                                    "voice_id": voice_id,
                                    "model_id": model_id,
                                    "output_format": "pcm_22050",
                                    "voice_settings": voice_settings,
                                }
                                if i > 0:
                                    convert_kwargs["previous_text"] = sentences[i - 1]
                                if i + 1 < len(sentences):
                                    convert_kwargs["next_text"] = sentences[i + 1]
                                try:
                                    audio_iter = client.text_to_speech.convert(**convert_kwargs)
                                except TypeError:
                                    # Older SDK without previous_text/next_text — retry plain.
                                    convert_kwargs.pop("previous_text", None)
                                    convert_kwargs.pop("next_text", None)
                                    audio_iter = client.text_to_speech.convert(**convert_kwargs)
                                async for chunk in audio_iter:
                                    if self._interrupt_event.is_set():
                                        break
                                    if chunk:
                                        await audio_queue.put(chunk)
                        finally:
                            await audio_queue.put(None)  # sentinel — playback done

                    producer_task = asyncio.create_task(_producer())
                    try:
                        while True:
                            try:
                                chunk = await asyncio.wait_for(
                                    audio_queue.get(), timeout=_TTS_CHUNK_TIMEOUT_S
                                )
                            except TimeoutError:
                                logger.warning(
                                    "[I/O] TTS watchdog: no audio chunk in %.0fs — aborting "
                                    "(ElevenLabs slow/unreachable?)",
                                    _TTS_CHUNK_TIMEOUT_S,
                                )
                                self._emit_tts_error(
                                    f"No audio from ElevenLabs in {_TTS_CHUNK_TIMEOUT_S:.0f}s"
                                )
                                self._interrupt_event.set()
                                break
                            if chunk is None:
                                break
                            if self._interrupt_event.is_set():
                                logger.debug("[I/O] TTS interrupted mid-stream")
                                break
                            if first_chunk_ts is None:
                                first_chunk_ts = _time.time()
                                logger.info(
                                    "[I/O] TTS first audio chunk in %.2fs (model=%s, chunks=%d)",
                                    first_chunk_ts - self._speak_started_at,
                                    model_id,
                                    len(sentences),
                                )
                            await asyncio.get_event_loop().run_in_executor(
                                None, stream.write, chunk
                            )
                    finally:
                        producer_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await producer_task
                        if self._interrupt_event.is_set():
                            stream.abort()  # drop the buffer immediately
                        else:
                            await asyncio.sleep(0.3)  # let buffer drain
                            stream.stop()
                        stream.close()
                except ImportError:
                    # sounddevice not installed — fall back to buffered play()
                    logger.debug(
                        "[I/O] sounddevice unavailable — falling back to buffered TTS playback"
                    )
                    from elevenlabs.play import play

                    audio_bytes = b""
                    for sentence in sentences:
                        if self._interrupt_event.is_set():
                            break
                        async for chunk in client.text_to_speech.convert(
                            text=sentence,
                            voice_id=voice_id,
                            model_id=model_id,
                            output_format="pcm_22050",
                            voice_settings=voice_settings,
                        ):
                            audio_bytes += chunk
                    if not self._interrupt_event.is_set():
                        await asyncio.get_event_loop().run_in_executor(None, play, audio_bytes)
            finally:
                self._speaking = False
                self._speaking_text = ""
                if self._on_speaking_change:
                    self._on_speaking_change(False)

        except Exception as e:
            # Make the failure LOUD. This handler used to silently print the
            # reply as text, which is indistinguishable from "no audio out".
            logger.error(
                "[I/O] Text-to-speech FAILED (%s: %s) — voice will not play this "
                "turn; falling back to text.",
                type(e).__name__,
                e,
                exc_info=True,
            )
            self._emit_tts_error(f"{type(e).__name__}: {e}")
            print(f"\nBrain: {text}\n", flush=True)

    async def mic_listen(self) -> str:
        """Capture mic input via Deepgram and return transcript."""
        try:
            import sounddevice as sd
            from deepgram import DeepgramClient, PrerecordedOptions

            SAMPLE_RATE = 16000
            DURATION = 5  # seconds, adjust as needed

            print("Listening... (5s)", flush=True)
            audio = sd.rec(
                int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16"
            )
            sd.wait()

            audio_bytes = audio.tobytes()

            # Publish raw audio for auditory cortex (fire-and-forget; dropped if cortex inactive)
            asyncio.ensure_future(
                self._bus.publish_dict(
                    "auditory.raw_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": SAMPLE_RATE,
                        "duration_s": float(DURATION),
                        "channels": 1,
                        "dtype": "int16",
                    },
                    source="pns",
                )
            )

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
                        diarized_words.append(
                            {
                                "word": getattr(w, "word", ""),
                                "start": float(getattr(w, "start", 0)),
                                "end": float(getattr(w, "end", 0)),
                                "speaker": int(getattr(w, "speaker", 0)),
                                "speaker_confidence": float(getattr(w, "speaker_confidence", 1.0)),
                            }
                        )
            except Exception as _e:
                logger.debug("PNS: diarized word extraction failed: %s", _e)

            # Publish diarized audio for speaker ID pipeline
            asyncio.ensure_future(
                self._bus.publish_dict(
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
                )
            )
            return transcript
        except Exception as e:
            logger.error(
                "[I/O] Microphone capture failed. Check DEEPGRAM_API_KEY in .env and that 'sounddevice' is installed: %s",
                e,
            )
            return ""
