"""
Vocal Events — general non-speech vocal-event detector (laughter, sigh, gasp, crying).

Third tier of voice laughter detection (above transcript sniffing and the DSP
heuristic in audio_dsp.laughter_likelihood), but deliberately general: the same
classifier pass yields sigh/gasp/crying probabilities for future appraisal paths.

Pluggable backend, fail-soft:
  - panns_inference (PANNs CNN14 audio tagger, AudioSet labels). torch is already
    a dependency via speechbrain's ECAPA speaker embeddings; panns_inference is an
    OPTIONAL extra — if it isn't installed the detector quietly disables itself
    and detect_vocal_events() returns {}.

Gated behind the `vocal_events` settings flag (default 0/off). Hooked into
auditory_cortex._process_raw on the same waveform as extract_prosody, in an
executor (model inference is blocking). Event probabilities are merged into the
auditory.prosody payload under "vocal_events"; hypothalamus consumes "laughter"
through the levity-scaled DA path.

No LLM calls — local model inference only.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

# PANNs CNN14 is trained on 32 kHz audio.
_PANNS_SR = 32000

# AudioSet label names contributing to each event, max-pooled per event.
EVENT_LABELS: dict[str, tuple[str, ...]] = {
    "laughter": ("Laughter", "Giggle", "Chuckle, chortle", "Snicker", "Belly laugh"),
    "sigh": ("Sigh",),
    "gasp": ("Gasp",),
    "crying": ("Crying, sobbing", "Whimper"),
}

_tagger = None
_label_index: dict[str, int] | None = None
_load_attempted = False


def _get_tagger():
    """Lazy-load the PANNs tagger once; on any failure disable permanently
    (fail-soft: a missing optional dep must never break the audio path)."""
    global _tagger, _label_index, _load_attempted
    if not _load_attempted:
        _load_attempted = True
        try:
            from panns_inference import AudioTagging, labels

            _tagger = AudioTagging(checkpoint_path=None, device="cpu")
            _label_index = {name: i for i, name in enumerate(labels)}
        except Exception as e:
            logger.warning(
                "Vocal events: panns_inference unavailable (%s) — "
                "vocal-event classifier disabled (DSP/transcript laughter tiers still active)",
                e,
            )
            _tagger = None
            _label_index = None
    return _tagger


def available() -> bool:
    """True if the classifier backend loaded. NOTE: first call may block on
    model load — call from an executor, not the event loop."""
    return _get_tagger() is not None


def detect_vocal_events(audio: np.ndarray, sr: int) -> dict:
    """
    Classify non-speech vocal events in a raw mono PCM buffer.

    Returns {"laughter": p, "sigh": p, "gasp": p, "crying": p} with each p in
    [0,1], or {} when the backend is unavailable or inference fails. Blocking
    (torch inference + possible resample) — run in an executor.
    """
    tagger = _get_tagger()
    if tagger is None or _label_index is None:
        return {}

    try:
        clip = np.asarray(audio, dtype=np.float32)
        if clip.ndim > 1:
            clip = clip.mean(axis=-1)
        if sr != _PANNS_SR:
            import librosa

            clip = librosa.resample(clip, orig_sr=sr, target_sr=_PANNS_SR)

        clipwise_output, _embedding = tagger.inference(clip[None, :])
        probs = np.asarray(clipwise_output)[0]

        out: dict[str, float] = {}
        for event, names in EVENT_LABELS.items():
            out[event] = float(
                max(
                    (probs[_label_index[n]] for n in names if n in _label_index),
                    default=0.0,
                )
            )
        return out
    except Exception as e:
        logger.debug("Vocal events: inference failed: %s", e)
        return {}
