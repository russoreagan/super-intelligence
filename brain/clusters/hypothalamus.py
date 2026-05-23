"""
Hypothalamus — drives and affect. 0 LLMs, pure switch logic.
Consumes temporal features, updates neuromod levels, names emotional state.
"""
from __future__ import annotations

import asyncio
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

        # Auditory prosody input (published by auditory cortex when --ears active)
        self._prosody_inbox = bus.subscribe("auditory.prosody")
        self._dynamics_inbox = bus.subscribe("auditory.speech_dynamics")
        # Metacognition's per-turn appraisal can override the neuromod-derived
        # emotion label for context-driven emotions (apologetic, grateful,
        # embarrassed, flirty, etc.) that pure neuromods can't produce.
        self._meta_override_inbox = bus.subscribe("meta.emotion_override")

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

        # ── Prosody modulation (from auditory cortex, if active) ──────────────
        # Drain expired messages; use most recent valid prosody
        prosody_tone = None
        while True:
            try:
                pros_msg = self._prosody_inbox.get_nowait()
                if not pros_msg.expired:
                    prosody_tone = pros_msg.payload.get("tone_label")
            except asyncio.QueueEmpty:
                break

        if prosody_tone:
            if prosody_tone == "stressed":
                nm.add("GABA", 0.08)
                nm.add("ACh", 0.05)
            elif prosody_tone == "energetic":
                nm.add("Glu", 0.06)
                nm.add("DA", 0.04)
            elif prosody_tone == "whisper":
                nm.add("ACh", 0.10)
            # "calm" and "monotone" need no correction
            logger.debug("Hypothalamus: prosody_tone=%s", prosody_tone)

        # ── Speech dynamics (pace + pauses) ───────────────────────────────────
        dynamics: dict | None = None
        while True:
            try:
                d_msg = self._dynamics_inbox.get_nowait()
                if not d_msg.expired:
                    dynamics = d_msg.payload
            except asyncio.QueueEmpty:
                break

        if dynamics:
            pace = dynamics.get("pace_label")
            if pace == "rushed":
                nm.add("Glu", 0.08)   # urgency
                nm.add("ACh", 0.04)
            elif pace == "brisk":
                nm.add("Glu", 0.04)
                nm.add("DA", 0.02)    # mild positive valence — animated
            elif pace == "halting":
                nm.add("ACh", 0.06)   # uncertainty → pay attention
            elif pace == "measured":
                nm.add("ACh", 0.02)
            # "normal" → no correction

            if dynamics.get("hesitant"):
                nm.add("ACh", 0.05)   # frequent long pauses → user is searching
            if dynamics.get("burst_score", 0.0) > 0.35:
                nm.add("GABA", 0.04)  # very bursty → mild caution flag (agitation)
            logger.debug("Hypothalamus: pace=%s pauses=%d hesitant=%s",
                         pace, dynamics.get("long_pause_count", 0), dynamics.get("hesitant"))

        # Name current emotion
        snap = nm.snapshot()
        emotion, tendency = name_emotion(snap["DA"], snap["GABA"], snap["ACh"], snap["Glu"])

        # ── Metacognition appraisal override ──────────────────────────────────
        # Drain any pending overrides; the most recent fresh one wins. This is
        # how context-driven emotions (apologetic, grateful, embarrassed, etc.)
        # enter the system — pure neuromods can't produce them.
        override_emotion: str | None = None
        override_reason: str = ""
        while True:
            try:
                ov_msg = self._meta_override_inbox.get_nowait()
                if not ov_msg.expired:
                    override_emotion = ov_msg.payload.get("emotion")
                    override_reason = ov_msg.payload.get("reason", "")
            except asyncio.QueueEmpty:
                break

        if override_emotion:
            emotion = override_emotion
            tendency = f"metacognition appraisal: {override_reason}"
            logger.debug("Hypothalamus: emotion override → %s (%s)",
                         override_emotion, override_reason)

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
            "vocal_tone": prosody_tone,
            "pace_label": (dynamics or {}).get("pace_label"),
            "hesitant_speech": bool((dynamics or {}).get("hesitant")),
            "emotion_source": "metacognition" if override_emotion else "neuromod",
            "emotion_override_reason": override_reason if override_emotion else None,
        }

        await self._bus.publish_dict("affect.state", affect, source=CLUSTER)
        logger.debug("Hypothalamus: emotion=%s DA=%.2f GABA=%.2f",
                     emotion, snap["DA"], snap["GABA"])
        return affect

    def decay_turn(self) -> None:
        self._bus.neuromod.decay()
