"""
Occipital Lobe — vision processing. Only active when image is present.
1 VLM integrator + 3 gating switches.
The VLM does all visual understanding in one call — no simulated vision pipeline.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron

logger = logging.getLogger(__name__)

CLUSTER = "occipital"

IMAGE_ROOT = Path(os.environ.get("BRAIN_IMAGE_DIR", str(Path.home() / "brain_images"))).expanduser().resolve()
IMAGE_ROOT.mkdir(parents=True, exist_ok=True)

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

VISION_SYSTEM = """You are the visual cortex of an AI brain.
Analyze the provided image and return structured JSON:
{
  "description": string,        // 1-2 sentence scene description
  "text_in_image": string,      // any text visible in the image (OCR)
  "key_entities": [string],     // objects, people, or concepts visible
  "chart_data": string | null,  // if this is a chart/diagram, summarize the data
  "emotional_tone": string,     // e.g. "neutral", "alarming", "joyful"
  "context_for_response": string // how this image relates to the user's question
}
Return ONLY JSON."""

MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5MB → use better VLM


class OccipitalCluster:
    def __init__(self, bus: Bus, router: ModelRouter) -> None:
        self._bus = bus
        self._router = router

        self._vision_integrator = IntegratorCell(
            name="vision_integrator",
            cluster=CLUSTER,
            model="flash",   # Gemini Flash multimodal; swap to "haiku" for better quality
            system_prompt=VISION_SYSTEM,
            topics=["sensory.image"],
            max_calls_per_turn=1,
        )
        self._vision_integrator.set_router(router)

        # 3 gating switches
        self._image_present = SwitchNeuron("image_present", CLUSTER)
        self._image_size_router = SwitchNeuron("image_size_router", CLUSTER)
        self._vision_needed = SwitchNeuron("vision_needed", CLUSTER)

    async def process(self, image_path: str, user_text: str, turn_id: str) -> dict | None:
        """
        Process an image. Returns vision features or None if image not needed.
        """
        if not image_path:
            return None

        path = Path(image_path)
        resolved = path.expanduser().resolve()

        # Extension allowlist check
        if resolved.suffix.lower() not in _ALLOWED_EXTENSIONS:
            logger.warning("[Vision] [Security] Blocked image — file type %r not allowed. Permitted: .jpg, .jpeg, .png, .gif, .webp", resolved.suffix)
            return None

        # Containment check — must be inside IMAGE_ROOT
        if not resolved.is_relative_to(IMAGE_ROOT):
            logger.warning("[Vision] [Security] Blocked image path — file is outside the allowed image folder (%s). Change the allowed folder with BRAIN_IMAGE_DIR in .env", resolved)
            return None

        if not resolved.exists():
            logger.warning("[Vision] Image file not found: %s", resolved)
            return None

        # Size check BEFORE reading bytes
        size = resolved.stat().st_size
        if size > MAX_IMAGE_SIZE_BYTES:
            logger.warning("[Vision] Image too large (%d bytes, limit is %d bytes) — skipping vision analysis: %s", size, MAX_IMAGE_SIZE_BYTES, resolved)
            return None

        # Use resolved path for all subsequent operations
        path = resolved

        # Size routing switch: larger files use higher-quality model
        if size > 1 * 1024 * 1024:  # > 1MB
            self._vision_integrator.model = "flash"
        else:
            self._vision_integrator.model = "flash-lite"

        # Vision-needed switch: is the user's question actually about the image?
        # If question has no visual reference words, skip the VLM
        visual_keywords = ["image", "picture", "photo", "diagram", "chart", "what is this",
                           "what does", "show", "see", "look", "color", "text in"]
        if not any(k in user_text.lower() for k in visual_keywords) and "?" not in user_text:
            logger.debug("[Vision] Skipping image analysis — user's message doesn't reference the image")
            return None

        self._vision_integrator.reset_turn(turn_id)

        try:
            import base64
            image_data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
            suffix = path.suffix.lower().lstrip(".")
            mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
            mime = mime_map.get(suffix, "image/jpeg")

            # For Anthropic models: use vision message format
            if self._vision_integrator.model in ("haiku", "sonnet"):
                messages = [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64",
                                                      "media_type": mime, "data": image_data}},
                        {"type": "text", "text": f"User question: {user_text}"}
                    ]
                }]
            else:
                # Google Gemini: include image inline
                messages = [{"role": "user", "content": f"[IMAGE: {image_path}]\n{user_text}"}]

            import json
            raw = await self._vision_integrator.call(messages)
            vision_features = json.loads(raw)
        except Exception as e:
            logger.error("[Vision] Image analysis API call failed — continuing without image context: %s", e)
            return None

        await self._bus.publish_dict("vision.features", vision_features, source=CLUSTER)
        logger.debug("[Vision] Image analysed: %s",
                     vision_features.get("description", "")[:60])
        return vision_features
