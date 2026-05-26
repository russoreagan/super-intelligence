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
import contextlib
import json
import logging
import os
import time
from collections import deque
from statistics import mean

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.security import sanitize_fact
from brain.settings import settings
from brain.utils import safe_json_parse

logger = logging.getLogger(__name__)

META_ENABLED = os.environ.get("BRAIN_METACOGNITION", "false").lower() == "true"


# ── Module-level relationship readers ────────────────────────────────────
# Standalone helpers so callers without a MetacognitionCell (e.g. run.py
# building the DMN's relationship context) can read the same fields the
# instance methods read. Pure regex-on-schema, no LLM, no side effects.

def read_affection_score(schema, speaker_name: str = "") -> int:
    """Read the current numeric affection score from a speaker's schema.
    Returns 0 on any failure or missing field (the neutral default)."""
    if schema is None:
        return 0
    try:
        import re
        if speaker_name:
            content = schema.read(schema.speaker_filename(speaker_name))
        else:
            content = schema.read("user.md")
        m = re.search(r"- Score:\s*(-?\d+)", content)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def read_familiarity(schema, speaker_name: str = "") -> str:
    """Read the familiarity tier ("new" / "acquainted" / "close") from a
    speaker's schema. Returns "new" on any failure — the most-reserved
    default for unknown speakers."""
    if schema is None:
        return "new"
    try:
        import re
        if speaker_name:
            content = schema.read(schema.speaker_filename(speaker_name))
        else:
            content = schema.read("user.md")
        m = re.search(r"- Familiarity:\s*(\w+)", content)
        return m.group(1).lower() if m else "new"
    except Exception:
        return "new"
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

        # Per-emotion cooldown (turns remaining until that override can fire again).
        # Prevents the entity from being "grateful" on five turns in a row when
        # the user keeps being warm — the appraisal should mark the moment, not
        # repeat itself into noise.
        self._override_cooldowns: dict[str, int] = {}
        self._cooldown_turns = int(settings.get("meta_cooldown_turns"))

        self._reflector = IntegratorCell(
            name="self_reflector",
            cluster="metacognition",
            model="haiku",
            system_prompt=SELF_REFLECTION_SYSTEM,
            topics=["meta.reflection"],
            max_calls_per_turn=1,
            sensitivity="sensitive",
        )
        self._reflector.set_router(router)

        # Self-monitor trigger — gates the reflection LLM call. ACh (curiosity
        # about own behaviour) lowers threshold → reflects more frequently.
        # GABA (defensive state) raises threshold → don't spiral when stressed.
        self._self_monitor_trigger = SwitchNeuron(
            "self_monitor_trigger", "metacognition", polarity="excitatory",
            threshold=0.5,
            modulators={"ACh": -0.10, "GABA": +0.10},
        )

    def record_turn(self, turn_id: str, llm_calls: int, elapsed_s: float,
                    emotion: str, neuromod: dict, surprise_score: float,
                    drafter_won: str | None = None,
                    features: dict | None = None,
                    draft_scores: list[dict] | None = None) -> None:
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

        # Decrement all cooldowns by one turn
        self._override_cooldowns = {
            e: c - 1 for e, c in self._override_cooldowns.items() if c > 1
        }

        # Appraise this turn for context-driven emotion override.
        # Fire-and-forget — must not block the run loop.
        with contextlib.suppress(RuntimeError):
            asyncio.create_task(self._appraise_and_emit(
                features or {}, neuromod, draft_scores or [],
            ))

    # ── Appraisal: detect context-driven emotions ─────────────────────────
    # These are emotions that pure neuromods can't produce — they require
    # self-other appraisal (embarrassed), accomplishment recognition (proud),
    # relational reading (flirty), or moral inference (apologetic). This is
    # what the prefrontal cortex does in biology: higher-order thought about
    # the affective state and the situation that produced it.

    def _affection_score(self, speaker_name: str = "") -> int:
        return read_affection_score(self._schema, speaker_name)

    def _familiarity(self, speaker_name: str = "") -> str:
        return read_familiarity(self._schema, speaker_name)

    # Public read-only accessors so external callers can fetch
    # affection/familiarity without poking at name-mangled helpers.
    def affection_score(self, speaker_name: str = "") -> int:
        return self._affection_score(speaker_name)

    def familiarity(self, speaker_name: str = "") -> str:
        return self._familiarity(speaker_name)

    def _appraise(self, features: dict, neuromod: dict, draft_scores: list[dict]) -> tuple[str | None, str]:
        """Return (emotion_override, reason). Order matters — first match wins.
        Reasons go into the bus payload for observability and debugging."""
        user_tone = (features.get("user_tone_toward_ai") or "neutral").lower()
        user_emotion = (features.get("user_emotion") or "unknown").lower()
        intent = (features.get("intent") or "other").lower()
        affection = self._affection_score(features.get("speaker_name", ""))

        # 1. Embarrassed — self-monitoring caught coherence/empathy failures.
        # Two or more drafts vetoed means the entity is genuinely "off" — the
        # appraisal of its own bad performance is what produces embarrassment.
        if draft_scores:
            vetoed = [d for d in draft_scores if d.get("vetoed")]
            if len(vetoed) >= 2:
                return "embarrassed", f"{len(vetoed)} drafts vetoed this turn"

        # 2. Apologetic — user is correcting or expressing displeasure AND
        # the previous turn shows we said something off.
        if (user_emotion in ("frustrated", "disappointed", "annoyed", "angry")
                and user_tone in ("dismissive", "impatient", "insulting")
                and len(self._turn_stats) >= 2):
            prev = self._turn_stats[-2]
            if prev.get("surprise_score", 0) > 0.5:
                return "apologetic", "user pushed back after a surprising prior turn"

        # 3. Sympathetic — user is struggling (regardless of whether they're
        # warm toward us). The entity should feel for them.
        if user_emotion in ("sad", "anxious", "distressed", "struggling",
                            "hurt", "lonely", "scared", "overwhelmed"):
            return "sympathetic", f"user emotion: {user_emotion}"

        # 4. Proud — well-executed turn (high score) with positive reception.
        # Must come BEFORE grateful: praise + accomplishment is pride, not just
        # gratitude. Specific patterns beat general ones.
        if draft_scores:
            selected = next((d for d in draft_scores if d.get("selected")), None)
            if selected and selected.get("overall", 0) > 0.85 \
                    and user_tone in ("warm", "praising"):
                return "proud", f"high-quality response (overall={selected['overall']:.2f}) received warmly"

        # 5. Grateful — user is praising without an obvious accomplishment to
        # be proud of. The entity's appraisal of being valued.
        if user_tone == "praising":
            return "grateful", "user praised the entity"

        # 6. Relieved — GABA dropped sharply this turn vs last (tension lifted).
        if len(self._neuromod_history) >= 2:
            prev_g = float(self._neuromod_history[-2].get("GABA", 0))
            cur_g = float(self._neuromod_history[-1].get("GABA", 0))
            if prev_g > 0.5 and cur_g < prev_g - settings.get("gaba_drop_threshold"):
                return "relieved", f"GABA dropped {prev_g:.2f}→{cur_g:.2f}"

        # 7. Disappointed — low DA + high salience (we wanted to engage but
        # the situation deflated).
        if float(neuromod.get("DA", 0.5)) < settings.get("da_threshold_disappointed") \
                and float(features.get("salience", 0.3)) > 0.6:
            return "disappointed", "low DA on a high-salience turn"

        # 8. Flirty — only with high affection AND warm playful context AND
        # not a serious/task topic. Read the room.
        if affection >= 40 and user_tone in ("warm", "joking", "praising") \
                and user_emotion in ("happy", "playful", "amused", "warm", "affectionate") \
                and intent not in ("hostile", "task", "informative", "epistemic"):
            return "flirty", f"high affection ({affection}) + warm playful context"

        return None, ""

    async def _appraise_and_emit(self, features: dict, neuromod: dict,
                                  draft_scores: list[dict]) -> None:
        candidate, reason = self._appraise(features, neuromod, draft_scores)
        if candidate is None:
            return
        if candidate in self._override_cooldowns:
            logger.debug("[Self-monitor] %s on cooldown (%d turns left) — skipped",
                         candidate, self._override_cooldowns[candidate])
            return
        self._override_cooldowns[candidate] = self._cooldown_turns
        try:
            await self._bus.publish_dict(
                "meta.emotion_override",
                {"emotion": candidate, "reason": reason, "ttl_turns": 1},
                source="metacognition",
            )
            logger.info("[Self-monitor] Appraisal → emotion=%s (%s)", candidate, reason)
        except Exception as e:
            logger.debug("[Self-monitor] failed to publish override: %s", e)

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

    def _chem_snapshot(self) -> dict[str, float]:
        """Merged neuromod + hormonal snapshot for switch modulation."""
        try:
            nm = self._bus.neuromod.snapshot()
        except Exception:
            nm = {}
        try:
            hs = self._bus.hormonal.snapshot()
        except Exception:
            hs = {}
        return {**nm, **hs}

    async def start(self) -> None:
        if not META_ENABLED:
            logger.debug("[Self-monitor] Disabled — set BRAIN_METACOGNITION=true to enable periodic self-reflection")
            return
        logger.info("[Self-monitor] Active — will reflect on behaviour every %.0fs", META_INTERVAL)
        asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.get("meta_interval") or META_INTERVAL)
            await self._reflect()

    async def _reflect(self) -> None:
        stats = self._compute_stats()
        if not stats:
            return

        self._reflection_count += 1
        turn_id = f"meta_{self._reflection_count}"

        # Chemistry gate: high GABA suppresses reflection (don't spiral when
        # stressed); high ACh invites it. Suppressed reflections still emit
        # the meta.stats event below (raw stats are free).
        chem = self._chem_snapshot()
        if not self._self_monitor_trigger.should_fire(0.6, chem, turn_id):
            logger.debug("[Self-monitor] Reflection LLM call suppressed by chemistry gate "
                          "(ACh=%.2f GABA=%.2f)", chem.get("ACh", 0), chem.get("GABA", 0))
            await self._bus.publish_dict("meta.stats", stats, source="metacognition")
            return
        self._self_monitor_trigger.fire(0.6, "reflection_engaged", snapshot=chem)

        # Publish raw stats to meta.stats
        await self._bus.publish_dict("meta.stats", stats, source="metacognition")

        # LLM reflection — runs under background resource policy (budgeted cloud)
        self._reflector.reset_turn(turn_id)
        prompt = f"Recent processing statistics:\n{json.dumps(stats, indent=2)}"
        self._router.enter_background_mode()
        try:
            raw = await self._reflector.call([{"role": "user", "content": prompt}])
        finally:
            self._router.exit_background_mode()

        reflection: dict = safe_json_parse(raw) or {}

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
