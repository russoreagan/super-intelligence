"""
Occipital Lobe — vision processing.

Handles two input modes:
  sensory.image       — single static image (file path), one-shot analysis
  sensory.video_frame — raw frame bytes from a live stream (Pipecat / webcam)

Video frames are sampled at BRAIN_VIDEO_SAMPLE_INTERVAL seconds (default 3.0),
change-detected, then batched into a single Gemini multimodal call.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.settings import settings as _settings

logger = logging.getLogger(__name__)

CLUSTER = "occipital"

IMAGE_ROOT = Path(os.environ.get("BRAIN_IMAGE_DIR", str(Path.home() / "brain_images"))).expanduser().resolve()
IMAGE_ROOT.mkdir(parents=True, exist_ok=True)

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024

VISION_SYSTEM = """You are the visual cortex of an AI brain.
Analyze the provided image or video frames and return structured JSON:
{
  "description": string,        // 1-2 sentence scene description
  "text_in_image": string,      // any text visible (OCR)
  "key_entities": [string],     // objects, people, or concepts visible
  "chart_data": string | null,  // if a chart/diagram, summarize the data
  "emotional_tone": string,     // e.g. "neutral", "alarming", "joyful"
  "context_for_response": string // how this visual relates to the conversation
}
Return ONLY JSON."""


def _rms_diff(a: bytes, b: bytes) -> float:
    """Cheap pixel-level RMS diff between two raw JPEG/PNG byte buffers."""
    try:
        import io

        import numpy as np
        from PIL import Image
        ia = np.array(Image.open(io.BytesIO(a)).convert("L"), dtype=float)
        ib = np.array(Image.open(io.BytesIO(b)).convert("L"), dtype=float)
        if ia.shape != ib.shape:
            return float("inf")
        return float(np.sqrt(np.mean((ia - ib) ** 2)))
    except Exception:
        return float("inf")  # treat as changed if we can't compare


class OccipitalCluster:
    def __init__(self, bus: Bus, router: ModelRouter) -> None:
        self._bus = bus
        self._router = router

        self._vision_integrator = IntegratorCell(
            name="vision_integrator",
            cluster=CLUSTER,
            model="flash",
            system_prompt=VISION_SYSTEM,
            topics=["sensory.image", "sensory.video_frame"],
            max_calls_per_turn=1,
        )
        self._vision_integrator.set_router(router)

        self._image_present = SwitchNeuron("image_present", CLUSTER)
        self._image_size_router = SwitchNeuron("image_size_router", CLUSTER)
        self._vision_needed = SwitchNeuron("vision_needed", CLUSTER)

        # Video frame state
        self._frame_buffer: list[bytes] = []
        self._last_sample_time: float = 0.0
        self._last_frame: bytes | None = None

    # ------------------------------------------------------------------
    # Static image processing (file path)
    # ------------------------------------------------------------------

    async def process(self, image_path: str, user_text: str, turn_id: str) -> dict | None:
        """Process a single static image file. Returns vision features or None."""
        if not image_path:
            return None

        path = Path(image_path)
        resolved = path.expanduser().resolve()

        if resolved.suffix.lower() not in _ALLOWED_EXTENSIONS:
            logger.warning("[Vision] Blocked image — file type %r not allowed.", resolved.suffix)
            return None

        import tempfile
        _tmp = Path(tempfile.gettempdir()).resolve()
        if not resolved.is_relative_to(IMAGE_ROOT) and not resolved.is_relative_to(_tmp):
            logger.warning("[Vision] Blocked image path — outside allowed folder (%s).", IMAGE_ROOT)
            return None

        if not resolved.exists():
            logger.warning("[Vision] Image file not found: %s", resolved)
            return None

        size = resolved.stat().st_size
        if size > MAX_IMAGE_SIZE_BYTES:
            logger.warning("[Vision] Image too large (%d bytes) — skipping.", size)
            return None

        self._vision_integrator.model = "flash" if size > 1 * 1024 * 1024 else "flash-lite"
        self._vision_integrator.reset_turn(turn_id)

        import base64
        image_data = base64.standard_b64encode(resolved.read_bytes()).decode("utf-8")
        suffix = resolved.suffix.lower().lstrip(".")
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime = mime_map.get(suffix, "image/jpeg")

        if self._vision_integrator.model in ("haiku", "sonnet"):
            # Anthropic format
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": mime, "data": image_data}},
                    {"type": "text", "text": f"User question: {user_text}"}
                ]
            }]
        else:
            # Gemini multimodal format
            raw_bytes = resolved.read_bytes()
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image", "data": raw_bytes, "mime": mime},
                    {"type": "text", "text": f"User question: {user_text}"},
                ]
            }]

        return await self._run_vision(messages, turn_id)

    # ------------------------------------------------------------------
    # Live video frame ingestion
    # ------------------------------------------------------------------

    def ingest_frame(self, frame_bytes: bytes, mime: str = "image/jpeg") -> None:
        """
        Called for every incoming video frame (e.g. from Pipecat ImageRawFrame).
        Samples at VIDEO_SAMPLE_INTERVAL, drops unchanged frames, buffers the rest.
        Non-blocking — actual VLM call is triggered by flush_frames().
        """
        sample_interval = _settings.get("video_sample_interval")
        max_frames = int(_settings.get("video_max_frames"))
        change_threshold = _settings.get("video_change_threshold")

        now = time.monotonic()
        if now - self._last_sample_time < sample_interval:
            return
        self._last_sample_time = now

        # Skip if scene hasn't changed meaningfully
        if self._last_frame is not None:
            diff = _rms_diff(self._last_frame, frame_bytes)
            if diff < change_threshold:
                logger.debug("[Vision] Frame unchanged (RMS %.1f) — skipping.", diff)
                return

        self._last_frame = frame_bytes
        self._frame_buffer.append((frame_bytes, mime))

        if len(self._frame_buffer) > max_frames:
            self._frame_buffer = self._frame_buffer[-max_frames:]

    async def flush_frames(self, context: str, turn_id: str) -> dict | None:
        """
        Analyse buffered video frames. Call this when the brain needs visual context
        from the live stream (e.g. at the start of a conversation turn).
        Clears the buffer after the call.
        """
        if not self._frame_buffer:
            return None

        frames = list(self._frame_buffer)
        self._frame_buffer.clear()
        self._vision_integrator.reset_turn(turn_id)

        parts: list[dict] = []
        for frame_bytes, mime in frames:
            parts.append({"type": "image", "data": frame_bytes, "mime": mime})
        parts.append({
            "type": "text",
            "text": f"These are {len(frames)} frames sampled from a live video stream. {context}"
        })

        messages = [{"role": "user", "content": parts}]
        logger.debug("[Vision] Analysing %d video frames.", len(frames))
        return await self._run_vision(messages, turn_id)

    # ------------------------------------------------------------------
    # Shared VLM call
    # ------------------------------------------------------------------

    async def _run_vision(self, messages: list[dict], turn_id: str) -> dict | None:
        try:
            import json
            raw = await self._vision_integrator.call(messages)
            vision_features = json.loads(raw)
        except Exception as e:
            logger.error("[Vision] VLM call failed — continuing without visual context: %s", e)
            return None

        await self._bus.publish_dict("vision.features", vision_features, source=CLUSTER)
        logger.debug("[Vision] %s", vision_features.get("description", "")[:80])
        return vision_features
