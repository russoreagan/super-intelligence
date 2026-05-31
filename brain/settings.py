"""
Brain Settings — runtime-tunable constants.
Loads from brain/settings.json (next to this file) at startup.
Falls back to built-in defaults when the file doesn't exist.
Changes take effect after restarting the brain.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).parent / "settings.json"

# ── Defaults ─────────────────────────────────────────────────────────────────
# Each entry: key → default value
# Groups mirror the settings UI sections.

DEFAULTS: dict[str, float | int | str] = {
    # ── Section 1: Emotional Reactivity ──────────────────────────────────────
    "emotional_reactivity_scale": 1.00,
    "sentiment_DA_weight": 0.15,
    "hostility_DA_weight": 0.10,
    "surprise_ACh_weight": 0.12,
    "salience_ACh_weight": 0.08,
    "salience_Glu_weight": 0.12,
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
    "decay_toward_rest_rate": 0.01,
    "weight_min": 0.10,
    "weight_max": 3.00,
    "gaba_skip_threshold_high": 0.55,
    # ── Section 4: Default Mode Network ──────────────────────────────────────
    "dmn_interval": 8.0,  # active baseline — fires when any mouse/keyboard activity detected
    "dmn_idle_interval": 45.0,  # when fully away from computer (OS idle > 60s)
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
    "topic_activation_decay": 0.70,
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
    # AEA color threshold (when elevated AEA buffers a stress state → "eased")
    "aea_eased_threshold": 0.58,  # AEA > this + stress base emotion → eased
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
    # ── Section: Motor Cortex / Autonomous Tasks ─────────────────────────────
    # ralph_max_total_attempts: hard ceiling on total tool dispatches across ALL
    # stories + retries in a single internal job. Prevents runaway loops
    # regardless of story count or per-story retry budget.
    # Can also be overridden per-session via BRAIN_RALPH_MAX_ATTEMPTS env var.
    "ralph_max_total_attempts": 12,
    # Motor-cortex job rate limits (cost guard now that planning runs on cloud).
    # Cloud spend is also bounded by bg_cloud_token_budget + cloud_daily_usd_budget;
    # these additionally cap how MANY autonomous jobs can run.
    "motor_max_concurrent_jobs": 1,      # only one autonomous job at a time
    "motor_max_jobs_per_window": 10,     # ≤ this many job starts per window
    "motor_job_window_s": 3600.0,        # rolling window = 1 hour
    "motor_max_jobs_per_session": 30,    # absolute ceiling per process lifetime
    # ── Section: Cloud call timeouts (anti-hang) ─────────────────────────────
    # Bound every Anthropic call so a stalled connection can't freeze a motor
    # job at the strategic-plan step. read timeout bounds long generations;
    # connect timeout catches dead sockets fast; retries are bounded.
    "anthropic_timeout_s": 120.0,
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
    # bg_cloud_token_budget: combined input+output token ceiling for all
    #   background cloud calls in one session. Exhausted budget routes to local.
    #   50k ≈ ~$0.04–0.20 at haiku/flash-lite prices — intentionally conservative.
    "bg_cloud_token_budget": 50_000,
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
    # cloud_daily_usd_budget: hard ceiling on total cloud spend per calendar day
    #   (UTC). 0 = no cap. Persisted to second_brain/cloud_usage.json so it
    #   survives restarts. When hit, all cloud calls fall back to local for the
    #   remainder of the day.
    #   Default $5.00 — generous for normal interactive use; tighten if needed.
    "cloud_daily_usd_budget": 5.0,
    # ── Section: RunPod ───────────────────────────────────────────────────────
    # Overrides the RUNPOD_HOST / RUNPOD_MODEL env vars at runtime — no restart
    # needed. Empty string = fall back to env var.
    "runpod_host": "",
    "runpod_model": "",
    # max_runpod_hours: watchdog stops the pod if the brain hasn't been seen
    # alive for this many hours. Resets continuously while the brain is running.
    # Acts as a backstop against runaway costs after a crash/force-kill.
    "max_runpod_hours": 8.0,
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
    # Active persona's ElevenLabs voice ID. Applied at boot via pns.set_voice_id().
    # Empty = use ELEVENLABS_VOICE_ID env var or built-in default.
    "persona_voice_id": "",
    # Per-persona saved voice IDs. Written when a persona is initialized with a voice.
    # Restored automatically when that persona is switched to.
    "persona_voice_the_visionary": "",
    "persona_voice_the_empath": "",
    "persona_voice_the_analyst": "",
    "persona_voice_the_poet": "",
    "persona_voice_the_sage": "",
    # ── Section: Channel Calibration (text vs. voice) ─────────────────────────
    # Text and voice are fundamentally different communication channels.
    # These weights scale how temporal features feed into neuromod updates
    # depending on which channel the input came from.
    "enable_channel_calibration": 1,      # 1 = on, 0 = off
    "enable_text_paralinguistics": 1,     # 1 = extract emoji/laughter/warmth markers for text
    # Text channel calibration weights
    "text_hostility_weight": 0.65,        # short/direct text ≠ hostile; discount this signal
    "text_sentiment_weight": 1.10,        # word-level sentiment is primary; up-weight slightly
    "text_length_signal_weight": 0.20,    # message brevity is normal; near-zero as signal
    # Text paralinguistic → neuromod contribution weights
    "text_para_laughter_DA": 0.10,        # lol/😂 → DA boost
    "text_para_warmth_DA": 0.07,          # :)/❤️ → DA boost
    "text_para_negativity_GABA": 0.08,    # :(/ 😡 → GABA
    "text_para_excitement_Glu": 0.07,     # omg/🔥 → Glu
    "text_para_excitement_NE": 0.04,      # omg/🔥 → NE
    # ── Section: Relationship Stage Progression ───────────────────────────────
    "enable_relationship_stage_progression": 1,   # 1 = auto-update familiarity tier at sleep
    "familiarity_acquainted_min_sessions": 3,      # sessions needed to reach acquainted
    "familiarity_acquainted_min_score": 0,         # affection score must be at least this
    "familiarity_close_min_sessions": 10,          # sessions needed to reach close
    "familiarity_close_min_score": 15,             # affection score must be at least this
    # ── Section: Bond model (relational decay + reunion recovery) ─────────────
    # Two quantities per speaker: affection (live warmth, injected into prompts)
    # and bond (latent closeness high-water mark). Closeness creates a bond that
    # decays slowly and recovers fast; a thin acquaintance fades to nothing over
    # the same gap. Half-lives grow EXPONENTIALLY with bond so the closer the
    # prior relationship the smaller the decline.
    "enable_bond_model": 1,
    "bond_aff_halflife_base_days": 25.0,    # affection half-life at bond=0 (days)
    "bond_bond_halflife_base_days": 90.0,   # bond half-life at bond=0 (days, much slower)
    "bond_halflife_scale": 23.0,            # exp denominator: HL = base * exp(bond/scale)
    "bond_reunion_gain": 8.0,               # positive-delta multiplier slope on reengagement
    "familiarity_close_bond": 35.0,         # bond ≥ this → "close"
    "familiarity_acquainted_bond": 12.0,    # bond ≥ this → "acquainted", else "new"
    # ── Section: Self-Disclosure Policy ──────────────────────────────────────
    "enable_self_disclosure_policy": 1,             # 1 = inject disclosure opportunity into drafter
    "self_disclosure_cooldown_turns": 8,            # min turns between disclosure prompts
    "self_disclosure_min_affection": 5,             # affection score floor for voice disclosure
    "self_disclosure_text_min_affection": 20,       # affection score floor for text disclosure
    # ── Section: Performed Emotion Gate ──────────────────────────────────────
    # Deliberate/performed emotion ([mood:X] markup, set_mood) is a playful,
    # humor-leaning intimacy device. Gate WHEN the drafter is encouraged to use
    # it by relationship depth + the user's mood. Off → preserve the old
    # always-offered behaviour.
    "enable_performed_emotion_gate": 1,
    "performed_emotion_min_affection": 10,          # general warmth floor (neutral mood)
    "performed_emotion_new_min_affection": 15,      # higher floor when familiarity is still "new"
    "performed_emotion_cheerup_min_affection": 20,  # bar to attempt cheer-up / tension-break when user is down
    # ── Section: Style Synchrony ─────────────────────────────────────────────
    "enable_style_synchrony": 1,                    # 1 = track and inject user style register
    "style_ema_alpha_voice": 0.25,                  # EMA weight for voice style (per turn)
    "style_ema_alpha_text": 0.20,                   # EMA weight for text style (slower — more variable)
    "style_max_shift": 0.12,                        # max drift toward user per dimension per session
    "style_entity_formality_baseline": 0.25,        # entity's natural formality (0=casual, 1=formal)
    "style_entity_verbosity_baseline": 0.45,        # entity's natural verbosity (0=terse, 1=expansive)
    "style_min_turns_for_injection": 3,             # turns tracked before injecting style note
    # ── Section: Graded plasticity (correctness fix — NOT colony-gated) ───────
    # The legacy all-or-nothing `defuse_path` skip (gaba_skip_threshold_high) is
    # biologically wrong: real plasticity is graded and neuromodulator-scaled
    # (three-factor learning rules), and aversive/stress states follow an
    # inverted-U (moderate arousal ENHANCES encoding; only extreme stress
    # impairs). When on, a per-turn plasticity factor keyed to arousal/emotional
    # intensity (not valence sign) multiplies the Hebbian delta, and the binary
    # skip becomes a graded high-stress dampener. Ships on its own flag,
    # independent of colony_features. Default 0 → flip to 1 after eval validates.
    "graded_plasticity": 0,                         # 1 = graded per-turn plasticity; 0 = legacy binary skip
    "plasticity_turn_min": 0.40,                    # floor of the per-turn plasticity multiplier
    "plasticity_turn_max": 1.30,                    # ceiling of the per-turn plasticity multiplier
    "plasticity_arousal_weight": 0.50,              # ACh+NE+surprise+|DA swing| → plasticity gain
    "plasticity_intensity_weight": 0.40,            # |valence| (either sign) → plasticity gain
    "plasticity_stress_knee": 0.70,                 # CORT/GABA above this = inverted-U descending limb
    "plasticity_stress_damp": 0.60,                 # max multiplicative dampening at extreme stress
    # ── Section: Colony / non-brain (superorganism) capabilities ─────────────
    # Single master toggle for the bio-inspired colony layer (Phases 2–8 of the
    # colony-features plan). 0 = every colony behaviour is a strict no-op and the
    # brain behaves exactly as before. Per-feature tuning knobs below take effect
    # only when this is on. The three feedback loops (concentration, recruitment,
    # chemistry self-feedback) are also independently observable in the decisions
    # log so they can be validated in increasing-risk order.
    "colony_features": 0,                           # 1 = enable colony layer; 0 = off (no-op)
    # Phase 2 — topic concentration / quorum / silence (threat channel first)
    "colony_conc_half_life_s": 45.0,                # exponential half-life of topic concentration
    "colony_conc_cap": 10.0,                        # max accumulated concentration (chatty-topic bound)
    "colony_arm_threshold": 1.00,                   # concentration must cross this to become ARMED
    "colony_quorum_threshold": 1.50,                # ARMED + concentration ≥ this → quorum
    "colony_silence_floor": 0.15,                   # ARMED concentration decays below this → QUIET
    "colony_silence_disarm_s": 600.0,               # zero-dwell this long → disarm back to UNARMED
    # Phase 3 — releaser + primer in one message
    "colony_primer_gain": 0.30,                     # scales Message.primer nudges into hormonal channels
    # Phase 4/7 — recruitment amplification + mobilization cascade
    "colony_recruit_gain": 0.40,                    # scales need_level → recruitment level
    # Phase 5 — threshold diversity (division of labor)
    "colony_threshold_spread": 0.08,                # ± bound on deterministic per-switch threshold jitter
    # Phase 8 — aggregate-state neuromodulation feedback (highest-risk loop)
    "colony_state_feedback_gain": 0.02,             # tiny gain on prior-turn aggregate → neuromod nudges
    "colony_state_feedback_clamp": 0.05,            # max total feedback contribution per channel per turn
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
            for k, v in on_disk.items():
                if k in DEFAULTS:
                    self._data[k] = type(DEFAULTS[k])(v)
            logger.info("[Settings] Loaded %d overrides from %s", len(on_disk), SETTINGS_PATH)
        except Exception as e:
            logger.warning("[Settings] Could not load settings.json: %s", e)

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
