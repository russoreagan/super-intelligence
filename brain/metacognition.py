"""
Metacognition — the brain watching itself (v0.3).
Higher-Order Theories of Consciousness (Rosenthal): a mental state is conscious
only when there is a higher-order thought about that state. This cell makes the
brain's states conscious in the technical HOT sense by representing them.

Watches: cost per turn, drafter win rates, neuromod variance, surprise trends.
Posts to meta.* topic. Other clusters can subscribe.
Updates self.md "Current mood signature" section.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from statistics import mean, stdev

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.security import sanitize_fact

logger = logging.getLogger(__name__)

META_ENABLED = os.environ.get("BRAIN_METACOGNITION", "false").lower() == "true"
META_INTERVAL = float(os.environ.get("BRAIN_META_INTERVAL", "30"))  # seconds

SELF_REFLECTION_SYSTEM = """You are the metacognitive layer of an AI brain — the part that
watches itself think. Given behavioral statistics from recent turns, write a brief
first-person reflection on what patterns you notice in your own processing.
Focus on: What am I spending compute on? What's surprising me often? How has my
emotional state trended? What should I adjust?
Keep it to 2-3 sentences. This is genuine self-reflection, not performance.
Return JSON: {
  "reflection": string,
  "adjustment_suggestion": string  // one concrete thing to try differently
}
Return ONLY JSON."""


class MetacognitionCell:
    def __init__(self, bus: Bus, router: ModelRouter, schema_store=None) -> None:
        self._bus = bus
        self._router = router
        self._schema = schema_store

        self._turn_stats: deque[dict] = deque(maxlen=20)
        self._neuromod_history: deque[dict] = deque(maxlen=50)
        self._reflection_count = 0

        self._reflector = IntegratorCell(
            name="self_reflector",
            cluster="metacognition",
            model="flash-lite",
            system_prompt=SELF_REFLECTION_SYSTEM,
            topics=["meta.reflection"],
            max_calls_per_turn=1,
            locality="local",
            sensitivity="sensitive",
        )
        self._reflector.set_router(router)

    def record_turn(self, turn_id: str, llm_calls: int, elapsed_s: float,
                    emotion: str, neuromod: dict, surprise_score: float,
                    drafter_won: str | None = None) -> None:
        self._turn_stats.append({
            "turn_id": turn_id,
            "llm_calls": llm_calls,
            "elapsed_s": elapsed_s,
            "emotion": emotion,
            "surprise_score": surprise_score,
            "drafter_won": drafter_won,
            "ts": time.time(),
        })
        self._neuromod_history.append({"ts": time.time(), **neuromod})

    def _compute_stats(self) -> dict:
        if not self._turn_stats:
            return {}

        calls = [t["llm_calls"] for t in self._turn_stats]
        surprises = [t["surprise_score"] for t in self._turn_stats]
        emotions = [t["emotion"] for t in self._turn_stats]
        elapsed = [t["elapsed_s"] for t in self._turn_stats]

        emotion_counts: dict[str, int] = {}
        for e in emotions:
            emotion_counts[e] = emotion_counts.get(e, 0) + 1
        dominant_emotion = max(emotion_counts, key=emotion_counts.get) if emotion_counts else "neutral"

        drafter_wins: dict[str, int] = {}
        for t in self._turn_stats:
            if t.get("drafter_won"):
                dw = t["drafter_won"]
                drafter_wins[dw] = drafter_wins.get(dw, 0) + 1

        nm_recent = list(self._neuromod_history)[-10:]
        nm_means = {}
        if nm_recent:
            for ch in ("DA", "GABA", "ACh", "Glu"):
                vals = [n.get(ch, 0) for n in nm_recent if ch in n]
                nm_means[ch] = round(mean(vals), 3) if vals else 0.0

        return {
            "turn_count": len(self._turn_stats),
            "avg_llm_calls": round(mean(calls), 1),
            "avg_surprise": round(mean(surprises), 3),
            "avg_elapsed_s": round(mean(elapsed), 2),
            "dominant_emotion": dominant_emotion,
            "emotion_distribution": emotion_counts,
            "drafter_win_rates": drafter_wins,
            "neuromod_averages": nm_means,
        }

    async def start(self) -> None:
        if not META_ENABLED:
            logger.debug("[Self-monitor] Disabled — set BRAIN_METACOGNITION=true to enable periodic self-reflection")
            return
        logger.info("[Self-monitor] Active — will reflect on behaviour every %.0fs", META_INTERVAL)
        asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(META_INTERVAL)
            await self._reflect()

    async def _reflect(self) -> None:
        stats = self._compute_stats()
        if not stats:
            return

        self._reflection_count += 1
        turn_id = f"meta_{self._reflection_count}"

        # Publish raw stats to meta.stats
        await self._bus.publish_dict("meta.stats", stats, source="metacognition")

        # LLM reflection
        self._reflector.reset_turn(turn_id)
        prompt = f"Recent processing statistics:\n{json.dumps(stats, indent=2)}"
        raw = await self._reflector.call([{"role": "user", "content": prompt}])

        reflection: dict = {}
        try:
            reflection = json.loads(raw)
        except Exception:
            import re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    reflection = json.loads(m.group(0))
                except Exception:
                    pass

        if reflection:
            await self._bus.publish_dict("meta.reflection", reflection, source="metacognition")
            logger.info("[Self-monitor] %s", reflection.get("reflection", "")[:120])

            # Update self.md with current mood signature
            if self._schema:
                mood_line = (
                    f"DA={stats['neuromod_averages'].get('DA', 0):.2f} "
                    f"GABA={stats['neuromod_averages'].get('GABA', 0):.2f} "
                    f"ACh={stats['neuromod_averages'].get('ACh', 0):.2f} "
                    f"dominant={stats['dominant_emotion']}"
                )
                fact = sanitize_fact(f"Mood signature: {mood_line}")
                if fact:
                    await self._schema.aappend_fact("self.md", fact)

    def summary(self) -> dict:
        return self._compute_stats()
