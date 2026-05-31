"""
Topic-tagged pub/sub blackboard with TTL, activation decay, hop limits.
Neuromodulator channels (ACh, DA, GABA, Glu) are persistent levels, not message queues.
Hormonal channels (5HT, CORT, OXT) are a slower endocrine layer that modulates neuromod dynamics.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from brain.settings import settings

MAX_HOPS = 8
DEFAULT_TTL = 30.0  # seconds

# Topic concentration state machine (Phase 2 — colony features).
# UNARMED: cold start — concentration has never crossed the arm threshold; its
#          silence is NOT meaningful. ARMED: was active. QUIET: was ARMED and has
#          since decayed below the silence floor — silence-as-signal.
CONC_UNARMED = "unarmed"
CONC_ARMED = "armed"
CONC_QUIET = "quiet"


@dataclass
class Message:
    topic: str
    payload: dict[str, Any]
    source: str
    confidence: float = 1.0
    ttl: float = DEFAULT_TTL
    hop_count: int = 0
    ts: float = field(default_factory=time.time)
    # Phase 3 (colony features): a message can carry a slow PRIMER effect
    # alongside its fast releaser payload — a dict of hormonal channel → nudge.
    # Applied to HormonalState by the hypothalamus on drain. None = releaser-only
    # (backward compatible).
    primer: dict[str, float] | None = None

    def hop(self) -> Message:
        return Message(
            topic=self.topic,
            payload=self.payload,
            source=self.source,
            confidence=self.confidence,
            ttl=self.ttl,
            hop_count=self.hop_count + 1,
            ts=self.ts,
            primer=self.primer,
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
        # ── Phase 2: topic concentration / quorum / silence (colony features) ──
        # Only topics explicitly registered via track_concentration() accumulate.
        # `_tracked` maps topic → optional magnitude_fn(payload)->float (default
        # uses msg.confidence). State machine + context ring are per-topic.
        self._tracked: dict[str, Callable[[dict], float] | None] = {}
        self._concentration: dict[str, float] = {}
        self._conc_ts: dict[str, float] = {}
        self._topic_status: dict[str, str] = {}
        self._conc_context: dict[str, deque] = {}
        self._zero_since: dict[str, float] = {}
        self._quiet_onset: set[str] = set()
        # C4 (colony-features-ii): previous (level, ts) sample per topic + last slope,
        # so quorum can fire on a fast RISE, not just an absolute level.
        self._conc_prev: dict[str, tuple[float, float]] = {}
        self._conc_slope: dict[str, float] = {}
        # ── Phase 3/4: pending primer nudges + recruitment levels ─────────────
        self._pending_primers: dict[str, float] = {}
        self._recruitment: dict[str, float] = {}
        self._recruit_ts: dict[str, float] = {}

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

    # ── Phase 2: topic concentration / quorum / silence ───────────────────────

    def track_concentration(
        self, topic: str, magnitude_fn: Callable[[dict], float] | None = None
    ) -> None:
        """Register `topic` for concentration tracking. `magnitude_fn(payload)`
        returns the per-message contribution (defaults to msg.confidence). Only
        registered topics accumulate — keeps the mechanism scoped (threat first)."""
        self._tracked[topic] = magnitude_fn
        self._topic_status.setdefault(topic, CONC_UNARMED)

    def _conc_settings(self) -> tuple[float, float, float, float, float]:
        return (
            float(settings.get("colony_conc_half_life_s", 45.0)),
            float(settings.get("colony_conc_cap", 10.0)),
            float(settings.get("colony_arm_threshold", 1.0)),
            float(settings.get("colony_silence_floor", 0.15)),
            float(settings.get("colony_silence_disarm_s", 600.0)),
        )

    def _decay_to(self, topic: str, now: float) -> None:
        """Apply exponential half-life decay to a topic's concentration up to `now`."""
        last = self._conc_ts.get(topic)
        if last is None:
            self._conc_ts[topic] = now
            self._concentration.setdefault(topic, 0.0)
            return
        elapsed = max(0.0, now - last)
        hl, *_ = self._conc_settings()
        if hl > 0 and elapsed > 0 and topic in self._concentration:
            self._concentration[topic] *= 0.5 ** (elapsed / hl)
        self._conc_ts[topic] = now

    def _update_state(self, topic: str, now: float) -> None:
        """Advance the UNARMED→ARMED→QUIET state machine (and disarm on long zero-dwell)."""
        level = self._concentration.get(topic, 0.0)
        status = self._topic_status.get(topic, CONC_UNARMED)
        _, _, arm, floor, disarm_s = self._conc_settings()
        if level >= arm:
            status = CONC_ARMED  # (re-)arm; covers UNARMED→ARMED and QUIET→ARMED
            self._zero_since.pop(topic, None)
        elif status == CONC_ARMED and level < floor:
            status = CONC_QUIET  # fresh ARMED→QUIET edge
            self._quiet_onset.add(topic)
        # Disarm back to UNARMED after a long dwell at ~zero so a one-time burst
        # long ago doesn't make permanent silence "meaningful".
        eps = floor * 0.1
        if level <= eps:
            z = self._zero_since.get(topic)
            if z is None:
                self._zero_since[topic] = now
            elif (now - z) >= disarm_s:
                status = CONC_UNARMED
                self._zero_since.pop(topic, None)
                self._quiet_onset.discard(topic)
        else:
            self._zero_since.pop(topic, None)
        self._topic_status[topic] = status

    def _accumulate(self, msg: Message, now: float | None = None) -> None:
        """Add a message's contribution to its topic concentration (colony-gated)."""
        if not settings.get("colony_features", 0):
            return
        topic = msg.topic
        if topic not in self._tracked:
            return
        now = time.time() if now is None else now
        self._decay_to(topic, now)
        fn = self._tracked[topic]
        try:
            mag = float(fn(msg.payload)) if fn else float(msg.confidence)
        except Exception:
            mag = float(msg.confidence)
        _, cap, *_ = self._conc_settings()
        new_level = min(cap, self._concentration.get(topic, 0.0) + max(0.0, mag))
        # C4: slope = rise in concentration per second since the previous sample.
        prev = self._conc_prev.get(topic)
        if prev is not None:
            dt = max(1e-6, now - prev[1])
            self._conc_slope[topic] = (new_level - prev[0]) / dt
        self._conc_prev[topic] = (new_level, now)
        self._concentration[topic] = new_level
        ring = self._conc_context.setdefault(topic, deque(maxlen=8))
        tags = msg.payload.get("tags") or msg.payload.get("entities") or []
        ring.append({"tags": list(tags), "neuromod": self.neuromod.snapshot(), "ts": now})
        self._update_state(topic, now)

    def concentration(self, topic: str, now: float | None = None) -> float:
        """Current decayed concentration for a tracked topic (also advances state)."""
        now = time.time() if now is None else now
        self._decay_to(topic, now)
        self._update_state(topic, now)
        return self._concentration.get(topic, 0.0)

    def topic_status(self, topic: str, now: float | None = None) -> str:
        self.concentration(topic, now)  # refresh state
        return self._topic_status.get(topic, CONC_UNARMED)

    def concentration_slope(self, topic: str) -> float:
        """C4: most recent rise-rate (concentration units per second) for a topic.
        Positive = rising fast; 0 when untracked or no prior sample."""
        return self._conc_slope.get(topic, 0.0)

    def quorum(self, topic: str, now: float | None = None) -> bool:
        """ARMED and (concentration ≥ level threshold OR rising fast). The slope term
        (C4) lets a fast-rising signal trip quorum before it reaches the level bar —
        a rapidly-escalating threat mobilises sooner than a slow accumulation."""
        level = self.concentration(topic, now)
        if self._topic_status.get(topic) != CONC_ARMED:
            return False
        if level >= float(settings.get("colony_quorum_threshold", 1.5)):
            return True
        return self.concentration_slope(topic) >= float(
            settings.get("colony_quorum_slope_threshold", 0.20)
        )

    def is_quiet(self, topic: str, now: float | None = None) -> bool:
        """True only for an ARMED→QUIET topic — never from cold start (UNARMED)."""
        self.concentration(topic, now)  # refresh state
        return self._topic_status.get(topic) == CONC_QUIET

    def consume_quiet_onset(self, topic: str, now: float | None = None) -> bool:
        """Fire-once edge: True the first time a topic newly enters QUIET, then clears.
        Debounces silence→recall so a single quiet onset triggers exactly one action."""
        self.concentration(topic, now)  # refresh state (may set the onset flag)
        if topic in self._quiet_onset:
            self._quiet_onset.discard(topic)
            return True
        return False

    def concentration_context(self, topic: str) -> list[dict]:
        """The associated-context ring captured during the high-concentration window."""
        return list(self._conc_context.get(topic, ()))

    def tracked_topics(self) -> list[str]:
        """Topics registered for concentration tracking."""
        return list(self._tracked.keys())

    # ── Phase 3: releaser + primer ────────────────────────────────────────────

    def _collect_primer(self, msg: Message) -> None:
        """Accumulate a message's primer nudges (colony-gated). Centralised here so
        hormonal writes stay owned by the hypothalamus, which drains them per turn."""
        if not settings.get("colony_features", 0) or not msg.primer:
            return
        for ch, v in msg.primer.items():
            try:
                self._pending_primers[ch] = self._pending_primers.get(ch, 0.0) + float(v)
            except (TypeError, ValueError):
                continue

    def drain_primers(self) -> dict[str, float]:
        """Return and clear the accumulated primer nudges (called once per turn by
        the hypothalamus, the single hormonal-state writer)."""
        out = dict(self._pending_primers)
        self._pending_primers.clear()
        return out

    # ── Phase 4/7: recruitment amplification ──────────────────────────────────

    def _decay_recruit(self, cluster: str, now: float) -> None:
        last = self._recruit_ts.get(cluster)
        if last is None:
            self._recruit_ts[cluster] = now
            self._recruitment.setdefault(cluster, 0.0)
            return
        elapsed = max(0.0, now - last)
        hl = float(settings.get("colony_conc_half_life_s", 45.0))
        if hl > 0 and elapsed > 0 and cluster in self._recruitment:
            self._recruitment[cluster] *= 0.5 ** (elapsed / hl)
        self._recruit_ts[cluster] = now

    def recruit(self, cluster: str, amount: float, now: float | None = None) -> None:
        """Raise a cluster's (decaying) recruitment level in proportion to need.
        Higher recruitment lowers recruitable switches' thresholds — more responders
        mobilise as the need escalates. No-op when colony features are off."""
        if not settings.get("colony_features", 0):
            return
        now = time.time() if now is None else now
        self._decay_recruit(cluster, now)
        self._recruitment[cluster] = max(
            0.0, min(1.0, self._recruitment.get(cluster, 0.0) + float(amount))
        )

    def satisfy(self, cluster: str, amount: float, now: float | None = None) -> None:
        """C3 (colony-features-ii): a met need actively LOWERS recruitment, rather
        than waiting for passive decay. Converts Phase-7 from a response-only
        (start) threshold into a composite start+stop threshold — the satisfaction
        signal that minimises task-switching (Lynch & Dornhaus, 2024). `amount` in
        [0,1] is the fraction of current recruitment to release (× colony_satisfy_rate)."""
        if not settings.get("colony_features", 0):
            return
        now = time.time() if now is None else now
        self._decay_recruit(cluster, now)
        cur = self._recruitment.get(cluster, 0.0)
        rate = float(settings.get("colony_satisfy_rate", 0.5))
        self._recruitment[cluster] = max(0.0, cur * (1.0 - max(0.0, min(1.0, amount)) * rate))

    def allocate_recruitment(self, needs: dict[str, float], now: float | None = None) -> None:
        """N2 (colony-features-ii): distribute a bounded recruitment budget across
        COMPETING cluster needs via a Boltzmann (softmax) allocation — the multi-task
        generalization of the response-threshold model (Lynch & Pavlic, 2024). The
        total spent scales with the strongest need (so zero need → no recruitment),
        and softmax sharpens the split toward the most-needed cluster. No-op when off."""
        if not settings.get("colony_features", 0) or not needs:
            return
        items = list(needs.items())
        mx = max(v for _, v in items)
        if mx <= 1e-6:
            return  # no real need → no mobilization
        temp = max(1e-6, float(settings.get("colony_recruit_softmax_temp", 0.5)))
        budget = float(settings.get("colony_recruit_budget", 1.0))
        exps = [math.exp(v / temp) for _, v in items]
        total = sum(exps) or 1.0
        saturation = min(1.0, mx)  # spend in proportion to the strongest need
        for (cluster, _), e in zip(items, exps, strict=False):
            self.recruit(cluster, budget * (e / total) * saturation, now)

    def recruitment_level(self, cluster: str, now: float | None = None) -> float:
        """Current decayed recruitment level for a cluster in [0, 1] (0 when off)."""
        if not settings.get("colony_features", 0):
            return 0.0
        now = time.time() if now is None else now
        self._decay_recruit(cluster, now)
        return self._recruitment.get(cluster, 0.0)

    def recruit_channel(self, cluster: str, now: float | None = None) -> float | None:
        """The RECRUIT value to inject into a cluster's chem snapshot, or None when
        colony features are off (so the modulator is simply skipped — strict no-op).
        Maps recruitment 0→1 onto 0.5→1.0 so that ZERO recruitment is NEUTRAL under
        effective_threshold's (level-0.5) centering, not threshold-raising."""
        if not settings.get("colony_features", 0):
            return None
        return 0.5 + 0.5 * self.recruitment_level(cluster, now)

    async def publish(self, msg: Message) -> None:
        if msg.expired:
            return
        self._accumulate(msg)
        self._collect_primer(msg)
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
