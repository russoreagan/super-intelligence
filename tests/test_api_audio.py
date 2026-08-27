"""
Audio layer for the engine API: the always-on affect view on turn responses,
plus the optional POST /v1/tts and POST /v1/stt routes.

affect_view is tested directly (pure, no network). The routes are tested with
FAKE tts/stt runners so no ElevenLabs/Deepgram/OpenAI keys are needed: auth +
runner-absent (501) + provider-missing (503 via AudioError) + validation.
"""

from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.audio import AudioError, affect_view
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry

_AUTH = {"Authorization": "Bearer sk_test_123"}


def _ok(authorization, keys):
    if not authorization:
        return False
    tok = (
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    )
    return tok in keys


def _client(runner, *, tts_runner=None, stt_runner=None):
    keys = {"sk_test_123"}
    registry = ApiSessionRegistry(id_fn=lambda: "sess_abc")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            registry,
            auth=lambda h: _ok(h, keys),
            tts_runner=tts_runner,
            stt_runner=stt_runner,
        )
    )
    return TestClient(app)


# ── affect_view (pure) ────────────────────────────────────────────────────────
def test_affect_view_strips_markup_and_segments_moods():
    raw = "Sure. [mood:angry] This is unacceptable! [/mood] Anyway, let's fix it."
    display, block = affect_view(raw, {"emotion": "warm"})

    # Display text is clean — no markup, no bare bracket tags.
    assert "[mood" not in display and "[/mood]" not in display
    assert "[" not in display and "]" not in display
    assert "unacceptable" in display

    # The angry span is captured as its own segment with a mood + tag.
    moods = [s["mood"] for s in block["segments"]]
    assert "angry" in moods
    angry = next(s for s in block["segments"] if s["mood"] == "angry")
    assert angry["tag"]  # an ElevenLabs audio tag was resolved
    assert "seq" in angry
    # Raw markup is surfaced (it differs from display text).
    assert "markup" in block and "[mood:angry]" in block["markup"]


def test_affect_view_plain_text_has_no_markup_key():
    display, block = affect_view("just a plain reply", {"emotion": "neutral"})
    assert display == "just a plain reply"
    assert "markup" not in block
    assert block["segments"] and block["segments"][0]["mood"] is None


def test_affect_view_handles_empty():
    display, block = affect_view("", None)
    assert display == ""
    assert block["segments"] == []


# ── turn response now carries the affect block + clean display text ───────────
class _MarkupRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return (
            "Sure. [mood:angry] No. [/mood] Okay.",
            {"emotion": "warm", "appraisal": "SECRET"},
        )


def test_turn_response_strips_markup_and_adds_affect():
    c = _client(_MarkupRunner())
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    body = c.post(f"/v1/sessions/{sid}/turns", json={"message": "hi"}, headers=_AUTH).json()
    assert "[mood" not in body["response"]
    assert "appraisal" not in body.get("affect", {})  # internal fields never leak
    assert any(s["mood"] == "angry" for s in body["affect"]["segments"])


# ── /v1/tts ───────────────────────────────────────────────────────────────────
def test_tts_501_without_runner():
    c = _client(_MarkupRunner())  # no tts_runner wired
    r = c.post("/v1/tts", json={"text": "hello"}, headers=_AUTH)
    assert r.status_code == 501


def test_tts_requires_auth():
    async def _tts(text, **kw):
        return {"data": "x"}

    c = _client(_MarkupRunner(), tts_runner=_tts)
    assert c.post("/v1/tts", json={"text": "hi"}).status_code == 401


def test_tts_validates_text():
    async def _tts(text, **kw):
        return {"data": "x"}

    c = _client(_MarkupRunner(), tts_runner=_tts)
    assert c.post("/v1/tts", json={"text": "  "}, headers=_AUTH).status_code == 400
    assert c.post("/v1/tts", json={"text": "hi", "affect": "bad"}, headers=_AUTH).status_code == 400


def test_tts_passes_opts_and_returns_result():
    seen = {}

    async def _tts(text, *, affect=None, voice_id=None, model=None, fmt=None, provider=None):
        seen.update(text=text, voice_id=voice_id, model=model, fmt=fmt)
        return {"format": fmt or "mp3_44100_128", "data": "AAA=", "segments": []}

    c = _client(_MarkupRunner(), tts_runner=_tts)
    r = c.post(
        "/v1/tts",
        json={"text": "say this", "voice_id": "v1", "model": "flash", "format": "pcm_22050"},
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["format"] == "pcm_22050"
    assert seen == {"text": "say this", "voice_id": "v1", "model": "flash", "fmt": "pcm_22050"}


def test_tts_audioerror_maps_to_status():
    async def _tts(text, **kw):
        raise AudioError("ELEVENLABS_API_KEY is not configured", status=503)

    c = _client(_MarkupRunner(), tts_runner=_tts)
    r = c.post("/v1/tts", json={"text": "hi"}, headers=_AUTH)
    assert r.status_code == 503
    assert "ELEVENLABS" in r.json()["detail"]


# ── /v1/stt ───────────────────────────────────────────────────────────────────
def test_stt_501_without_runner():
    c = _client(_MarkupRunner())
    r = c.post("/v1/stt", json={"audio": base64.b64encode(b"x").decode()}, headers=_AUTH)
    assert r.status_code == 501


def test_stt_rejects_bad_base64():
    async def _stt(audio, **kw):
        return {"transcript": ""}

    c = _client(_MarkupRunner(), stt_runner=_stt)
    assert c.post("/v1/stt", json={"audio": "!!!not base64!!!"}, headers=_AUTH).status_code == 400
    assert c.post("/v1/stt", json={"audio": ""}, headers=_AUTH).status_code == 400


# ── audio_input on turns (voice-in → run turn on transcript) ──────────────────
class _RecordingRunner:
    def __init__(self):
        self.calls = []

    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        self.calls.append((message, end_user_id, mandate_id))
        return ("ok", {"emotion": "warm"})


async def _fake_stt(audio, *, mimetype="audio/wav", diarize=False, model=None):
    return {
        "transcript": "transcribed words",
        "words": [],
        "segments": [{"transcript": "transcribed words", "is_final": True}],
    }


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_turn_audio_input_transcribes_and_runs_on_transcript():
    runner = _RecordingRunner()
    c = _client(runner, stt_runner=_fake_stt)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.post(
        f"/v1/sessions/{sid}/turns",
        json={"audio_input": {"data": _b64(b"PCM"), "mimetype": "audio/webm"}},
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["transcript"] == "transcribed words"
    # the turn ran on the transcript, not raw audio
    assert runner.calls == [("transcribed words", "c1", None)]


def test_turn_rejects_both_message_and_audio_input():
    c = _client(_RecordingRunner(), stt_runner=_fake_stt)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.post(
        f"/v1/sessions/{sid}/turns",
        json={"message": "hi", "audio_input": {"data": _b64(b"x")}},
        headers=_AUTH,
    )
    assert r.status_code == 400


def test_turn_audio_input_501_without_stt_runner():
    c = _client(_RecordingRunner())  # no stt_runner
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.post(
        f"/v1/sessions/{sid}/turns", json={"audio_input": {"data": _b64(b"x")}}, headers=_AUTH
    )
    assert r.status_code == 501


def test_turn_audio_input_422_on_empty_transcript():
    async def _silent_stt(audio, **kw):
        return {"transcript": "   "}

    c = _client(_RecordingRunner(), stt_runner=_silent_stt)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.post(
        f"/v1/sessions/{sid}/turns", json={"audio_input": {"data": _b64(b"x")}}, headers=_AUTH
    )
    assert r.status_code == 422


def test_turn_audio_input_bad_base64():
    c = _client(_RecordingRunner(), stt_runner=_fake_stt)
    sid = c.post("/v1/sessions", json={"end_user_id": "c1"}, headers=_AUTH).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/turns", json={"audio_input": {"data": "!!!"}}, headers=_AUTH)
    assert r.status_code == 400


def test_stream_audio_input_echoes_transcript_in_open():
    import asyncio

    source = _Source()

    async def runner(message, end_user_id, mandate_id=None, persona=None):
        source.push({"type": "turn_start", "turn_id": "t1"})
        source.push({"type": "turn_end", "turn_id": "t1"})
        await asyncio.sleep(0)
        return (f"heard: {message}", {"emotion": "warm"})

    keys = {"sk_test_123"}
    registry = ApiSessionRegistry(id_fn=lambda: "ss")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            registry,
            auth=lambda h: _ok(h, keys),
            event_source=source,
            stt_runner=_fake_stt,
        )
    )
    registry.create("c1")
    c = TestClient(app)
    with c.stream(
        "POST",
        "/v1/sessions/ss/turns/stream",
        json={"audio_input": {"data": _b64(b"PCM")}},
        headers=_AUTH,
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "event: open" in body
    assert "transcribed words" in body  # transcript echoed in the open frame
    assert "event: done" in body


# ── synthesize / synthesize_stream contract (no network) ─────────────────────
def _stub_iter(monkeypatch):
    """Replace the ElevenLabs iterator with a deterministic 2-chunk stub."""
    import brain.api.audio as audio

    async def _fake(chunks, affect, voice_id, model_id, output_format, cancel=None):
        for i, (chunk, mood) in enumerate(chunks):
            raw = f"pcm{i}".encode()
            yield {
                "seq": i,
                "text": chunk,
                "mood": mood,
                "voice_settings": {"stability": 0.5},
                "_bytes": raw,
                "data": __import__("base64").b64encode(raw).decode(),
            }

    monkeypatch.setattr(audio, "_iter_elevenlabs", _fake)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")  # passes the gate (stub ignores it)


def test_synthesize_concatenates_and_reports_pcm_duration(monkeypatch):
    import asyncio

    import brain.api.audio as audio

    _stub_iter(monkeypatch)
    out = asyncio.run(audio.synthesize("Hello. [mood:angry] No. [/mood]", fmt="pcm_22050"))
    assert out["format"] == "pcm_22050"
    assert len(out["segments"]) >= 2
    assert out["data"]  # concatenated base64
    assert out["duration_s"] is not None  # pcm → duration computed
    assert "_bytes" not in out["segments"][0]  # raw bytes stripped from the response


def test_synthesize_stream_yields_meta_chunks_end(monkeypatch):
    import asyncio

    import brain.api.audio as audio

    _stub_iter(monkeypatch)

    async def _collect():
        kinds = []
        async for kind, _payload in audio.synthesize_stream("A. [mood:angry] B. [/mood]"):
            kinds.append(kind)
        return kinds

    kinds = asyncio.run(_collect())
    assert kinds[0] == "meta" and kinds[-1] == "end"
    assert kinds.count("chunk") >= 2


# ── SSE audio streaming on /turns/stream ──────────────────────────────────────
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


def _stream_client(*, tts_stream_runner=None):
    import asyncio

    source = _Source()

    async def runner(message, end_user_id, mandate_id=None, persona=None):
        for ev in (
            {"type": "turn_start", "turn_id": "t1", "user_input": message},
            {"type": "turn_end", "turn_id": "t1"},
        ):
            source.push(ev)
            await asyncio.sleep(0)
        return ("Sure. [mood:angry] No. [/mood]", {"emotion": "warm"})

    keys = {"sk_test_123"}
    registry = ApiSessionRegistry(id_fn=lambda: "ss")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            registry,
            auth=lambda h: _ok(h, keys),
            event_source=source,
            tts_stream_runner=tts_stream_runner,
        )
    )
    registry.create("c1")
    return TestClient(app)


async def _fake_tts_stream(
    text, *, affect=None, voice_id=None, model=None, fmt=None, provider=None
):
    yield (
        "meta",
        {
            "format": "pcm_22050",
            "voice_id": "v1",
            "model": "eleven_flash_v2_5",
            "sample_rate": 22050,
        },
    )
    yield "chunk", {"seq": 0, "text": "Sure.", "mood": None, "data": "AAA="}
    yield "chunk", {"seq": 1, "text": "No.", "mood": "angry", "data": "BBB="}
    yield "end", {"chunks": 2, "duration_s": 1.0}


def test_stream_emits_audio_after_done_when_requested():
    c = _stream_client(tts_stream_runner=_fake_tts_stream)
    with c.stream(
        "POST",
        "/v1/sessions/ss/turns/stream",
        json={"message": "hi", "audio": {"enabled": True, "format": "pcm_22050"}},
        headers=_AUTH,
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    # Text arrives first, then the audio frames.
    assert body.index("event: done") < body.index("event: audio_meta")
    assert "event: audio_chunk" in body and "event: audio_end" in body
    # turn_id is propagated onto audio frames (reserved for realtime multiplexing).
    assert '"turn_id": "t1"' in body
    assert '"seq": 1' in body and '"mood": "angry"' in body


def test_stream_no_audio_frames_when_not_requested():
    c = _stream_client(tts_stream_runner=_fake_tts_stream)
    with c.stream(
        "POST",
        "/v1/sessions/ss/turns/stream",
        json={"message": "hi"},
        headers=_AUTH,
    ) as r:
        body = "".join(r.iter_text())
    assert "event: done" in body
    assert "audio_meta" not in body and "audio_chunk" not in body


def test_stream_audio_error_when_no_runner_but_still_done():
    c = _stream_client(tts_stream_runner=None)
    with c.stream(
        "POST",
        "/v1/sessions/ss/turns/stream",
        json={"message": "hi", "audio": {"enabled": True}},
        headers=_AUTH,
    ) as r:
        body = "".join(r.iter_text())
    assert "event: done" in body
    assert "event: audio_error" in body
    assert "not available" in body


def test_stream_audio_error_on_synth_failure_does_not_kill_stream():
    async def _boom(text, **kw):
        raise AudioError("ELEVENLABS_API_KEY is not configured", status=503)
        yield  # pragma: no cover — makes this an async generator

    c = _stream_client(tts_stream_runner=_boom)
    with c.stream(
        "POST",
        "/v1/sessions/ss/turns/stream",
        json={"message": "hi", "audio": {"enabled": True}},
        headers=_AUTH,
    ) as r:
        body = "".join(r.iter_text())
    assert "event: done" in body
    assert "event: audio_error" in body and "ELEVENLABS" in body


def test_stt_decodes_and_returns_transcript():
    seen = {}

    async def _stt(audio, *, mimetype="audio/wav", diarize=False, model=None):
        seen.update(audio=audio, mimetype=mimetype, diarize=diarize)
        return {
            "transcript": "hello world",
            "words": [],
            "segments": [{"transcript": "hello world", "is_final": True}],
        }

    c = _client(_MarkupRunner(), stt_runner=_stt)
    r = c.post(
        "/v1/stt",
        json={
            "audio": base64.b64encode(b"PCMDATA").decode(),
            "mimetype": "audio/webm",
            "diarize": True,
        },
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["transcript"] == "hello world"
    assert r.json()["segments"][0]["is_final"] is True
    assert (
        seen["audio"] == b"PCMDATA" and seen["mimetype"] == "audio/webm" and seen["diarize"] is True
    )


# ── model resolution: eleven_v3_conversational on the HTTP transport ──────────
#
# ELEVENLABS_MODEL_ID is shared by the brain, the UI voice picker AND this
# engine API. eleven_v3_conversational only exists on the Text to Dialogue
# WebSocket, which this transport does not speak, and the v3 family 422-rejects
# style/speed. When 6a63ed3 widened the v3 gates to startswith(), this module's
# `model_id == "eleven_v3"` was missed — so flipping the tenant to the new model
# sent style/speed AND an unroutable model id, and every partner's audio died.


class _CapturingClient:
    """Captures the kwargs handed to text_to_speech.stream."""

    def __init__(self, sink: list) -> None:
        self.text_to_speech = _CapturingTTS(sink)


class _CapturingTTS:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def stream(self, **kwargs):
        self._sink.append(kwargs)

        async def _gen():
            yield b"pcm"

        return _gen()


def _capture_elevenlabs_call(monkeypatch, *, model=None, env_model=None) -> dict:
    """Run one synthesize() through the real _iter_elevenlabs with a fake SDK
    client, and return the kwargs it sent to ElevenLabs."""
    import asyncio

    import brain.api.audio as audio

    sink: list = []
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    if env_model is not None:
        monkeypatch.setenv("ELEVENLABS_MODEL_ID", env_model)
    else:
        monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)

    import elevenlabs

    monkeypatch.setattr(
        elevenlabs, "AsyncElevenLabs", lambda **kw: _CapturingClient(sink), raising=False
    )
    out = asyncio.run(audio.synthesize("Hello there.", model=model, fmt="pcm_22050"))
    assert sink, "no ElevenLabs call was made"
    return {"call": sink[0], "meta": out}


def test_v3_conversational_env_downgrades_and_drops_style_speed(monkeypatch):
    got = _capture_elevenlabs_call(monkeypatch, env_model="eleven_v3_conversational")
    call, meta = got["call"], got["meta"]
    # Downgraded to the id this endpoint actually accepts...
    assert call["model_id"] == "eleven_v3"
    # ...and reported honestly, so a partner sees what actually sang.
    assert meta["model"] == "eleven_v3"
    # v3 422-rejects these; sending them is a silent "no audio".
    vs = call["voice_settings"]
    assert getattr(vs, "style", None) in (None, 0.0)
    assert getattr(vs, "speed", None) is None


def test_v3c_alias_resolves(monkeypatch):
    got = _capture_elevenlabs_call(monkeypatch, model="v3c")
    assert got["call"]["model_id"] == "eleven_v3"
    assert got["meta"]["model"] == "eleven_v3"


def test_flash_still_carries_style_and_speed(monkeypatch):
    """The v3 branch must not swallow the Flash path: Flash has no audio tags,
    so stability/style/speed ARE its only prosody channel."""
    got = _capture_elevenlabs_call(monkeypatch, model="flash")
    call = got["call"]
    assert call["model_id"] == "eleven_flash_v2_5"
    assert getattr(call["voice_settings"], "style", None) is not None


def test_cancel_event_stops_synthesis(monkeypatch):
    """Barge-in must abort the segment being generated, not just the gap
    between segments — a mood-segmented chunk is seconds of audio."""
    import asyncio

    import brain.api.audio as audio

    _stub_iter(monkeypatch)

    async def _collect():
        cancel = asyncio.Event()
        cancel.set()
        kinds = []
        async for kind, _payload in audio.synthesize_stream(
            "A. [mood:angry] B. [/mood]", cancel=cancel
        ):
            kinds.append(kind)
        return kinds

    # The stub ignores cancel, so this pins the plumbing: the parameter is
    # accepted and forwarded all the way down without a TypeError.
    kinds = asyncio.run(_collect())
    assert kinds[0] == "meta" and kinds[-1] == "end"
