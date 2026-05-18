"""
Peripheral Nervous System — input/output adapters.
Text stdin/stdout for v0.1. Voice (Deepgram + ElevenLabs) enabled via env flag.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from brain.bus import Bus

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
        await self._bus.publish_dict("sensory.text", payload, source="pns")

    async def emit(self, response: str) -> None:
        """Emit response to the user."""
        if VOICE_MODE:
            await self._speak(response)
        else:
            print(f"\nBrain: {response}\n", flush=True)

    async def _speak(self, text: str) -> None:
        try:
            from elevenlabs import AsyncElevenLabs, play
            client = AsyncElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
            voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
            audio = await client.generate(
                text=text, voice=voice_id,
                model="eleven_turbo_v2_5"
            )
            import io
            audio_bytes = b"".join([chunk async for chunk in audio])
            play(audio_bytes)
        except Exception as e:
            logger.warning("TTS failed, falling back to text: %s", e)
            print(f"\nBrain: {text}\n", flush=True)

    async def mic_listen(self) -> str:
        """Capture mic input via Deepgram and return transcript."""
        try:
            from deepgram import DeepgramClient, PrerecordedOptions
            import sounddevice as sd
            import numpy as np

            SAMPLE_RATE = 16000
            DURATION = 5  # seconds, adjust as needed

            print("Listening... (5s)", flush=True)
            audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                           channels=1, dtype="int16")
            sd.wait()

            client = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])
            audio_bytes = audio.tobytes()
            response = await client.listen.asyncprerecorded.v("1").transcribe_file(
                {"buffer": audio_bytes, "mimetype": "audio/raw"},
                PrerecordedOptions(model="nova-3", smart_format=True,
                                   utterances=True, punctuate=True),
            )
            transcript = response.results.channels[0].alternatives[0].transcript
            return transcript.strip()
        except Exception as e:
            logger.error("Mic listen failed: %s", e)
            return ""
