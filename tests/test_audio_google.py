"""Unit tests for the additive Google voice providers in brain/api/audio.py.

All HTTP is mocked — no network, no billing. Covers the Chirp 3 HD TTS branch,
the STT provider dispatch (Deepgram default, Google opt-in), the mimetype→encoding
map, and that the feature is purely additive (default paths untouched).
"""

from __future__ import annotations

import asyncio
import base64
import struct

import pytest

from brain.api import audio


def _fake_wav(pcm: bytes = b"\x01\x02" * 200, rate: int = 24000) -> bytes:
    n = len(pcm)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + n)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", n)
        + pcm
    )


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._p = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._p

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._r = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self._r

    async def get(self, *a, **k):
        return self._r


def _patch_httpx(monkeypatch, resp: _Resp) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(resp))


# ── TTS ────────────────────────────────────────────────────────────────────────
def test_tts_google_requires_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(audio.AudioError) as ei:
        asyncio.run(audio.synthesize("hello", provider="google"))
    assert ei.value.status == 503


def test_tts_google_synthesizes_mood_segments(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    payload = {"audioContent": base64.b64encode(_fake_wav()).decode("ascii")}
    _patch_httpx(monkeypatch, _Resp(200, payload))
    out = asyncio.run(
        audio.synthesize(
            "[mood:excited]Big news![/mood] Calmer now.",
            provider="google",
            affect={"emotion": "calm"},
        )
    )
    assert out["format"] == "pcm_24000"
    assert out["model"] == "chirp3-hd"
    assert len(out["segments"]) == 2
    # excited span runs faster than the calm-affect remainder
    rates = [s["voice_settings"]["speaking_rate"] for s in out["segments"]]
    assert rates[0] == 1.10 and rates[1] == 0.94
    assert out["data"]  # base64 PCM, header stripped


def test_tts_google_upstream_error_raises(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    _patch_httpx(monkeypatch, _Resp(403, {}, text="PERMISSION_DENIED"))
    with pytest.raises(audio.AudioError) as ei:
        asyncio.run(audio.synthesize("hi", provider="google"))
    assert ei.value.status == 502


# ── STT ────────────────────────────────────────────────────────────────────────
def test_stt_dispatch_default_is_deepgram(monkeypatch):
    """No provider + no STT_PROVIDER → Deepgram path (503 without its key proves
    the default route, not a Google fallthrough)."""
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(audio.AudioError) as ei:
        asyncio.run(audio.transcribe(b"abc", mimetype="audio/wav"))
    assert ei.value.status == 503 and "DEEPGRAM" in ei.value.detail


def test_stt_google_transcribes(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    payload = {
        "results": [
            {"alternatives": [{"transcript": "hello there"}]},
            {"alternatives": [{"transcript": "general kenobi"}]},
        ]
    }
    _patch_httpx(monkeypatch, _Resp(200, payload))
    out = asyncio.run(audio.transcribe(b"abc", provider="google", mimetype="audio/wav"))
    assert out["transcript"] == "hello there general kenobi"
    assert out["segments"] == [{"transcript": "hello there general kenobi", "is_final": True}]


def test_stt_google_requires_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(audio.AudioError) as ei:
        asyncio.run(audio.transcribe(b"abc", provider="google"))
    assert ei.value.status == 503


@pytest.mark.parametrize(
    "mime,enc,rate",
    [
        ("audio/webm;codecs=opus", "WEBM_OPUS", 48000),
        ("audio/ogg", "OGG_OPUS", 48000),
        ("audio/flac", "FLAC", None),
        ("audio/wav", None, None),
        ("audio/l16", "LINEAR16", 16000),
    ],
)
def test_google_stt_encoding_map(mime, enc, rate):
    assert audio._google_stt_encoding(mime) == (enc, rate)


# ── additive guarantee ──────────────────────────────────────────────────────────
def test_google_voice_settings_present():
    from brain import settings as S

    assert S.DEFAULTS["google_tts_voice"].startswith("en-US-Chirp3-HD")
    assert S.DEFAULTS["tts_provider"] == "elevenlabs"  # default unchanged
    assert S.DEFAULTS["stt_provider"] == "deepgram"  # default unchanged
