"""
Thalamus — the global-workspace spotlight (Global Workspace Theory: Baars, Dehaene).

Each turn the thalamus reads the whole workspace, decides what the mind is
currently ignited on, and broadcasts that verdict so the specialist clusters can
condition on it. This is the one place that sees every tracked topic at once; no
single downstream cluster does. That integrated, cross-turn view is what the
broadcast carries, and it is why the broadcast is not a re-derivation of what a
consumer already holds.

WHERE THE WORKSPACE STATE LIVES. The decaying salience field itself is the bus
concentration layer (Bus.track_concentration, registered in session_setup.py for
affect.state / temporal.features / mem.recall). It accumulates per-topic
magnitude with decay, tracks an UNARMED→ARMED→QUIET state machine, and keeps a
ring of what was hot and in what chemical state. The thalamus does not duplicate
that field — it reads it, fuses the per-topic signals into one ranked ignition
verdict, and pushes that verdict out. The bus holds the workspace; the thalamus
is the spotlight over it.

THE FOUR GWT PREDICATES, made real:
  - Competition — the tracked topics compete on integrated concentration; one wins.
  - Ignition — a coalition that is ARMED and has genuinely accumulated (level ≥
    workspace_ignition_threshold) ignites. The threshold is deliberately high, so
    ignition marks sustained focus, not passing mention.
  - Broadcast — the verdict rides on the turn's affect/features dicts for the
    same-turn consumers, and is published on `attention.focus` for the DMN, which
    subscribes to it (a real subscriber, so "available system-wide" is literal).
  - Persistence — `_spotlight` and the per-topic activation carry the ignited
    focus across turns, so "what the entity is currently thinking about" outlives
    the turn that lit it.

WHO CONDITIONS ON THE VERDICT (the fan-out the architecture figure draws):
  - frontal / recall gate — a threat coalition that ignited from slow accumulation
    (no single turn's GABA tripped the per-turn veto) forces the integrator awake.
    This is ignition pulling in a specialist: the genuinely new signal the local
    gate lacks.
  - hippocampus — an ignited focus widens recall and seeds the cue with the
    workspace's hot entities.
  - parietal — records the current focus in session state (advisory).
  - DMN — subscribes to `attention.focus`; the persistent spotlight seeds what the
    idle mind dwells on between turns.

BEHAVIOUR WHEN DISABLED. With `thalamus_workspace_enabled` = 0 the verdict is the
neutral one (ignited False everywhere) and every consumer falls through to its
prior logic, so the turn is observationally identical to the pre-workspace path.
The flag ships on with a conservative threshold.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from brain.bus import Bus
from brain.settings import settings

logger = logging.getLogger(__name__)

CLUSTER = "thalamus"

# The topics that count as workspace content, mapped to the coalition each one
# represents. Keys mirror the concentration registrations in session_setup.py;
# a topic that is tracked but unmapped still competes, it just gets no label.
WORKSPACE_TOPICS = {"temporal.features", "mem.recall", "affect.state", "sensory.image"}

_COALITION = {
    "affect.state": "threat",
    "temporal.features": "salience",
    "mem.recall": "memory",
    "sensory.image": "vision",
}

# The verdict returned when the workspace is disabled or a topic never ignited.
# `priorities` keeps the old advisory shape so any reader of it still works.
_NEUTRAL_PRIORITIES = {"hippocampus": 0.0, "frontal": 0.5, "occipital": 0.0}


def _neutral_verdict() -> dict:
    return {
        "ignited": False,
        "focus": None,
        "coalition": None,
        "salience": 0.0,
        "quorum": False,
        "rising": False,
        "sustained_turns": 0,
        "hot_entities": [],
        "priorities": dict(_NEUTRAL_PRIORITIES),
    }


class ThalamusCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        # Cross-turn spotlight persistence: decayed activation per topic, plus the
        # currently-ignited focus and how many consecutive turns it has held.
        self._topic_activation: dict[str, float] = defaultdict(float)
        self._spotlight_focus: str | None = None
        self._spotlight_streak: int = 0
        # Last verdict, exposed to the DMN which reads it between turns.
        self._spotlight: dict = _neutral_verdict()
        # Dedupe the attention.focus broadcast to spotlight *changes* (the serial
        # bottleneck: re-announce only when the winning coalition changes).
        self._broadcast_focus: str | None = None

    def current_spotlight(self) -> dict:
        """The most recent workspace verdict. Read by the DMN between turns so the
        idle mind dwells on what the workspace ignited on during conversation."""
        return dict(self._spotlight)

    def _hot_entities(self, focus: str, limit: int = 6) -> list[str]:
        """Aggregate the tags/entities the focus topic carried while it was hot,
        most-recent first, deduped. This is the content of the spotlight — what the
        mind is focused on, not merely which channel is loud."""
        seen: list[str] = []
        for frame in reversed(self._bus.concentration_context(focus)):
            for tag in frame.get("tags", ()):
                if tag and tag not in seen:
                    seen.append(tag)
                    if len(seen) >= limit:
                        return seen
        return seen

    async def route(self, features: dict, affect: dict) -> dict:
        """Read the workspace, decide what is ignited, broadcast the verdict.

        Returns the verdict dict (see module docstring for its shape). The caller
        writes it onto the turn's affect/features so downstream clusters read it,
        and the ignition is published on `attention.focus` for the DMN.
        """
        if not settings.get("thalamus_workspace_enabled", 1):
            self._spotlight = _neutral_verdict()
            return self._spotlight

        nm = self._bus.neuromod.snapshot()

        # ── Decay the cross-turn spotlight, then read the live workspace ──────────
        decay = settings.get("topic_activation_decay")
        for topic in list(self._topic_activation):
            self._topic_activation[topic] *= decay
            if self._topic_activation[topic] < 1e-3:
                del self._topic_activation[topic]

        # Competition: every tracked topic bids its integrated concentration. The
        # thalamus is the only reader that sees all of them at once.
        topics = self._bus.tracked_topics()
        levels = {t: self._bus.concentration(t) for t in topics}
        focus = max(levels, key=levels.get) if levels else None
        top_level = levels.get(focus, 0.0) if focus else 0.0

        at_quorum = bool(focus) and self._bus.quorum(focus)
        rising = bool(focus) and self._bus.concentration_slope(focus) >= settings.get(
            "workspace_rising_slope", 0.20
        )

        # Ignition: ARMED and genuinely accumulated. Deliberately stricter than the
        # bus quorum (which mobilises recruitment) — ignition marks sustained focus,
        # so the threshold sits above the quorum level.
        ignition_threshold = settings.get("workspace_ignition_threshold", 2.0)
        ignited = bool(focus) and top_level >= ignition_threshold and self._bus.topic_status(
            focus
        ) == "armed"

        coalition = _COALITION.get(focus) if focus else None

        # Per-persona focus floor for the salience coalition: a non-affective topic
        # holds the spotlight only while turns stay salient, so a stale focus fades
        # once the conversation moves on. `salience_workspace_threshold` is the Focus
        # temperament dial — a more focused persona has a higher bar for what grabs
        # the workspace. Threat and memory coalitions are exempt (a threat should not
        # need a "salient" turn to keep the mind's attention).
        if (
            ignited
            and coalition == "salience"
            and features.get("salience", 0.0) < settings.get("salience_workspace_threshold")
        ):
            ignited = False

        # ── Persist the spotlight across turns ───────────────────────────────────
        if ignited:
            self._topic_activation[focus] = top_level
            if focus == self._spotlight_focus:
                self._spotlight_streak += 1
            else:
                self._spotlight_focus = focus
                self._spotlight_streak = 1
        else:
            self._spotlight_focus = None
            self._spotlight_streak = 0

        hot = self._hot_entities(focus) if (ignited and focus) else []

        # The advisory per-cluster priorities the architecture figure shows — kept
        # so parietal can record them, but no longer the load-bearing signal.
        salience = features.get("salience", 0.3)
        intent = features.get("intent", "other")
        priorities = dict(_NEUTRAL_PRIORITIES)
        if features.get("requires_memory") or features.get("epistemic_action"):
            priorities["hippocampus"] += settings.get(
                "hippocampus_priority_base"
            ) + salience * settings.get("hippocampus_salience_weight")
        if features.get("requires_vision"):
            priorities["occipital"] += settings.get("occipital_priority_base")
        if intent in ("hostile", "task"):
            priorities["frontal"] += settings.get("frontal_hostile_priority")
        if nm["ACh"] > settings.get("ach_threshold_frontal"):
            priorities["frontal"] += nm["ACh"] * settings.get("frontal_ach_weight")

        verdict = {
            "ignited": ignited,
            "focus": focus if ignited else None,
            "coalition": coalition if ignited else None,
            "salience": round(top_level, 3),
            "quorum": at_quorum,
            "rising": rising,
            "sustained_turns": self._spotlight_streak,
            "hot_entities": hot,
            "priorities": priorities,
        }
        self._spotlight = verdict

        # ── Broadcast: publish on spotlight change so the DMN (a real subscriber)
        # picks up what is now globally available. Announce only on change — the
        # serial bottleneck: one coalition holds the workspace at a time.
        broadcast_key = focus if ignited else None
        if broadcast_key != self._broadcast_focus:
            self._broadcast_focus = broadcast_key
            if ignited:
                await self._bus.publish_dict(
                    "attention.focus",
                    {
                        "cluster": focus,
                        "coalition": coalition,
                        "salience": round(top_level, 3),
                        "hot_entities": hot,
                        "sustained_turns": self._spotlight_streak,
                    },
                    source=CLUSTER,
                )
                logger.debug(
                    "Thalamus: workspace ignited on %s (%s, level=%.2f, %d turns)",
                    focus,
                    coalition,
                    top_level,
                    self._spotlight_streak,
                )

        return verdict
