"""
Hypothalamus — drives and affect. 0 LLMs, pure switch logic.
Consumes temporal features, updates neuromod levels, names emotional state.
"""
from __future__ import annotations

import logging

from brain.bus import Bus
from brain.emotion_vocabulary import name_emotion, appraisal, prosody_prefix
from brain.neuron import StatefulSwitch

logger = logging.getLogger(__name__)

CLUSTER = "hypothalamus"


class HypothalamusCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus

        # Stateful switches with decay
        self._valence_switch = StatefulSwitch("valence_to_DA", CLUSTER, decay=0.85)
        self._threat_switch = StatefulSwitch("threat_to_GABA", CLUSTER, decay=0.80)
        self._novelty_switch = StatefulSwitch("novelty_to_ACh", CLUSTER, decay=0.90)
        self._arousal_switch = StatefulSwitch("arousal_homeostat", CLUSTER, decay=0.88)
        # Inhibitory: receptor desensitization — suppresses novelty on repeated topics
        self._satiation_inhibitor = StatefulSwitch("satiation_inhibitor", CLUSTER,
                                                    decay=0.95, polarity="inhibitory")

    async def process(self, features: dict) -> dict:
        """Update neuromod levels from temporal features. Return affect summary."""
        nm = self._bus.neuromod

        sentiment = features.get("sentiment", 0.0)
        hostility = features.get("hostility", 0.0)
        salience = features.get("salience", 0.3)
        surprise = features.get("surprise_score", 0.0)

        # DA: valence signal (reward / positive engagement)
        valence_delta = (sentiment * 0.15) - (hostility * 0.1)
        nm.add("DA", valence_delta)

        # GABA: threat / caution signal (inhibitory)
        if hostility > 0.5:
            nm.add("GABA", hostility * 0.2)
        elif hostility > 0.2:
            nm.add("GABA", 0.05)

        # ACh: novelty / attention signal
        novelty_delta = surprise * 0.12 + salience * 0.08
        if self._satiation_inhibitor.state > 0.5:
            novelty_delta *= (1.0 - self._satiation_inhibitor.state * 0.5)
        nm.add("ACh", novelty_delta)

        # Glu: general arousal
        arousal_delta = salience * 0.08
        if features.get("intent") == "hostile":
            arousal_delta += 0.15
        nm.add("Glu", arousal_delta)

        # Satiation: if salience is low (routine), desensitize
        if salience < 0.3:
            self._satiation_inhibitor.update(0.05)
        else:
            self._satiation_inhibitor.update(-0.1)

        # Name current emotion
        snap = nm.snapshot()
        emotion, tendency = name_emotion(snap["DA"], snap["GABA"], snap["ACh"], snap["Glu"])
        appraisal_str = appraisal(emotion, features.get("topic_summary", "input"))
        prefix = prosody_prefix(emotion)

        affect = {
            "emotion": emotion,
            "tendency": tendency,
            "appraisal": appraisal_str,
            "prosody_prefix": prefix,
            "neuromod": snap,
            "high_GABA": snap["GABA"] > 0.4,
            "high_ACh": snap["ACh"] > 0.5,
        }

        await self._bus.publish_dict("affect.state", affect, source=CLUSTER)
        logger.debug("Hypothalamus: emotion=%s DA=%.2f GABA=%.2f",
                     emotion, snap["DA"], snap["GABA"])
        return affect

    def decay_turn(self) -> None:
        self._bus.neuromod.decay()
