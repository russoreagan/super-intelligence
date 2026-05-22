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


class PNS:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._deepgram_client = None
        self._elevenlabs_client = None

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

    async def emit(self, response: str) -> None:
        """Emit response to the user."""
        if VOICE_MODE:
            await self._speak(response)
        else:
            print(f"\nBrain: {response}\n", flush=True)

    def set_voice_id(self, voice_id: str) -> None:
        self._voice_id = voice_id
        logger.info("[I/O] ElevenLabs voice changed to %s", voice_id)

    async def _speak(self, text: str) -> None:
        try:
            from elevenlabs import AsyncElevenLabs, play
            client = AsyncElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
            voice_id = getattr(self, "_voice_id", None) or os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
            audio_iter = client.text_to_speech.convert(
                text=text, voice_id=voice_id,
                model_id="eleven_turbo_v2_5"
            )
            # convert() returns an async iterator of bytes chunks
            audio_bytes = b"".join([chunk async for chunk in audio_iter])
            play(audio_bytes)
        except Exception as e:
            logger.warning("[I/O] Text-to-speech failed — printing response as text instead. Check ELEVENLABS_API_KEY in .env: %s", e)
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
