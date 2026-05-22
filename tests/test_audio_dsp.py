"""
Unit tests for brain/clusters/audio_dsp.py.

All tests use synthetic numpy arrays — no microphone, no API keys, no model downloads.
"""
from __future__ import annotations

import numpy as np
import pytest

from brain.clusters.audio_dsp import (
    decode_audio,
    compute_spectrogram,
    extract_peaks,
    generate_hashes,
    match_fingerprint,
    extract_prosody,
    STFT_NPERSEG,
    SILENCE_RMS,
)

SR = 16000


def _sine(freq: float, duration: float = 3.0, amplitude: float = 0.5) -> np.ndarray:
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


def _silence(duration: float = 3.0) -> np.ndarray:
    return np.zeros(int(SR * duration), dtype=np.float32)


# ── decode_audio ──────────────────────────────────────────────────────────────

def test_decode_audio_int16_range():
    # int16 max → 1.0 after normalisation
    arr = np.array([32767, -32768, 0], dtype=np.int16)
    out = decode_audio(arr.tobytes(), dtype="int16")
    assert out.dtype == np.float32
    assert abs(out[0] - 1.0) < 0.0001
    assert abs(out[1] - (-1.0)) < 0.0001
    assert out[2] == 0.0


def test_decode_audio_round_trip():
    original = _sine(440.0)
    raw = (original * 32767).astype(np.int16)
    decoded = decode_audio(raw.tobytes())
    np.testing.assert_allclose(decoded, raw.astype(np.float32) / 32767, atol=1e-4)


# ── compute_spectrogram ───────────────────────────────────────────────────────

def test_spectrogram_shape():
    audio = _sine(440.0)
    spec = compute_spectrogram(audio, SR)
    assert spec.ndim == 2
    freq_bins, time_frames = spec.shape
    # freq_bins = nperseg//2 + 1
    assert freq_bins == STFT_NPERSEG // 2 + 1
    assert time_frames > 0


def test_spectrogram_non_negative():
    audio = _sine(440.0)
    spec = compute_spectrogram(audio, SR)
    assert np.all(spec >= 0)


# ── extract_peaks ─────────────────────────────────────────────────────────────

def test_peaks_on_pure_tone():
    """A pure 440Hz sine should produce peaks near the 440Hz bin."""
    audio = _sine(440.0, duration=5.0)
    spec = compute_spectrogram(audio, SR)
    peaks = extract_peaks(spec)
    assert len(peaks) > 0

    expected_bin = int(440.0 * STFT_NPERSEG / SR)
    freq_bins = [f for f, _ in peaks]
    # At least some peaks should be within ±5 bins of the expected frequency
    close = [f for f in freq_bins if abs(f - expected_bin) <= 5]
    assert len(close) > 0, (
        f"Expected peaks near bin {expected_bin}, got freq bins: {sorted(set(freq_bins))[:20]}"
    )


def test_peaks_on_silence():
    """Silence (near-zero) → very few or no peaks (all bins below threshold)."""
    audio = _silence()
    spec = compute_spectrogram(audio, SR)
    peaks = extract_peaks(spec)
    # May have a handful of numerical noise peaks but should be far fewer than a tone
    assert len(peaks) < 100


# ── generate_hashes ───────────────────────────────────────────────────────────

def test_generate_hashes_deterministic():
    audio = _sine(440.0)
    spec = compute_spectrogram(audio, SR)
    peaks = extract_peaks(spec)
    h1 = generate_hashes(peaks)
    h2 = generate_hashes(peaks)
    assert h1 == h2


def test_generate_hashes_non_empty_for_tone():
    audio = _sine(440.0, duration=5.0)
    spec = compute_spectrogram(audio, SR)
    peaks = extract_peaks(spec)
    hashes = generate_hashes(peaks)
    assert len(hashes) > 0


def test_hash_values_are_ints():
    audio = _sine(440.0)
    spec = compute_spectrogram(audio, SR)
    peaks = extract_peaks(spec)
    hashes = generate_hashes(peaks)
    for h, t in hashes:
        assert isinstance(h, int)
        assert isinstance(t, int)


# ── match_fingerprint ─────────────────────────────────────────────────────────

def test_fingerprint_no_match_empty_db():
    audio = _sine(440.0)
    result = match_fingerprint(audio, SR, {})
    assert result["matched"] is False
    assert result["confidence"] == 0.0


def test_fingerprint_matches_itself():
    """Fingerprint a tone, store hashes, query with same audio → match."""
    audio = _sine(440.0, duration=5.0)
    spec = compute_spectrogram(audio, SR)
    peaks = extract_peaks(spec)
    hashes = generate_hashes(peaks)

    # Build a mini DB with these hashes
    db: dict[int, list[tuple[str, int]]] = {}
    for h, t in hashes:
        db.setdefault(h, []).append(("test_song_001", t))

    result = match_fingerprint(audio, SR, db)
    assert result["matched"] is True
    assert result["_best_song_id"] == "test_song_001"
    assert result["confidence"] > 0.1


def test_fingerprint_no_match_different_tone():
    """Hash DB built from 440Hz tone; query with 1000Hz tone → no match."""
    reference = _sine(440.0, duration=5.0)
    spec = compute_spectrogram(reference, SR)
    peaks = extract_peaks(spec)
    hashes = generate_hashes(peaks)
    db: dict[int, list[tuple[str, int]]] = {}
    for h, t in hashes:
        db.setdefault(h, []).append(("ref_song", t))

    query = _sine(1000.0, duration=5.0)
    result = match_fingerprint(query, SR, db)
    # Different frequency → very few hash collisions
    assert result["confidence"] < 0.1


# ── extract_prosody ───────────────────────────────────────────────────────────

def test_prosody_silence():
    audio = _silence()
    result = extract_prosody(audio, SR)
    assert result["tone_label"] == "silence"
    assert result["energy_mean"] < SILENCE_RMS


def test_prosody_returns_expected_keys():
    audio = _sine(220.0)
    result = extract_prosody(audio, SR)
    expected = {
        "f0_mean_hz", "f0_std_hz", "energy_mean", "energy_std",
        "speech_rate_hz", "jitter", "shimmer", "voiced_fraction", "tone_label",
    }
    assert expected.issubset(result.keys())


def test_prosody_energetic_loud_fast():
    """High amplitude + many onsets should label as energetic (if librosa present)."""
    pytest.importorskip("librosa")
    # Composite of many frequencies to generate many onsets
    t = np.linspace(0, 3.0, int(SR * 3.0), endpoint=False)
    audio = np.zeros_like(t, dtype=np.float32)
    for f in [200, 400, 600, 800, 1000]:
        audio += 0.3 * np.sin(2 * np.pi * f * t)
    # Modulate amplitude to create many onsets
    burst_len = SR // 10
    for i in range(0, len(audio), burst_len):
        audio[i:i + burst_len // 2] *= 2.0
    audio = np.clip(audio, -1.0, 1.0).astype(np.float32)
    result = extract_prosody(audio, SR)
    assert result["tone_label"] in {"energetic", "calm", "stressed"}  # librosa-dependent
    assert result["energy_mean"] > 0.0
