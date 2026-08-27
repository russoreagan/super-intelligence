"""
WebSocket realtime transport — /v1/sessions/{id}/stream.

Tests use FastAPI's TestClient.websocket_connect().  The fake ``stt_live_runner``
factory returns a scripted DeepgramLiveSession substitute so no real Deepgram
connection is made.  TTS is injected via a fake ``tts_stream_runner``.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry

# ── helpers / fakes ──────────────────────────────────────────────────────────


class _FakeTurnRunner:
    def __init__(self, text="hello back", affect=None):
        self.calls: list = []
        self._text = text
        self._affect = affect or {"emotion": "warm", "user_emotion": "curious", "hormonal": {}}

    # Mirrors BrainSession.api_turn: the WS transport passes inline_tools=False so
    # reactive tools keep the non-blocking defer→proactive loop (request/response
    # transports default to True / inline). Recorded so tests can assert the contract.
    async def __call__(
        self, message, end_user_id, mandate_id=None, persona=None, inline_tools=True
    ):
        self.calls.append((message, end_user_id, mandate_id, inline_tools))
        return self._text, self._affect


class _FakeSttSession:
    """Scripted STT session: replays (text, is_final, duration_s) tuples."""

    def __init__(self, transcripts: list[tuple[str, bool, float]]):
        self._transcripts = transcripts
        self._cb = None

    async def open(self, on_transcript):
        self._cb = on_transcript

    async def send(self, pcm_bytes: bytes) -> None:
        # Fire the scripted transcripts when audio arrives.
        for text, is_final, dur in self._transcripts:
            if self._cb:
                await self._cb(text, is_final, dur)
        self._transcripts = []  # only fire once

    async def close(self) -> None:
        pass


def _stt_factory(transcripts: list[tuple[str, bool, float]]):
    """Returns a factory that yields a single _FakeSttSession."""
    session = _FakeSttSession(transcripts)

    def factory():
        return session

    return factory, session


async def _fake_tts_stream(
    text, *, affect=None, voice_id=None, model=None, fmt=None, provider=None, cancel=None
):
    """Fake TTS that yields meta → one chunk → end."""
    audio_b64 = base64.b64encode(b"\x00" * 32).decode()
    yield "meta", {"format": "mp3", "sample_rate": 24000}
    yield "chunk", {"seq": 0, "data": audio_b64}
    yield "end", {"chunks": 1, "duration_s": 0.1, "chars": len(text)}


async def _slow_tts_stream(
    text, *, affect=None, voice_id=None, model=None, fmt=None, provider=None, cancel=None
):
    """TTS that yields many chunks with asyncio yields so barge-in can fire."""
    audio_b64 = base64.b64encode(b"\x00" * 32).decode()
    yield "meta", {"format": "mp3", "sample_rate": 24000}
    for i in range(10):
        await asyncio.sleep(0)
        yield "chunk", {"seq": i, "data": audio_b64}
    yield "end", {"chunks": 10, "duration_s": 1.0, "chars": len(text)}


def _ok(authorization, keys):
    if not authorization:
        return False
    tok = (
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    )
    return tok in keys


def _client(runner=None, *, keys=None, tts=None, stt_factory=None, event_source=None):
    keys = keys or {"sk_test_123"}
    runner = runner or _FakeTurnRunner()
    registry = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner,
            registry,
            auth=lambda h: _ok(h, keys),
            tts_stream_runner=tts,
            stt_live_runner=stt_factory,
            event_source=event_source,
        )
    )
    return TestClient(app), registry


_AUTH = {"Authorization": "Bearer sk_test_123"}


def _make_session(registry):
    """Create a session and return its id."""
    from brain.api.sessions import ApiSession

    s = ApiSession(session_id="sess_abc", end_user_id="cust-1", partner_id="p1")
    registry._sessions["sess_abc"] = s  # type: ignore[attr-defined]
    return "sess_abc"


# ── auth tests ────────────────────────────────────────────────────────────────


def test_ws_rejects_missing_auth():
    c, _ = _client()
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        c.websocket_connect("/v1/sessions/sess_abc/stream"),
    ):
        pass
    assert exc_info.value.code == 1008


def test_ws_rejects_bad_token():
    c, _ = _client()
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        c.websocket_connect(
            "/v1/sessions/sess_abc/stream", headers={"Authorization": "Bearer bad"}
        ),
    ):
        pass
    assert exc_info.value.code == 1008


def test_ws_rejects_unknown_session():
    c, _ = _client()
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        c.websocket_connect("/v1/sessions/no_such_id/stream", headers=_AUTH),
    ):
        pass
    assert exc_info.value.code == 1008


# ── connect / ready ───────────────────────────────────────────────────────────


def test_ws_sends_ready_on_connect():
    c, reg = _client()
    _make_session(reg)
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ready"
        assert msg["session_id"] == "sess_abc"
        assert msg["expects"] == "pcm_16000"


# ── ping / pong ───────────────────────────────────────────────────────────────


def test_ping_pong():
    c, reg = _client()
    _make_session(reg)
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong"


# ── text turn ─────────────────────────────────────────────────────────────────


def test_text_message_produces_done():
    runner = _FakeTurnRunner(text="hi there")
    c, reg = _client(runner)
    _make_session(reg)
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "text", "message": "hello"})
        done = ws.receive_json()
        assert done["type"] == "done"
        assert done["response"] == "hi there"
        assert "mood" in done
    # inline_tools=False: the WS transport keeps the deferred→proactive loop.
    assert runner.calls == [("hello", "cust-1", None, False)]


def test_text_message_requires_nonempty_message():
    c, reg = _client()
    _make_session(reg)
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "text", "message": "  "})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 400


# ── text turn with TTS ────────────────────────────────────────────────────────


def test_text_with_audio_enabled_streams_tts():
    runner = _FakeTurnRunner(text="speaking now")
    c, reg = _client(runner, tts=_fake_tts_stream)
    _make_session(reg)
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json(
            {
                "type": "text",
                "message": "speak",
                "audio": {"enabled": True, "format": "mp3"},
            }
        )
        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "audio_end":
                break
        types = [f["type"] for f in frames]
        assert "done" in types
        assert "audio_meta" in types
        assert "audio_chunk" in types
        assert "audio_end" in types


# ── audio / STT ───────────────────────────────────────────────────────────────


def test_audio_interim_transcript_forwarded():
    """Interim transcripts should be forwarded as is_final=False."""
    factory, _ = _stt_factory([("hearing you...", False, 0.0)])
    runner = _FakeTurnRunner()
    c, reg = _client(runner, stt_factory=factory)
    _make_session(reg)
    audio_b64 = base64.b64encode(b"\x00" * 64).decode()
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "audio", "data": audio_b64})
        transcript = ws.receive_json()
        assert transcript["type"] == "transcript"
        assert transcript["text"] == "hearing you..."
        assert transcript["is_final"] is False


def test_audio_final_transcript_triggers_turn():
    """A final transcript should fire a turn and return a done frame."""
    factory, _ = _stt_factory([("what time is it", True, 1.5)])
    runner = _FakeTurnRunner(text="it is noon")
    c, reg = _client(runner, stt_factory=factory)
    _make_session(reg)
    audio_b64 = base64.b64encode(b"\x00" * 64).decode()
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "audio", "data": audio_b64})
        # Collect until done (skip transcript frames)
        frames = {}
        for _ in range(5):
            f = ws.receive_json()
            frames[f["type"]] = f
            if "done" in frames:
                break
        assert "transcript" in frames
        assert frames["transcript"]["is_final"] is True
        assert frames["transcript"]["duration_s"] == pytest.approx(1.5)
        assert "done" in frames
        assert frames["done"]["response"] == "it is noon"
        assert frames["done"]["transcript"] == "what time is it"
    assert runner.calls[0][0] == "what time is it"


def test_audio_requires_valid_base64():
    factory, _ = _stt_factory([])
    c, reg = _client(stt_factory=factory)
    _make_session(reg)
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "audio", "data": "!!!not_b64!!!"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 400


def test_audio_without_stt_factory_returns_error():
    """When no STT factory is configured, sending audio returns a 501."""
    c, reg = _client(stt_factory=None)
    _make_session(reg)
    audio_b64 = base64.b64encode(b"\x00" * 64).decode()
    with c.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "audio", "data": audio_b64})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 501


# ── STT quota ─────────────────────────────────────────────────────────────────


def _partner_resolver(authorization):
    """Resolver that returns partner context (not owner) for the test key."""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
        if tok == "sk_test_123":
            return {"partner_id": "p1", "owner": False}
    return None


def test_stt_quota_exceeded_returns_error_no_turn(monkeypatch):
    from brain.api.audio_quota import STT_SECONDS, AudioQuota
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "audio_stt_seconds_per_window", 1)
    monkeypatch.setitem(settings._data, "audio_quota_window_s", 3600.0)

    quota = AudioQuota()
    quota.record("p1", STT_SECONDS, 2.0)  # exhaust the 1-second budget

    factory, _ = _stt_factory([])
    runner = _FakeTurnRunner()

    from brain.api.sessions import ApiSession

    reg2 = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    reg2._sessions["sess_abc"] = ApiSession(
        session_id="sess_abc", end_user_id="cust-1", partner_id="p1"
    )  # type: ignore[attr-defined]

    app2 = FastAPI()
    app2.include_router(
        build_api_router(
            runner,
            reg2,
            auth=lambda h: _ok(h, {"sk_test_123"}),
            resolver=_partner_resolver,
            stt_live_runner=factory,
            audio_quota=quota,
        )
    )
    c2 = TestClient(app2)

    audio_b64 = base64.b64encode(b"\x00" * 64).decode()
    with c2.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "audio", "data": audio_b64})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 429
    assert not runner.calls


# ── TTS quota ─────────────────────────────────────────────────────────────────


def test_tts_quota_exceeded_delivers_done_then_audio_error(monkeypatch):
    from brain.api.audio_quota import TTS_CHARS, AudioQuota
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "audio_tts_chars_per_window", 1)
    monkeypatch.setitem(settings._data, "audio_quota_window_s", 3600.0)

    quota = AudioQuota()
    quota.record("p1", TTS_CHARS, 100)  # exhaust

    runner = _FakeTurnRunner(text="will not speak")
    reg2 = ApiSessionRegistry(now_fn=lambda: 1000.0, id_fn=lambda: "sess_abc")
    from brain.api.sessions import ApiSession

    reg2._sessions["sess_abc"] = ApiSession(
        session_id="sess_abc", end_user_id="cust-1", partner_id="p1"
    )  # type: ignore[attr-defined]

    app2 = FastAPI()
    app2.include_router(
        build_api_router(
            runner,
            reg2,
            auth=lambda h: _ok(h, {"sk_test_123"}),
            resolver=_partner_resolver,
            tts_stream_runner=_fake_tts_stream,
            audio_quota=quota,
        )
    )
    c2 = TestClient(app2)

    with c2.websocket_connect("/v1/sessions/sess_abc/stream", headers=_AUTH) as ws:
        ws.receive_json()  # ready
        ws.send_json(
            {
                "type": "text",
                "message": "say something",
                "audio": {"enabled": True},
            }
        )
        frames = {}
        for _ in range(3):
            f = ws.receive_json()
            frames[f["type"]] = f
            if "done" in frames and "audio_error" in frames:
                break
        assert "done" in frames
        assert "audio_error" in frames


# ── barge-in + echo guard on the hosted transport ────────────────────────────
#
# efa5852 rebuilt voice barge-in on the server-mic path only. On this transport
# the interrupt waited for Flux's EndOfTurn (a full endpointing pause late), and
# there was no echo guard at all: a client on open speakers streams our own
# playback back to Flux, which transcribes it, cancels the reply and dispatches
# it as the next user turn — a loop that also burns a real LLM call per lap.


def _bare_session(**overrides):
    """A WsSession wired to nothing but what _on_transcript touches."""
    from brain.api.ws import WsSession

    sent: list[dict] = []
    turns: list[str] = []

    class _Ws:
        async def send_json(self, payload):
            sent.append(payload)

    sess = WsSession(
        _Ws(),
        SimpleNamespace(session_id="sess_abc", agent_id="p.a", end_user_id=None, mandate_id=None),
        {"owner": True},
        turn_runner=None,
        registry=None,
    )

    async def _fake_run_turn(message, *, transcript=None):
        turns.append(message)

    sess._run_turn = _fake_run_turn
    for k, v in overrides.items():
        setattr(sess, k, v)
    return sess, sent, turns


SPEAKING = (
    "The scheduler polls every five minutes, which is why the pod keeps coming "
    "back up after you press sleep. There is no latch today, so the cron job "
    "respawns the brain by design."
)


async def test_interim_speech_cancels_tts():
    sess, sent, turns = _bare_session(_speaking_text=SPEAKING)
    await sess._on_transcript("no wait forget deployment", False, 0.0)
    assert sess._tts_cancel.is_set()
    # Interim only stops playback — the turn still dispatches on the final.
    assert turns == []
    assert sent[-1]["type"] == "transcript" and sent[-1]["is_final"] is False


async def test_interim_echo_does_not_cancel_tts():
    """Regression for the containment fix, at the transport level: a fragment of
    our own long reply coming back through the mic must not cut the reply."""
    sess, _sent, _turns = _bare_session(_speaking_text=SPEAKING)
    await sess._on_transcript("the scheduler polls every five minutes", False, 0.0)
    assert not sess._tts_cancel.is_set()


async def test_interim_while_silent_never_cancels():
    sess, _sent, _turns = _bare_session(_speaking_text="")
    await sess._on_transcript("anything at all here", False, 0.0)
    assert not sess._tts_cancel.is_set()


async def test_final_echo_does_not_start_a_turn():
    """The loop-breaker: without this, every reply on open speakers answers
    itself."""
    sess, sent, turns = _bare_session(_speaking_text=SPEAKING)
    await sess._on_transcript("there is no latch today so the cron job", True, 2.0)
    await asyncio.sleep(0)  # let any dispatched turn task start
    assert turns == []
    # Still forwarded to the client — the UI may want to show what was heard.
    assert sent[-1]["type"] == "transcript" and sent[-1]["is_final"] is True


async def test_final_real_speech_starts_a_turn_while_speaking():
    sess, _sent, turns = _bare_session(_speaking_text=SPEAKING)
    await sess._on_transcript("actually check the webhook logs", True, 1.2)
    await asyncio.sleep(0)  # _run_turn is dispatched as a task
    assert turns == ["actually check the webhook logs"]
    assert sess._tts_cancel.is_set()


async def test_final_does_not_close_the_flux_session():
    """One connection spans many turns. Closing per utterance cost a handshake
    in the gap, and send() drops audio while the socket is None — so the first
    words of a fast follow-up went missing."""
    closed: list[bool] = []

    class _Dg:
        async def close(self):
            closed.append(True)

    sess, _sent, turns = _bare_session(_speaking_text="", _dg_session=_Dg())
    await sess._on_transcript("what time is it", True, 1.0)
    await asyncio.sleep(0)  # _run_turn is dispatched as a task
    assert turns == ["what time is it"]
    assert closed == [], "the Flux session was torn down mid-conversation"
    assert sess._dg_session is not None


async def test_barge_mode_off_keeps_final_only_behaviour(monkeypatch):
    """`off` is the escape hatch if interim barge misbehaves for a partner."""
    monkeypatch.setenv("BRAIN_BARGE_IN_MODE", "off")
    sess, _sent, _turns = _bare_session(_speaking_text=SPEAKING)
    await sess._on_transcript("no wait forget deployment", False, 0.0)
    assert not sess._tts_cancel.is_set()


async def test_echo_arriving_just_after_playback_ends_is_still_dropped():
    """Flux's EndOfTurn lands after its endpointing pause, so the tail of an
    echo can arrive once _speaking_text is already back to ''. Without the tail
    window, that trailing bleed-through became a turn."""
    import time

    from brain.api import ws as ws_mod

    sess, _sent, turns = _bare_session(
        _speaking_text="",
        _echo_tail_text=SPEAKING,
        _echo_tail_until=time.monotonic() + ws_mod._ECHO_TAIL_S,
    )
    await sess._on_transcript("there is no latch today so the cron job", True, 2.0)
    await asyncio.sleep(0)
    assert turns == []


async def test_speech_after_the_tail_window_starts_a_turn():
    """The window must expire — otherwise a user quoting the reply back a minute
    later would be silently ignored."""
    import time

    sess, _sent, turns = _bare_session(
        _speaking_text="",
        _echo_tail_text=SPEAKING,
        _echo_tail_until=time.monotonic() - 0.01,  # already elapsed
    )
    await sess._on_transcript("there is no latch today so the cron job", True, 2.0)
    await asyncio.sleep(0)
    assert turns == ["there is no latch today so the cron job"]
