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

DEFAULTS: dict[str, float | int] = {
    # ── Section 1: Emotional Reactivity ──────────────────────────────────────
    "emotional_reactivity_scale":  1.00,
    "sentiment_DA_weight":         0.15,
    "hostility_DA_weight":         0.10,
    "surprise_ACh_weight":         0.12,
    "salience_ACh_weight":         0.08,
    "salience_Glu_weight":         0.12,
    "hostility_GABA_threshold_high": 0.50,
    "hostility_GABA_increment_high": 0.20,
    "hostility_GABA_threshold_med":  0.20,
    "hostility_GABA_increment_med":  0.05,
    "hostile_intent_Glu_bonus":    0.15,

    # ── Section 2: Neuromodulator Homeostasis ─────────────────────────────────
    "valence_to_DA_decay":         0.85,
    "threat_to_GABA_decay":        0.80,
    "novelty_to_ACh_decay":        0.90,
    "arousal_homeostat_decay":     0.88,
    "satiation_inhibitor_decay":   0.95,
    "salience_satiation_threshold": 0.30,
    "salience_satiation_increase":  0.05,
    "salience_satiation_decrease": -0.10,
    "satiation_inhibition_factor":  0.50,

    # ── Section 3: Plasticity & Learning ─────────────────────────────────────
    "hebbian_delta":               0.02,
    "hebbian_outcome_delta":       0.02,
    "decay_toward_rest_rate":      0.01,
    "weight_min":                  0.10,
    "weight_max":                  3.00,
    "gaba_skip_threshold_high":    0.55,

    # ── Section 4: Default Mode Network ──────────────────────────────────────
    "dmn_interval":                8.0,    # active baseline (was 15s)
    "dmn_idle_interval":           25.0,   # when get_idle_seconds > 60s
    "dmn_overlap_threshold":       0.35,
    "ach_suppression_weight":      1.00,
    "glu_suppression_weight":      0.30,
    "gaba_suppression_reduction":  0.15,
    "suppression_skip_prob_max":   0.85,
    "speak_gate_poll_interval":    5.0,    # how often the speak gate evaluates candidates
    "speak_candidate_max_age_s":   60.0,   # drop unspoken candidates older than this
    # Bridge rewriter (local Ollama only — no paid LLM calls). When a
    # candidate is approved AND its topic-overlap with the live conversation
    # is below `speak_bridge_overlap_threshold`, the brain rewrites the
    # spoken form locally so the change-of-subject doesn't feel abrupt.
    # Set enabled=0 to disable bridging; set threshold=1.0 to bridge every
    # approved candidate (most polish, most local-LLM latency).
    "speak_bridge_enabled":          1,    # 1 = on, 0 = off (kept int for settings UI sliders)
    "speak_bridge_overlap_threshold": 0.20,

    # ── Section 5: Metacognition ──────────────────────────────────────────────
    "meta_interval":               30.0,
    "meta_cooldown_turns":         3,
    "da_threshold_disappointed":   0.25,
    "gaba_drop_threshold":         0.20,

    # ── Section 6: Prediction & Surprise ─────────────────────────────────────
    "surprise_threshold":          0.40,
    "confidence_skip_threshold":   0.70,
    "predictor_window":            8,

    # ── Section 7: Voice Expressiveness ──────────────────────────────────────
    "voice_stability_default":     0.45,
    "voice_style_default":         0.40,
    "voice_speed_default":         1.00,
    "voice_stability_threat":      0.65,
    "voice_style_threat":          0.25,
    "voice_speed_threat":          0.95,
    "voice_stability_bright":      0.35,
    "voice_style_bright":          0.55,
    "voice_speed_bright":          1.05,
    "voice_stability_low_mood":    0.55,
    "voice_style_low_mood":        0.30,
    "voice_speed_low_mood":        0.93,
    "breath_pause_count_max":      2,
    "gaba_single_pause_threshold": 0.50,
    "da_double_pause_threshold":   0.30,
    "glu_urgently_threshold":      0.55,
    "gaba_urgently_threshold":     0.35,
    "gaba_gently_threshold":       0.50,
    "da_excited_threshold":        0.60,
    "glu_excited_threshold":       0.55,
    "ach_curious_threshold":       0.55,
    "gaba_curious_threshold":      0.35,
    "da_softly_threshold":         0.30,

    # ── Section 8: Proactive Behavior ─────────────────────────────────────────
    # idle_threshold is now INVERTED in spirit: the brain STOPS speaking
    # proactively when the user has been OS-idle for more than this. Internal
    # thoughts still flow to the UI; only TTS interjections are suppressed.
    "proactive_idle_threshold":    300.0,  # was 180
    "proactive_response_window":   10.0,   # was 8 — min gap between brain utterances

    # ── Section 9: Attention & Routing ───────────────────────────────────────
    "hippocampus_priority_base":   0.60,
    "hippocampus_salience_weight": 0.30,
    "occipital_priority_base":     0.80,
    "frontal_hostile_priority":    0.30,
    "frontal_ach_weight":          0.20,
    "ach_threshold_frontal":       0.50,
    "salience_workspace_threshold": 0.60,
    "topic_activation_decay":      0.70,

    # ── Section 10: Speaker Recognition ──────────────────────────────────────
    "speaker_store_threshold":     0.70,
    "speaker_session_threshold":   0.62,
    "speaker_min_audio_s":         0.40,
    # Soft threshold for "could this be the primary user?" check on unrecognized voices.
    # If an unrecognized voice scores >= this against the primary user's profile, treat
    # it as the primary user tentatively rather than creating a stranger placeholder.
    "speaker_primary_soft_threshold": 0.55,

    # ── Section 11: Vision / Video ────────────────────────────────────────────
    "video_sample_interval":       5.0,
    "video_max_frames":            8,
    "video_change_threshold":      8.0,

    # ── Section 12: Endocrine / Hormonal System ───────────────────────────────
    # Update rates (added per turn when condition is met)
    "oxt_positive_increment":      0.008,   # OXT gain per warm/positive exchange (~50 turns to connected)
    "oxt_hostility_drain":         0.008,   # OXT drain per hostile exchange (symmetric)
    "cort_threat_increment":       0.022,   # CORT gain when hostility > threshold
    "cort_hostility_threshold":    0.35,    # hostility score that triggers CORT build (text-based, not prosody)
    "sht_reward_increment":        0.003,   # 5HT gain per rewarding interaction
    "sht_reward_sentiment_min":    0.40,    # min sentiment to earn 5HT
    "sht_hostility_drain":         0.004,   # 5HT drain per hostile exchange (enables dysphoric state)
    # OXT ↔ CORT antagonism
    "oxt_cort_buffer_rate":        0.020,   # OXT level × this = CORT drain per turn (~60% offset at OXT=0.5)
    "oxt_cort_buffer_threshold":   0.40,    # OXT must exceed this to buffer CORT
    # Hormonal → DA modulation (effective DA = raw DA + offset)
    "sht_da_floor_lift":           0.12,    # 5HT × this added to effective DA
    "oxt_da_lift":                 0.05,    # OXT × this added to effective DA
    "cort_da_suppress":            0.08,    # CORT × this subtracted from effective DA
    # Hormonal → GABA modulation (effective GABA = raw GABA × scale)
    "cort_gaba_amplify":           0.30,    # CORT × this amplifies GABA scale
    "oxt_gaba_buffer":             0.15,    # OXT × this reduces GABA scale
    # Hormonal color thresholds (when to override base emotion)
    "hormonal_oxt_connected_threshold":   0.60,   # OXT > this + positive base → connected
    "hormonal_cort_withdrawn_threshold":  0.45,   # CORT > this → withdrawn/guarded (~17 hostile turns)
    "hormonal_oxt_guarded_threshold":     0.35,
    "hormonal_sht_dysphoric_threshold":   0.25,

    # ── Section 13: Time-weighted decay ──────────────────────────────────────
    # decay_turn() measures wall-clock seconds since the last turn and applies
    # rate ** (elapsed / reference_interval_s) instead of a fixed rate ** 1.
    # This makes emotional state decay proportional to real time, not message count:
    # slow conversations decay faster between turns; rapid exchanges stay stickier.
    "decay_reference_interval_s":  60.0,   # elapsed seconds that equals 1 decay turn
    "decay_min_turns":              0.25,   # floor — even instant replies apply some decay
    "decay_max_turns":             10.0,   # cap — silence > 10 min treated as 10 turns

    # ── Section 14: Norepinephrine (NE) ──────────────────────────────────────
    # NE = focused alertness signal; inverted-U curve (optimal 0.20–0.55)
    # Per-turn update weights (applied before er_scale)
    "ne_salience_weight":          0.07,   # NE gain per unit salience (alert to what matters)
    "ne_surprise_weight":          0.05,   # NE gain per unit surprise (re-orient fast)
    "ne_hostility_weight":         0.10,   # NE gain per unit hostility (threat → vigilance)
    # Prosody / dynamics contributions
    "ne_prosody_stressed":         0.06,   # NE gain when tone_label == "stressed"
    "ne_rush_increment":           0.05,   # NE gain when pace == "rushed"
    # Inverted-U thresholds (above high → vigilant; above scatter → degraded focus)
    "ne_high_threshold":           0.55,   # NE > this → heightened vigilance modifier
    "ne_scatter_threshold":        0.82,   # NE > this → attention narrowed, scattered

    # ── Section 15: Anandamide / AEA (endocannabinoid) ───────────────────────
    # AEA = homeostatic buffer; medium-speed (decay 0.90 vs. neuromod 0.85 / hormone 0.97+)
    # Rises automatically when Glu + NE arousal sum exceeds threshold
    "aea_arousal_threshold":       0.65,   # Glu + NE sum that triggers homeostatic AEA rise
    "aea_arousal_increment":       0.018,  # AEA gain per turn when arousal is high (~15 turns to effect)
    "aea_positive_increment":      0.005,  # AEA gain per warm/positive turn (social afterglow)
    "aea_cort_drain":              0.004,  # AEA drain per turn under sustained stress (CORT antagonism)
    # AEA → effective NE / Glu suppression (applied above resting baseline of 0.30)
    "aea_ne_suppression":          0.50,   # excess AEA × this reduces effective NE scale
    "aea_glu_suppression":         0.35,   # excess AEA × this reduces effective Glu scale
    # AEA → DA lift ("afterglow": elevated AEA adds mild positive valence)
    "aea_da_lift":                 0.04,   # AEA × this added to effective DA
    # AEA color threshold (when elevated AEA buffers a stress state → "eased")
    "aea_eased_threshold":         0.58,   # AEA > this + stress base emotion → eased

    # ── Section: Switch Modulation ────────────────────────────────────────────
    # Single gain that scales every SwitchNeuron's modulator coefficient.
    # 0.0 = chemistry has no effect on switches (pure deterministic gating).
    # 1.0 = profiles fire at their declared strength (default).
    # >1.0 = amplified chemistry response; <1.0 = damped.
    "modulation_gain":             1.00,
}


class Settings:
    """Singleton that holds the current runtime settings."""

    def __init__(self) -> None:
        self._data: dict[str, float | int] = dict(DEFAULTS)
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
        SETTINGS_PATH.write_text(
            json.dumps(self._data, indent=2), encoding="utf-8"
        )
        logger.info("[Settings] Saved to %s", SETTINGS_PATH)

    def reset_to_defaults(self) -> None:
        self._data = dict(DEFAULTS)


# Module-level singleton — import this everywhere
settings = Settings()
