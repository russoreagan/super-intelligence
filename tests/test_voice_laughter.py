"""
Voice laughter detection — three composable tiers feeding the levity DA path.

Tier 1: transcript sniffing (text_paralinguistics.extract_laughter on voice turns)
Tier 2: DSP heuristic (audio_dsp.laughter_likelihood from prosody features)
Tier 3: vocal-event classifier (vocal_events, flag-gated, fail-soft)

Hypothalamus composes the tiers via max() and releases DA scaled by
text_para_laughter_DA × reward_weight(persona, "levity") — the same path as
text-channel laughter, so the channels stay comparable.
"""

from __future__ import annotations

import numpy as np

from brain.bus import Bus
from brain.clusters.audio_dsp import extract_prosody, laughter_likelihood
from brain.clusters.hypothalamus import HypothalamusCluster
from brain.clusters.text_paralinguistics import extract_laughter
from brain.settings import settings

# ===========================================================================
# Tier 1 — transcript laughter extraction
# ===========================================================================


class TestExtractLaughter:
    def test_stt_style_ha_ha_detected(self):
        # Deepgram commonly transcribes a real laugh as separated syllables
        assert extract_laughter("ha ha ha that's great") > 0.0

    def test_heh_heh_detected(self):
        assert extract_laughter("heh heh you got me") > 0.0

    def test_annotation_detected(self):
        assert extract_laughter("(laughs) oh no, not again") > 0.0
        assert extract_laughter("[laughter] stop it") > 0.0

    def test_classic_text_markers_still_work(self):
        assert extract_laughter("haha that is so true") > 0.0
        assert extract_laughter("lol") > 0.0

    def test_single_ha_is_not_laughter(self):
        # A lone "ha" is sarcasm as often as mirth — two+ syllables required
        assert extract_laughter("ha very funny") == 0.0

    def test_plain_speech_scores_zero(self):
        assert extract_laughter("can you check the deploy status for me") == 0.0

    def test_empty_scores_zero(self):
        assert extract_laughter("") == 0.0
        assert extract_laughter("   ") == 0.0


# ===========================================================================
# Tier 2 — DSP heuristic
# ===========================================================================

# Laughter signature: ~5 Hz rhythmic energy bursts, high voiced fraction,
# elevated mean f0 with large pitch variability.
LAUGHTER_LIKE = {
    "voiced_fraction": 0.85,
    "f0_mean_hz": 280.0,
    "f0_std_hz": 90.0,
    "energy_mean": 0.15,
    "energy_std": 0.09,
    "speech_rate_hz": 5.0,
    "jitter": 0.02,
    "shimmer": 0.08,
}

# Animated/energetic speech: shares the rate and energy bands but lacks the
# pitch signature (modest f0_std around a normal speaking register).
ENERGETIC_SPEECH_LIKE = {
    "voiced_fraction": 0.6,
    "f0_mean_hz": 140.0,
    "f0_std_hz": 18.0,
    "energy_mean": 0.18,
    "energy_std": 0.05,
    "speech_rate_hz": 5.5,
    "jitter": 0.015,
    "shimmer": 0.07,
}

CALM_SPEECH_LIKE = {
    "voiced_fraction": 0.5,
    "f0_mean_hz": 120.0,
    "f0_std_hz": 20.0,
    "energy_mean": 0.05,
    "energy_std": 0.015,
    "speech_rate_hz": 2.5,
    "jitter": 0.01,
    "shimmer": 0.05,
}


class TestLaughterLikelihood:
    def test_laughter_like_features_clear_threshold(self):
        score = laughter_likelihood(LAUGHTER_LIKE)
        assert score >= settings.get("laughter_dsp_threshold")
        assert score <= 1.0

    def test_energetic_speech_does_not_false_positive(self):
        # Conservative AND-gate: missing pitch signature → 0, not just "low"
        assert laughter_likelihood(ENERGETIC_SPEECH_LIKE) == 0.0

    def test_calm_speech_scores_zero(self):
        assert laughter_likelihood(CALM_SPEECH_LIKE) == 0.0

    def test_slow_rhythm_outside_band_scores_zero(self):
        feats = dict(LAUGHTER_LIKE, speech_rate_hz=1.5)
        assert laughter_likelihood(feats) == 0.0

    def test_low_voiced_fraction_scores_zero(self):
        # Unvoiced bursts (e.g. applause, keyboard) must not read as laughter
        feats = dict(LAUGHTER_LIKE, voiced_fraction=0.3)
        assert laughter_likelihood(feats) == 0.0

    def test_calibrated_baseline_raises_pitch_threshold(self):
        # A speaker whose normal f0_std is huge: same features stop registering
        baseline = {
            "jitter": 0.02,
            "shimmer": 0.08,
            "energy_mean": 0.1,
            "f0_std": 80.0,
            "count": 20,
        }
        uncalibrated = laughter_likelihood(LAUGHTER_LIKE)
        calibrated = laughter_likelihood(LAUGHTER_LIKE, baseline)
        assert calibrated < uncalibrated

    def test_uncalibrated_baseline_ignored(self):
        # < 10 observations → universal thresholds, identical score
        baseline = {"jitter": 0.02, "shimmer": 0.08, "energy_mean": 0.1, "f0_std": 80.0, "count": 3}
        assert laughter_likelihood(LAUGHTER_LIKE, baseline) == laughter_likelihood(LAUGHTER_LIKE)

    def test_extract_prosody_payload_carries_key(self):
        # Silence early-return path must still ship the key downstream
        silent = np.zeros(1600, dtype=np.float32)
        result = extract_prosody(silent, 16000)
        assert result["laughter_likelihood"] == 0.0


# ===========================================================================
# Hypothalamus — voice laughter → levity-scaled DA
# ===========================================================================


def _voice_features(**extra) -> dict:
    return {
        "sentiment": 0.0,
        "hostility": 0.0,
        "salience": 0.3,
        "surprise_score": 0.0,
        "topic_summary": "test",
        "input_modality": "voice",
        **extra,
    }


async def _run_turn(features: dict, prosody_payload: dict | None = None) -> float:
    """Process one hypothalamus turn, return the resulting DA level."""
    bus = Bus()
    hypo = HypothalamusCluster(bus)
    if prosody_payload is not None:
        await bus.publish_dict("auditory.prosody", prosody_payload, source="test")
    await hypo.process(features)
    return bus.neuromod.snapshot()["DA"]


class TestHypothalamusVoiceLaughter:
    async def test_transcript_laughter_releases_da(self):
        da_control = await _run_turn(_voice_features())
        da_laugh = await _run_turn(_voice_features(transcript_laughter=1.0))
        expected = settings.get("text_para_laughter_DA")
        assert da_laugh - da_control > expected * 0.5

    async def test_acoustic_laughter_above_threshold_releases_da(self):
        calm = {"tone_label": "calm", "tone_strength": 0.0}
        da_control = await _run_turn(_voice_features(), prosody_payload=calm)
        da_laugh = await _run_turn(
            _voice_features(),
            prosody_payload={**calm, "laughter_likelihood": 0.9},
        )
        assert da_laugh - da_control > settings.get("text_para_laughter_DA") * 0.5

    async def test_acoustic_laughter_below_threshold_is_ignored(self):
        calm = {"tone_label": "calm", "tone_strength": 0.0}
        da_control = await _run_turn(_voice_features(), prosody_payload=calm)
        da_sub = await _run_turn(
            _voice_features(),
            prosody_payload={**calm, "laughter_likelihood": 0.2},
        )
        assert abs(da_sub - da_control) < 0.01

    async def test_tiers_compose_via_max_not_sum(self):
        # transcript 0.4 + acoustic 0.9 must release DA for 0.9, not 1.3
        calm = {"tone_label": "calm", "tone_strength": 0.0}
        da_control = await _run_turn(_voice_features(), prosody_payload=calm)
        da_both = await _run_turn(
            _voice_features(transcript_laughter=0.4),
            prosody_payload={**calm, "laughter_likelihood": 0.9},
        )
        released = da_both - da_control
        weight = settings.get("text_para_laughter_DA")
        assert released < weight * 1.1  # max() → ~0.9×weight, never ~1.3×weight
        assert released > weight * 0.5

    async def test_vocal_event_laughter_releases_da(self):
        calm = {"tone_label": "calm", "tone_strength": 0.0}
        da_control = await _run_turn(_voice_features(), prosody_payload=calm)
        da_event = await _run_turn(
            _voice_features(),
            prosody_payload={**calm, "vocal_events": {"laughter": 0.8, "sigh": 0.05}},
        )
        assert da_event - da_control > settings.get("text_para_laughter_DA") * 0.5

    async def test_text_turn_unaffected_by_voice_path(self):
        # Text turns keep the existing text_paralinguistics path; the voice
        # block must not fire without modality=voice or a prosody message.
        feats = {
            "sentiment": 0.0,
            "hostility": 0.0,
            "salience": 0.3,
            "surprise_score": 0.0,
            "topic_summary": "test",
            "input_modality": "text",
            "transcript_laughter": 1.0,  # would only matter on a voice turn
        }
        da_text = await _run_turn(feats)
        da_control = await _run_turn({**feats, "transcript_laughter": 0.0})
        assert abs(da_text - da_control) < 0.01


# ===========================================================================
# Tier 3 — vocal-event classifier (fail-soft + label pooling)
# ===========================================================================


class TestVocalEvents:
    def test_fail_soft_when_backend_missing(self, monkeypatch):
        import brain.clusters.vocal_events as ve

        monkeypatch.setattr(ve, "_load_attempted", True)
        monkeypatch.setattr(ve, "_tagger", None)
        monkeypatch.setattr(ve, "_label_index", None)
        assert ve.available() is False
        assert ve.detect_vocal_events(np.zeros(32000, dtype=np.float32), 32000) == {}

    def test_event_probs_max_pooled_over_labels(self, monkeypatch):
        import brain.clusters.vocal_events as ve

        class FakeTagger:
            def inference(self, clip):
                # Laughter 0.1 but Giggle 0.7 → laughter event must report 0.7
                return np.array([[0.1, 0.3, 0.05, 0.2, 0.7]]), None

        monkeypatch.setattr(ve, "_load_attempted", True)
        monkeypatch.setattr(ve, "_tagger", FakeTagger())
        monkeypatch.setattr(
            ve,
            "_label_index",
            {"Laughter": 0, "Sigh": 1, "Gasp": 2, "Crying, sobbing": 3, "Giggle": 4},
        )
        out = ve.detect_vocal_events(np.zeros(32000, dtype=np.float32), 32000)
        assert out["laughter"] == 0.7
        assert out["sigh"] == 0.3
        assert out["gasp"] == 0.05
        assert out["crying"] == 0.2

    def test_inference_error_returns_empty(self, monkeypatch):
        import brain.clusters.vocal_events as ve

        class BrokenTagger:
            def inference(self, clip):
                raise RuntimeError("boom")

        monkeypatch.setattr(ve, "_load_attempted", True)
        monkeypatch.setattr(ve, "_tagger", BrokenTagger())
        monkeypatch.setattr(ve, "_label_index", {"Laughter": 0})
        assert ve.detect_vocal_events(np.zeros(32000, dtype=np.float32), 32000) == {}
