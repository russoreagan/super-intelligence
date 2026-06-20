"""
Audio DSP utilities for the Auditory Cortex.

All functions are synchronous and CPU-bound — call via run_in_executor.
Three independent pipelines:
  1. Fingerprinting (Shazam-style): spectrogram → peaks → hashes → match
  2. Speaker ID (SpeechBrain ECAPA-TDNN): embedding → cosine similarity
  3. Prosody: pitch, energy, speech rate, jitter, shimmer → tone label
"""

from __future__ import annotations

import logging
import re as _re
import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# One-time guard so the "prosody disabled" warning logs once, not per-utterance.
_LOGGED_NO_LIBROSA = False

# ── Identity name patterns (for enrollment auto-detection) ────────────────────

_IDENTITY_PATTERNS = [
    _re.compile(
        r"(?:I'?m|my name(?:'?s| is)|I am|it'?s me[,\s]+|call me|I'?m called)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        _re.I,
    ),
    _re.compile(r"(?:it'?s|this is)\s+([A-Z][a-z]+)", _re.I),
    _re.compile(r"^([A-Z][a-z]{1,19})\.?\s*$"),  # bare name like "Russ"
]


def extract_identity_name(text: str) -> str | None:
    """Extract a person's self-identification name from text, or None."""
    for pat in _IDENTITY_PATTERNS:
        m = pat.search(text)
        if m:
            candidate = m.group(1).strip().title()
            if 2 <= len(candidate) <= 40:
                return candidate
    return None


# ── Constants (override via env vars) ─────────────────────────────────────────
STFT_NPERSEG = 1024
STFT_NOVERLAP = 512
PEAK_THRESHOLD = 1.5  # × per-frame mean to qualify as a peak
FAN_OUT_T_MIN = 2  # frames minimum offset for hash target
FAN_OUT_T_MAX = 80  # frames maximum offset for hash target
SILENCE_RMS = 0.01  # below this → treat as silence

# ── SpeechBrain model singleton ────────────────────────────────────────────────
_encoder = None
_encoder_lock = threading.Lock()


def _get_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder
    with _encoder_lock:
        if _encoder is not None:
            return _encoder
        try:
            from speechbrain.inference.speaker import EncoderClassifier

            logger.info("Auditory DSP: loading SpeechBrain ECAPA-TDNN speaker model…")
            _encoder = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cpu"},
            )
            logger.info("Auditory DSP: speaker model ready")
        except Exception as e:
            logger.warning("Auditory DSP: SpeechBrain unavailable (%s) — speaker ID disabled", e)
            _encoder = None
    return _encoder


# ── Audio decoding ─────────────────────────────────────────────────────────────


def extract_speaker_audio_segments(
    audio: np.ndarray,
    sr: int,
    words: list[dict],
    pad_s: float = 0.05,
) -> np.ndarray:
    """
    Concatenate audio samples corresponding to a single speaker's word timestamps.
    words: list of {"start": float, "end": float} dicts (seconds).
    Returns concatenated float32 audio, or an empty array if no valid segments.
    """
    segments = []
    for w in words:
        start = max(0, int((w.get("start", 0) - pad_s) * sr))
        end = min(len(audio), int((w.get("end", 0) + pad_s) * sr))
        if end > start:
            segments.append(audio[start:end])
    return np.concatenate(segments) if segments else np.array([], dtype=np.float32)


def decode_audio(audio_bytes: bytes, dtype: str = "int16") -> np.ndarray:
    """Convert raw PCM bytes to normalised float32 in [-1, 1]."""
    dt = np.dtype(dtype)
    arr = np.frombuffer(audio_bytes, dtype=dt)
    max_val = float(np.iinfo(dt).max) if np.issubdtype(dt, np.integer) else 1.0
    return arr.astype(np.float32) / max_val


# ── Pipeline 1: Fingerprinting ─────────────────────────────────────────────────


def compute_spectrogram(audio: np.ndarray, sr: int) -> np.ndarray:
    """STFT magnitude spectrogram, shape (freq_bins, time_frames)."""
    from scipy.signal import stft

    _, _, Zxx = stft(audio, fs=sr, nperseg=STFT_NPERSEG, noverlap=STFT_NOVERLAP)
    return np.abs(Zxx)


def extract_peaks(spec: np.ndarray) -> list[tuple[int, int]]:
    """
    Find locally prominent frequency peaks in each time frame.
    Returns list of (freq_bin, time_frame) pairs — the constellation map.
    """
    from scipy.signal import find_peaks

    peaks: list[tuple[int, int]] = []
    n_freqs, n_frames = spec.shape
    for t in range(n_frames):
        col = spec[:, t]
        threshold = col.mean() * PEAK_THRESHOLD
        idxs, _ = find_peaks(col, height=threshold, distance=5)
        for f in idxs:
            peaks.append((int(f), t))
    return peaks


def generate_hashes(peaks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Shazam-style combinatorial hash pairs.
    For each anchor peak, pair with target peaks in the fan-out window.
    Hash encodes (f_anchor, f_target, delta_t) → single int.
    Returns list of (hash_val, anchor_time_frame).
    """
    hashes: list[tuple[int, int]] = []
    n = len(peaks)
    for i, (f_a, t_a) in enumerate(peaks):
        for j in range(i + 1, n):
            f_b, t_b = peaks[j]
            dt = t_b - t_a
            if dt < FAN_OUT_T_MIN:
                continue
            if dt > FAN_OUT_T_MAX:
                break  # peaks are time-sorted, so we can break early
            h = int((f_a & 1023) << 20 | (f_b & 1023) << 10 | (dt & 1023))
            hashes.append((h, t_a))
    return hashes


def match_fingerprint(
    audio: np.ndarray,
    sr: int,
    fingerprint_db: dict[int, list[tuple[str, int]]],
) -> dict:
    """
    Full fingerprint pipeline: audio → match result dict.
    fingerprint_db maps hash_val → [(song_id, reference_time_frame), ...].
    Returns auditory.song_match payload.
    """
    if len(fingerprint_db) == 0:
        return {
            "matched": False,
            "song_id": None,
            "song_title": None,
            "confidence": 0.0,
            "match_count": 0,
            "query_hash_count": 0,
        }

    spec = compute_spectrogram(audio, sr)
    peaks = extract_peaks(spec)
    if not peaks:
        return {
            "matched": False,
            "song_id": None,
            "song_title": None,
            "confidence": 0.0,
            "match_count": 0,
            "query_hash_count": 0,
        }

    hashes = generate_hashes(peaks)
    if not hashes:
        return {
            "matched": False,
            "song_id": None,
            "song_title": None,
            "confidence": 0.0,
            "match_count": 0,
            "query_hash_count": 0,
        }

    # Time-coherent voting: true matches cluster at the same delta
    votes: dict[str, dict[int, int]] = {}
    for h, t_q in hashes:
        if h not in fingerprint_db:
            continue
        for song_id, t_ref in fingerprint_db[h]:
            delta = t_q - t_ref
            votes.setdefault(song_id, {}).setdefault(delta, 0)
            votes[song_id][delta] += 1

    if not votes:
        return {
            "matched": False,
            "song_id": None,
            "song_title": None,
            "confidence": 0.0,
            "match_count": 0,
            "query_hash_count": len(hashes),
        }

    best_song = max(votes, key=lambda s: max(votes[s].values()))
    best_count = max(votes[best_song].values())
    confidence = best_count / max(len(hashes), 1)

    return {
        "matched": confidence > 0.08,
        "song_id": best_song if confidence > 0.08 else None,
        "song_title": None,  # caller should look up from songs dict
        "confidence": float(confidence),
        "match_count": best_count,
        "query_hash_count": len(hashes),
        "_best_song_id": best_song,  # always set so caller can look up title
    }


# ── Pipeline 2: Speaker identification ────────────────────────────────────────


def extract_speaker_embedding(audio: np.ndarray, sr: int) -> np.ndarray | None:
    """
    Extract 192-dim speaker embedding using SpeechBrain ECAPA-TDNN.
    Returns L2-normalised numpy array, or None if model unavailable.
    """
    encoder = _get_encoder()
    if encoder is None:
        return None

    try:
        import torch

        # SpeechBrain expects (batch, time) tensor at 16kHz
        tensor = torch.tensor(audio, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            emb = encoder.encode_batch(tensor)  # (1, 1, 192)
        vec = emb.squeeze().numpy()  # (192,)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec
    except Exception as e:
        logger.debug("Auditory DSP: speaker embedding failed: %s", e)
        return None


# ── Pipeline 3: Prosody extraction ────────────────────────────────────────────

_smile = None
_smile_loaded = False
_smile_lock = threading.Lock()


def _get_smile():
    """Lazy singleton for openSMILE eGeMAPSv02 Functionals extractor."""
    global _smile, _smile_loaded
    if _smile_loaded:
        return _smile
    with _smile_lock:
        if _smile_loaded:
            return _smile
        try:
            import opensmile

            _smile = opensmile.Smile(
                feature_set=opensmile.FeatureSet.eGeMAPSv02,
                feature_level=opensmile.FeatureLevel.Functionals,
            )
            logger.info("Auditory DSP: openSMILE eGeMAPSv02 loaded")
        except Exception as e:
            logger.warning("Auditory DSP: openSMILE unavailable (%s) — using librosa fallback", e)
            _smile = None
        _smile_loaded = True
    return _smile


def extract_prosody(audio: np.ndarray, sr: int) -> dict:
    """
    Extract prosodic features from audio.
    Returns auditory.prosody payload dict.
    Gracefully degrades if librosa not installed.
    """
    duration = len(audio) / max(sr, 1)
    base = {
        "f0_mean_hz": 0.0,
        "f0_std_hz": 0.0,
        "energy_mean": 0.0,
        "energy_std": 0.0,
        "speech_rate_hz": 0.0,
        "jitter": 0.0,
        "shimmer": 0.0,
        "voiced_fraction": 0.0,
        "tone_label": "calm",
        "laughter_likelihood": 0.0,
    }

    # Quick energy check
    rms_global = float(np.sqrt(np.mean(audio**2)))
    base["energy_mean"] = rms_global

    if rms_global < SILENCE_RMS:
        base["tone_label"] = "silence"
        return base

    try:
        import librosa

        # ── Pitch (F0) via YIN ──
        f0 = librosa.yin(audio, fmin=70, fmax=450, sr=sr)
        voiced = f0[f0 > 0]
        if len(voiced) > 0:
            base["f0_mean_hz"] = float(np.mean(voiced))
            base["f0_std_hz"] = float(np.std(voiced))
            base["voiced_fraction"] = float(len(voiced) / len(f0))
        else:
            base["voiced_fraction"] = 0.0

        # ── Energy / loudness ──
        rms = librosa.feature.rms(
            y=audio, frame_length=STFT_NPERSEG, hop_length=STFT_NPERSEG - STFT_NOVERLAP
        )[0]
        base["energy_mean"] = float(np.mean(rms))
        base["energy_std"] = float(np.std(rms))

        # ── Speech rate via onset detection ──
        onsets = librosa.onset.onset_detect(y=audio, sr=sr, units="time")
        base["speech_rate_hz"] = float(len(onsets) / max(duration, 0.001))

        # ── Jitter (pitch period perturbation) ──
        if len(voiced) > 2:
            periods = 1.0 / voiced
            base["jitter"] = float(np.mean(np.abs(np.diff(periods))) / np.mean(periods))

        # ── Shimmer (amplitude perturbation) — librosa fallback ──
        if len(audio) > 10:
            from scipy.signal import find_peaks as _find_peaks

            peaks_idx, _ = _find_peaks(np.abs(audio), distance=max(1, sr // 500))
            if len(peaks_idx) > 2:
                amps = np.abs(audio[peaks_idx])
                base["shimmer"] = float(np.mean(np.abs(np.diff(amps))) / max(np.mean(amps), 1e-9))

    except ImportError:
        # Visible (once) so a missing dep degrades obviously to text-only affect
        # rather than silently. When openSMILE is also unavailable this means no
        # prosody/vocal-tone signal at all — worth surfacing.
        global _LOGGED_NO_LIBROSA
        if not _LOGGED_NO_LIBROSA:
            logger.warning(
                "Auditory DSP: librosa not installed — vocal-tone/prosody "
                "features disabled (emotion sensing falls back to text only)"
            )
            _LOGGED_NO_LIBROSA = True
    except Exception as e:
        logger.debug("Auditory DSP: prosody extraction error: %s", e)

    # ── openSMILE eGeMAPS: validated jitter, shimmer, F0 ──
    # Overwrites librosa estimates when available; librosa values above are the fallback.
    smile = _get_smile()
    if smile is not None:
        try:
            features_df = smile.process_signal(audio, sr)
            row = features_df.iloc[0]

            f0_st = float(row.get("F0semitoneFrom27.5Hz_sma3nz_amean", 0.0))
            if f0_st > 0:
                f0_hz = 27.5 * (2 ** (f0_st / 12))
                f0_std_norm = float(row.get("F0semitoneFrom27.5Hz_sma3nz_stddevNorm", 0.0))
                base["f0_mean_hz"] = f0_hz
                base["f0_std_hz"] = f0_std_norm * f0_hz

            jitter_val = float(row.get("jitterLocal_sma3nz_amean", 0.0))
            if jitter_val > 0:
                base["jitter"] = jitter_val

            shimmer_db = float(row.get("shimmerLocaldB_sma3nz_amean", 0.0))
            if shimmer_db > 0:
                base["shimmer"] = 10 ** (shimmer_db / 20) - 1

        except Exception as e:
            logger.debug("Auditory DSP: openSMILE extraction failed: %s", e)

    base["tone_label"] = label_prosody_tone(base)
    base["tone_strength"] = prosody_tone_strength(base, base["tone_label"])
    base["laughter_likelihood"] = laughter_likelihood(base)
    return base


def _prosody_thresholds(baseline: dict | None = None) -> tuple[float, float, float]:
    """Shared (jitter, shimmer, energy) thresholds for tone classification and
    strength scoring. Falls back to universal values; a calibrated speaker
    baseline (>= 10 obs) raises them proportionally to that person's own range."""
    calibrated = baseline is not None and baseline.get("count", 0) >= 10
    if calibrated:
        jitter_thresh = max(0.03, baseline["jitter"] * 1.8)
        shimmer_thresh = max(0.10, baseline["shimmer"] * 1.8)
        energy_thresh = max(0.12, baseline["energy_mean"] * 1.5)
    else:
        jitter_thresh = 0.03
        shimmer_thresh = 0.10
        energy_thresh = 0.12
    return jitter_thresh, shimmer_thresh, energy_thresh


def _overshoot(value: float, threshold: float) -> float:
    """How far `value` exceeds `threshold`, as a fraction of the threshold,
    clamped to [0,1] (value == 2×threshold → 1.0). 0 at or below threshold."""
    if threshold <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - threshold) / threshold))


def prosody_tone_strength(features: dict, label: str, baseline: dict | None = None) -> float:
    """Normalized [0,1] intensity of a prosodic tone — how far the feature(s)
    that *define* `label` exceed the threshold that produced it.

    This is the magnitude that `label_prosody_tone` discards: a slightly tense
    voice and a trembling one both label "stressed", but score ~0.1 vs ~0.9 here
    so downstream neuromod release can scale with degree, not just the category.

    Returns 0.0 for calm/monotone/silence (no defining excess to grade).
    """
    jitter_thresh, shimmer_thresh, energy_thresh = _prosody_thresholds(baseline)

    if label == "whisper":
        # Quieter (lower voiced fraction) = stronger whisper.
        vf = features.get("voiced_fraction", 0.0)
        return max(0.0, min(1.0, (0.25 - vf) / 0.25))
    if label == "energetic":
        e_mean = features.get("energy_mean", 0.0)
        rate = features.get("speech_rate_hz", 0.0)
        return 0.5 * (_overshoot(e_mean, energy_thresh) + _overshoot(rate, 4.0))
    if label == "stressed":
        jitter = features.get("jitter", 0.0)
        shimmer = features.get("shimmer", 0.0)
        return 0.5 * (_overshoot(jitter, jitter_thresh) + _overshoot(shimmer, shimmer_thresh))
    return 0.0


def laughter_likelihood(features: dict, baseline: dict | None = None) -> float:
    """
    Graded [0,1] likelihood that the segment contains laughter, from features
    extract_prosody already computes — no extra DSP passes.

    Laughter's acoustic signature: rhythmic energy bursts at ~4–6 Hz (the
    "ha-ha-ha" pulse train, visible as speech_rate_hz in that band with high
    energy_std), a high voiced fraction (laughs are voiced exhalations), and
    elevated, highly variable pitch (f0 well above speech with large f0_std).

    Deliberately conservative: ALL four components must register (each above a
    floor) or the score is 0.0, so merely animated/energetic speech — which
    shares the rate and energy bands but not the pitch signature — does not
    false-positive. Downstream additionally gates on settings
    "laughter_dsp_threshold" before any chemistry is released.

    Like label_prosody_tone, a calibrated per-speaker `baseline` (>= 10 obs)
    raises the energy/pitch-variability thresholds proportionally to that
    person's own normal range.
    """
    vf = features.get("voiced_fraction", 0.0)
    f0_mean = features.get("f0_mean_hz", 0.0)
    f0_std = features.get("f0_std_hz", 0.0)
    e_std = features.get("energy_std", 0.0)
    rate = features.get("speech_rate_hz", 0.0)

    # Universal thresholds; a calibrated speaker baseline raises them so an
    # expressive talker's normal variability doesn't read as laughter.
    energy_std_thresh = 0.04
    f0_std_thresh = 40.0
    calibrated = baseline is not None and baseline.get("count", 0) >= 10
    if calibrated:
        energy_std_thresh = max(energy_std_thresh, baseline.get("energy_mean", 0.0) * 0.5)
        f0_std_thresh = max(f0_std_thresh, baseline.get("f0_std", 0.0) * 1.5)

    # Rhythmic syllable bursts peaked at ~5 Hz, zero outside ~3.5–7 Hz.
    rhythm = max(0.0, 1.0 - abs(rate - 5.0) / 2.0) if 3.5 <= rate <= 7.0 else 0.0
    burst = _overshoot(e_std, energy_std_thresh)
    voiced = max(0.0, min(1.0, (vf - 0.5) / 0.3))
    # Pitch component requires elevated mean f0 — laughter sits above the
    # speaking register — AND large pitch variability.
    pitch = _overshoot(f0_std, f0_std_thresh) if f0_mean > 150.0 else 0.0

    # Conservative AND-gate: every component must clear a floor, otherwise
    # this is just lively speech missing part of the signature.
    _floor = 0.2
    if min(rhythm, burst, voiced, pitch) < _floor:
        return 0.0
    return min(1.0, 0.35 * rhythm + 0.25 * burst + 0.2 * voiced + 0.2 * pitch)


def label_prosody_tone(features: dict, baseline: dict | None = None) -> str:
    """
    Classify a prosody feature dict into a tone label.

    If `baseline` is provided and has >= 10 observations, thresholds for
    stressed and energetic are computed relative to that speaker's personal
    baseline rather than universal values. This lets the system adapt to each
    person's natural speaking style over time.

    baseline dict shape: {"jitter": float, "shimmer": float,
                          "energy_mean": float, "f0_std": float, "count": int}
    """
    vf = features.get("voiced_fraction", 0.0)
    f0_std = features.get("f0_std_hz", 0.0)
    e_mean = features.get("energy_mean", 0.0)
    e_std = features.get("energy_std", 0.0)
    rate = features.get("speech_rate_hz", 0.0)
    jitter = features.get("jitter", 0.0)
    shimmer = features.get("shimmer", 0.0)

    # Thresholds — fall back to universal values; baseline raises them
    # proportionally to the speaker's own normal range (shared with
    # prosody_tone_strength so label and magnitude stay consistent).
    jitter_thresh, shimmer_thresh, energy_thresh = _prosody_thresholds(baseline)

    if vf < 0.25:
        return "whisper"
    elif f0_std < 15.0 and e_std < 0.02:
        return "monotone"
    elif e_mean > energy_thresh and rate > 4.0:
        return "energetic"
    elif jitter > jitter_thresh and shimmer > shimmer_thresh:
        # Require both to be elevated — either alone is normal variation.
        return "stressed"
    else:
        return "calm"


# ── Music feature extraction ───────────────────────────────────────────────────

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def extract_music_features(audio: np.ndarray, sr: int) -> dict:
    """Extract music-domain features from a raw PCM buffer.

    Runs entirely in librosa — no additional deps beyond what prosody already
    needs. Returns auditory.music payload dict.

    Features:
      bpm                  Estimated tempo in beats per minute
      bpm_confidence       Beat strength 0–1
      key                  Most likely tonic: 'C', 'C#', … 'B'
      mode                 'major' | 'minor'
      spectral_centroid    Mean spectral centroid in Hz (brightness)
      spectral_rolloff     Mean 85% rolloff in Hz (spectral weight)
      rms_mean             Mean RMS energy
      rms_std              RMS standard deviation (dynamic range)
      onset_rate           Onsets per second (rhythmic density)
      mfcc_mean            First 13 MFCCs (timbre fingerprint)
      mood_label           energetic | bright | tense | melancholic | calm
    """
    base: dict = {
        "bpm": 0.0,
        "bpm_confidence": 0.0,
        "key": "C",
        "mode": "major",
        "spectral_centroid": 0.0,
        "spectral_rolloff": 0.0,
        "rms_mean": float(np.sqrt(np.mean(audio**2))),
        "rms_std": 0.0,
        "onset_rate": 0.0,
        "mfcc_mean": [],
        "mood_label": "calm",
    }

    if base["rms_mean"] < SILENCE_RMS:
        return base

    try:
        import librosa

        # ── Tempo / BPM ──
        # onset_envelope gives a smoother tempo estimate than raw beat_track on
        # short clips; pulse strength from the autocorrelation peak is confidence.
        onset_env = librosa.onset.onset_strength(y=audio, sr=sr)
        tempo_arr, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
        bpm = float(np.atleast_1d(tempo_arr)[0])
        conf = (
            float(np.mean(onset_env[beats]) / (np.max(onset_env) + 1e-9)) if len(beats) > 1 else 0.0
        )
        base["bpm"] = bpm
        base["bpm_confidence"] = round(conf, 3)

        # ── Key + Mode via chroma ──
        # CQT chroma is more key-stable than STFT chroma on short clips.
        chroma = librosa.feature.chroma_cqt(y=audio, sr=sr)
        chroma_mean = chroma.mean(axis=1)  # (12,) — C through B
        key_idx = int(np.argmax(chroma_mean))
        base["key"] = _NOTE_NAMES[key_idx]
        # Major vs minor: compare energy of major 3rd (4 semitones) vs minor 3rd (3 semitones)
        major_3rd = float(chroma_mean[(key_idx + 4) % 12])
        minor_3rd = float(chroma_mean[(key_idx + 3) % 12])
        base["mode"] = "major" if major_3rd >= minor_3rd else "minor"

        # ── Spectral shape ──
        sc = librosa.feature.spectral_centroid(y=audio, sr=sr)[0]
        base["spectral_centroid"] = float(np.mean(sc))
        ro = librosa.feature.spectral_rolloff(y=audio, sr=sr, roll_percent=0.85)[0]
        base["spectral_rolloff"] = float(np.mean(ro))

        # ── Energy dynamics ──
        rms_frames = librosa.feature.rms(y=audio, hop_length=512)[0]
        base["rms_mean"] = float(np.mean(rms_frames))
        base["rms_std"] = float(np.std(rms_frames))

        # ── Rhythmic density ──
        duration = len(audio) / max(sr, 1)
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="time")
        base["onset_rate"] = float(len(onsets) / max(duration, 0.001))

        # ── Timbre (MFCCs 1–13) ──
        mfccs = librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=13)
        base["mfcc_mean"] = [round(float(v), 3) for v in mfccs.mean(axis=1)]

        base["mood_label"] = _label_music_mood(base)

    except ImportError:
        pass
    except Exception as e:
        logger.debug("Auditory DSP: music feature extraction error: %s", e)

    return base


def _label_music_mood(f: dict) -> str:
    """Map music features to a mood label.

    BPM and mode are the primary signals; spectral centroid refines within each
    quadrant. Deliberately coarse — intended as a soft neuromod hint, not a
    music-theory classifier.
    """
    bpm = f.get("bpm", 0.0)
    mode = f.get("mode", "major")
    energy = f.get("rms_mean", 0.0)
    centroid = f.get("spectral_centroid", 0.0)

    if energy < SILENCE_RMS * 10:
        return "calm"

    if mode == "minor":
        if bpm > 100 or centroid > 3000:
            return "tense"
        return "melancholic"

    # major
    if bpm > 120 and energy > 0.05:
        return "energetic"
    if centroid > 3500 or bpm > 100:
        return "bright"
    return "calm"


# Threshold (seconds) above which an inter-word gap counts as a "long pause"
_LONG_PAUSE_S = 0.5


def compute_speech_dynamics(diarized_words: list[dict]) -> dict:
    """pace_switch + pause_distribution_switch (PLAN.md): convert Deepgram word
    timestamps into pace + pause-shape features.

    Returns dict with:
      wpm:               words per minute (0 if not computable)
      pace_label:        halting | measured | normal | brisk | rushed
      long_pause_count:  inter-word gaps > 0.5s
      max_pause_s:       biggest mid-utterance gap
      burst_score:       std-dev of inter-word gaps (high = bursty/agitated)
      hesitant:          true if many long pauses for the utterance length
    """
    base = {
        "wpm": 0.0,
        "pace_label": "normal",
        "pace_strength": 0.0,
        "long_pause_count": 0,
        "max_pause_s": 0.0,
        "burst_score": 0.0,
        "hesitant": False,
    }
    if not diarized_words or len(diarized_words) < 2:
        return base

    words = [w for w in diarized_words if w.get("word")]
    if len(words) < 2:
        return base

    starts = [float(w.get("start", 0.0)) for w in words]
    ends = [float(w.get("end", 0.0)) for w in words]
    duration = max(ends[-1] - starts[0], 0.001)

    base["wpm"] = float(60.0 * len(words) / duration)

    gaps = [max(starts[i + 1] - ends[i], 0.0) for i in range(len(words) - 1)]
    if gaps:
        base["max_pause_s"] = float(max(gaps))
        base["long_pause_count"] = int(sum(1 for g in gaps if g > _LONG_PAUSE_S))
        mean = sum(gaps) / len(gaps)
        var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        base["burst_score"] = float(var**0.5)

    # pace_label + pace_strength: how deep into the (non-normal) band the speaker
    # is, 0→1, so neuromod release can scale with degree. Strength runs from 0 at
    # the band edge nearest "normal" to 1 at the far edge (rushed/halting are
    # open-ended, normalized against the band width / a reference span).
    wpm = base["wpm"]
    if wpm < 90:  # halting — slower = stronger
        base["pace_label"] = "halting"
        base["pace_strength"] = min(1.0, (90 - wpm) / 90)
    elif wpm < 130:  # measured
        base["pace_label"] = "measured"
        base["pace_strength"] = min(1.0, (130 - wpm) / 40)
    elif wpm < 170:  # normal — no defining excess
        base["pace_label"] = "normal"
        base["pace_strength"] = 0.0
    elif wpm < 220:  # brisk
        base["pace_label"] = "brisk"
        base["pace_strength"] = min(1.0, (wpm - 170) / 50)
    else:  # rushed — faster = stronger
        base["pace_label"] = "rushed"
        base["pace_strength"] = min(1.0, (wpm - 220) / 220)

    # "hesitant" — long pauses dominate the utterance shape
    base["hesitant"] = bool(
        base["long_pause_count"] >= 2 and base["long_pause_count"] / max(len(gaps), 1) >= 0.3
    )

    return base
