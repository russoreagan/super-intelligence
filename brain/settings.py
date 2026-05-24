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
    "salience_Glu_weight":         0.08,
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
    "dmn_interval":                15.0,
    "dmn_overlap_threshold":       0.35,
    "ach_suppression_weight":      1.00,
    "glu_suppression_weight":      0.30,
    "gaba_suppression_reduction":  0.15,
    "suppression_skip_prob_max":   0.85,

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
    "proactive_idle_threshold":    180.0,
    "proactive_response_window":   8.0,

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
