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
        # Phase 8 (colony features): the PRIOR turn's aggregate cluster activity,
        # snapshotted at end-of-turn and fed back into chemistry at the start of the
        # next turn (prior-turn read avoids within-turn runaway).
        self._prev_aggregate: dict[str, float] | None = None

        # Stateful switches with decay
        self._valence_switch = StatefulSwitch(
            "valence_to_DA", CLUSTER, decay=settings.get("valence_to_DA_decay")
        )
        self._threat_switch = StatefulSwitch(
            "threat_to_GABA", CLUSTER, decay=settings.get("threat_to_GABA_decay")
        )
        self._novelty_switch = StatefulSwitch(
            "novelty_to_ACh", CLUSTER, decay=settings.get("novelty_to_ACh_decay")
        )
        self._arousal_switch = StatefulSwitch(
            "arousal_homeostat", CLUSTER, decay=settings.get("arousal_homeostat_decay")
        )
        # Inhibitory: receptor desensitization — suppresses novelty on repeated topics
        self._satiation_inhibitor = StatefulSwitch(
            "satiation_inhibitor",
            CLUSTER,
            decay=settings.get("satiation_inhibitor_decay"),
            polarity="inhibitory",
        )

        # Auditory prosody input (published by auditory cortex when --ears active)
        self._prosody_inbox = bus.subscribe("auditory.prosody")
        self._dynamics_inbox = bus.subscribe("auditory.speech_dynamics")
        self._music_inbox = bus.subscribe("auditory.music")
        # Metacognition's per-turn appraisal can override the neuromod-derived
        # emotion label for context-driven emotions (apologetic, grateful,
        # embarrassed, flirty, etc.) that pure neuromods can't produce.
        self._meta_override_inbox = bus.subscribe("meta.emotion_override")

    # ── Channel calibration ───────────────────────────────────────────────────

    def _calibrate_for_channel(
        self, sentiment: float, hostility: float, modality: str
    ) -> tuple[float, float]:
        """Apply channel-specific weights to raw temporal signals.

        Text and voice are different communication channels with different norms:
        - Text tends toward brevity; short messages ≠ hostility or disengagement.
        - Voice carries prosody; the text weight discounts hostility from brevity
          while up-weighting explicit word-level sentiment (which IS the primary
          signal in text, with no prosodic supplement).

        Returns (calibrated_sentiment, calibrated_hostility).
        """
        if not settings.get("enable_channel_calibration"):
            return sentiment, hostility

        if modality == "text":
            calibrated_hostility = hostility * settings.get("text_hostility_weight")
            calibrated_sentiment = sentiment * settings.get("text_sentiment_weight")
        else:
            # Voice: prosody handles the rest; keep weights at nominal
            calibrated_hostility = hostility
            calibrated_sentiment = sentiment

        return calibrated_sentiment, calibrated_hostility

    def _snapshot_aggregate(self) -> None:
        """Phase 8: capture this turn's aggregate cluster activity at end-of-turn,
        for feedback into chemistry next turn. Pure read of the current trace."""
        if not settings.get("colony_features", 0):
            return
        try:
            from brain.observability.firing_path import current_turn_trace

            tr = current_turn_trace.get()
        except Exception:
            tr = None
        if tr is None:
            self._prev_aggregate = None
            return
        fired = getattr(tr, "fired_path", None) or []
        switch_fires = sum(1 for e in fired if e.get("kind") == "switch")
        self._prev_aggregate = {
            # firing volume → arousal (effortful turns)
            "arousal": min(1.0, switch_fires / 25.0),
            # graded inhibitory tone → inhibition/conflict load. Reads the
            # continuous gain-control pressure (summed upward threshold shift the
            # chemistry applied to the gates this turn), not the rare near-miss
            # count — so the conflict→caution feedback actually senses inhibition
            # instead of resting at zero. ~0.5 of summed raise ≈ full load
            # (calibration constant; tune alongside colony_state_feedback_gain).
            "inhibition": min(1.0, float(getattr(tr, "suppression_pressure", 0.0)) / 0.5),
        }

    def _apply_state_feedback(self) -> None:
        """Phase 8: nudge neuromods from the PRIOR turn's aggregate activity. Small
        gain + per-channel clamp keep this activity→chemistry loop bounded; reading
        the prior turn (not the current one) prevents within-turn runaway."""
        if not settings.get("colony_features", 0) or not self._prev_aggregate:
            return
        gain = float(settings.get("colony_state_feedback_gain", 0.02))
        clamp = float(settings.get("colony_state_feedback_clamp", 0.05))
        agg = self._prev_aggregate

        def _bounded(signal: float) -> float:
            return max(-clamp, min(clamp, gain * signal))

        glu_d = _bounded(agg.get("arousal", 0.0))  # effort → general arousal
        gaba_d = _bounded(agg.get("inhibition", 0.0))  # conflict → caution
        self._bus.neuromod.add("Glu", glu_d)
        self._bus.neuromod.add("GABA", gaba_d)
        try:
            from brain.observability.decisions import decisions

            decisions.log(
                "state_feedback_applied",
                prior_arousal=round(agg.get("arousal", 0.0), 3),
                prior_inhibition=round(agg.get("inhibition", 0.0), 3),
                Glu_delta=round(glu_d, 4),
                GABA_delta=round(gaba_d, 4),
            )
        except Exception:
            pass

    async def process(self, features: dict) -> dict:
        """Update neuromod levels from temporal features. Return affect summary."""
        nm = self._bus.neuromod

        # Phase 8 (colony features): fold in the prior turn's aggregate cluster
        # activity before appraisal-driven updates (clamped, prior-turn → bounded).
        self._apply_state_feedback()

        # Compute time-weighted turns (elapsed since last decay / reference interval).
        # Increments for text-derived signals are multiplied by turns so that
        # equilibrium levels stay pace-independent — a 3-minute gap applies 3× the
        # per-turn delta but also decays 3× as much, keeping the fixed point stable.
        # Prosody and dynamics signals are NOT scaled (they are one-shot observations).
        now = time.monotonic()
        elapsed = now - self._last_decay_time
        ref = settings.get("decay_reference_interval_s")
        raw_turns = elapsed / ref
        turns = max(
            settings.get("decay_min_turns"), min(raw_turns, settings.get("decay_max_turns"))
        )
        self._current_turns = turns

        modality = features.get("input_modality", "text")
        raw_sentiment = features.get("sentiment", 0.0)
        raw_hostility = features.get("hostility", 0.0)
        sentiment, hostility = self._calibrate_for_channel(raw_sentiment, raw_hostility, modality)
        salience = features.get("salience", 0.3)
        surprise = features.get("surprise_score", 0.0)

        er_scale = settings.get("emotional_reactivity_scale")

        # N3 (colony-features-ii): per-persona SENSORY-FILTER gains. A persona
        # perceives some categories of input more strongly than others (the real
        # division-of-labor axis — differential detection, not response threshold).
        # Identity (1.0) unless colony_features AND colony_sensory_filter are on.
        from brain.neuron import reward_weight, sensory_gain
        from brain.persona_key import active_or_home_persona

        _persona = active_or_home_persona()
        _affect_gain = sensory_gain(_persona, "affective")
        _novelty_gain = sensory_gain(_persona, "novelty")
        # reward_weight is VALUATION (how much this persona values the source) vs sensory_gain's
        # DETECTION (does it notice the input). Both legitimately multiply; weights are kept
        # gentle to avoid compounding, and sensory_gain is colony-gated off by default so in
        # practice reward_weight is the active per-persona differentiator on these paths.
        _connection_value = reward_weight(_persona, "connection")
        _novelty_value = reward_weight(_persona, "novelty")

        # DA: valence signal. The positive (sentiment) term is a reward SOURCE — "connection"/
        # approval — scaled by how much this persona draws reward from it. The hostility term is
        # punishment, not a reward source, so it is left unscaled by the valuation weight.
        valence_delta = (
            sentiment
            * settings.get("sentiment_DA_weight")
            * er_scale
            * _affect_gain
            * _connection_value
        ) - (hostility * settings.get("hostility_DA_weight"))
        nm.add("DA", valence_delta * turns, source="external")

        # GABA: threat / caution signal (inhibitory). Graded with hostility — a dead
        # zone below the med threshold, then a smooth ramp up to the high-band value,
        # then the high-band slope above it. No flat mid-band: the release tracks HOW
        # hostile, not just that a line was crossed, and is continuous at both knees
        # (→0 at med, → high_threshold×high_increment at high). (hostility_GABA_
        # increment_med is now derived from this ramp rather than a fixed step.)
        _h_med = settings.get("hostility_GABA_threshold_med")
        _h_high = settings.get("hostility_GABA_threshold_high")
        _h_slope = settings.get("hostility_GABA_increment_high")
        if hostility > _h_high:
            nm.add("GABA", hostility * _h_slope * turns)
        elif hostility > _h_med:
            _knee = _h_high * _h_slope  # GABA value at the high knee, for continuity
            nm.add("GABA", _knee * (hostility - _h_med) / (_h_high - _h_med) * turns)

        # ACh: novelty / attention signal — scaled by the persona's novelty DETECTION
        # (_novelty_gain) and its novelty VALUATION (_novelty_value: how rewarding new
        # information is to this identity — the Visionary feeds on it, the Analyst less so).
        novelty_delta = (
            (
                surprise * settings.get("surprise_ACh_weight")
                + salience * settings.get("salience_ACh_weight")
            )
            * er_scale
            * _novelty_gain
            * _novelty_value
        )
        if self._satiation_inhibitor.state > 0.5:
            novelty_delta *= 1.0 - self._satiation_inhibitor.state * settings.get(
                "satiation_inhibition_factor"
            )
        nm.add("ACh", novelty_delta * turns)

        # Glu: general arousal
        arousal_delta = salience * settings.get("salience_Glu_weight") * er_scale
        if features.get("intent") == "hostile":
            # Scale the hostile-intent arousal bonus with how hostile, not a flat add
            # on the label — a mild jab and a tirade should not spike Glu equally.
            arousal_delta += settings.get("hostile_intent_Glu_bonus") * hostility
        nm.add("Glu", arousal_delta * turns)

        # NE: focused alertness — rises with salience, surprise, and threat.
        # Distinct from Glu (general arousal): NE is the sharp attentional spotlight,
        # with an inverted-U performance curve (too much narrows attention).
        # NE is NOT scaled by er_scale — its inverted-U performance curve
        # (Yerkes-Dodson) is governed by its own weights and should not be
        # amplified by the emotional reactivity dial (which controls valence/
        # arousal swings, not alertness overload).
        # NE is also NOT scaled by turns — LC phasic release depends on stimulus
        # intensity, not elapsed time. Long gaps don't accumulate alertness;
        # if anything, rest brings NE toward baseline (handled by decay).
        ne_delta = (
            salience * settings.get("ne_salience_weight")
            + surprise * settings.get("ne_surprise_weight")
            + hostility * settings.get("ne_hostility_weight")
        )
        nm.add("NE", ne_delta)

        # Satiation: if salience is low (routine), desensitize
        if salience < settings.get("salience_satiation_threshold"):
            self._satiation_inhibitor.update(settings.get("salience_satiation_increase"))
        else:
            self._satiation_inhibitor.update(settings.get("salience_satiation_decrease"))

        # Habituation: sustained familiarity (high satiation) raises GABA slightly —
        # represents the inhibitory signal of "I know this territory" settling in.
        if self._satiation_inhibitor.state > settings.get("satiation_gaba_threshold"):
            nm.add("GABA", settings.get("satiation_gaba_increment") * turns)

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

        # Graded release: scale each per-category increment by how strong the
        # acoustic signal is (tone_strength ∈ [0,1] from the DSP), so a slightly
        # tense voice and a trembling one don't add the same fixed jump. The
        # scale maps strength→[min,max] centered on 1.0 at strength=0.5, so the
        # existing fixed increments are preserved at mid-strength (average kept,
        # mirroring the graded-hostility design). Flag off → legacy fixed path.
        graded = settings.get("prosody_graded_release", 1)

        def _prosody_scale(strength: float) -> float:
            if not graded:
                return 1.0
            lo = settings.get("prosody_graded_min_scale")
            hi = settings.get("prosody_graded_max_scale")
            return lo + (hi - lo) * max(0.0, min(1.0, strength))

        if prosody_tone:
            tone_strength = (prosody_features or {}).get("tone_strength", 0.0)
            ps = _prosody_scale(tone_strength)
            if prosody_tone == "stressed":
                nm.add("GABA", 0.08 * ps)
                nm.add("ACh", 0.05 * ps)
                nm.add("NE", settings.get("ne_prosody_stressed") * ps)
            elif prosody_tone == "energetic":
                nm.add("Glu", 0.06 * ps)
                nm.add("DA", 0.04 * ps, source="external")
            elif prosody_tone == "whisper":
                nm.add("ACh", 0.10 * ps)
            # "calm" and "monotone" need no correction
            logger.debug(
                "Hypothalamus: prosody_tone=%s strength=%.2f scale=%.2f",
                prosody_tone,
                tone_strength,
                ps,
            )

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
            # Same graded scaling as prosody: deeper into the pace band → bigger
            # increment, fixed values preserved at mid-strength. Flag off → legacy.
            pace_scale = _prosody_scale(dynamics.get("pace_strength", 0.0))
            if pace == "rushed":
                nm.add("Glu", 0.08 * pace_scale)  # urgency
                nm.add("ACh", 0.04 * pace_scale)
                nm.add("NE", settings.get("ne_rush_increment") * pace_scale)
            elif pace == "brisk":
                nm.add("Glu", 0.04 * pace_scale)
                nm.add("DA", 0.02 * pace_scale, source="external")  # mild positive valence — animated
            elif pace == "halting":
                nm.add("ACh", 0.06 * pace_scale)  # uncertainty → pay attention
            elif pace == "measured":
                nm.add("ACh", 0.02 * pace_scale)
            # "normal" → no correction

            if dynamics.get("hesitant"):
                nm.add("ACh", 0.05)  # frequent long pauses → user is searching
            if dynamics.get("burst_score", 0.0) > 0.35:
                nm.add("GABA", 0.04)  # very bursty → mild caution flag (agitation)
            logger.debug(
                "Hypothalamus: pace=%s pauses=%d hesitant=%s",
                pace,
                dynamics.get("long_pause_count", 0),
                dynamics.get("hesitant"),
            )

        # ── Music features (background audio — softer deltas than speech) ────────
        music: dict | None = None
        while True:
            try:
                m_msg = self._music_inbox.get_nowait()
                if not m_msg.expired:
                    music = m_msg.payload
            except asyncio.QueueEmpty:
                break

        if music:
            mood = music.get("mood_label", "calm")
            if mood == "energetic":
                nm.add("Glu", 0.05)
                nm.add("DA", 0.04, source="external")
            elif mood == "bright":
                nm.add("DA", 0.05, source="external")
                nm.add("ACh", 0.02)
            elif mood == "tense":
                nm.add("GABA", 0.04)
                nm.add("NE", 0.03)
            elif mood == "melancholic":
                nm.add("DA", -0.03, source="external")
            elif mood == "calm":
                nm.add("Glu", -0.02)
            logger.debug(
                "Hypothalamus: music mood=%s bpm=%.0f key=%s%s",
                mood,
                music.get("bpm", 0),
                music.get("key", "?"),
                music.get("mode", ""),
            )

        # ── Text paralinguistics (text turns only; skipped when prosody arrived) ──
        # Text has no acoustic channel, so emoji/laughter/warmth markers are the
        # closest equivalent to prosody. Apply only when voice prosody is absent.
        if not prosody_tone and settings.get("enable_text_paralinguistics"):
            text_para = features.get("text_paralinguistics")
            if text_para:
                laughter = text_para.get("laughter", 0.0)
                warmth = text_para.get("warmth", 0.0)
                negativity = text_para.get("negativity", 0.0)
                excitement = text_para.get("excitement", 0.0)

                if laughter > 0.0:
                    # The user laughing IS the reward event for levity — scaled by
                    # how much this persona draws reward from landing a laugh
                    # (reward-source valuation, same pattern as connection/novelty
                    # above). A levity-driven persona feels a laugh land harder,
                    # and the resulting DA swing feeds the Hebbian funnel, so
                    # what earned laughs gets reinforced.
                    _levity_value = reward_weight(_persona, "levity")
                    nm.add("DA", laughter * settings.get("text_para_laughter_DA") * _levity_value, source="external")
                if warmth > 0.0:
                    nm.add("DA", warmth * settings.get("text_para_warmth_DA"), source="external")
                if negativity > 0.0:
                    nm.add("GABA", negativity * settings.get("text_para_negativity_GABA"))
                if excitement > 0.0:
                    nm.add("Glu", excitement * settings.get("text_para_excitement_Glu"))
                    nm.add("NE", excitement * settings.get("text_para_excitement_NE"))

                if laughter > 0.1 or warmth > 0.1 or excitement > 0.1:
                    logger.debug(
                        "Hypothalamus text_para: laughter=%.2f warmth=%.2f "
                        "negativity=%.2f excitement=%.2f",
                        laughter,
                        warmth,
                        negativity,
                        excitement,
                    )

        # ── Voice laughter (transcript ∨ DSP heuristic ∨ event classifier) ────
        # Narrow exception to the "prosody skips text-para" rule above: laughter
        # markers in a TRANSCRIPT are evidence of a real laugh regardless of
        # channel (Deepgram often transcribes laughs as "ha ha"), and the
        # acoustic tiers add what the transcript misses. Tiers compose via
        # max() — the same laugh seen by two detectors is one laugh, not two.
        # All channels reuse text_para_laughter_DA so text and voice laughter
        # release comparable DA. Like the text path, this is PRE-draft appraisal
        # (the user's laugh is the stimulus), not post-draft reward.
        if modality == "voice" or prosody_tone:
            _pf = prosody_features or {}
            voice_laughter = features.get("transcript_laughter", 0.0)
            _dsp_laugh = _pf.get("laughter_likelihood", 0.0)
            if _dsp_laugh >= settings.get("laughter_dsp_threshold"):
                voice_laughter = max(voice_laughter, _dsp_laugh)
            _events = _pf.get("vocal_events") or {}
            _event_laugh = _events.get("laughter", 0.0)
            # The classifier emits small probs for everything; gate at the same
            # conservative threshold as the DSP tier before releasing chemistry.
            if _event_laugh >= settings.get("laughter_dsp_threshold"):
                voice_laughter = max(voice_laughter, _event_laugh)

            if voice_laughter > 0.0:
                # Same reward-source valuation as the text laughter path: the
                # user laughing IS the reward event for levity, and the DA
                # swing feeds the Hebbian funnel.
                _levity_value = reward_weight(_persona, "levity")
                nm.add(
                    "DA",
                    voice_laughter * settings.get("text_para_laughter_DA") * _levity_value,
                )
                logger.debug(
                    "Hypothalamus voice laughter: transcript=%.2f dsp=%.2f event=%.2f → %.2f",
                    features.get("transcript_laughter", 0.0),
                    _dsp_laugh,
                    _event_laugh,
                    voice_laughter,
                )
            # Future: _events also carries sigh/gasp/crying probabilities —
            # candidates for small GABA (sigh = release/fatigue) and NE
            # (gasp = startle) nudges once the classifier has eval coverage.

        snap = nm.snapshot()

        # ── Endocrine (hormonal) updates ──────────────────────────────────────
        hs = self._bus.hormonal

        # OXT: build on warm positive exchange; drain under hostility.
        # Gate lowered to sentiment>0.2 (was hardcoded 0.3): the old gate fired
        # on only ~1.7% of turns, so OXT never reached the "connected" threshold.
        # Ordinary warm exchanges (sentiment 0.2–0.3) now accrue bond. (F4)
        if sentiment > settings.get("oxt_sentiment_gate") and hostility < settings.get(
            "oxt_hostility_gate"
        ):
            hs.add("OXT", settings.get("oxt_positive_increment") * turns)
        elif hostility > settings.get("hostility_GABA_threshold_high"):
            hs.add("OXT", -settings.get("oxt_hostility_drain") * turns)

        # CORT: accumulates under sustained social threat (direct hostility, not prosody).
        # Decoupled from GABA so that animated/focused voice patterns don't trigger
        # false cortisol build — prosody raises GABA for alertness, not social stress.
        if hostility > settings.get("cort_hostility_threshold"):
            hs.add("CORT", settings.get("cort_threat_increment") * turns)
        # flock_dynamics (4): ground CORT in a real stake, not just hostile words.
        # Sustained above-average surprise (prediction-error) accrues cortisol —
        # "the world keeps diverging from my model" is the AI's stress analog,
        # and it's what the trajectory-based rumination (rising CORT) should
        # track. Flag-off: CORT stays hostility-lexicon-only as before.
        if settings.get("flock_dynamics", 0):
            surprise_excess = max(0.0, surprise - 0.5)
            if surprise_excess > 0.0:
                hs.add("CORT", surprise_excess * settings.get("flock_cort_surprise_weight") * turns)

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
            h_snap["5HT"],
            h_snap["CORT"],
            h_snap["OXT"],
            h_snap["AEA"],
        )
        # Apply hormonal modulation to effective neuromod values for emotion
        # naming. Raw accumulator levels are unchanged; the math lives in
        # _effective_emotion so refresh_emotion() can re-derive mid-turn
        # (post-recall-affect) with identical adjustments.
        emotion, tendency = self._effective_emotion(snap, hs, h_snap)

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
            logger.debug(
                "Hypothalamus: emotion override → %s (%s)", override_emotion, override_reason
            )

        # ── Coarse text-affect fallback ───────────────────────────────────────
        # If the neuromod basin still names "neutral" but the text signal is
        # clearly emotional, nudge toward a coarse label rather than reporting
        # neutral. Catches the common case where a single negative / affectionate
        # message hasn't yet moved the slow-decaying neuromods enough to leave
        # the neutral region. Skipped when metacognition already overrode.
        if not override_emotion and emotion == "neutral":
            user_emo = (features.get("user_emotion") or "").lower()
            fallback = None
            # Use RAW signals for emotion detection (channel calibration reduces
            # neuromod UPDATES but should not suppress hostile emotion recognition)
            if raw_hostility > 0.55:
                fallback = ("wary", f"hostility={raw_hostility:.2f}")
            elif raw_sentiment < -0.45:
                fallback = ("down", f"sentiment={raw_sentiment:.2f}")
            elif raw_sentiment > 0.55:
                fallback = ("content", f"sentiment={raw_sentiment:.2f}")
            elif user_emo in ("frustrated", "annoyed", "disappointed", "angry"):
                fallback = ("irritated", f"user_emotion={user_emo}")
            elif user_emo in ("sad", "anxious", "distressed", "struggling", "tired"):
                fallback = ("concerned", f"user_emotion={user_emo}")
            elif user_emo in ("happy", "playful", "amused", "warm", "affectionate", "excited"):
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
            "prosody_speech_rate": round((prosody_features or {}).get("speech_rate_hz", 0.0), 2),
            "pace_label": (dynamics or {}).get("pace_label"),
            "hesitant_speech": bool((dynamics or {}).get("hesitant")),
            "emotion_source": "metacognition" if override_emotion else "neuromod",
            "emotion_override_reason": override_reason if override_emotion else None,
        }

        await self._bus.publish_dict("affect.state", affect, source=CLUSTER)
        logger.debug(
            "Hypothalamus: emotion=%s DA=%.2f GABA=%.2f", emotion, snap["DA"], snap["GABA"]
        )
        return affect

    def _effective_emotion(self, snap: dict, hs, h_snap: dict) -> tuple[str, str]:
        """Name the emotion from hormonally-adjusted effective neuromod values.
        Pure read — raw accumulators are untouched. Shared by process() and
        refresh_emotion() so a mid-turn re-derivation uses identical math."""
        # AEA suppresses effective NE and Glu when elevated above resting baseline.
        ne_scale, glu_scale = hs.aea_suppress(
            settings.get("aea_ne_suppression"),
            settings.get("aea_glu_suppression"),
        )
        eff_NE = max(0.0, min(1.0, snap["NE"] * ne_scale))
        eff_Glu = max(0.0, min(1.0, snap["Glu"] * glu_scale))

        # DA: hormonal offset + AEA afterglow lift
        da_offset = hs.da_offset(
            settings.get("sht_da_floor_lift"),
            settings.get("oxt_da_lift"),
            settings.get("cort_da_suppress"),
        )
        aea_suppress = max(
            0.0, h_snap["AEA"] - settings.get("aea_da_suppress_threshold")
        ) * settings.get("aea_da_suppress")
        eff_DA = max(
            0.0,
            min(
                1.0,
                snap["DA"] + da_offset + h_snap["AEA"] * settings.get("aea_da_lift") - aea_suppress,
            ),
        )
        eff_GABA = max(
            0.0,
            min(
                1.0,
                snap["GABA"]
                * hs.gaba_scale(
                    settings.get("cort_gaba_amplify"),
                    settings.get("oxt_gaba_buffer"),
                ),
            ),
        )

        # Name current emotion (using fully-adjusted effective values)
        emotion, tendency = name_emotion(eff_DA, eff_GABA, snap["ACh"], eff_Glu)

        # NE color: inverted-U modifier (vigilant / alert-curious / scattered)
        emotion, tendency = apply_ne_color(
            emotion,
            tendency,
            eff_NE,
            ne_high=settings.get("ne_high_threshold"),
            ne_scatter=settings.get("ne_scatter_threshold"),
        )

        # Hormonal color: connected / withdrawn / guarded / eased / dysphoric
        emotion, tendency = apply_hormonal_color(
            emotion,
            tendency,
            h_snap,
            oxt_connected=settings.get("hormonal_oxt_connected_threshold"),
            cort_withdrawn=settings.get("hormonal_cort_withdrawn_threshold"),
            oxt_guarded=settings.get("hormonal_oxt_guarded_threshold"),
            sht_dysphoric=settings.get("hormonal_sht_dysphoric_threshold"),
            aea_eased=settings.get("aea_eased_threshold"),
        )
        return emotion, tendency

    def refresh_emotion(self) -> tuple[str, str]:
        """Re-derive (emotion, tendency) from the CURRENT bus state — for mid-turn
        neuromod updates that land after process() named the emotion (recall
        affect). Same effective-value math as process(); does NOT re-drain the
        metacognition override inbox (overrides are consumed once, by process)."""
        snap = self._bus.neuromod.snapshot()
        hs = self._bus.hormonal
        return self._effective_emotion(snap, hs, hs.snapshot())

    def decay_turn(self) -> None:
        """Apply time-weighted decay using turns computed by process() this turn.

        process() measures elapsed time and caches turns in self._current_turns;
        decay_turn() consumes that value and resets the clock, so both increment
        scaling and decay use the same elapsed-time measurement per turn.
        """
        self._last_decay_time = time.monotonic()
        # Phase 3 (colony features): apply any primer nudges deposited by messages
        # this turn BEFORE decay, so they integrate then relax naturally. The
        # hypothalamus is the single hormonal-state writer.
        if settings.get("colony_features", 0):
            primers = self._bus.drain_primers()
            if primers:
                gain = float(settings.get("colony_primer_gain", 0.30))
                for ch, v in primers.items():
                    if ch in self._bus.hormonal.CHANNELS:
                        self._bus.hormonal.add(ch, v * gain)
        # flock_dynamics (1): record the per-turn chemistry trajectory BEFORE
        # decay, so velocity reflects the turn's settled level vs. the prior
        # turn's. Sampling at this single per-turn writer keeps the series clean.
        if settings.get("flock_dynamics", 0):
            self._bus.neuromod.mark_turn(self._current_turns)
            self._bus.hormonal.mark_turn(self._current_turns)
        self._bus.neuromod.decay(self._current_turns)
        self._bus.hormonal.decay(self._current_turns)
        # Phase 8: snapshot this turn's aggregate for next turn's feedback.
        self._snapshot_aggregate()
