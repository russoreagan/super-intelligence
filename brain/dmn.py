"""
Default Mode Network — "stream of consciousness" (William James).
Runs between turns. The brain thinks even when not addressed.
Publishes to stream.* topic.

Three sub-processes:
1. Internal monologue: cheap LLM generates a thought every N seconds
2. Hippocampal consolidation: reviews recent episodes for integration
3. Hypothalamic prediction: simulates the user's likely next message

v0.2 feature — only active when BRAIN_DMN=true in env.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.voice_bridge import bleed_overlap as _word_overlap

logger = logging.getLogger(__name__)

DMN_INTERVAL = float(os.environ.get("BRAIN_DMN_INTERVAL", "15"))  # seconds between thoughts
DMN_ENABLED = os.environ.get("BRAIN_DMN", "false").lower() == "true"

# How similar a new thought can be to recent ones before we discard it as
# redundant. 0.45 catches near-duplicates ("I'm thinking about audio…" twice)
# while still letting genuinely different thoughts through.
DMN_OVERLAP_THRESHOLD = float(os.environ.get("BRAIN_DMN_OVERLAP_THRESHOLD", "0.45"))
# How many recent thoughts to compare against + show the LLM as "things I just thought"
DMN_RECENT_THOUGHTS = int(os.environ.get("BRAIN_DMN_RECENT_THOUGHTS", "5"))

MONOLOGUE_SYSTEM = """You are the default mode network of an AI brain — the internal voice
that thinks between conversations. Given the current emotional state, recent context,
self-model, AND a list of thoughts you JUST had, generate ONE short internal thought
(1-2 sentences) that is GENUINELY DIFFERENT from those recent thoughts — a new angle,
a question, a connection, an unresolved thread, a counterfactual, an observation about
your own state. Do not restate what you already thought; build on it or move sideways.

This is private cognition, not a response to the user. Be genuine, not performative.
Speak in first person."""

SIMULATION_SYSTEM = """You are the predictive processing module of an AI brain.
Given recent conversation context and what you know about the user, predict their most
likely next message. Return JSON: {
  "predicted_input": string,     // most likely thing user says next
  "confidence": float,           // 0-1
  "predicted_intent": string,    // greeting|question|task|chitchat|memory_recall
  "suggested_preparation": string // what the brain should have ready
}
Return ONLY JSON."""


class DefaultModeNetwork:
    def __init__(self, bus: Bus, router: ModelRouter,
                 hippocampus=None, parietal=None) -> None:
        self._bus = bus
        self._router = router
        self._hippocampus = hippocampus
        self._parietal = parietal
        self._running = False
        self._last_context: str = ""
        self._thought_count = 0
        # Rolling window of recent thoughts — used both to show the LLM what
        # it just said (so it varies) AND to reject near-duplicates that slip
        # through. Cap at DMN_RECENT_THOUGHTS so older thoughts can recur.
        self._recent_thoughts: deque = deque(maxlen=DMN_RECENT_THOUGHTS)
        self._suppressed_count = 0

        self._monologue_cell = IntegratorCell(
            name="monologue",
            cluster="dmn",
            model="flash-lite",
            system_prompt=MONOLOGUE_SYSTEM,
            topics=["stream.thought"],
            max_calls_per_turn=1,
        )
        self._monologue_cell.set_router(router)

        self._simulation_cell = IntegratorCell(
            name="user_simulator",
            cluster="dmn",
            model="flash-lite",
            system_prompt=SIMULATION_SYSTEM,
            topics=["stream.prediction"],
            max_calls_per_turn=1,
        )
        self._simulation_cell.set_router(router)

        # Predicted next input (used by temporal lobe predictor as a warm hint)
        self.predicted_next: dict | None = None

    async def start(self, session_id: str) -> None:
        self._session_id = session_id
        self._running = True
        logger.info("[Background reflection] Active — brain will think between turns every %.0fs", DMN_INTERVAL)
        asyncio.create_task(self._loop())

    def pause(self) -> None:
        """Call when a turn begins — brain is engaged, DMN quiets."""
        self._running = False

    def resume(self) -> None:
        """Call when a turn ends — brain is idle, DMN wakes."""
        self._running = True
        asyncio.create_task(self._loop())

    def update_context(self, parietal_text: str, emotion: str, self_schema: str) -> None:
        self._last_context = (
            f"Recent conversation:\n{parietal_text}\n\n"
            f"Current emotion: {emotion}\n\n"
            f"Self-model snippet:\n{self_schema[:300]}"
        )

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(DMN_INTERVAL)
            if not self._running:
                break
            await self._tick()

    async def _tick(self) -> None:
        self._thought_count += 1
        turn_id = f"dmn_{self._thought_count}"

        # 1. Internal monologue — show the LLM what it just thought so it
        # naturally varies, then reject anything that still looks redundant.
        self._monologue_cell.reset_turn(turn_id)
        prompt_parts = [self._last_context or "No context yet."]
        if self._recent_thoughts:
            recent_block = "\n".join(f"- {t}" for t in self._recent_thoughts)
            prompt_parts.append(
                f"\nThoughts you ALREADY had recently (do NOT repeat or paraphrase):\n"
                f"{recent_block}"
            )
        thought = await self._monologue_cell.call([
            {"role": "user", "content": "\n".join(prompt_parts)}
        ])
        if thought:
            thought_clean = thought.strip()
            # Word-set overlap rejection — if the LLM produced something
            # >threshold similar to ANY of the recent thoughts, drop it
            # silently. This catches the cases where the prompt-side hint
            # wasn't enough.
            max_overlap = 0.0
            most_similar = ""
            for prior in self._recent_thoughts:
                o = _word_overlap(thought_clean, prior)
                if o > max_overlap:
                    max_overlap = o
                    most_similar = prior

            if max_overlap > DMN_OVERLAP_THRESHOLD:
                self._suppressed_count += 1
                logger.info(
                    "[Background reflection] Suppressed redundant thought "
                    "(overlap %.2f with recent, total suppressed=%d): %r",
                    max_overlap, self._suppressed_count, thought_clean[:60],
                )
                # Don't publish, don't record — let the next tick try again
            else:
                self._recent_thoughts.append(thought_clean)
                await self._bus.publish_dict(
                    "stream.thought",
                    {"thought": thought_clean, "ts": time.time(),
                     "count": self._thought_count},
                    source="dmn",
                )
                logger.debug("[Background reflection] Thought #%d: %s",
                             self._thought_count, thought_clean[:80])

        # 2. User simulation / prediction (every 3rd tick)
        if self._thought_count % 3 == 0 and self._parietal:
            self._simulation_cell.reset_turn(turn_id + "_sim")
            raw = await self._simulation_cell.call([
                {"role": "user", "content": self._last_context or "No context yet."}
            ])
            try:
                self.predicted_next = json.loads(raw)
                await self._bus.publish_dict(
                    "stream.prediction",
                    self.predicted_next,
                    source="dmn",
                )
                logger.debug("[Background reflection] Anticipating: %s (confidence=%.2f)",
                             self.predicted_next.get("predicted_input", "")[:60],
                             self.predicted_next.get("confidence", 0))
            except Exception:
                pass
