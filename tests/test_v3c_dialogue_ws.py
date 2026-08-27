"""eleven_v3_conversational must stream over the Text to Dialogue WebSocket —
one socket per utterance, flush always sent (the server buffers ~40 chars, so
short replies are silent without it), and a WS failure before first audio must
fall back to the per-chunk HTTP eleven_v3 path rather than dropping the reply.
Flipping ELEVENLABS_MODEL_ID (or the BRAIN_TTS_DIALOGUE_WS kill switch) must
actually change the transport (settings-schema-whitelist lesson)."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types

import pytest

import brain.pns as pns_mod
from brain.pns import PNS

WS_PCM = b"\x01\x02" * 400
HTTP_PCM = b"\x03\x04" * 400


class _FakeBus:
    def subscribe(self, _topic):
        return asyncio.Queue()

    async def publish_dict(self, *_a, **_kw):
        return None


class _FakeWS:
    """Scripted dialogue-WS server: records sent frames, serves audio frames."""

    def __init__(self, recorder: list, n_audio_frames: int = 1, frame_delay: float = 0.0):
        self.sent = recorder
        self._frames = [
            json.dumps({"audio": base64.b64encode(WS_PCM).decode()}) for _ in range(n_audio_frames)
        ] + [json.dumps({"is_final_audio_for_turn": True})]
        self._delay = frame_delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, frame: str):
        self.sent.append(json.loads(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


def _install_fake_websockets(monkeypatch, recorder: list, *, fail=False, n_audio_frames=1,
                             frame_delay=0.0, urls: list | None = None):
    mod = types.ModuleType("websockets")

    def connect(url, **_kw):
        if urls is not None:
            urls.append(url)
        if fail:
            raise ConnectionRefusedError("no route to elevenlabs")
        return _FakeWS(recorder, n_audio_frames=n_audio_frames, frame_delay=frame_delay)

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", mod)
    return mod


class _StubStream:
    """Stands in for AsyncElevenLabs; records HTTP streaming calls."""

    calls: list[dict] = []

    def __init__(self, api_key: str):
        self.text_to_speech = self

    def stream(self, **kwargs):
        _StubStream.calls.append(kwargs)

        async def _gen():
            yield HTTP_PCM

        return _gen()


@pytest.fixture()
def pns(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-test")
    monkeypatch.delenv("BRAIN_TTS_DIALOGUE_WS", raising=False)
    monkeypatch.setattr(pns_mod, "BROWSER_AUDIO_MODE", True)
    import elevenlabs

    _StubStream.calls = []
    monkeypatch.setattr(elevenlabs, "AsyncElevenLabs", _StubStream)
    p = PNS(_FakeBus())
    p._tts_ws_queue = asyncio.Queue(maxsize=4096)
    return p


def _browser_audio(p: PNS) -> bytes:
    out = b""
    while not p._tts_ws_queue.empty():
        item = p._tts_ws_queue.get_nowait()
        if isinstance(item, bytes) and item not in (b"\xff",):
            out += item
    return out


def test_v3c_streams_over_dialogue_ws(monkeypatch, pns):
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_v3_conversational")
    sent: list = []
    urls: list = []
    _install_fake_websockets(monkeypatch, sent, urls=urls)

    asyncio.run(pns._speak("Hello there, friend."))

    assert urls and "text-to-dialogue/stream-input" in urls[0]
    assert "model_id=eleven_v3_conversational" in urls[0]
    # Frame order: voices registration, inputs, flush (mandatory — the server
    # buffers ~40 chars), close_socket.
    assert sent[0]["voices"] == ["voice-test"]
    assert sent[0]["voice_settings"]["stability"] in (0.0, 0.5, 1.0)  # snapped
    assert "Hello there, friend." in sent[1]["inputs"][0]["text"]
    assert sent[2] == {"flush": True}
    assert sent[3] == {"close_socket": True}
    assert WS_PCM in _browser_audio(pns)
    assert _StubStream.calls == []  # HTTP path never touched


def test_ws_failure_falls_back_to_http_v3(monkeypatch, pns):
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_v3_conversational")
    _install_fake_websockets(monkeypatch, [], fail=True)

    asyncio.run(pns._speak("Hello there, friend."))

    assert _StubStream.calls, "HTTP fallback must synthesize when the WS fails"
    call = _StubStream.calls[0]
    assert call["model_id"] == "eleven_v3"
    assert "previous_text" not in call and "next_text" not in call  # v3: no stitching
    assert HTTP_PCM in _browser_audio(pns)


def test_kill_switch_forces_http(monkeypatch, pns):
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_v3_conversational")
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS", "0")
    urls: list = []
    _install_fake_websockets(monkeypatch, [], urls=urls)

    asyncio.run(pns._speak("Hello there, friend."))

    assert urls == []  # socket never opened
    assert _StubStream.calls and _StubStream.calls[0]["model_id"] == "eleven_v3"


def test_flash_default_unaffected(monkeypatch, pns):
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    urls: list = []
    _install_fake_websockets(monkeypatch, [], urls=urls)

    asyncio.run(pns._speak("Hello there, friend."))

    assert urls == []
    assert _StubStream.calls and _StubStream.calls[0]["model_id"] == "eleven_flash_v2_5"


def test_interrupt_stops_dialogue_stream(monkeypatch, pns):
    """Barge-in mid-stream: the drain loop must stop as soon as the interrupt
    event is set, without waiting for the remaining frames."""
    sent: list = []
    _install_fake_websockets(monkeypatch, sent, n_audio_frames=50, frame_delay=0.01)
    from elevenlabs.types import VoiceSettings

    vs = VoiceSettings(stability=0.5, similarity_boost=0.8, use_speaker_boost=True)
    queue: asyncio.Queue = asyncio.Queue()

    async def scenario():
        task = asyncio.create_task(
            pns._stream_dialogue_ws("long utterance " * 20, "voice-test", vs, queue)
        )
        await asyncio.sleep(0.03)  # a few frames through
        pns._interrupt_event.set()
        return await task

    delivered = asyncio.run(scenario())
    assert delivered is True  # audio reached the queue — caller must not retry
    assert 0 < queue.qsize() < 50
