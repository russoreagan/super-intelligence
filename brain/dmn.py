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

PREFETCHER_SYSTEM = """You are the proactive context-prefetcher of an AI brain.
The entity has been musing about the recent conversation. Your job: identify
which TOPICS or ENTITIES the user is likely to come back to, so the brain can
proactively pull related memory and have it ready.

Given the recent conversation and self-model, return JSON:
{
  "queries": [
    {"topic": string, "reason": string},
    ...
  ]
}

Return between 0 and 3 queries. Each topic should be a short, search-friendly
noun phrase (e.g. "audio bleed troubleshooting", "Ableton plugin choices",
"Russ's kid"). Skip topics already saturated in the immediate conversation
context. Return ONLY JSON."""

ANTICIPATOR_SYSTEM = """You are the anticipatory thinking process of an AI brain.
The entity has JUST asked the user a question and is now waiting for the answer.
Simulate the 2-3 most likely answers the user might give, and for each one sketch
a short response the brain could give back. This is preparation — not commitment.

Given the recent conversation, the entity's last (question-ending) message, and what
you know about the user, return JSON:
{
  "scenarios": [
    {
      "user_answer": string,        // a plausible thing the user says next
      "response_sketch": string,    // 1-2 sentence sketch of how to respond
      "context_needed": [string]    // facts/memory that would help — empty if none
    },
    ...
  ]
}
Return between 1 and 3 scenarios. Return ONLY JSON."""


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

        self._anticipator_cell = IntegratorCell(
            name="anticipator",
            cluster="dmn",
            model="flash-lite",
            system_prompt=ANTICIPATOR_SYSTEM,
            topics=["stream.anticipation"],
            max_calls_per_turn=1,
        )
        self._anticipator_cell.set_router(router)

        self._prefetcher_cell = IntegratorCell(
            name="prefetcher",
            cluster="dmn",
            model="flash-lite",
            system_prompt=PREFETCHER_SYSTEM,
            topics=["stream.prefetch"],
            max_calls_per_turn=1,
        )
        self._prefetcher_cell.set_router(router)

        # Predicted next input (used by temporal lobe predictor as a warm hint)
        self.predicted_next: dict | None = None
        # When the brain's last response ended with a question, the DMN runs
        # an anticipator that pre-generates response sketches for likely
        # answers. Cleared once the user actually replies.
        self.last_was_question: bool = False
        self.last_assistant_message: str = ""
        # Most recent anticipation scenarios — surfaced to next turn's drafter
        # as "you already started thinking about this" context.
        self.anticipations: list[dict] = []
        # Proactively fetched context: list of {topic, snippets} the prefetcher
        # pulled from memory while idle. Consumed by next turn's drafter.
        self.prefetched: list[dict] = []

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

    def recent_thoughts(self, n: int = 5) -> list[str]:
        """Return the last N internal thoughts the brain had between turns.
        Consumed by run.py to seed the next turn's drafter context — so the
        entity can reference what it was musing about when the user speaks."""
        return list(self._recent_thoughts)[-n:]

    def note_last_response(self, response: str) -> None:
        """Called by run.py after each turn end. Records whether the entity's
        last message ended with a question — if so, the DMN's next tick will
        also run the anticipator to pre-prepare for likely user answers."""
        self.last_assistant_message = (response or "").strip()
        # Simple heuristic: ends with '?' OR final clause looks like a Q
        text = self.last_assistant_message
        self.last_was_question = (
            text.endswith("?")
            or any(text.lower().endswith(p) for p in
                   ("right?", "yeah?", "huh?", "ok?", "okay?", "yes?"))
        )
        # New turn arriving = stale anticipations go away (the user already replied)
        self.anticipations = []

    def take_anticipations(self) -> list[dict]:
        """Pop the anticipation scenarios so they're consumed exactly once."""
        out, self.anticipations = self.anticipations, []
        return out

    def take_prefetched(self) -> list[dict]:
        """Pop the prefetched-context items so they're consumed exactly once."""
        out, self.prefetched = self.prefetched, []
        return out

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

        # 3. Anticipator — if the entity's last message ended with a question,
        # pre-think 2-3 likely user answers and sketch responses for each.
        # Runs once per question (then anticipations get consumed by the next
        # actual turn). Skips if we already have anticipations queued.
        if self.last_was_question and not self.anticipations:
            await self._run_anticipator(turn_id)

        # 4. Prefetcher — every 4th tick, identify topics likely to come up
        # again and proactively pull related episodes from memory. Skip if
        # we already have prefetched context waiting (next turn will use it).
        if (self._thought_count % 4 == 0
                and self._hippocampus is not None
                and not self.prefetched):
            await self._run_prefetcher(turn_id)

    async def _run_prefetcher(self, turn_id: str) -> None:
        self._prefetcher_cell.reset_turn(turn_id + "_pre")
        prompt = self._last_context or "No context yet."
        raw = await self._prefetcher_cell.call([{"role": "user", "content": prompt}])
        try:
            parsed = json.loads(raw)
            queries = parsed.get("queries", []) or []
        except Exception as e:
            logger.debug("[Background reflection] Prefetcher parse failed: %s", e)
            return

        if not queries:
            return

        # Run each recall in parallel (capped to 3); pull the schema + episode
        # text for each topic and cache as prefetched_context.
        async def _one_query(q: dict) -> dict | None:
            topic = str(q.get("topic", "")).strip()
            reason = str(q.get("reason", "")).strip()
            if not topic:
                return None
            try:
                result = await self._hippocampus.recall(
                    query=topic,
                    entities=[topic],
                    turn_id=turn_id + "_pre",
                    embedding_fn=self._router.embed,
                )
                snippets = []
                if result.get("episodes"):
                    snippets.append(result["episodes"][:400])
                if result.get("schema"):
                    snippets.append(result["schema"][:300])
                joined = "\n".join(s for s in snippets if s.strip())
                if not joined:
                    return None
                return {"topic": topic, "reason": reason, "snippets": joined}
            except Exception as e:
                logger.debug("[Background reflection] Prefetcher recall failed for %r: %s",
                             topic, e)
                return None

        results = await asyncio.gather(
            *(_one_query(q) for q in queries[:3]),
            return_exceptions=False,
        )
        self.prefetched = [r for r in results if r]
        if self.prefetched:
            await self._bus.publish_dict(
                "stream.prefetch",
                {"items": self.prefetched, "ts": time.time()},
                source="dmn",
            )
            logger.info("[Background reflection] Prefetched context for %d topics: %s",
                        len(self.prefetched),
                        ", ".join(p["topic"][:30] for p in self.prefetched))

    async def _run_anticipator(self, turn_id: str) -> None:
        self._anticipator_cell.reset_turn(turn_id + "_ant")
        prompt = (
            f"{self._last_context or 'No context yet.'}\n\n"
            f"Your last message (which ended with a question): "
            f"{self.last_assistant_message[:400]!r}\n\n"
            "Pre-think the user's likely answers and your responses."
        )
        raw = await self._anticipator_cell.call([{"role": "user", "content": prompt}])
        try:
            parsed = json.loads(raw)
            scenarios = parsed.get("scenarios", []) or []
            # Normalize + cap
            self.anticipations = [
                {
                    "user_answer": str(s.get("user_answer", ""))[:200],
                    "response_sketch": str(s.get("response_sketch", ""))[:300],
                    "context_needed": list(s.get("context_needed", []) or [])[:5],
                }
                for s in scenarios[:3]
                if s.get("user_answer") and s.get("response_sketch")
            ]
            if self.anticipations:
                await self._bus.publish_dict(
                    "stream.anticipation",
                    {"scenarios": self.anticipations, "ts": time.time()},
                    source="dmn",
                )
                logger.info("[Background reflection] Anticipated %d follow-up scenarios",
                            len(self.anticipations))
        except Exception as e:
            logger.debug("[Background reflection] Anticipator parse failed: %s", e)
