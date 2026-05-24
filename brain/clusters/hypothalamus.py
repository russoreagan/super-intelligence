"""
Hypothalamus — drives and affect. 0 LLMs, pure switch logic.
Consumes temporal features, updates neuromod levels, names emotional state.
"""
from __future__ import annotations

import asyncio
import logging

from brain.bus import Bus
from brain.emotion_vocabulary import (
    name_emotion, apply_hormonal_color, appraisal, prosody_prefix, compute_affect_dims,
)
from brain.neuron import StatefulSwitch
from brain.settings import settings

logger = logging.getLogger(__name__)

CLUSTER = "hypothalamus"


class HypothalamusCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus

        # Stateful switches with decay
        self._valence_switch = StatefulSwitch("valence_to_DA", CLUSTER, decay=settings.get("valence_to_DA_decay"))
        self._threat_switch = StatefulSwitch("threat_to_GABA", CLUSTER, decay=settings.get("threat_to_GABA_decay"))
        self._novelty_switch = StatefulSwitch("novelty_to_ACh", CLUSTER, decay=settings.get("novelty_to_ACh_decay"))
        self._arousal_switch = StatefulSwitch("arousal_homeostat", CLUSTER, decay=settings.get("arousal_homeostat_decay"))
        # Inhibitory: receptor desensitization — suppresses novelty on repeated topics
        self._satiation_inhibitor = StatefulSwitch("satiation_inhibitor", CLUSTER,
                                                    decay=settings.get("satiation_inhibitor_decay"), polarity="inhibitory")

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

        er_scale = settings.get("emotional_reactivity_scale")

        # DA: valence signal (reward / positive engagement)
        valence_delta = (sentiment * settings.get("sentiment_DA_weight") * er_scale) - (hostility * settings.get("hostility_DA_weight"))
        nm.add("DA", valence_delta)

        # GABA: threat / caution signal (inhibitory)
        if hostility > settings.get("hostility_GABA_threshold_high"):
            nm.add("GABA", hostility * settings.get("hostility_GABA_increment_high"))
        elif hostility > settings.get("hostility_GABA_threshold_med"):
            nm.add("GABA", settings.get("hostility_GABA_increment_med"))

        # ACh: novelty / attention signal
        novelty_delta = (surprise * settings.get("surprise_ACh_weight") + salience * settings.get("salience_ACh_weight")) * er_scale
        if self._satiation_inhibitor.state > 0.5:
            novelty_delta *= (1.0 - self._satiation_inhibitor.state * settings.get("satiation_inhibition_factor"))
        nm.add("ACh", novelty_delta)

        # Glu: general arousal
        arousal_delta = salience * settings.get("salience_Glu_weight") * er_scale
        if features.get("intent") == "hostile":
            arousal_delta += settings.get("hostile_intent_Glu_bonus")
        nm.add("Glu", arousal_delta)

        # Satiation: if salience is low (routine), desensitize
        if salience < settings.get("salience_satiation_threshold"):
            self._satiation_inhibitor.update(settings.get("salience_satiation_increase"))
        else:
            self._satiation_inhibitor.update(settings.get("salience_satiation_decrease"))

        # ── Prosody modulation (from auditory cortex, if active) ──────────────
        # Drain expired messages; use most recent valid prosody
        prosody_tone = None
        prosody_features: dict | None = None
        while True:
            try:
                pros_msg = self._prosody_inbox.get_nowait()
                if not pros_msg.expired:
                    prosody_tone = pros_msg.payload.get("tone_label")
                    prosody_features = pros_msg.payload
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

        snap = nm.snapshot()

        # ── Endocrine (hormonal) updates ──────────────────────────────────────
        hs = self._bus.hormonal

        # OXT: build on warm positive exchange; drain under hostility
        if sentiment > 0.3 and hostility < 0.2:
            hs.add("OXT", settings.get("oxt_positive_increment"))
        elif hostility > settings.get("hostility_GABA_threshold_high"):
            hs.add("OXT", -settings.get("oxt_hostility_drain"))

        # CORT: accumulates under sustained social threat (direct hostility, not prosody).
        # Decoupled from GABA so that animated/focused voice patterns don't trigger
        # false cortisol build — prosody raises GABA for alertness, not social stress.
        if hostility > settings.get("cort_hostility_threshold"):
            hs.add("CORT", settings.get("cort_threat_increment"))

        # 5HT: slow lift from rewarding interaction; drain under hostility
        if sentiment > settings.get("sht_reward_sentiment_min") and hostility < 0.1:
            hs.add("5HT", settings.get("sht_reward_increment"))
        elif hostility > settings.get("hostility_GABA_threshold_high"):
            hs.add("5HT", -settings.get("sht_hostility_drain"))

        # OXT buffers CORT (cross-channel antagonism)
        if hs.get("OXT") > settings.get("oxt_cort_buffer_threshold"):
            hs.add("CORT", -hs.get("OXT") * settings.get("oxt_cort_buffer_rate"))

        h_snap = hs.snapshot()
        logger.debug(
            "Hypothalamus hormonal: 5HT=%.3f CORT=%.3f OXT=%.3f",
            h_snap["5HT"], h_snap["CORT"], h_snap["OXT"],
        )

        # Apply hormonal modulation to effective neuromod values for emotion naming.
        # Raw accumulator levels are unchanged; only the values passed to name_emotion
        # are adjusted so hormonal state shapes the emotion without touching the bus.
        da_offset = hs.da_offset(
            settings.get("sht_da_floor_lift"),
            settings.get("oxt_da_lift"),
            settings.get("cort_da_suppress"),
        )
        gaba_scale = hs.gaba_scale(
            settings.get("cort_gaba_amplify"),
            settings.get("oxt_gaba_buffer"),
        )
        eff_DA   = max(0.0, min(1.0, snap["DA"]   + da_offset))
        eff_GABA = max(0.0, min(1.0, snap["GABA"] * gaba_scale))

        # Name current emotion (using hormone-adjusted effective values)
        emotion, tendency = name_emotion(eff_DA, eff_GABA, snap["ACh"], snap["Glu"])

        # Overlay hormonal color (connected / withdrawn / guarded / dysphoric)
        emotion, tendency = apply_hormonal_color(
            emotion, tendency, h_snap,
            oxt_connected=settings.get("hormonal_oxt_connected_threshold"),
            cort_withdrawn=settings.get("hormonal_cort_withdrawn_threshold"),
            oxt_guarded=settings.get("hormonal_oxt_guarded_threshold"),
            sht_dysphoric=settings.get("hormonal_sht_dysphoric_threshold"),
        )

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
        affect_dims = compute_affect_dims(snap, h_snap)

        affect = {
            "emotion": emotion,
            "tendency": tendency,
            "appraisal": appraisal_str,
            "prosody_prefix": prefix,
            "affect_dims": affect_dims,
            "neuromod": snap,
            "hormonal": h_snap,
            "high_GABA": snap["GABA"] > 0.4,
            "high_ACh": snap["ACh"] > 0.5,
            "vocal_tone": prosody_tone,
            "prosody_f0_hz": round((prosody_features or {}).get("f0_mean_hz", 0.0), 1),
            "prosody_energy": round((prosody_features or {}).get("energy_mean", 0.0), 4),
            "prosody_jitter": round((prosody_features or {}).get("jitter", 0.0), 4),
            "prosody_shimmer": round((prosody_features or {}).get("shimmer", 0.0), 4),
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
        self._bus.hormonal.decay()
