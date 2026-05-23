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
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Identity name patterns (for enrollment auto-detection) ────────────────────
import re as _re

_IDENTITY_PATTERNS = [
    _re.compile(r"(?:I'?m|my name(?:'?s| is)|I am|it'?s me[,\s]+|call me|I'?m called)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", _re.I),
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
PEAK_THRESHOLD = 1.5       # × per-frame mean to qualify as a peak
FAN_OUT_T_MIN = 2          # frames minimum offset for hash target
FAN_OUT_T_MAX = 80         # frames maximum offset for hash target
SILENCE_RMS = 0.01         # below this → treat as silence

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
        return {"matched": False, "song_id": None, "song_title": None,
                "confidence": 0.0, "match_count": 0, "query_hash_count": 0}

    spec = compute_spectrogram(audio, sr)
    peaks = extract_peaks(spec)
    if not peaks:
        return {"matched": False, "song_id": None, "song_title": None,
                "confidence": 0.0, "match_count": 0, "query_hash_count": 0}

    hashes = generate_hashes(peaks)
    if not hashes:
        return {"matched": False, "song_id": None, "song_title": None,
                "confidence": 0.0, "match_count": 0, "query_hash_count": 0}

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
        return {"matched": False, "song_id": None, "song_title": None,
                "confidence": 0.0, "match_count": 0, "query_hash_count": len(hashes)}

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
    }

    # Quick energy check
    rms_global = float(np.sqrt(np.mean(audio ** 2)))
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
        rms = librosa.feature.rms(y=audio, frame_length=STFT_NPERSEG,
                                   hop_length=STFT_NPERSEG - STFT_NOVERLAP)[0]
        base["energy_mean"] = float(np.mean(rms))
        base["energy_std"] = float(np.std(rms))

        # ── Speech rate via onset detection ──
        onsets = librosa.onset.onset_detect(y=audio, sr=sr, units="time")
        base["speech_rate_hz"] = float(len(onsets) / max(duration, 0.001))

        # ── Jitter (pitch period perturbation) ──
        if len(voiced) > 2:
            periods = 1.0 / voiced
            base["jitter"] = float(
                np.mean(np.abs(np.diff(periods))) / np.mean(periods)
            )

        # ── Shimmer (amplitude perturbation) ──
        if len(audio) > 10:
            from scipy.signal import find_peaks as _find_peaks
            peaks_idx, _ = _find_peaks(np.abs(audio), distance=max(1, sr // 500))
            if len(peaks_idx) > 2:
                amps = np.abs(audio[peaks_idx])
                base["shimmer"] = float(
                    np.mean(np.abs(np.diff(amps))) / max(np.mean(amps), 1e-9)
                )

    except ImportError:
        logger.debug("Auditory DSP: librosa not installed — prosody features limited")
    except Exception as e:
        logger.debug("Auditory DSP: prosody extraction error: %s", e)

    # ── Tone label (switch tree, no LLM) ──
    vf = base["voiced_fraction"]
    f0_std = base["f0_std_hz"]
    e_mean = base["energy_mean"]
    e_std = base["energy_std"]
    rate = base["speech_rate_hz"]
    jitter = base["jitter"]
    shimmer = base["shimmer"]

    if vf < 0.25:
        base["tone_label"] = "whisper"
    elif f0_std < 15.0 and e_std < 0.02:
        base["tone_label"] = "monotone"
    elif e_mean > 0.12 and rate > 4.0:
        base["tone_label"] = "energetic"
    elif jitter > 0.03 or shimmer > 0.05:
        base["tone_label"] = "stressed"
    else:
        base["tone_label"] = "calm"

    return base


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
        base["burst_score"] = float(var ** 0.5)

    wpm = base["wpm"]
    if wpm < 90:
        base["pace_label"] = "halting"
    elif wpm < 130:
        base["pace_label"] = "measured"
    elif wpm < 170:
        base["pace_label"] = "normal"
    elif wpm < 220:
        base["pace_label"] = "brisk"
    else:
        base["pace_label"] = "rushed"

    # "hesitant" — long pauses dominate the utterance shape
    base["hesitant"] = bool(
        base["long_pause_count"] >= 2
        and base["long_pause_count"] / max(len(gaps), 1) >= 0.3
    )

    return base
