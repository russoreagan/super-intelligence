"""
Per-partner audio quota — the AudioQuota tracker and its enforcement on the
engine API audio routes.

Provider-native meters: TTS by characters, STT by input audio-seconds. Caps come
from settings (0 = unlimited); enforcement is "refuse when already over, record
actual after". Owner keys are never metered. Persistence is multitenant-only, so
these tests (no BRAIN_MULTITENANT) stay fully in-memory.
"""

from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.audio_quota import STT_SECONDS, TTS_CHARS, AudioQuota
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.settings import settings


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _set_caps(monkeypatch, *, tts=0, stt=0, window=86400.0):
    monkeypatch.setitem(settings._data, "audio_tts_chars_per_window", tts)
    monkeypatch.setitem(settings._data, "audio_stt_seconds_per_window", stt)
    monkeypatch.setitem(settings._data, "audio_quota_window_s", window)


# ── AudioQuota unit ───────────────────────────────────────────────────────────
def test_unlimited_when_cap_zero(monkeypatch):
    _set_caps(monkeypatch, tts=0)
    q = AudioQuota(now_fn=_Clock())
    q.record("partnerA", TTS_CHARS, 10_000)
    assert q.check("partnerA", TTS_CHARS) is None  # 0 cap = unlimited


def test_owner_never_metered(monkeypatch):
    _set_caps(monkeypatch, tts=5)
    q = AudioQuota(now_fn=_Clock())
    assert q.check(None, TTS_CHARS) is None  # owner (partner_id None) bypasses
    q.record(None, TTS_CHARS, 9999)  # no-op
    assert q.window_total("anyone", TTS_CHARS) == 0


def test_blocks_once_over_cap(monkeypatch):
    _set_caps(monkeypatch, tts=100)
    q = AudioQuota(now_fn=_Clock())
    assert q.check("A", TTS_CHARS) is None
    q.record("A", TTS_CHARS, 100)  # now at cap
    reason = q.check("A", TTS_CHARS)
    assert reason and "quota reached" in reason and "characters" in reason
    # a different partner is unaffected
    assert q.check("B", TTS_CHARS) is None


def test_window_expiry_prunes(monkeypatch):
    _set_caps(monkeypatch, stt=60, window=100.0)
    clock = _Clock(1000.0)
    q = AudioQuota(now_fn=clock)
    q.record("A", STT_SECONDS, 60)
    assert q.check("A", STT_SECONDS) is not None  # at cap
    clock.t += 101  # advance past the window
    assert q.check("A", STT_SECONDS) is None  # old usage expired
    assert q.window_total("A", STT_SECONDS) == 0


def test_meters_are_independent(monkeypatch):
    _set_caps(monkeypatch, tts=10, stt=10)
    q = AudioQuota(now_fn=_Clock())
    q.record("A", TTS_CHARS, 10)
    assert q.check("A", TTS_CHARS) is not None  # tts capped
    assert q.check("A", STT_SECONDS) is None  # stt untouched


# ── route enforcement ─────────────────────────────────────────────────────────
_AUTH_A = {"Authorization": "Bearer ka"}
_AUTH_OWNER = {"Authorization": "Bearer ko"}


def _resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else authorization
    )
    return {
        "ka": {"partner_id": "A", "owner": False},
        "ko": {"partner_id": None, "owner": True},
    }.get(tok)


async def _tts_runner(text, **kw):
    return {"format": "mp3_44100_128", "data": "AAA=", "chars": len(text), "segments": []}


async def _stt_runner(audio, **kw):
    return {"transcript": "hello", "duration_s": 30.0, "words": [], "segments": []}


def _client(quota):
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            tts_runner=_tts_runner,
            stt_runner=_stt_runner,
            audio_quota=quota,
        )
    )
    return TestClient(app)


def test_tts_route_records_then_blocks(monkeypatch):
    _set_caps(monkeypatch, tts=10)
    c = _client(AudioQuota(now_fn=_Clock()))
    # first call (10 chars) succeeds and records to the cap
    assert c.post("/v1/tts", json={"text": "0123456789"}, headers=_AUTH_A).status_code == 200
    # partner is now at cap → next call refused
    r = c.post("/v1/tts", json={"text": "more"}, headers=_AUTH_A)
    assert r.status_code == 429
    assert "quota reached" in r.json()["detail"]


def test_owner_bypasses_route_quota(monkeypatch):
    _set_caps(monkeypatch, tts=1)
    c = _client(AudioQuota(now_fn=_Clock()))
    # owner key, even over a tiny cap, is never metered
    for _ in range(3):
        assert (
            c.post("/v1/tts", json={"text": "a long line of text"}, headers=_AUTH_OWNER).status_code
            == 200
        )


def test_stt_route_blocks_when_over(monkeypatch):
    _set_caps(monkeypatch, stt=30)
    q = AudioQuota(now_fn=_Clock())
    c = _client(q)
    audio = base64.b64encode(b"PCM").decode()
    # first call records 30s → at cap
    assert c.post("/v1/stt", json={"audio": audio}, headers=_AUTH_A).status_code == 200
    assert c.post("/v1/stt", json={"audio": audio}, headers=_AUTH_A).status_code == 429


def test_no_enforcement_when_caps_zero(monkeypatch):
    _set_caps(monkeypatch, tts=0, stt=0)
    c = _client(AudioQuota(now_fn=_Clock()))
    for _ in range(5):
        assert (
            c.post(
                "/v1/tts", json={"text": "lots and lots of text here"}, headers=_AUTH_A
            ).status_code
            == 200
        )


def test_turn_audio_blocked_by_quota_emits_audio_error(monkeypatch):
    import asyncio

    _set_caps(monkeypatch, tts=10)
    quota = AudioQuota(now_fn=_Clock())
    quota.record("A", TTS_CHARS, 10)  # partner A already at cap

    class _Source:
        def __init__(self):
            self.taps = set()

        def add_tap(self, q):
            self.taps.add(q)

        def remove_tap(self, q):
            self.taps.discard(q)

        def push(self, ev):
            for q in list(self.taps):
                q.put_nowait(ev)

    source = _Source()

    async def runner(message, end_user_id, mandate_id=None, persona=None):
        source.push({"type": "turn_start", "turn_id": "t1"})
        source.push({"type": "turn_end", "turn_id": "t1"})
        await asyncio.sleep(0)
        return ("reply text", {"emotion": "warm"})

    async def _stream_runner(text, **kw):
        yield "meta", {"format": "pcm_22050", "sample_rate": 22050}
        yield "chunk", {"seq": 0, "text": text, "data": "AAA="}
        yield "end", {"chunks": 1, "chars": len(text)}

    registry = ApiSessionRegistry(id_fn=lambda: "ss")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            registry,
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            event_source=source,
            tts_stream_runner=_stream_runner,
            audio_quota=quota,
        )
    )
    registry.create("c1")
    c = TestClient(app)
    with c.stream(
        "POST",
        "/v1/sessions/ss/turns/stream",
        json={"message": "hi", "audio": {"enabled": True}},
        headers=_AUTH_A,
    ) as r:
        body = "".join(r.iter_text())
    assert "event: done" in body  # text still delivered
    assert "event: audio_error" in body and "quota reached" in body
    assert "event: audio_chunk" not in body  # synth never ran
