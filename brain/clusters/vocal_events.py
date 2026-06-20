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

# Where panns_inference expects its checkpoint, and the canonical source.
# We pre-fetch it OURSELVES because the library shells out to wget (absent on
# macOS, flaky in containers) and leaves corrupt partials behind on failure.
_CKPT_PATH_ENV = "BRAIN_PANNS_CHECKPOINT"
_CKPT_URL = "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
_CKPT_BYTES = 327_428_481  # exact size — anything else is a corrupt partial


def _checkpoint_path():
    import os
    from pathlib import Path

    override = os.environ.get(_CKPT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / "panns_data" / "Cnn14_mAP=0.431.pth"


def _ensure_checkpoint() -> str | None:
    """Make sure the CNN14 checkpoint exists and is complete; download with
    urllib if not (Python-native — works on macOS, Railway, RunPod alike).
    Returns the path, or None when the download failed (caller disables)."""
    import urllib.request

    path = _checkpoint_path()
    if path.exists() and path.stat().st_size == _CKPT_BYTES:
        return str(path)
    if path.exists():
        logger.warning(
            "Vocal events: checkpoint at %s is %d bytes (expected %d) — corrupt partial, refetching",
            path,
            path.stat().st_size,
            _CKPT_BYTES,
        )
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pth.partial")
    logger.info("Vocal events: downloading PANNs CNN14 checkpoint (~312 MB, one-time) …")
    try:
        # _CKPT_URL is a hardcoded https constant (the PANNs checkpoint), not user input.
        urllib.request.urlretrieve(_CKPT_URL, tmp)  # nosec B310
        if tmp.stat().st_size != _CKPT_BYTES:
            raise OSError(f"downloaded {tmp.stat().st_size} bytes, expected {_CKPT_BYTES}")
        tmp.replace(path)
        logger.info("Vocal events: checkpoint ready at %s", path)
        return str(path)
    except Exception as e:
        logger.warning("Vocal events: checkpoint download failed (%s) — classifier disabled", e)
        tmp.unlink(missing_ok=True)
        return None


def _get_tagger():
    """Lazy-load the PANNs tagger once; on any failure disable permanently
    (fail-soft: a missing optional dep must never break the audio path)."""
    global _tagger, _label_index, _load_attempted
    if not _load_attempted:
        _load_attempted = True
        try:
            from panns_inference import AudioTagging, labels

            ckpt = _ensure_checkpoint()
            if ckpt is None:
                raise RuntimeError("checkpoint unavailable")
            _tagger = AudioTagging(checkpoint_path=ckpt, device="cpu")
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
