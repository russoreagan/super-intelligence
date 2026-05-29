"""
Tests for the streaming-mic noise gate. The gate replaces low-energy
chunks with silence so background noise (HVAC, distant chatter, keyboard
clicks) doesn't reach Deepgram's VAD and trigger spurious utterances.
"""
from __future__ import annotations

import struct

from brain.streaming_mic import StreamingMicSession


def _make_mic(monkeypatch, threshold: float = 120.0):
    monkeypatch.setattr(StreamingMicSession, "NOISE_GATE_RMS", threshold)

    class _StubBus:
        def subscribe(self, *a, **kw): pass

        async def publish_dict(self, *a, **kw): pass

    mic = StreamingMicSession(_StubBus(), is_speaking_fn=lambda: False)
    # Sessions start muted (push-to-talk default); the gate tests below operate
    # on the unmuted path (the dedicated mute test flips _muted itself).
    mic._muted = False
    return mic


def _chunk(amplitude: int, n_samples: int = 1600) -> bytes:
    """Build a PCM chunk of `n_samples` int16 samples, all at +amplitude."""
    return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))


def test_silence_chunk_passes_unchanged(monkeypatch):
    # Silence is already below the gate, but it's also semantically "no audio" —
    # we still enqueue it so Deepgram's WS stays warm.
    mic = _make_mic(monkeypatch, threshold=120.0)
    chunk = b"\x00" * 3200
    mic._enqueue_chunk(chunk)
    assert not mic._pcm_in.empty()
    sent = mic._pcm_in.get_nowait()
    assert sent == chunk


def test_quiet_noise_replaced_with_silence(monkeypatch):
    # Low-amplitude background noise: amplitude 50 → RMS ~50, below 120 gate
    mic = _make_mic(monkeypatch, threshold=120.0)
    quiet_chunk = _chunk(amplitude=50)
    mic._enqueue_chunk(quiet_chunk)
    sent = mic._pcm_in.get_nowait()
    assert sent == b"\x00" * len(quiet_chunk)


def test_loud_speech_passes_through(monkeypatch):
    # Speech-level amplitude: 3000 → RMS ~3000, way above 120 gate
    mic = _make_mic(monkeypatch, threshold=120.0)
    speech_chunk = _chunk(amplitude=3000)
    mic._enqueue_chunk(speech_chunk)
    sent = mic._pcm_in.get_nowait()
    assert sent == speech_chunk


def test_threshold_zero_disables_gate(monkeypatch):
    # Threshold 0 → gate disabled → even quiet chunks pass through unchanged
    mic = _make_mic(monkeypatch, threshold=0.0)
    quiet_chunk = _chunk(amplitude=50)
    mic._enqueue_chunk(quiet_chunk)
    sent = mic._pcm_in.get_nowait()
    assert sent == quiet_chunk


def test_gate_applies_when_unmuted_not_when_muted(monkeypatch):
    # Mute takes precedence over the gate: muted chunks become silence
    # regardless of energy. Important so the user can't accidentally
    # "leak" via the noise gate.
    mic = _make_mic(monkeypatch, threshold=120.0)
    mic.mute()
    loud_chunk = _chunk(amplitude=3000)
    mic._enqueue_chunk(loud_chunk)
    sent = mic._pcm_in.get_nowait()
    assert sent == b"\x00" * len(loud_chunk)


def test_borderline_chunk_just_above_threshold_passes(monkeypatch):
    # A chunk whose RMS is comfortably above the gate should not be gated
    mic = _make_mic(monkeypatch, threshold=120.0)
    chunk = _chunk(amplitude=200)   # RMS = 200
    mic._enqueue_chunk(chunk)
    sent = mic._pcm_in.get_nowait()
    assert sent == chunk


def test_borderline_chunk_just_below_threshold_gated(monkeypatch):
    mic = _make_mic(monkeypatch, threshold=120.0)
    chunk = _chunk(amplitude=100)   # RMS = 100, below 120 gate
    mic._enqueue_chunk(chunk)
    sent = mic._pcm_in.get_nowait()
    assert sent == b"\x00" * len(chunk)
