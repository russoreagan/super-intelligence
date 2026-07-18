"""
Brain Settings — runtime-tunable constants.
Loads from brain/settings.json (next to this file) at startup.
Falls back to built-in defaults when the file doesn't exist.
Changes take effect after restarting the brain.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to the settings file. Defaults to brain/settings.json next to this module.
# Overridable via BRAIN_SETTINGS_PATH so each multi-tenant pod points at its own
# settings.json on the user's volume (e.g. /data/settings.json) — without an
# override, all per-user processes on one host would share (and race on) the one
# bundled file.
SETTINGS_PATH = Path(
    os.environ.get("BRAIN_SETTINGS_PATH", "").strip() or (Path(__file__).parent / "settings.json")
)

# ── Defaults ─────────────────────────────────────────────────────────────────
# Each entry: key → default value
# Groups mirror the settings UI sections.

DEFAULTS: dict[str, float | int | str] = {
    # ── Section 1: Emotional Reactivity ──────────────────────────────────────
    "emotional_reactivity_scale": 1.00,
    "sentiment_DA_weight": 0.10,
    "hostility_DA_weight": 0.10,
    "surprise_ACh_weight": 0.12,
    "salience_ACh_weight": 0.08,
    "salience_Glu_weight": 0.12,
    # ── Intrinsic reward-source magnitudes ───────────────────────────────────
    # Base deltas for the appraisal layer; scaled per persona by neuron.reward_weight()
    # and by emotional_reactivity_scale. See plan: per-persona reward-source vector.
    "correctness_reward_base": 0.10,  # DA on CONFIRMED-correct (verified — strongest)
    "correctness_self_base": 0.06,  # DA on high self-judged draft quality (weaker evidence)
    "correctness_penalty_base": 0.06,  # DA dip on confirmed-wrong / low self-eval
    "correctness_5ht_drain": 0.010,  # 5HT drain on wrong (the lingering-sadness component)
    "anticipation_reward_scale": 0.03,  # DMN anticipatory DA(hoped)/CORT(dreaded) per scenario
    "self_standard_gate": 0.85,  # self-score above which pride fires WITHOUT user praise
    # ── Self-verified correctness (Stage 5): prediction confirmed by reality ──
    "prediction_reward_base": 0.04,  # DA on a confident, NON-trivial prediction confirmed
    "prediction_confidence_min": 0.55,  # below this a "prediction" is a guess — no reward
    "prediction_informativeness_min": 0.20,  # skip near-degenerate (always-same) predictions
    "prediction_reward_turn_cap": 0.08,  # max prediction-confirmation DA per turn (anti-farm)
    # ── Accomplishment / mastery (Stage 6): effort overcome × success ────────
    "accomplishment_base": 0.07,  # DA at completion, before difficulty scaling
    # Per-JOB ceiling on self-administered DA (story-criteria rewards mid-run +
    # the terminal accomplishment reward together). Keeps one job's self-reward
    # in the same league as an explicit user affirm (~0.10) instead of 3-4×. 0 = off.
    "job_intrinsic_da_cap": 0.20,
    "accomplishment_fail_ratio": 0.40,  # failed-hard-task penalty = base*difficulty*this (< reward)
    # Expectation baselines: effort the upfront complexity label braces for (r = measured/expected).
    "accomplishment_expected_low": 2.0,
    "accomplishment_expected_medium": 6.0,
    "accomplishment_expected_high": 14.0,
    "accomplishment_overshoot_band": 1.5,  # r up to here = "rose to the challenge" (peak)
    "accomplishment_overshoot_k": 0.5,  # how fast satisfaction erodes past the band (frustration)
    "accomplishment_anticlimax": 0.85,  # terminal modifier when much easier than feared (r << 1)
    "frustration_overshoot_gain": 0.04,  # in-the-moment NE/GABA per unit of effort-overshoot mid-task
    # ── Idle cognition rewards (Stage 7): reinforce thinking done while alone ──
    "idle_thought_quality_base": 0.04,  # DA for a good idle thought (novelty-weighted)
    "idle_thought_quality_min": 0.55,  # quality below which an idle thought earns nothing
    "hostility_GABA_threshold_high": 0.50,
    "hostility_GABA_increment_high": 0.20,
    "hostility_GABA_threshold_med": 0.20,
    "hostility_GABA_increment_med": 0.05,
    "hostile_intent_Glu_bonus": 0.15,
    # 1 = AI can deliberately set its own mood via set_mood tool; 0 = disabled
    "emotional_expression_enabled": 1,
    # ── Section 2: Neuromodulator Homeostasis ─────────────────────────────────
    "valence_to_DA_decay": 0.85,
    "threat_to_GABA_decay": 0.80,
    "novelty_to_ACh_decay": 0.90,
    "arousal_homeostat_decay": 0.88,
    "satiation_inhibitor_decay": 0.95,
    "salience_satiation_threshold": 0.30,
    "salience_satiation_increase": 0.05,
    "salience_satiation_decrease": -0.10,
    "satiation_inhibition_factor": 0.50,
    # ── Section 3: Plasticity & Learning ─────────────────────────────────────
    "hebbian_delta": 0.02,
    "hebbian_outcome_delta": 0.02,
    # Composite outcome mix used when a turn carries an EXTERNAL grade (thumbs press,
    # validator verdict) — hebbian._composite_outcome. Ungraded turns keep their own
    # fixed self-appraisal blend; these dials apply only on the graded path, where the
    # outside verdict earns a share of the signal. Defaults reproduce the previous
    # inline fallbacks exactly.
    "hebbian_w_da_ext": 0.4,  # DA swing — the brain's own reward signal
    "hebbian_w_critic_ext": 0.2,  # internal critic score on the selected draft
    "hebbian_w_user_ext": 0.2,  # inferred user emotion
    "hebbian_w_external": 0.2,  # the external grade itself
    # DA nudge on an external grade (session_loops.api_grade_turn), scaled by the grade.
    # 0 = record and observe only, no chemistry — external grading ships observability
    # first. Raising it opens the only reward channel sourced outside the brain's own
    # appraisal. Keep this a float: an int default would coerce a fractional nudge to 0
    # on load and silently re-break the dial.
    "external_grade_da_nudge": 0.15,
    "decay_toward_rest_rate": 0.01,
    # Eligibility traces: a turn's outcome also credits the fired paths of the
    # previous N turns, decayed e^(-age/τ) — delayed conversational payoff
    # reaches the turns that set it up. lookback 0 restores instantaneous-only.
    "eligibility_lookback": 2,
    "eligibility_tau_turns": 2.0,
    # Force a real critic run (no predictor skip) on high-information turns:
    # an explicit user verdict tone, NE at/above this, or DA this far from the
    # persona baseline. Phasic outcome sampling for the Hebbian signal.
    "critic_force_ne": 0.70,
    "critic_force_da_dev": 0.15,
    # Affect carryover: a post-draft DA swing past this threshold becomes a one-
    # line interoceptive hint in the NEXT turn's drafter prompt (two-phase rule:
    # post-draft chemistry is reward, never re-colors the current response).
    "affect_carryover": 1,
    "affect_carryover_da_threshold": 0.10,
    # Motivation overrides (the Motivation dials): per-persona multipliers on
    # the innate reward-source table in neuron._PERSONA_REWARD_WEIGHTS, centred
    # at 1.0. >1 = this deployment's persona draws MORE reward from the source.
    "reward_weight_correctness": 1.0,
    "reward_weight_connection": 1.0,
    "reward_weight_novelty": 1.0,
    "reward_weight_aesthetic": 1.0,
    "reward_weight_relief": 1.0,
    "reward_weight_mastery": 1.0,
    "reward_weight_levity": 1.0,
    "weight_min": 0.10,
    "weight_max": 3.00,
    # Risk posture (independent of the reward-source dials above): the ASYMMETRY between how
    # losses and gains are felt, plus how aversive raw uncertainty is on its own. Innate per-
    # persona baselines live in neuron._PERSONA_RISK_POSTURE; these scales (centred 1.0) tune
    # them per deployment, the same way the Motivation dials scale reward_weight.
    "loss_aversion_scale": 1.0,  # ×λ: how much harder a loss bites than an equal gain (1.0 = symmetric)
    "loss_aversion_min": 0.5,  # λ floor (below 1.0 = loss-SEEKING / reckless)
    "loss_aversion_max": 3.0,  # λ ceiling (prospect-theory rarely exceeds ~2.5)
    "uncertainty_aversion_scale": 1.0,  # ×κ: dread drawn from outcome SPREAD/variance (0 baseline = risk-neutral)
    "uncertainty_aversion_max": 1.5,  # κ ceiling
    "gaba_skip_threshold_high": 0.55,
    # Drafter selection: 1 = sample drafters ∝ softmax(learned weight) so a Hebbian
    # ranking shift changes the response MIX even when the count saturates the slate
    # (lets learning express behaviorally); 0 = legacy ε-greedy hard top-N (rollback).
    # Temperature: lower = more decisive toward high-weight drafters, higher = more even.
    "drafter_weighted_sampling": 1,
    "drafter_sampling_temperature": 0.20,
    # Switch-ordering learning surface (PAPER.md §4.7's "switch evaluation order").
    # Consume: learned sensory.text→temporal.<switch> weight scales that switch's
    # firing readiness via a BOUNDED, DIRECTION-AWARE efficacy on its threshold —
    # so safety gates can never be learned away. Credit: reinforce those edges for
    # gated switches that fired on good-outcome turns (half-scale vs the path credit
    # on the second hop, to avoid a runaway loop). Both 1=on; frozen wiring disables.
    "switch_efficacy_routing": 1,
    "switch_routing_credit": 1,
    "switch_routing_credit_scale": 0.5,
    # Per-switch efficacy band [min,max] on the threshold multiplier. >1 lowers the
    # threshold (fires more readily), <1 raises it. Direction is constrained per
    # switch by its dangerous direction: template_match may only get LESS eager
    # (never skip understanding more); self_reference may only get MORE eager (never
    # suppress the safety block). Switches absent here are exempt (no efficacy).
    "switch_efficacy_bands": {
        "template_match": [0.85, 1.0],
        "self_reference": [1.0, 1.4],
        "epistemic_action": [0.9, 1.25],
    },
    # Recall fan-out learning surface (PAPER.md §4.7's "recall fan-out"). The third
    # named Hebbian surface. Consume (already live): learned mem.recall→hippocampus.
    # <strategy> weights set the schema-vs-episode budget split in hippocampus.
    # _allocate_recall_budget. Credit (this flag): on good-outcome turns, reinforce
    # the side that actually contributed recalls, by contribution share — so the
    # split learns toward the productive pathway. Half-scale, signed by outcome.
    # Attribution is at side granularity (schema={schema_grep,entity_tracker},
    # episode={cosine_recall,time_filter}) by hit-count share — a volume proxy for
    # usefulness, the faithful granularity since the weights only gate the split.
    "recall_routing_credit": 1,
    "recall_routing_credit_scale": 0.5,
    # Embedding-based intent detection for the fast-path gates (self_reference,
    # epistemic_action). A per-persona exemplar bank is matched by cosine against the
    # input embedding so paraphrases the seed list never anticipated still fire; the
    # bank grows whenever the understanding integrator confirms an intent the match
    # missed, so LLM cost falls with use. Falls back to the literal seed list when
    # disabled or no embedder is available. Threshold is deliberately conservative —
    # the learning loop, not a loose threshold, is what makes detection comprehensive.
    "intent_detector_enabled": 1,
    # 0.60 calibrated against nomic-embed-text on real phrasings: true-positive floor
    # ~0.62 (e.g. "are you conscious" 0.616), cross-talk false-positive ceiling ~0.58.
    # 0.60 sits in that gap. The learning loop catches anything still missed.
    "intent_fire_threshold": 0.60,
    "intent_bank_max": 200,
    "intent_dedup_threshold": 0.95,
    # ── Section 4: Default Mode Network ──────────────────────────────────────
    "dmn_enabled": 1,  # owner kill-switch (runtime, PUT /v1/dmn) — distinct from the BRAIN_DMN env gate
    "dmn_interval": 8.0,  # active baseline — fires when any mouse/keyboard activity detected
    "dmn_idle_interval": 45.0,  # when fully away from computer (OS idle > 60s)
    "dmn_min_tick_interval": 5.0,  # floor between DMN ticks regardless of computed interval (dmn.py)
    "dmn_overlap_threshold": 0.35,
    "ach_suppression_weight": 0.70,  # was 1.00 — was over-suppressing idle thought
    "glu_suppression_weight": 0.25,  # was 0.30
    "gaba_suppression_reduction": 0.15,
    "suppression_skip_prob_max": 0.55,  # was 0.85 — cap skip at 55% not 85%
    "speak_gate_poll_interval": 5.0,  # how often the speak gate evaluates candidates
    "speak_candidate_max_age_s": 60.0,  # drop unspoken candidates older than this
    "speak_candidate_max_attempts": 4,  # drop a candidate after this many judge re-defers
    # Bridge rewriter (local Ollama only — no paid LLM calls). When a
    # candidate is approved AND its topic-overlap with the live conversation
    # is below `speak_bridge_overlap_threshold`, the brain rewrites the
    # spoken form locally so the change-of-subject doesn't feel abrupt.
    # Set enabled=0 to disable bridging; set threshold=1.0 to bridge every
    # approved candidate (most polish, most local-LLM latency).
    "speak_bridge_enabled": 1,  # 1 = on, 0 = off (kept int for settings UI sliders)
    "speak_bridge_overlap_threshold": 0.20,
    # ── DMN resilience: skip-and-backoff ─────────────────────────────────────
    # A missed idle thought is harmless, so the thoughts path never retries.
    # After this many CONSECUTIVE failed ticks (model unavailable / step error),
    # the loop lengthens its interval geometrically to stop hammering a saturated
    # or down local model — freeing it for the subsystems that need it. The
    # backoff resets to 1 on the first successful tick.
    "dmn_backoff_after_failures": 2,  # consecutive failures before backoff kicks in
    "dmn_backoff_factor": 2.0,  # interval multiplier per failure beyond the threshold
    "dmn_backoff_max_multiplier": 8.0,  # cap so the loop never sleeps absurdly long
    # ── DMN semantic dedup ───────────────────────────────────────────────────
    # Cosine over thought embeddings is the real anti-repetition gate; the
    # word-overlap check stays as a cheap pre-filter. Because cosine doesn't
    # over-fire on shared function words, we compare against the FULL recent
    # window (not just the last few) without the over-suppression that forced
    # the narrow word-overlap window.
    "dmn_semantic_dedup_enabled": 1,  # 1 = on, 0 = word-overlap only
    "dmn_semantic_dup_threshold": 0.88,  # cosine ≥ this vs any recent thought → suppress
    # ── DMN rumination (idle-only, chemistry-gated) ──────────────────────────
    # Rumination = one bounded episode that deepens a single seed through several
    # analytical skill packages. Eligible ONLY when the user is OS-idle (never
    # mid-conversation). A dual-driver chemistry score fires it under worry
    # (CORT/NE high, 5HT low) AND under high interest (DA/ACh high).
    "dmn_rumination_enabled": 1,
    "dmn_rumination_idle_threshold_s": 60.0,  # user must be OS-idle at least this long
    "dmn_rumination_drive_threshold": 0.45,  # rumination_drive ≥ this makes a tick eligible
    "dmn_rumination_prob_at_threshold": 0.5,  # P(ruminate) once eligible (×drive scaling)
    "dmn_rumination_max_consecutive": 2,  # depth cap: max back-to-back ruminations on one seed
    "dmn_rumination_max_iters": 4,  # chain length cap inside ruminate()
    "dmn_rumination_time_budget_s": 25.0,  # wall-clock cap for one rumination episode
    # rumination_drive weights (see _rumination_drive)
    "rum_w_cort": 0.50,  # cortisol — anxious/brooding driver
    "rum_w_ne": 0.40,  # norepinephrine over 0.30 baseline — anxious vigilance
    "rum_w_da": 0.45,  # dopamine over 0.50 — engaged "can't stop chasing it"
    "rum_w_ach": 0.35,  # acetylcholine over 0.50 — focused interest
    "rum_w_5ht": 0.40,  # serotonin — high 5HT lets you DISENGAGE, so it subtracts
    # TONIC idle drive (Stage 7 fix): the phasic worry/interest drive above decays to ~0 during
    # deep idle, so rumination never fired. The DMN is most active AT REST — these add a
    # mind-wandering (boredom) + finish-out (unfinished business) pull that grows during idle.
    "rum_w_boredom": 0.30,  # weight on idle-duration boredom term. Kept modest on purpose:
    #                         time-to-bored is meant to be chemistry/persona-driven (see the
    #                         `disposition` term in _tonic_idle_drive — Poet chews soonest,
    #                         Sage latest), so different agents get bored at different rates.
    "rum_idle_saturation_s": 300.0,  # boredom ramps 0→1 over this much idle past the threshold
    "rum_w_unfinished": 0.30,  # weight on open-thread finish-out pull
    "rum_unfinished_cap": 4.0,  # advances at which unfinished-business pull saturates
    # Per-step costs that let anxious rumination self-limit (added each chain step)
    "rum_step_gaba_cost": 0.02,
    "rum_step_satiation_cost": 0.05,
    # Interest threshold for varying skills on NORMAL idle ticks (non-rumination)
    "dmn_skill_vary_drive_threshold": 0.30,
    # ── Section 5: Metacognition ──────────────────────────────────────────────
    "meta_interval": 30.0,
    "meta_cooldown_turns": 3,
    "da_threshold_disappointed": 0.25,
    "gaba_drop_threshold": 0.20,
    # ── Section 6: Prediction & Surprise ─────────────────────────────────────
    "surprise_threshold": 0.40,
    "confidence_skip_threshold": 0.70,
    "predictor_window": 8,
    # Fraction of gated skips to shadow-validate: run the integrator anyway purely
    # for measurement (records actual vs. predicted + feeds the true label back into
    # predictor history for self-correction) WITHOUT changing the gated behavior.
    # 0 = off. Gating is rare, so this adds ~1% to integrator-call volume at 0.15.
    "gating_shadow_sample_rate": 0.15,
    # ── Section 7: Voice Expressiveness ──────────────────────────────────────
    "voice_stability_default": 0.45,
    "voice_style_default": 0.40,
    "voice_speed_default": 1.00,
    "voice_stability_threat": 0.65,
    "voice_style_threat": 0.25,
    "voice_speed_threat": 0.95,
    "voice_stability_bright": 0.35,
    "voice_style_bright": 0.55,
    "voice_speed_bright": 1.05,
    "voice_stability_low_mood": 0.55,
    "voice_style_low_mood": 0.30,
    "voice_speed_low_mood": 0.93,
    # Continuous voice: blend the per-turn base VoiceSettings as a chemistry-
    # weighted average of the emotion anchors instead of snapping to one discrete
    # state. 1 = continuous (each chemistry gets its own settings); 0 = legacy
    # threshold branches. Temperature controls blend sharpness: low → closer to
    # picking one anchor (more discrete); high → softer/more averaged.
    "voice_continuous_blend": 1,
    "voice_blend_temperature": 0.15,
    "breath_pause_count_max": 2,
    "gaba_single_pause_threshold": 0.50,
    "da_double_pause_threshold": 0.30,
    "glu_urgently_threshold": 0.55,
    "gaba_urgently_threshold": 0.35,
    "gaba_gently_threshold": 0.50,
    "da_excited_threshold": 0.60,
    "glu_excited_threshold": 0.55,
    "ach_curious_threshold": 0.55,
    "gaba_curious_threshold": 0.35,
    "da_softly_threshold": 0.30,
    # ── Section 8: Proactive Behavior ─────────────────────────────────────────
    # idle_threshold is now INVERTED in spirit: the brain STOPS speaking
    # proactively when the user has been OS-idle for more than this. Internal
    # thoughts still flow to the UI; only TTS interjections are suppressed.
    "proactive_idle_threshold": 300.0,  # was 180
    "proactive_response_window": 10.0,  # was 8 — min gap between brain utterances
    # ── Section 9: Attention & Routing ───────────────────────────────────────
    "hippocampus_priority_base": 0.60,
    "hippocampus_salience_weight": 0.30,
    "occipital_priority_base": 0.80,
    "frontal_hostile_priority": 0.30,
    "frontal_ach_weight": 0.20,
    "ach_threshold_frontal": 0.50,
    "salience_workspace_threshold": 0.60,
    "topic_activation_decay": 0.70,  # cross-turn decay of the thalamus spotlight
    # Global-workspace spotlight (GWT). Ships on with a conservative threshold.
    # The thalamus reads the bus concentration layer, decides what is ignited, and
    # broadcasts. 0 → neutral verdict, every consumer falls through to prior logic.
    "thalamus_workspace_enabled": 1,
    # Ignition level. Sits above the bus quorum threshold (1.5) so ignition marks
    # sustained focus, not passing mention. Concentration caps at 10.
    "workspace_ignition_threshold": 2.0,
    "workspace_rising_slope": 0.20,  # slope (units/s) that counts as "rising"
    # ── Section 10: Speaker Recognition ──────────────────────────────────────
    "speaker_store_threshold": 0.70,
    "speaker_session_threshold": 0.62,
    "speaker_min_audio_s": 0.40,
    # Soft threshold for "could this be the primary user?" check on unrecognized voices.
    # If an unrecognized voice scores >= this against the primary user's profile, treat
    # it as the primary user tentatively rather than creating a stranger placeholder.
    "speaker_primary_soft_threshold": 0.55,
    # ── Section 11: Vision / Video ────────────────────────────────────────────
    "video_sample_interval": 5.0,
    "video_max_frames": 8,
    "video_change_threshold": 8.0,
    # ── Section 12: Endocrine / Hormonal System ───────────────────────────────
    # Update rates (added per turn when condition is met)
    "oxt_positive_increment": 0.008,  # OXT gain per warm/positive exchange (~50 turns to connected)
    "oxt_hostility_drain": 0.008,  # OXT drain per hostile exchange (symmetric)
    # OXT accrual gate (F4): old hardcoded sentiment>0.3 fired ~1.7% of turns, so
    # OXT plateaued ~0.52 and never crossed the 0.60 connected threshold. Lower
    # the gate so ordinary warm exchanges build bond. Tune via re-measurement.
    "oxt_sentiment_gate": 0.20,  # min sentiment to earn OXT (was 0.30 hardcoded)
    "oxt_hostility_gate": 0.20,  # max hostility to earn OXT (was 0.20 hardcoded)
    "cort_threat_increment": 0.022,  # CORT gain when hostility > threshold
    "cort_hostility_threshold": 0.35,  # hostility score that triggers CORT build (text-based, not prosody)
    "sht_reward_increment": 0.003,  # 5HT gain per rewarding interaction
    "sht_reward_sentiment_min": 0.40,  # min sentiment to earn 5HT
    "sht_hostility_drain": 0.004,  # 5HT drain per hostile exchange (enables dysphoric state)
    # OXT ↔ CORT antagonism
    "oxt_cort_buffer_rate": 0.020,  # OXT level × this = CORT drain per turn (~60% offset at OXT=0.5)
    "oxt_cort_buffer_threshold": 0.40,  # OXT must exceed this to buffer CORT
    # Hormonal → DA modulation (effective DA = raw DA + offset)
    "sht_da_floor_lift": 0.12,  # 5HT × this added to effective DA
    "oxt_da_lift": 0.05,  # OXT × this added to effective DA
    "cort_da_suppress": 0.08,  # CORT × this subtracted from effective DA
    # Hormonal → GABA modulation (effective GABA = raw GABA × scale)
    "cort_gaba_amplify": 0.30,  # CORT × this amplifies GABA scale
    "oxt_gaba_buffer": 0.15,  # OXT × this reduces GABA scale
    # Hormonal color thresholds (when to override base emotion)
    "hormonal_oxt_connected_threshold": 0.60,  # OXT > this + positive base → connected
    "hormonal_cort_withdrawn_threshold": 0.45,  # CORT > this → withdrawn/guarded (~17 hostile turns)
    "hormonal_oxt_guarded_threshold": 0.35,
    "hormonal_sht_dysphoric_threshold": 0.25,
    # ── Section 13: Time-weighted decay ──────────────────────────────────────
    # decay_turn() measures wall-clock seconds since the last turn and applies
    # rate ** (elapsed / reference_interval_s) instead of a fixed rate ** 1.
    # This makes emotional state decay proportional to real time, not message count:
    # slow conversations decay faster between turns; rapid exchanges stay stickier.
    "decay_reference_interval_s": 60.0,  # elapsed seconds that equals 1 decay turn
    "decay_min_turns": 0.25,  # floor — even instant replies apply some decay
    "decay_max_turns": 10.0,  # cap — silence > 10 min treated as 10 turns
    # ── Section 14: Norepinephrine (NE) ──────────────────────────────────────
    # NE = focused alertness signal; inverted-U curve (optimal 0.20–0.55)
    # Per-turn update weights (applied before er_scale)
    "ne_salience_weight": 0.07,  # NE gain per unit salience (alert to what matters)
    "ne_surprise_weight": 0.05,  # NE gain per unit surprise (re-orient fast)
    "ne_hostility_weight": 0.10,  # NE gain per unit hostility (threat → vigilance)
    # Prosody / dynamics contributions
    "ne_prosody_stressed": 0.06,  # NE gain when tone_label == "stressed"
    "ne_rush_increment": 0.05,  # NE gain when pace == "rushed"
    # Graded prosody/pace release: scale each per-category neuromod increment by
    # the acoustic signal's strength (tone_strength / pace_strength ∈ [0,1]) so a
    # near-threshold voice nudges and a strong one spikes, instead of a fixed jump
    # on the label. The scale maps strength → [min, max] linearly, centered on 1.0
    # at strength=0.5 so the existing fixed increments are the mid-strength value.
    # 1 = graded; 0 = legacy fixed-increment path (rollback). Voice input only.
    "prosody_graded_release": 1,
    "prosody_graded_min_scale": 0.5,  # multiplier at strength=0 (near threshold)
    "prosody_graded_max_scale": 1.5,  # multiplier at strength=1 (very strong)
    # Inverted-U thresholds (above high → vigilant; above scatter → degraded focus)
    "ne_high_threshold": 0.55,  # NE > this → heightened vigilance modifier
    "ne_scatter_threshold": 0.82,  # NE > this → attention narrowed, scattered
    # ── Section 15: Anandamide / AEA (endocannabinoid) ───────────────────────
    # AEA = homeostatic buffer; medium-speed (decay 0.90 vs. neuromod 0.85 / hormone 0.97+)
    # Rises automatically when Glu + NE arousal sum exceeds threshold
    "aea_arousal_threshold": 0.65,  # Glu + NE sum that triggers homeostatic AEA rise
    "aea_arousal_increment": 0.018,  # AEA gain per turn when arousal is high (~15 turns to effect)
    "aea_positive_increment": 0.005,  # AEA gain per warm/positive turn (social afterglow)
    "aea_cort_drain": 0.004,  # AEA drain per turn under sustained stress (CORT antagonism)
    # AEA → effective NE / Glu suppression (applied above resting baseline of 0.30)
    "aea_ne_suppression": 0.50,  # excess AEA × this reduces effective NE scale
    "aea_glu_suppression": 0.35,  # excess AEA × this reduces effective Glu scale
    # AEA → DA lift ("afterglow": elevated AEA adds mild positive valence)
    "aea_da_lift": 0.04,  # AEA × this added to effective DA
    # AEA → DA suppress (plateau effect: sustained high AEA caps excitement)
    "aea_da_suppress_threshold": 0.50,  # AEA above this starts capping effective DA
    "aea_da_suppress": 0.15,  # (AEA - threshold) × this subtracted from effective DA
    # AEA color threshold (when elevated AEA buffers a stress state → "eased")
    "aea_eased_threshold": 0.58,  # AEA > this + stress base emotion → eased
    # Satiation → GABA habituation trickle (familiarity/boredom inhibitory signal)
    "satiation_gaba_threshold": 0.60,  # satiation state above this triggers GABA trickle
    "satiation_gaba_increment": 0.015,  # GABA added per turn when saturated with familiar topics
    # ── Section: Switch Modulation ────────────────────────────────────────────
    # Single gain that scales every SwitchNeuron's modulator coefficient.
    # 0.0 = chemistry has no effect on switches (pure deterministic gating).
    # 1.0 = profiles fire at their declared strength (default).
    # >1.0 = amplified chemistry response; <1.0 = damped.
    "modulation_gain": 1.00,
    # ── Section: Sleep Consolidation ─────────────────────────────────────────
    # Periodic in-process consolidation lets the brain learn (extract facts,
    # update self-model, run Hebbian, observe personality + mood-response
    # patterns) without ever exiting the process. The brainstem wakes on
    # `sleep_check_interval_s`, and runs a pass if EITHER the user has been
    # idle ≥ `sleep_idle_threshold_s` OR ≥ `sleep_hard_cap_s` has elapsed
    # since the last pass, and there are ≥ `sleep_min_turns` buffered.
    # End-of-session consolidation always runs as a safety net.
    "sleep_periodic_enabled": 1,  # 1 = on, 0 = off
    "sleep_check_interval_s": 1800.0,  # 30 min — how often to check
    "sleep_idle_threshold_s": 7200.0,  # 2 h  — fire after this much user idle
    "sleep_hard_cap_s": 21600.0,  # 6 h  — fire regardless of idle
    "sleep_min_turns": 5,  # don't bother with tiny batches
    "session_trace_cap": 300,  # buffered-turn ceiling that forces a consolidation pass (0 = off)
    # Cross-learning chain (private rumination → de-id gate → hypothesis store) at
    # sleep, plus established-principle injection into the turn context. Off until
    # the chain has been observed end-to-end on a real consolidation.
    "cross_learning": 1,  # graduated from experimental 2026-06 — production default
    # Learning stories — narrate what this session's Hebbian pass changed
    # (sleep.learning_story_pass, runs after the Hebbian pass in the same persona
    # binding). 1 = on, 0 = skip the narration; the weight updates land either way.
    "learning_narrator": 1,
    "resting_mood_consolidation_alpha": 0.3,  # blend rate of the day's aggregate mood into resting disposition at consolidation (session_loops.py)
    # ── Section: Motor Cortex / Autonomous Tasks ─────────────────────────────
    # ralph_max_total_attempts: hard ceiling on total tool dispatches across ALL
    # stories + retries in a single internal job. Prevents runaway loops
    # regardless of story count or per-story retry budget.
    # Can also be overridden per-session via BRAIN_RALPH_MAX_ATTEMPTS env var.
    "ralph_max_total_attempts": 12,
    # Motor-cortex job rate limits (cost guard now that planning runs on cloud).
    # Cloud spend is also bounded by bg_cloud_token_rate + cloud_daily_usd_budget;
    # these additionally cap how MANY autonomous jobs can run.
    "motor_max_concurrent_jobs": 1,  # only one autonomous job at a time
    "motor_max_jobs_per_window": 10,  # ≤ this many job starts per window
    "motor_job_window_s": 3600.0,  # rolling window = 1 hour
    "motor_max_jobs_per_session": 30,  # absolute ceiling per process lifetime
    "job_store_max_jobs": 100,  # JobStore: max completed-job files retained (oldest trimmed first)
    "job_store_max_mb": 100,  # JobStore: max total size in MB of retained job files
    # ── Engine API audio quotas (per-partner cost guard) ─────────────────────
    # STT/TTS hit paid third parties (Deepgram/ElevenLabs), so a partner key's
    # audio usage is metered the way those services bill: TTS by characters
    # synthesised, STT by input audio-seconds. Each is a rolling-window ceiling
    # per partner_id. 0 = unlimited (default → inert until an operator sets a
    # cap). Owner-key calls are never metered. See brain/api/audio_quota.py.
    "audio_tts_chars_per_window": 0,  # 0 = unlimited
    "audio_stt_seconds_per_window": 0,  # 0 = unlimited
    "audio_quota_window_s": 86400.0,  # rolling window = 1 day
    # ── Section: Cloud call timeouts (anti-hang) ─────────────────────────────
    # Bound every Anthropic call so a stalled connection can't freeze a motor
    # job at the strategic-plan step. read timeout bounds long generations;
    # connect timeout catches dead sockets fast; retries are bounded.
    # ── Provider selection ───────────────────────────────────────────────────
    # cloud_provider: which cloud serves COGNITION (frontal/temporal/DMN-deep…).
    # "anthropic" (default) | "openai" — openai reroutes Claude-bound calls to
    # OPENAI_MODEL/OPENAI_MODEL_MINI (motor cluster stays on Anthropic: its
    # tool-use loop is Anthropic-shaped). OPENAI_BASE_URL points the same client
    # at Groq/Mistral/DeepSeek/Together.
    "cloud_provider": "anthropic",
    # Vertex AI (additive, default OFF). cloud_provider="vertex" serves the reasoning
    # path via Google-hosted Claude (Vertex Model Garden) when enable_vertex=1 — the
    # SAME Claude models, just billed/authed through Google.
    # Gemini-on-Vertex is reachable via the vertex-gemini-* model keys. Auth is ADC
    # (service account / workload identity / `gcloud auth application-default login`),
    # NOT an API key — see api_key_google_vertex_sa for the hosted SA-JSON path.
    "enable_vertex": 0,
    "vertex_project": "",  # GCP project id (falls back to GOOGLE_CLOUD_PROJECT env)
    "vertex_location": "us-central1",  # region for Gemini-on-Vertex
    "vertex_claude_location": "us-east5",  # region for Claude-on-Vertex
    # tts_provider: "elevenlabs" (default) | "openai" (gpt-4o-mini-tts — emotion
    # rides the instructions parameter instead of VoiceSettings).
    "tts_provider": "elevenlabs",
    "openai_tts_model": "gpt-4o-mini-tts",
    "openai_tts_voice": "alloy",
    # tts_provider: also "google" (Cloud Text-to-Speech, Chirp 3 HD). Mood maps to
    # speakingRate. google_tts_voice picks the Chirp voice; needs GOOGLE_API_KEY.
    "google_tts_voice": "en-US-Chirp3-HD-Charon",
    # stt_provider: "deepgram" (default) | "openai" (Realtime transcription —
    # NOTE: no per-word diarization, so multi-speaker attribution degrades).
    "stt_provider": "deepgram",
    "openai_stt_model": "gpt-4o-transcribe",
    "anthropic_timeout_s": 300.0,
    "anthropic_connect_timeout_s": 10.0,
    "anthropic_max_retries": 2,
    # Hard ceiling on a single structured (tool-use) call, enforced via
    # asyncio.wait_for on top of the client timeout (belt-and-suspenders).
    "structured_call_timeout_s": 150.0,
    # ── Section 16: Resource Policy ───────────────────────────────────────────
    # Controls how much compute the brain is allowed to use for autonomous /
    # background work (self-initiated tasks, metacognition, DMN exploration).
    #
    # LOCAL (Ollama) — free to use liberally; semaphore prevents device overload.
    # CLOUD (Anthropic / Gemini) — allowed for background work when genuinely
    # more efficient, but budgeted to avoid accidental large bills.
    #
    # bg_cloud_token_rate: token bucket refill rate — how many non-cache input+output
    #   tokens background cloud calls may consume per hour on average. The bucket
    #   starts full (one hour's worth) and refills continuously at this rate, capped
    #   at one hour's allowance so idle time doesn't stack indefinitely. A single job
    #   can exceed the hourly rate (borrowing from the next hour); the bucket goes
    #   negative and background calls fall back to local until it refills.
    #   100k/hr ≈ ~$0.08–0.40/hr at haiku/flash-lite prices.
    "bg_cloud_token_rate": 100_000,
    # bg_cloud_max_tokens_per_call: output token cap applied to every background
    #   cloud call. Keeps individual calls short and cost-predictable.
    "bg_cloud_max_tokens_per_call": 512,
    # bg_cloud_timeout_s: hard timeout on each background cloud API call.
    #   Falls back to local on timeout so background work never hangs.
    "bg_cloud_timeout_s": 20.0,
    # local_max_concurrent: max simultaneous Ollama inference calls.
    #   Prevents saturating CPU/GPU during multi-cell background work.
    "local_max_concurrent": 3,
    # cloud_max_concurrent: max simultaneous Anthropic API calls from interactive turns.
    #   Prevents burst fan-outs (parallel drafters, critic, skill selector) from
    #   all firing at once and tripping RPM rate limits on the Standard tier.
    #   3 keeps well under the 50 RPM ceiling while adding only ~1-2s to turns
    #   that fan out 6+ calls.
    "cloud_max_concurrent": 3,
    # bg_cloud_max_concurrent: max simultaneous Anthropic calls from background tasks
    #   (DMN ticks, metacognition, motor background). Separate pool from cloud_max_concurrent
    #   so background work can never starve interactive-turn cells waiting for a slot.
    "bg_cloud_max_concurrent": 2,
    # cloud_daily_usd_budget: per-ORG hard ceiling on total cloud spend per calendar
    #   day (UTC) — one daily total across every cloud call this brain makes, NOT
    #   per-agent (a bound agent can only narrow it further, never raise it). 0 = no
    #   cap. The running total is persisted to second_brain/cloud_usage.json so it
    #   survives restarts. When hit, a FULL brain falls back to local for the rest of
    #   the day; a LITE brain (no local pod) raises CloudBudgetExceeded → HTTP 402.
    #   Default $20.00 — generous for normal interactive use; tighten per-org if needed.
    "cloud_daily_usd_budget": 20.0,
    # ── Section: Autonomy (brain.autonomy — cloud-only autonomous work) ─────────
    # autonomous_soft_usd / autonomous_hard_usd: the AUTONOMOUS-ONLY daily spend pool,
    #   separate from interactive (tracked as usd_autonomous in cloud_usage.json). At the
    #   soft cap, new autonomous jobs PAUSE and record one owner "continue spending"
    #   approval; at the hard cap they STOP for the UTC day. 0 disables that tier.
    "autonomous_soft_usd": 30.0,
    "autonomous_hard_usd": 50.0,
    # autonomy_approve_external_only: 1 = approvals are reserved for EXTERNAL side-effects
    #   (comms out) + irreversible destructive actions; internal reads/sandboxed writes
    #   run unattended. 0 = the older, broader gate (writes/bash/edit also ask).
    "autonomy_approve_external_only": 1,
    # cloud_web_search_max: per-cloud-task budget of web searches, injected into every
    #   composed task ("at most N targeted searches"). Web search is the token-burn
    #   fallback — connectors first; broad multi-query sweeps were eating the budget.
    "cloud_web_search_max": 3,
    # bg_cloud_timeout_trip: consecutive background cloud-call timeouts before the gate
    #   parks autonomous work (CLOUD_UNREACHABLE) for a cooldown instead of hammering.
    "bg_cloud_timeout_trip": 3,
    # cloud_unreachable_cooldown_s: how long autonomous work stays parked after tripping.
    "cloud_unreachable_cooldown_s": 120.0,
    # semaphore_acquire_timeout_s: bound on acquiring the interactive cloud semaphore so a
    #   saturated pool can't pin a structured call indefinitely.
    "semaphore_acquire_timeout_s": 30.0,
    # job_defer_backoff_base_s: base wall-clock backoff for a deferred (requeued) job;
    #   compounds on repeated defers so a dead cloud parks the queue rather than spinning.
    "job_defer_backoff_base_s": 30.0,
    # ── Section: Motor chunking + API pacing ────────────────────────────────────
    # motor_query_page_size: default records per list/search step, so a job requests one
    #   bounded page at a time (small, fast) and pages via the "[... offset=X]" signal.
    "motor_query_page_size": 50,
    # motor_pacing_enabled / motor_pacing_min_interval_s: minimum spacing between calls to
    #   the same EXTERNAL endpoint (fetch_url / world_* / cloud_action) so a multi-page
    #   sweep doesn't trip provider rate limits. Internal filesystem tools are unthrottled.
    "motor_pacing_enabled": 1,
    "motor_pacing_min_interval_s": 0.5,
    # motor_http_retries: attempts for a fetch_url GET on 429/503 (exponential backoff).
    "motor_http_retries": 3,
    # ── Section: RunPod ───────────────────────────────────────────────────────
    # Overrides the RUNPOD_HOST / RUNPOD_MODEL env vars at runtime — no restart
    # needed. Empty string = fall back to env var.
    "runpod_host": "",
    "runpod_model": "",
    # runpod_pod_ready: 1 iff a local RunPod GPU pod has been confirmed resident
    # and its real host published this process. Distinct from runpod_host, which
    # stays a plain routing override (empty = fall back to env var/Ollama) and
    # can't by itself distinguish "no override" from "pod genuinely up" — this is
    # the dedicated liveness signal for frontal._local_available()/downshift.
    "runpod_pod_ready": 0,
    # runpod_pod_ready_at: wall-clock stamp of the last time the pod's residency
    # was confirmed (set with runpod_pod_ready=1 and re-stamped by the manager's
    # periodic refresh loops). frontal._local_available() treats the ready flag
    # as STALE — i.e. false — when this stamp is older than runpod_pod_ready_ttl_s,
    # so a pod that dies between refreshes stops receiving downshifted drafts
    # instead of timing out against a dead host until the next refresh notices.
    "runpod_pod_ready_at": 0.0,
    # TTL for the stamp above. Comfortably above both refresh cadences that
    # re-stamp it (consumer host-file poll ~30s, owner liveness watcher 120s):
    # ≈2.5× the slower one. 0 disables the staleness check (flag-only behavior).
    "runpod_pod_ready_ttl_s": 300.0,
    # max_runpod_hours: watchdog stops the pod if the brain hasn't been seen
    # alive for this many hours. Resets continuously while the brain is running.
    # Acts as a backstop against runaway costs after a crash/force-kill.
    "max_runpod_hours": 8.0,
    # runpod_stream_retries: extra attempts (beyond the first) for a RunPod /api/chat
    # stream before falling back to a non-streaming POST. Each retry drops the pooled
    # httpx client first, so a stale keep-alive socket left by a pod restart is
    # replaced with a fresh connection — this is what makes inference reconnect after
    # a restart instead of returning empty. 0 = single attempt + POST fallback.
    "runpod_stream_retries": 2,
    # ── Section: Chemistry model & Personas ──────────────────────────────────
    # chem_decay_model controls how neuromodulator/hormone levels relax each turn:
    #   "baseline" — homeostatic setpoint; gradual two-way relaxation toward the
    #                baseline (level = baseline + (level-baseline)*rate). A depleted
    #                channel recovers GRADUALLY, honouring the slow-hormone design.
    #   "floor"    — legacy clamp (level = max(baseline, level*rate)). A channel
    #                below baseline snaps back up in one turn. Kept for regression
    #                diffing and instant rollback; "floor" reproduces old behaviour.
    "chem_decay_model": "baseline",
    # Persona resting baselines — the setpoint each channel relaxes toward (the
    # sustained trait). Defaults equal the historical bus floors, so a brain with
    # no persona set keeps its current resting point; only the decay CURVE changes.
    "chem_baseline_DA": 0.30,
    "chem_baseline_ACh": 0.10,
    "chem_baseline_GABA": 0.02,
    "chem_baseline_Glu": 0.15,
    "chem_baseline_NE": 0.15,
    "chem_baseline_5HT": 0.20,
    "chem_baseline_CORT": 0.02,
    "chem_baseline_OXT": 0.15,
    "chem_baseline_AEA": 0.10,
    # Persona starting levels — the value at boot. Defaults equal the historical
    # warm-start levels, so the no-persona brain starts exactly where it used to.
    # Personas write init == baseline (start at rest).
    "chem_init_DA": 0.50,
    "chem_init_ACh": 0.20,
    "chem_init_GABA": 0.05,
    "chem_init_Glu": 0.30,
    "chem_init_NE": 0.25,
    "chem_init_5HT": 0.50,
    "chem_init_CORT": 0.05,
    "chem_init_OXT": 0.30,
    "chem_init_AEA": 0.30,
    # Persona identity — written when a persona is initialized. Empty = neutral.
    # persona_name also routes per-persona learned state into
    # second_brain/personas/<slug>/ (see brain/run.py) and tags every eval row.
    "persona_name": "",
    "persona_born": "",
    # User-created personas + each persona's saved knob setup. JSON object keyed
    # by persona display name: {name: {custom: bool, tag, note, chem: {...},
    # vals: {settings-key: value}}}. The Settings page reads this to populate the
    # persona rail (built-ins + custom) and to re-apply a persona's full dial/
    # manual configuration when it's selected. Stored as a JSON string so it
    # round-trips through settings.json untouched. Chemistry still also persists
    # per-persona in second_brain/personas/<slug>/chemistry.json (the brain's
    # source of truth); this mirror is what the UI restores from.
    "persona_store": "",
    # Active persona's ElevenLabs voice ID. Applied at boot via pns.set_voice_id().
    # Empty = use ELEVENLABS_VOICE_ID env var or built-in default.
    "persona_voice_id": "",
    # Per-persona saved voice IDs. A per-tenant settings.json override still wins
    # (see Settings._load), but these canonical defaults are the floor so a persona
    # always resolves to its designed voice — even on a tenant whose volume
    # settings.json predates the key. Empty defaults silently fell through to the
    # ELEVENLABS hardcoded default (Rachel/female), which is the analyst-voice bug.
    "persona_voice_the_visionary": "4e32WqNVWRquDa1OcRYZ",
    "persona_voice_the_empath": "pqHfZKP75CvOlQylNhV4",
    "persona_voice_the_analyst": "c6SfcYrb2t09NHXiT80T",  # Jarnathan — warm, confident (male)
    "persona_voice_the_poet": "LEnmbrrxYsUYS7vsRRwD",
    "persona_voice_the_sage": "nPczCjzI2devNBz1zQrb",
    # ── Section: Channel Calibration (text vs. voice) ─────────────────────────
    # Text and voice are fundamentally different communication channels.
    # These weights scale how temporal features feed into neuromod updates
    # depending on which channel the input came from.
    "enable_channel_calibration": 1,  # 1 = on, 0 = off
    "enable_text_paralinguistics": 1,  # 1 = extract emoji/laughter/warmth markers for text
    # Text channel calibration weights
    "text_hostility_weight": 0.65,  # short/direct text ≠ hostile; discount this signal
    "text_sentiment_weight": 1.10,  # word-level sentiment is primary; up-weight slightly
    "text_length_signal_weight": 0.20,  # message brevity is normal; near-zero as signal
    # Text paralinguistic → neuromod contribution weights
    "text_para_laughter_DA": 0.10,  # lol/😂 → DA boost
    "text_para_warmth_DA": 0.07,  # :)/❤️ → DA boost
    "text_para_negativity_GABA": 0.08,  # :(/ 😡 → GABA
    "text_para_excitement_Glu": 0.07,  # omg/🔥 → Glu
    "text_para_excitement_NE": 0.04,  # omg/🔥 → NE
    # Voice laughter detection (transcript markers + DSP heuristic + classifier all
    # feed the same levity-scaled DA path as text_para_laughter_DA, composed via max)
    "laughter_dsp_threshold": 0.5,  # min audio_dsp.laughter_likelihood before DA release
    "vocal_events": 1,  # PANNs vocal-event classifier — production default (graduated 2026-06)
    # ── Section: Relationship Stage Progression ───────────────────────────────
    "enable_relationship_stage_progression": 1,  # 1 = auto-update familiarity tier at sleep
    "familiarity_acquainted_min_sessions": 3,  # sessions needed to reach acquainted
    "familiarity_acquainted_min_score": 0,  # affection score must be at least this
    "familiarity_close_min_sessions": 10,  # sessions needed to reach close
    "familiarity_close_min_score": 15,  # affection score must be at least this
    # ── Section: Bond model (relational decay + reunion recovery) ─────────────
    # Two quantities per speaker: affection (live warmth, injected into prompts)
    # and bond (latent closeness high-water mark). Closeness creates a bond that
    # decays slowly and recovers fast; a thin acquaintance fades to nothing over
    # the same gap. Half-lives grow EXPONENTIALLY with bond so the closer the
    # prior relationship the smaller the decline.
    "enable_bond_model": 1,
    "bond_aff_halflife_base_days": 25.0,  # affection half-life at bond=0 (days)
    "bond_bond_halflife_base_days": 90.0,  # bond half-life at bond=0 (days, much slower)
    "bond_halflife_scale": 23.0,  # exp denominator: HL = base * exp(bond/scale)
    "bond_reunion_gain": 8.0,  # positive-delta multiplier slope on reengagement
    "familiarity_close_bond": 35.0,  # bond ≥ this → "close"
    "familiarity_acquainted_bond": 12.0,  # bond ≥ this → "acquainted", else "new"
    # ── Section: Self-Disclosure Policy ──────────────────────────────────────
    "enable_self_disclosure_policy": 1,  # 1 = inject disclosure opportunity into drafter
    "self_disclosure_cooldown_turns": 8,  # min turns between disclosure prompts
    "self_disclosure_min_affection": 5,  # affection score floor for voice disclosure
    "self_disclosure_text_min_affection": 20,  # affection score floor for text disclosure
    # ── Section: Performed Emotion Gate ──────────────────────────────────────
    # Deliberate/performed emotion ([mood:X] markup, set_mood) is a playful,
    # humor-leaning intimacy device. Gate WHEN the drafter is encouraged to use
    # it by relationship depth + the user's mood. Off → preserve the old
    # always-offered behaviour.
    "enable_performed_emotion_gate": 1,
    "performed_emotion_min_affection": 10,  # general warmth floor (neutral mood)
    "performed_emotion_new_min_affection": 15,  # higher floor when familiarity is still "new"
    "performed_emotion_cheerup_min_affection": 20,  # bar to attempt cheer-up / tension-break when user is down
    # ── Section: Style Synchrony ─────────────────────────────────────────────
    "enable_style_synchrony": 1,  # 1 = track and inject user style register
    "style_ema_alpha_voice": 0.25,  # EMA weight for voice style (per turn)
    "style_ema_alpha_text": 0.20,  # EMA weight for text style (slower — more variable)
    "style_max_shift": 0.12,  # max drift toward user per dimension per session
    "style_entity_formality_baseline": 0.25,  # entity's natural formality (0=casual, 1=formal)
    "style_entity_verbosity_baseline": 0.45,  # entity's natural verbosity (0=terse, 1=expansive)
    "style_min_turns_for_injection": 3,  # turns tracked before injecting style note
    "register_ema_alpha": 0.30,  # EMA weight for the rolling per-speaker register profile
    # ── Section: Graded plasticity (correctness fix — NOT colony-gated) ───────
    # The legacy all-or-nothing `defuse_path` skip (gaba_skip_threshold_high) is
    # biologically wrong: real plasticity is graded and neuromodulator-scaled
    # (three-factor learning rules), and aversive/stress states follow an
    # inverted-U (moderate arousal ENHANCES encoding; only extreme stress
    # impairs). When on, a per-turn plasticity factor keyed to arousal/emotional
    # intensity (not valence sign) multiplies the Hebbian delta, and the binary
    # skip becomes a graded high-stress dampener. Ships on its own flag,
    # independent of colony_features. Default 0 → flip to 1 after eval validates.
    "graded_plasticity": 1,  # 1 = graded per-turn plasticity (Yerkes-Dodson); 0 = legacy binary skip
    "plasticity_turn_min": 0.40,  # floor of the per-turn plasticity multiplier
    "plasticity_turn_max": 1.30,  # ceiling of the per-turn plasticity multiplier
    "plasticity_arousal_weight": 0.50,  # ACh+NE+surprise+|DA swing| → plasticity gain
    "plasticity_intensity_weight": 0.40,  # |valence| (either sign) → plasticity gain
    "plasticity_stress_knee": 0.70,  # CORT/GABA above this = inverted-U descending limb
    "plasticity_stress_damp": 0.60,  # max multiplicative dampening at extreme stress
    # ── Evidence gates (bounded evidence accumulation; drift-diffusion) ───────
    # Per-decision accumulators for inferences no single turn can make (e.g.
    # habituation, "the user is avoiding X"). NOT spiking neurons — sequential-
    # sampling decision-making, own verdict (evidence_gate.py; docs/SYSTEMS.md).
    # 0 = every gate is a strict no-op and callers fall through to prior behaviour.
    "evidence_gates": 1,  # master flag (graduated 2026-07-17 after neutral-when-off proof)
    # Learning (drift-cue weights): plasticity is weighted toward EXTERNAL/grounded
    # confirmation so a gate cannot farm its own appraiser (premise-audit fix).
    "evidence_cue_lr": 0.05,  # cue-weight learning rate on a resolved prediction
    "evidence_cue_w_min": 0.10,  # floor of a learned drift-cue weight
    "evidence_cue_w_max": 3.00,  # ceiling of a learned drift-cue weight
    "evidence_external_weight": 1.00,  # plasticity scale for an external (grounded) confirmation
    "evidence_self_weight": 0.35,  # plasticity scale for a self/critic-graded confirmation (discounted)
    # Satiation as the first live EvidenceGate (hypothalamus habituation). Long
    # half-life: near-identical to the StatefulSwitch during active back-and-forth,
    # but idle time now RELAXES satiation (the fix for the dead-tick() gap the RFC
    # names) instead of it only relaxing when a salient turn arrives. Only in effect
    # when evidence_gates=1; flag off keeps the exact StatefulSwitch path.
    "satiation_half_life_s": 1800.0,  # ~30 min: idle-time relaxation of habituation
    # User-avoidance gate (first LEARNING EvidenceGate; avoidance_gate.py). Accumulates
    # per-entity avoidance evidence from ACTIVE dodge signals only — a topic the agent
    # surfaced that the user didn't pick up, an abrupt subject change off a live thread,
    # discomfort riding a dodge; mere staleness (the user simply moved on) contributes
    # nothing. Learns its cue weights from external (behavioural) confirmation. LIVE BY
    # DEFAULT (ships-on policy: flags are kill switches, not enable switches): runs
    # whenever evidence_gates=1, and avoidance_gate below is the kill switch for the
    # steering + chemistry side (0 = shadow: keep learning on real data, no influence).
    "avoidance_gate": 1,  # 1 = armed avoidance biases the DMN deflect judge + moves chemistry
    "avoidance_arm_threshold": 1.5,  # accumulated evidence needed to believe "avoiding X"
    "avoidance_release_ratio": 0.5,  # hysteresis: release below arm * this
    "avoidance_half_life_s": 900.0,  # ~15 min leak: an unfed suspicion fades
    "avoidance_cap": 5.0,  # max accumulated avoidance evidence per entity
    "avoidance_stale_turns": 2,  # an entity unseen this many turns becomes a candidate
    "avoidance_informativeness": 0.6,  # informativeness passed to prediction_reward on resolve
    "avoidance_max_armed_s": 86400.0,  # escape hatch: a belief armed this long expires (clear the slate)
    "avoidance_evict_floor": 0.05,  # decayed accumulator slices below this are deleted from the store
    # ── Section: Colony / non-brain (superorganism) capabilities ─────────────
    # Single master toggle for the bio-inspired colony layer (Phases 2–8 of the
    # colony-features plan). 0 = every colony behaviour is a strict no-op and the
    # brain behaves exactly as before. Per-feature tuning knobs below take effect
    # only when this is on. The three feedback loops (concentration, recruitment,
    # chemistry self-feedback) are also independently observable in the decisions
    # log so they can be validated in increasing-risk order.
    "colony_features": 1,  # colony layer — production default (graduated 2026-06)
    # Phase 2 — topic concentration / quorum / silence (threat channel first)
    "colony_conc_half_life_s": 45.0,  # exponential half-life of topic concentration
    "colony_conc_cap": 10.0,  # max accumulated concentration (chatty-topic bound)
    "colony_arm_threshold": 1.00,  # concentration must cross this to become ARMED
    "colony_quorum_threshold": 1.50,  # ARMED + concentration ≥ this → quorum
    "colony_silence_floor": 0.15,  # ARMED concentration decays below this → QUIET
    "colony_silence_disarm_s": 600.0,  # zero-dwell this long → disarm back to UNARMED
    # Phase 3 — releaser + primer in one message
    "colony_primer_gain": 0.30,  # scales Message.primer nudges into hormonal channels
    # Phase 4/7 — recruitment amplification + mobilization cascade
    "colony_recruit_gain": 0.40,  # scales need_level → recruitment level
    # Phase 5 — threshold diversity (DEPRECATED — see colony-features-ii / N3).
    # spread_threshold is left inert; variance without real specialization is
    # noise (Lynch et al. 2024). Do NOT wire it in. Kept only for the dormant helper.
    "colony_threshold_spread": 0.08,  # ± bound (UNUSED — spread_threshold is deprecated)
    # Phase 8 — aggregate-state neuromodulation feedback (highest-risk loop)
    "colony_state_feedback_gain": 0.02,  # tiny gain on prior-turn aggregate → neuromod nudges
    "colony_state_feedback_clamp": 0.05,  # max total feedback contribution per channel per turn
    # ── Colony Layer II — ant-colony lessons (all under colony_features) ──────
    # C3 — recruitment satisfaction/stop signal: a met need actively lowers
    # recruitment (composite start+stop thresholds) instead of only passive decay.
    "colony_satisfy_rate": 0.50,  # fraction of recruitment removed per unit satisfaction
    "colony_satisfy_critic_floor": 0.6,  # critic score above which a commit counts as "need met"
    # C4 — rate-of-change in quorum: a fast-rising signal trips quorum via slope
    # even before the level threshold is reached.
    "colony_quorum_slope_threshold": 0.20,  # concentration rise per second that trips quorum
    # N2 — softmax multi-need recruitment allocation across competing clusters.
    "colony_recruit_budget": 1.0,  # total recruitment budget shared per turn
    "colony_recruit_softmax_temp": 0.5,  # Boltzmann temperature (lower = sharper allocation)
    # N3 — sensory-filter specialization: per-persona input-sensitivity gain over
    # feature categories (the real division-of-labor axis; supersedes Phase 5).
    "colony_sensory_filter": 0,  # 1 = apply per-persona sensitivity gains; 0 = off
    "colony_sensory_gain_span": 0.30,  # ± span of the per-(persona,category) sensitivity gain
    # N1 — live trail reinforcement. Ran shadow-only (log, don't apply) from
    # graduation until 2026-07-01; flipped live per russ after the premise audit
    # confirmed the write path + read path are sound.
    "colony_trail_apply": 1,  # 1 = apply overlay to live weights; 0 = shadow (log only)
    "colony_trail_gain": 0.05,  # per-turn trail bump scale (× outcome)
    "colony_trail_clamp": 0.50,  # max |overlay| added to any edge's persisted weight
    "colony_trail_half_life_s": 120.0,  # trail overlay decay half-life within a session
    # ── Fragment wiring — Tier 1 structural plasticity (learned-attached fragments) ──
    # Curated, screened skills learned-attached onto host cells as per-persona wiring edges
    # `fragment.<skill_id> → host`. Drafters EXPLORE varied fragments; the critic + outcome
    # reward the winner (contrastive); proven attachments consolidate and can DOWNSHIFT a
    # drafter to the free local RunPod model. Ships LIVE (master ON); BRAIN_WIRING_FROZEN
    # disables everything. Safety = curated parts + fenced injection + non-safety hosts only.
    "fragment_wiring": 1,  # master gate for attachment learning + injection (0 = off)
    "fragment_explore_rate": 0.20,  # P(a given drafter explores a non-established fragment)
    "fragment_explore_max_drafters": 2,  # cap exploring drafters/turn (keeps a cloud/baseline floor)
    "fragment_inject_threshold": 1.30,  # attachment weight ≥ this → injected into its host
    "fragment_max_per_host": 2,  # max fragments injected into any one host per turn
    "fragment_prune_floor": 1.05,  # fragment edges ≤ this are pruned (rest=1.0)
    "fragment_forget": 0.05,  # per-sleep decay of fragment edges toward rest (use-it-or-lose-it)
    "fragment_gain": 0.20,  # reinforcement step = outcome × gain (one good win clears the floor)
    "fragment_explore_penalty": 0.02,  # decrement to an existing attachment explored on a loser
    "fragment_downshift": 1,  # 1 = route proven-attachment drafters to local RunPod (0 = off)
    "fragment_downshift_threshold": 2.20,  # attachment weight ≥ this → eligible to downshift
    "fragment_downshift_cloud_floor": 2,  # min drafters kept on cloud even if all are proven
    "fragment_downshift_model": "runpod",  # local model key for a downshifted drafter
    # ── Node creation — Tier 2 structural plasticity (recruit new nodes + self-authoring) ──
    # Cap 1 (recruitment): a persona recruits a dormant reserve drafter into its active set when
    # a host accumulates a stable cluster of PROVEN fragments; the new node's identity = those
    # fragments, injected via the Tier-1 seam. Cap 2 (self-authoring): a gated sleep pass drafts a
    # candidate specialization, admits it through the SAME screener as untrusted skills (auto-live
    # only if screener-clean; else the owner review queue), feeding the fragment pool. Two gates:
    # screener admits authored content; reward promotes any fragment into a node.
    "node_recruitment": 1,  # master gate for reserve-node recruitment (0 = off)
    "node_reserve_pool": 3,  # K dormant reserve drafter slots (F.. up to fragment_pool ceiling)
    "node_promote_threshold": 2.20,  # fragment weight ≥ this counts as "proven" for recruitment
    "node_promote_min_cluster": 2,  # min proven fragments on a host to crystallize into a node
    # (a recruited reserve is DEMOTED when it has no fragment ≥ fragment_inject_threshold left —
    #  it lost its specialization; that reuses the inject threshold, no separate flag.)
    "node_self_authoring": 1,  # master gate for the self-authoring sleep pass (0 = off)
    "node_author_min_traces": 20,  # min new traces in a session before authoring may run
    "node_author_min_hours": 24.0,  # min hours since the last authored proposal for a persona
    "node_author_max_per_persona": 5,  # cap on total self-authored skills per persona
    # Alternative Tier-2 trigger (GWT): sustained Global-Workspace ignition marks a
    # plasticity window — a persona whose mind keeps igniting may crystallize a specialist
    # from a cluster that is ESTABLISHED (≥ the inject/promote midpoint) but not yet fully
    # proven. The tally is content-free (coalition label + decayed count only).
    "node_recruit_from_ignition": 1,  # kill switch for the ignition-pressure trigger (0 = off)
    "ignition_recruit_min_score": 3.0,  # decayed total ignition tally ≥ this arms the relaxed path
    "ignition_tally_half_life_h": 72.0,  # exponential half-life (hours) of the ignition tally
    # ── Section: Flock dynamics — criticality + chemistry trajectory ─────────
    # Murmuration-derived collective-dynamics layer (sibling to colony_features,
    # but kept on its OWN flag so criticality control can be run without the
    # colony layer and vice-versa). 0 = every path below is a strict no-op and
    # the brain behaves exactly as before. Three parts, all flag-gated:
    #   (1) chemistry trajectory/velocity — per-turn derivative of neuromod +
    #       hormonal channels, fed to DMN gating (rising CORT ruminates harder
    #       than steady-high CORT). Learning stays keyed to LEVEL (asymmetry).
    #   (2) criticality observable — branching ratio σ + avalanche-size stats
    #       from the per-turn firing path (reconstructed via the wiring graph).
    #   (3) closed loop — arousal sets a criticality setpoint σ* and the σ-error
    #       drives the global modulation_gain toward it (never super-critical).
    "flock_dynamics": 1,  # flock/criticality layer — production default (graduated 2026-06)
    # (1) chemistry trajectory — DMN rumination velocity weights
    "flock_rum_w_cort_vel": 0.60,  # positive CORT velocity (rising stress) → extra worry drive
    "flock_rum_w_ne_vel": 0.40,  # positive NE velocity (rising alertness) → extra worry drive
    "flock_rum_w_da_vel": 0.30,  # positive DA velocity (rising interest) → extra engaged drive
    "flock_idle_gate_vel_nudge": 0.10,  # rising worry-velocity lowers the idle-gate threshold by up to this
    # (4) ground arousal's triggers — CORT was driven ONLY by a hostility
    # lexicon (vestigial threat semantics). When flock_dynamics is on, ABOVE-
    # average surprise (sustained prediction-error — the world diverging from
    # the model) also accrues cortisol, so the stress signal that feeds
    # trajectory-based rumination reflects a real information-processing stake,
    # not just hostile words. Only the >0.5 (above-average) surprise contributes.
    "flock_cort_surprise_weight": 0.05,  # surprise-excess → cortisol accrual (per turn)
    # (2) criticality observable — measurement window + heavy-tail heuristic
    "flock_sigma_window": 12,  # turns of firing-path history used to smooth σ / build the distribution
    "flock_sigma_min_nodes": 4,  # fewer fired internal nodes than this → σ undefined for the turn (skip)
    # (3) closed loop — arousal-modulated setpoint + conservative controller
    "flock_sigma_target_low": 0.90,  # σ* at low arousal (sub-critical: efficient, quiet at rest)
    "flock_sigma_target_high": 1.00,  # σ* at high arousal (critical: hard cap — never steer super-critical)
    "flock_gain_kp": -0.30,  # proportional constant: Δgain = kp·(σ − σ*). SIGN IS EMPIRICAL —
    # validated in verification; negative is the starting guess.
    "flock_gain_min": 0.50,  # clamp band on the driven modulation_gain (lower rail)
    "flock_gain_max": 1.80,  # clamp band on the driven modulation_gain (upper rail)
    "flock_gain_ema_alpha": 0.25,  # EMA smoothing on the gain so it can't thrash turn-to-turn
    # ── Day-trading capability (advise-only; dark by default) ────────────────
    # When 0, the trading tools are not documented to the planner and the layer
    # is never constructed. Even when 1, the layer is READ-ONLY: it never places
    # an order (read-only Alpaca key + per-tool allow-list + ALPACA_TOOLSETS).
    "trading_enabled": 0,
    "trading_cache_ttl_s": 30.0,  # market-data cache TTL (seconds)
    "trading_max_scan_symbols": 50,  # cap on watchlist symbols scanned per pass
    "trading_default_benchmark": "QQQ",  # benchmark for alpha when none specified
    # growth management
    "trading_execlog_max_days": 365,  # execution_log: hard-delete fills older than N days
    # journal compaction — progressive summarization cascade (see compaction.py)
    "trading_journal_max_resolved": 200,  # compact oldest batch when resolved count exceeds this
    "trading_journal_max_era_summaries": 50,  # compact oldest depth-1 summaries when this is exceeded
    "trading_compaction_batch_size": 20,  # records condensed per compaction pass
    "trading_journal_md_max_kb": 512,  # journal.md: condense oldest section when exceeded
    # real-time websocket stream
    "trading_stream_enabled": 0,  # no longer used for auto-start (stream is manually triggered)
    "trading_alert_cooldown_min": 30,  # min minutes before same trigger can re-fire
    # ── Section: Cloud-action executor (CloudExecutor vs Managed Agents) ───────
    # brain_executor: which backend runs cloud_action tasks. "cma" = Anthropic
    #   Managed Agents (CMAExecutor, server-side, no local CLI) — the default now
    #   that the app runs fully online. "local" = the local Claude CLI subprocess
    #   (CloudExecutor), only useful on a dev box with the `claude` CLI installed.
    #   Overridable per-process via the BRAIN_EXECUTOR env var.
    #   "generic" = the in-process, provider-agnostic agent loop over the brain's
    #   own toolset (GenericExecutor) — works with GPT/Groq/DeepSeek or a local
    #   model, no Anthropic dependency; fewer agentic capabilities than CMA but
    #   the only motor tier that doesn't require Anthropic.
    "brain_executor": "cma",
    # motor_model: model key the generic executor plans actions with (any router
    #   key — gpt, gpt-mini, local-general, runpod-general). Only used when
    #   brain_executor=generic.
    "motor_model": "gpt",
    # cma_enabled: belt-and-suspenders flag (reserved); selection is driven by
    #   brain_executor / BRAIN_EXECUTOR. 1 = on (matches the cma default).
    "cma_enabled": 1,
    # cma_model: model id for the Managed-Agents agents (read + write).
    "cma_model": "claude-sonnet-4-6",
    # cma_networking: cloud sandbox egress — "unrestricted" (needed for web +
    #   remote MCP) or "limited".
    "cma_networking": "unrestricted",
    # cma_task_timeout_s: wall-clock cap on one cloud_action (mirrors the local
    #   executor's SUBPROCESS_TIMEOUT). Primary cost/runaway guard since the SDK
    #   has no managed-agents task budget.
    "cma_task_timeout_s": 300.0,
    # cma_session_warm_reuse: 1 = reuse one warm session per process across
    #   calls (pay cold-start once); 0 = fresh session per task (debug).
    "cma_session_warm_reuse": 1,
    # cma_max_reconnects: bounded SSE reconnect-and-replay attempts on stream drop.
    "cma_max_reconnects": 3,
    "cma_budget_check_interval_s": 30.0,  # how often the CMA executor re-checks cloud spend mid-task
    # ── Section: Motor cortex (tool use) ──────────────────────────────────────
    # motor_allowed_dirs: directories the motor cortex may read/write, one per
    #   line. Locally this is left empty and the allowlist is inherited from
    #   Claude Desktop's trusted folders. On a hosted tenant there is no Claude
    #   Desktop, so this setting IS the allowlist — the user manages it here.
    #   Merged with the BRAIN_MOTOR_PATHS env var at session setup. Empty + no
    #   env = no filesystem access (fails closed). The app's own source root is
    #   never auto-granted in multi-tenant mode.
    "motor_allowed_dirs": "",
    # motor_read_only_dirs: like motor_allowed_dirs, but READ-ONLY — the motor
    #   cortex may list/read/search these but never write, and only inspection
    #   shell commands run with a cwd inside them. One absolute path per line.
    "motor_read_only_dirs": "",
    # Capability switches for the motor toolset. All default ON for backwards
    #   compatibility; turn off in Settings → Motor Permissions.
    "motor_enable_shell": 1,  # run_command tool (shell execution)
    "motor_enable_network": 1,  # fetch_url tool (outbound HTTP)
    "motor_enable_cloud_actions": 1,  # cloud_action tool (Claude CLI / CMA executor)
    # World-grounding tools (Google Maps Platform: geocode, places, directions,
    #   weather, air quality, timezone). Default OFF — these bill per call and need
    #   GOOGLE_MAPS_API_KEY. Additive: when off the brain behaves exactly as before.
    "motor_enable_world": 0,  # world_* tools (real-world data via Google Maps Platform)
    # Auto-confirm cloud WRITE actions instead of holding them for confirmation.
    # Off by default (writes pause for a human/API confirm). Engine mode: an agent
    # may enable it WITHIN this org ceiling for trusted autonomous writes.
    "motor_auto_confirm_writes": 0,
    "motor_write_approval_bytes": 5_000_000,  # cloud WRITE actions above this size need sign-off (cma_executor._write_approval_bytes)
    # motor_allowed_commands: shell command allowlist, one binary name per line.
    #   Empty = the built-in DEFAULT_COMMANDS set. BRAIN_MOTOR_COMMANDS env wins.
    "motor_allowed_commands": "",
    # ── Autonomy policy: user-directed column ─────────────────────────────────
    # Same structure as the motor_self_* column below — applied when the motor
    # executes a live user command. Defaults are wide open (current behavior).
    "motor_user_writes": 1,
    "motor_user_network": 1,
    "motor_user_cloud": "full",  # full | ro | off
    "motor_user_connectors": "",  # one per line; empty = all configured
    # ── Autonomy policy: self-directed column ─────────────────────────────────
    # Applied ONLY when the motor runs a task the brain initiated itself (DMN
    # self-tasks, recovery jobs) — tasks executing a live user command keep the
    # full grants above. Read each dispatch, so changes apply without restart.
    "motor_self_writes": 0,  # 1 = self-directed jobs may write files / run mutating commands
    "motor_self_network": 1,  # 1 = self-directed jobs may fetch_url
    "motor_self_cloud": "ro",  # cloud_action when self-directed: full | ro | off
    # motor_self_connectors: connector allowlist for self-directed cloud work,
    #   one name per line (as shown in the executor's connector summary, e.g.
    #   "github"). Empty = all configured connectors. User-commanded tasks
    #   always get the full connector set.
    "motor_self_connectors": "",
    # ── Section: API keys (user-supplied, set via the Settings → API Keys page) ─
    # Empty = fall back to the platform-provided env var (the resolution chain is
    # user key → platform default → none). When a value is set here it is applied
    # to the corresponding env var at startup (apply_api_key_overrides), so it
    # takes precedence over any platform default. Stored in settings.json; the UI
    # masks them on read and never overwrites a stored key with an empty value.
    "api_key_anthropic": "",  # → ANTHROPIC_API_KEY (required for the app to run)
    "api_key_elevenlabs": "",  # → ELEVENLABS_API_KEY (optional; enables voice output)
    "api_key_deepgram": "",  # → DEEPGRAM_API_KEY (optional; user key beats platform key)
    "api_key_google": "",  # → GOOGLE_API_KEY (optional; Gemini — image processing, Cloud TTS/STT)
    "api_key_google_maps": "",  # → GOOGLE_MAPS_API_KEY (optional; world-grounding tools)
    "api_key_google_vertex_sa": "",  # → GOOGLE_VERTEX_SA_JSON (optional; Vertex service-account JSON)
}

# Maps each user-supplied API-key setting to the env var the clients read.
API_KEY_ENV = {
    "api_key_anthropic": "ANTHROPIC_API_KEY",
    "api_key_elevenlabs": "ELEVENLABS_API_KEY",
    "api_key_deepgram": "DEEPGRAM_API_KEY",
    "api_key_google": "GOOGLE_API_KEY",
    "api_key_google_maps": "GOOGLE_MAPS_API_KEY",
    "api_key_google_vertex_sa": "GOOGLE_VERTEX_SA_JSON",
    "api_key_openai": "OPENAI_API_KEY",
}


class Settings:
    """Singleton that holds the current runtime settings."""

    def __init__(self) -> None:
        self._data: dict[str, float | int | str] = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        if not SETTINGS_PATH.exists():
            return
        try:
            on_disk = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[Settings] Could not load settings.json: %s", e)
            return
        unknown: list[str] = []
        for k, v in on_disk.items():
            if k not in DEFAULTS:
                unknown.append(k)
                continue
            try:
                self._data[k] = type(DEFAULTS[k])(v)
            except Exception as e:
                # One malformed value must not discard every other override.
                logger.warning(
                    "[Settings] Ignoring %s=%r (cannot coerce to %s): %s",
                    k,
                    v,
                    type(DEFAULTS[k]).__name__,
                    e,
                )
        if unknown:
            # A typo'd key silently doing nothing is the worst failure mode of a
            # 240-key schema — say which keys were dropped.
            logger.warning(
                "[Settings] %d unknown key(s) in %s ignored (typo or removed setting?): %s",
                len(unknown),
                SETTINGS_PATH,
                ", ".join(sorted(unknown)[:20]),
            )
        logger.info("[Settings] Loaded %d overrides from %s", len(on_disk), SETTINGS_PATH)

    def get(self, key: str, default=None):
        return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def all(self) -> dict:
        return dict(self._data)

    def update(self, patch: dict) -> None:
        """Merge a partial dict of settings into memory (does not persist)."""
        for k, v in patch.items():
            if k in DEFAULTS:
                self._data[k] = type(DEFAULTS[k])(v)

    def save(self, patch: dict | None = None) -> None:
        """Optionally merge patch, then write the full settings to disk."""
        if patch:
            self.update(patch)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        logger.info("[Settings] Saved to %s", SETTINGS_PATH)

    def reset_to_defaults(self) -> None:
        self._data = dict(DEFAULTS)


# Module-level singleton — import this everywhere
settings = Settings()


def apply_api_key_overrides() -> None:
    """Apply user-supplied API keys (from settings.json) to the process env.

    Implements the resolution chain `user key → platform default → none`: when a
    key is set in settings it overrides the platform-provided env var, so the
    clients (which all read os.environ) pick up the user's own key. Empty
    settings leave the platform default untouched. Call this AFTER load_dotenv so
    a user key always wins over a .env default.
    """
    for setting_key, env_name in API_KEY_ENV.items():
        val = str(settings.get(setting_key) or "").strip()
        if val:
            os.environ[env_name] = val
    _materialize_vertex_credentials()


def _materialize_vertex_credentials() -> None:
    """If a Vertex service-account JSON is provided as a string (GOOGLE_VERTEX_SA_JSON,
    e.g. from the key vault on a hosted pod), write it to a temp file and point
    GOOGLE_APPLICATION_CREDENTIALS at it so the Google/Anthropic-Vertex SDKs find ADC.
    A pre-set GOOGLE_APPLICATION_CREDENTIALS (local `gcloud auth application-default
    login`, or workload identity) always wins and is left untouched."""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    sa_json = (os.environ.get("GOOGLE_VERTEX_SA_JSON") or "").strip()
    if not sa_json:
        return
    try:
        import tempfile

        fd, path = tempfile.mkstemp(prefix="vertex-sa-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(sa_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        logger.info("[Settings] Vertex SA credentials materialized to %s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("[Settings] Could not materialize Vertex SA credentials: %s", e)
