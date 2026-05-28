"""
Hypothalamus — drives and affect. 0 LLMs, pure switch logic.
Consumes temporal features, updates neuromod levels, names emotional state.
"""
from __future__ import annotations

import asyncio
import logging
import time

from brain.bus import Bus
from brain.emotion_vocabulary import (
    apply_hormonal_color,
    apply_ne_color,
    appraisal,
    compute_affect_dims,
    name_emotion,
    prosody_prefix,
)
from brain.neuron import StatefulSwitch
from brain.settings import settings

logger = logging.getLogger(__name__)

CLUSTER = "hypothalamus"


class HypothalamusCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._last_decay_time: float = time.monotonic()
        self._current_turns: float = 1.0  # set by process(), consumed by decay_turn()

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

        # Compute time-weighted turns (elapsed since last decay / reference interval).
        # Increments for text-derived signals are multiplied by turns so that
        # equilibrium levels stay pace-independent — a 3-minute gap applies 3× the
        # per-turn delta but also decays 3× as much, keeping the fixed point stable.
        # Prosody and dynamics signals are NOT scaled (they are one-shot observations).
        now = time.monotonic()
        elapsed = now - self._last_decay_time
        ref = settings.get("decay_reference_interval_s")
        raw_turns = elapsed / ref
        turns = max(settings.get("decay_min_turns"), min(raw_turns, settings.get("decay_max_turns")))
        self._current_turns = turns

        sentiment = features.get("sentiment", 0.0)
        hostility = features.get("hostility", 0.0)
        salience = features.get("salience", 0.3)
        surprise = features.get("surprise_score", 0.0)

        er_scale = settings.get("emotional_reactivity_scale")

        # DA: valence signal (reward / positive engagement)
        valence_delta = (sentiment * settings.get("sentiment_DA_weight") * er_scale) - (hostility * settings.get("hostility_DA_weight"))
        nm.add("DA", valence_delta * turns)

        # GABA: threat / caution signal (inhibitory)
        if hostility > settings.get("hostility_GABA_threshold_high"):
            nm.add("GABA", hostility * settings.get("hostility_GABA_increment_high") * turns)
        elif hostility > settings.get("hostility_GABA_threshold_med"):
            nm.add("GABA", settings.get("hostility_GABA_increment_med") * turns)

        # ACh: novelty / attention signal
        novelty_delta = (surprise * settings.get("surprise_ACh_weight") + salience * settings.get("salience_ACh_weight")) * er_scale
        if self._satiation_inhibitor.state > 0.5:
            novelty_delta *= (1.0 - self._satiation_inhibitor.state * settings.get("satiation_inhibition_factor"))
        nm.add("ACh", novelty_delta * turns)

        # Glu: general arousal
        arousal_delta = salience * settings.get("salience_Glu_weight") * er_scale
        if features.get("intent") == "hostile":
            arousal_delta += settings.get("hostile_intent_Glu_bonus")
        nm.add("Glu", arousal_delta * turns)

        # NE: focused alertness — rises with salience, surprise, and threat.
        # Distinct from Glu (general arousal): NE is the sharp attentional spotlight,
        # with an inverted-U performance curve (too much narrows attention).
        # NE is NOT scaled by er_scale — its inverted-U performance curve
        # (Yerkes-Dodson) is governed by its own weights and should not be
        # amplified by the emotional reactivity dial (which controls valence/
        # arousal swings, not alertness overload).
        ne_delta = (
            salience  * settings.get("ne_salience_weight") +
            surprise  * settings.get("ne_surprise_weight") +
            hostility * settings.get("ne_hostility_weight")
        )
        nm.add("NE", ne_delta * turns)

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
                nm.add("NE", settings.get("ne_prosody_stressed"))
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
                nm.add("NE", settings.get("ne_rush_increment"))
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
            hs.add("OXT", settings.get("oxt_positive_increment") * turns)
        elif hostility > settings.get("hostility_GABA_threshold_high"):
            hs.add("OXT", -settings.get("oxt_hostility_drain") * turns)

        # CORT: accumulates under sustained social threat (direct hostility, not prosody).
        # Decoupled from GABA so that animated/focused voice patterns don't trigger
        # false cortisol build — prosody raises GABA for alertness, not social stress.
        if hostility > settings.get("cort_hostility_threshold"):
            hs.add("CORT", settings.get("cort_threat_increment") * turns)

        # 5HT: slow lift from rewarding interaction; drain under hostility
        if sentiment > settings.get("sht_reward_sentiment_min") and hostility < 0.1:
            hs.add("5HT", settings.get("sht_reward_increment") * turns)
        elif hostility > settings.get("hostility_GABA_threshold_high"):
            hs.add("5HT", -settings.get("sht_hostility_drain") * turns)

        # OXT buffers CORT (cross-channel antagonism)
        if hs.get("OXT") > settings.get("oxt_cort_buffer_threshold"):
            hs.add("CORT", -hs.get("OXT") * settings.get("oxt_cort_buffer_rate") * turns)

        # AEA: homeostatic buffer — rises when Glu + NE arousal exceeds threshold.
        # Also gets a small positive lift from warm exchanges (social afterglow),
        # and drains slightly when CORT is sustained (stress antagonises AEA).
        if snap["Glu"] + snap["NE"] > settings.get("aea_arousal_threshold"):
            hs.add("AEA", settings.get("aea_arousal_increment") * turns)
        if sentiment > 0.4 and hostility < 0.1:
            hs.add("AEA", settings.get("aea_positive_increment") * turns)
        if hs.get("CORT") > settings.get("cort_hostility_threshold"):
            hs.add("AEA", -settings.get("aea_cort_drain") * turns)

        h_snap = hs.snapshot()
        logger.debug(
            "Hypothalamus hormonal: 5HT=%.3f CORT=%.3f OXT=%.3f AEA=%.3f",
            h_snap["5HT"], h_snap["CORT"], h_snap["OXT"], h_snap["AEA"],
        )

        # Apply hormonal modulation to effective neuromod values for emotion naming.
        # Raw accumulator levels are unchanged; only the values passed to name_emotion
        # and the color functions are adjusted so hormonal state shapes the emotion
        # without touching the bus.

        # AEA suppresses effective NE and Glu when elevated above resting baseline.
        ne_scale, glu_scale = hs.aea_suppress(
            settings.get("aea_ne_suppression"),
            settings.get("aea_glu_suppression"),
        )
        eff_NE  = max(0.0, min(1.0, snap["NE"]  * ne_scale))
        eff_Glu = max(0.0, min(1.0, snap["Glu"] * glu_scale))

        # DA: hormonal offset + AEA afterglow lift
        da_offset = hs.da_offset(
            settings.get("sht_da_floor_lift"),
            settings.get("oxt_da_lift"),
            settings.get("cort_da_suppress"),
        )
        eff_DA   = max(0.0, min(1.0, snap["DA"] + da_offset
                                     + h_snap["AEA"] * settings.get("aea_da_lift")))
        eff_GABA = max(0.0, min(1.0, snap["GABA"] * hs.gaba_scale(
            settings.get("cort_gaba_amplify"),
            settings.get("oxt_gaba_buffer"),
        )))

        # Name current emotion (using fully-adjusted effective values)
        emotion, tendency = name_emotion(eff_DA, eff_GABA, snap["ACh"], eff_Glu)

        # NE color: inverted-U modifier (vigilant / alert-curious / scattered)
        emotion, tendency = apply_ne_color(
            emotion, tendency, eff_NE,
            ne_high=settings.get("ne_high_threshold"),
            ne_scatter=settings.get("ne_scatter_threshold"),
        )

        # Hormonal color: connected / withdrawn / guarded / eased / dysphoric
        emotion, tendency = apply_hormonal_color(
            emotion, tendency, h_snap,
            oxt_connected=settings.get("hormonal_oxt_connected_threshold"),
            cort_withdrawn=settings.get("hormonal_cort_withdrawn_threshold"),
            oxt_guarded=settings.get("hormonal_oxt_guarded_threshold"),
            sht_dysphoric=settings.get("hormonal_sht_dysphoric_threshold"),
            aea_eased=settings.get("aea_eased_threshold"),
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

        # ── Coarse text-affect fallback ───────────────────────────────────────
        # If the neuromod basin still names "neutral" but the text signal is
        # clearly emotional, nudge toward a coarse label rather than reporting
        # neutral. Catches the common case where a single negative / affectionate
        # message hasn't yet moved the slow-decaying neuromods enough to leave
        # the neutral region. Skipped when metacognition already overrode.
        if not override_emotion and emotion == "neutral":
            user_emo = (features.get("user_emotion") or "").lower()
            fallback = None
            if hostility > 0.55:
                fallback = ("wary", f"hostility={hostility:.2f}")
            elif sentiment < -0.45:
                fallback = ("down", f"sentiment={sentiment:.2f}")
            elif sentiment > 0.55:
                fallback = ("content", f"sentiment={sentiment:.2f}")
            elif user_emo in ("frustrated", "annoyed", "disappointed", "angry"):
                fallback = ("irritated", f"user_emotion={user_emo}")
            elif user_emo in ("sad", "anxious", "distressed", "struggling", "tired"):
                fallback = ("concerned", f"user_emotion={user_emo}")
            elif user_emo in ("happy", "playful", "amused", "warm",
                              "affectionate", "excited"):
                fallback = ("warm", f"user_emotion={user_emo}")
            elif user_emo in ("curious", "engaged"):
                fallback = ("engaged", f"user_emotion={user_emo}")
            if fallback:
                emotion, why = fallback
                tendency = f"text-affect fallback: {why}"
                logger.debug("Hypothalamus: text-affect fallback %s → %s", why, emotion)

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
        """Apply time-weighted decay using turns computed by process() this turn.

        process() measures elapsed time and caches turns in self._current_turns;
        decay_turn() consumes that value and resets the clock, so both increment
        scaling and decay use the same elapsed-time measurement per turn.
        """
        self._last_decay_time = time.monotonic()
        self._bus.neuromod.decay(self._current_turns)
        self._bus.hormonal.decay(self._current_turns)
