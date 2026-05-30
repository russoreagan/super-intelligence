"""
Topic-tagged pub/sub blackboard with TTL, activation decay, hop limits.
Neuromodulator channels (ACh, DA, GABA, Glu) are persistent levels, not message queues.
Hormonal channels (5HT, CORT, OXT) are a slower endocrine layer that modulates neuromod dynamics.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

MAX_HOPS = 8
DEFAULT_TTL = 30.0  # seconds


@dataclass
class Message:
    topic: str
    payload: dict[str, Any]
    source: str
    confidence: float = 1.0
    ttl: float = DEFAULT_TTL
    hop_count: int = 0
    ts: float = field(default_factory=time.time)

    def hop(self) -> Message:
        return Message(
            topic=self.topic,
            payload=self.payload,
            source=self.source,
            confidence=self.confidence,
            ttl=self.ttl,
            hop_count=self.hop_count + 1,
            ts=self.ts,
        )

    @property
    def expired(self) -> bool:
        return time.time() - self.ts > self.ttl or self.hop_count >= MAX_HOPS


class Neuromodulators:
    """
    Five persistent scalar channels with exponential decay.
    ACh  = attention/novelty (broad curiosity signal)
    DA   = reward/valence
    GABA = inhibition/threat
    Glu  = general arousal/excitation
    NE   = norepinephrine — focused alertness; inverted-U performance curve
           (distinct from Glu: NE is threat-/salience-driven sharp focus,
            not just general activation)
    """

    DECAY = 0.85  # per-turn decay multiplier
    CHANNELS = ("ACh", "DA", "GABA", "Glu", "NE")
    # Absolute safety bounds — values can never sit below these regardless of
    # baseline. Distinct from the resting baseline (which is the homeostatic
    # setpoint and is persona-configurable). Rarely binding.
    _HARD_MIN = {"ACh": 0.02, "DA": 0.02, "GABA": 0.0, "Glu": 0.02, "NE": 0.02}
    # Historical resting/init values, used as the default baseline/start when no
    # persona (or older settings.json) overrides them. _DEF_BASELINE mirrors the
    # original _FLOORS; _DEF_INIT mirrors the original warm-start levels.
    _DEF_BASELINE = {"ACh": 0.10, "DA": 0.30, "GABA": 0.02, "Glu": 0.15, "NE": 0.15}
    _DEF_INIT = {"ACh": 0.20, "DA": 0.50, "GABA": 0.05, "Glu": 0.30, "NE": 0.25}

    def __init__(self) -> None:
        from brain.settings import settings as _s  # local import: settings loads before Bus()

        self._baseline: dict[str, float] = {
            ch: float(_s.get(f"chem_baseline_{ch}", self._DEF_BASELINE[ch])) for ch in self.CHANNELS
        }
        self._levels: dict[str, float] = {
            ch: float(_s.get(f"chem_init_{ch}", self._DEF_INIT[ch])) for ch in self.CHANNELS
        }
        self._model: str = str(_s.get("chem_decay_model", "baseline"))

    def add(self, channel: str, delta: float) -> None:
        self._levels[channel] = max(0.0, min(1.0, self._levels[channel] + delta))

    def get(self, channel: str) -> float:
        return self._levels[channel]

    def decay(self, turns: float = 1.0) -> None:
        """Relax all channels toward their resting baselines.

        turns > 1.0 means more time passed than the reference interval (slow
        conversation); turns < 1.0 means less (rapid back-and-forth).

        "baseline" (default): homeostatic relaxation toward the setpoint from
            both directions — a depleted channel recovers gradually, an elevated
            one settles gradually. "floor": legacy clamp that snaps anything
            below baseline back up in a single turn (kept for regression/rollback).
        """
        rate = self.DECAY**turns
        for ch in self.CHANNELS:
            b = self._baseline[ch]
            if self._model == "floor":
                lvl = max(b, self._levels[ch] * rate)
            else:
                lvl = b + (self._levels[ch] - b) * rate
            self._levels[ch] = max(self._HARD_MIN[ch], min(1.0, lvl))

    def snapshot(self) -> dict[str, float]:
        return dict(self._levels)


class HormonalState:
    """
    Slow-timescale endocrine layer. Four channels:
      5HT  = serotonin      — affective baseline; contentment vs. dysphoria
      CORT = cortisol       — cumulative stress; builds under sustained threat
      OXT  = oxytocin       — trust/affiliation; grows with positive exchange
      AEA  = anandamide     — homeostatic buffer; rises when arousal is high,
                              suppresses NE + Glu, adds mild DA lift ("afterglow")
                              Decay ~0.90 — faster than other hormones but slower
                              than neurotransmitters; responds within a few turns.

    Decay rates are 5–100× slower than Neuromodulators.
    Acts as gain-control on neuromod effective values (DA floor, GABA sensitivity,
    NE/Glu suppression from AEA).
    """

    CHANNELS = ("5HT", "CORT", "OXT", "AEA")
    _DECAY = {"5HT": 0.995, "CORT": 0.970, "OXT": 0.998, "AEA": 0.930}
    # See Neuromodulators for the meaning of these three. The slow per-channel
    # rates above are exactly why baseline relaxation matters here: a depleted
    # 5HT/OXT should lift over many turns, not snap back in one.
    _HARD_MIN = {"5HT": 0.02, "CORT": 0.0, "OXT": 0.02, "AEA": 0.02}
    _DEF_BASELINE = {"5HT": 0.20, "CORT": 0.02, "OXT": 0.15, "AEA": 0.10}
    _DEF_INIT = {"5HT": 0.50, "CORT": 0.05, "OXT": 0.30, "AEA": 0.30}

    def __init__(self) -> None:
        from brain.settings import settings as _s  # local import: settings loads before Bus()

        self._baseline: dict[str, float] = {
            ch: float(_s.get(f"chem_baseline_{ch}", self._DEF_BASELINE[ch])) for ch in self.CHANNELS
        }
        self._levels: dict[str, float] = {
            ch: float(_s.get(f"chem_init_{ch}", self._DEF_INIT[ch])) for ch in self.CHANNELS
        }
        self._model: str = str(_s.get("chem_decay_model", "baseline"))

    def add(self, channel: str, delta: float) -> None:
        self._levels[channel] = max(0.0, min(1.0, self._levels[channel] + delta))

    def get(self, channel: str) -> float:
        return self._levels[channel]

    def decay(self, turns: float = 1.0) -> None:
        """Relax all channels toward their resting baselines at each channel's
        own rate. See Neuromodulators.decay for the "baseline"/"floor" models."""
        for ch in self.CHANNELS:
            rate = self._DECAY[ch] ** turns
            b = self._baseline[ch]
            if self._model == "floor":
                lvl = max(b, self._levels[ch] * rate)
            else:
                lvl = b + (self._levels[ch] - b) * rate
            self._levels[ch] = max(self._HARD_MIN[ch], min(1.0, lvl))

    def snapshot(self) -> dict[str, float]:
        return dict(self._levels)

    # ── Modulation helpers (used by hypothalamus) ─────────────────────────────

    def da_offset(self, sht_lift: float, oxt_lift: float, cort_suppress: float) -> float:
        """Net DA floor shift from hormonal state."""
        return (
            self._levels["5HT"] * sht_lift
            + self._levels["OXT"] * oxt_lift
            - self._levels["CORT"] * cort_suppress
        )

    def gaba_scale(self, cort_amplify: float, oxt_buffer: float) -> float:
        """GABA sensitivity multiplier. 1.0 = no change."""
        return max(
            0.5, 1.0 + self._levels["CORT"] * cort_amplify - self._levels["OXT"] * oxt_buffer
        )

    def aea_suppress(
        self, ne_rate: float, glu_rate: float, base: float = 0.30
    ) -> tuple[float, float]:
        """
        Compute NE and Glu scale factors from AEA homeostatic suppression.
        Only activates above the resting AEA baseline (default 0.30) so that
        normal-level AEA has no effect. Returns (ne_scale, glu_scale), both ≥ 0.5.
        """
        excess = max(0.0, self._levels["AEA"] - base)
        ne_scale = max(0.5, 1.0 - excess * ne_rate)
        glu_scale = max(0.5, 1.0 - excess * glu_rate)
        return ne_scale, glu_scale


class Bus:
    """
    Async pub/sub blackboard. Each topic has a queue of live messages.
    Subscribers register interest; the bus fan-outs on publish.
    Dead-letter: expired messages are silently dropped.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self.neuromod = Neuromodulators()
        self.hormonal = HormonalState()
        self._lock = asyncio.Lock()

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(topic, []).append(q)
        return q

    def subscribe_prefix(self, prefix: str) -> asyncio.Queue:
        """Subscribe to all topics starting with prefix (registered at publish time)."""
        sentinel = f"__prefix__{prefix}"
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(sentinel, []).append(q)
        return q

    async def publish(self, msg: Message) -> None:
        if msg.expired:
            return
        # exact-topic subscribers
        for q in self._subscribers.get(msg.topic, []):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)
        # prefix subscribers
        for key, qs in self._subscribers.items():
            if key.startswith("__prefix__"):
                prefix = key[len("__prefix__") :]
                if msg.topic.startswith(prefix):
                    for q in qs:
                        with contextlib.suppress(asyncio.QueueFull):
                            q.put_nowait(msg)

    async def publish_dict(self, topic: str, payload: dict, source: str, **kwargs) -> None:
        await self.publish(Message(topic=topic, payload=payload, source=source, **kwargs))
