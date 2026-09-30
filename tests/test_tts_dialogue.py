"""eleven_v4_turbo is the default voice and it exists only on the Text to
Dialogue WebSocket, so the shared transport (brain.tts_dialogue) must carry both
the brain's own _speak and the engine API, open early at turn start, stay alive
while the turn runs, and fall back to Flash (never to silence, never to tags read
aloud) when the socket can't deliver. Numbers behind the design come from the
Phase 0 probe in docs/ELEVEN_V4_TURBO_PLAN.md."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types

import pytest

import brain.pns as pns_mod
import brain.tts_dialogue as td
from brain.pns import PNS

PCM = b"\x01\x02" * 400
HTTP_PCM = b"\x03\x04" * 400


def _audio_frame(alignment: dict | None = None) -> str:
    msg: dict = {"audio": base64.b64encode(PCM).decode()}
    if alignment is not None:
        msg["alignment"] = alignment
    return json.dumps(msg)


class _FakeWS:
    """Scripted dialogue server. Serves `frames` once the client has sent
    close_socket (as the real server finishes an utterance), or holds the socket
    open until the client closes it when `frames` is None."""

    def __init__(self, server, frames: list[str] | None, *, dead_after_open: bool = False):
        self.server = server
        self.frames = list(frames) if frames is not None else None
        self.closed = asyncio.Event()
        # Dropped by the network after the handshake, before anything was fed,
        # and nobody has noticed yet: the next send is what finds out.
        self.dead_after_open = dead_after_open

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed.set()
        self.server.closes += 1
        return False

    async def send(self, frame: str):
        if self.closed.is_set():
            raise ConnectionError("socket closed")
        if self.dead_after_open and "voices" not in json.loads(frame):
            raise ConnectionError("no close frame received or sent")
        self.server.sent.append(json.loads(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.frames is None:
            await self.closed.wait()
            raise StopAsyncIteration
        while not any(m.get("close_socket") for m in self.server.sent):
            if self.closed.is_set():
                raise StopAsyncIteration
            await asyncio.sleep(0.001)
        if not self.frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.frames.pop(0)


class _FakeServer:
    def __init__(self, frames=None, *, fail=False, hold_open=False, first_dead=False):
        self.frames = (
            frames if frames is not None else [_audio_frame(), json.dumps({"is_final": True})]
        )
        self.fail = fail
        self.hold_open = hold_open
        self.first_dead = first_dead
        self.urls: list[str] = []
        self.sent: list[dict] = []
        self.closes = 0

    def install(self, monkeypatch):
        mod = types.ModuleType("websockets")

        def connect(url, **_kw):
            self.urls.append(url)
            if self.fail:
                raise ConnectionRefusedError("no route to elevenlabs")
            dead = self.first_dead and len(self.urls) == 1
            return _FakeWS(self, None if self.hold_open else self.frames, dead_after_open=dead)

        mod.connect = connect
        monkeypatch.setitem(sys.modules, "websockets", mod)
        return self


class _FakeBus:
    def subscribe(self, _topic):
        return asyncio.Queue()

    async def publish_dict(self, *_a, **_kw):
        return None


class _StubHTTP:
    calls: list[dict] = []

    def __init__(self, api_key: str = "", **_kw):
        self.text_to_speech = self

    def stream(self, **kwargs):
        _StubHTTP.calls.append(kwargs)

        async def _gen():
            yield HTTP_PCM

        return _gen()


@pytest.fixture()
def pns(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-test")
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)  # the default: v4 Turbo
    monkeypatch.delenv("BRAIN_TTS_DIALOGUE_WS", raising=False)
    monkeypatch.setattr(pns_mod, "BROWSER_AUDIO_MODE", True)
    monkeypatch.setattr(pns_mod, "VOICE_MODE", True)
    import elevenlabs

    _StubHTTP.calls = []
    monkeypatch.setattr(elevenlabs, "AsyncElevenLabs", _StubHTTP)
    p = PNS(_FakeBus())
    p._tts_ws_queue = asyncio.Queue(maxsize=4096)
    return p


def _played(p: PNS) -> bytes:
    out = b""
    while not p._tts_ws_queue.empty():
        item = p._tts_ws_queue.get_nowait()
        if isinstance(item, bytes) and item != b"\xff":
            out += item
    return out


# ── routing helpers ──────────────────────────────────────────────────────────


def test_model_routing(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
    assert td.default_model_id() == "eleven_v4_turbo"
    assert td.is_dialogue_model("eleven_v4_turbo") and td.is_dialogue_model("eleven_v4")
    assert td.is_dialogue_model("eleven_v3_conversational")
    assert not td.is_dialogue_model("eleven_v3") and not td.is_dialogue_model("eleven_flash_v2_5")
    assert td.http_fallback_model("eleven_v4_turbo") == "eleven_flash_v2_5"
    assert td.http_fallback_model("eleven_v3_conversational") == "eleven_v3"
    assert td.uses_audio_tags("eleven_v4_turbo") and not td.uses_audio_tags("eleven_flash_v2_5")


def test_breaker_ignores_config_errors(monkeypatch):
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES", "2")
    b = td.DialogueBreaker()
    for _ in range(5):
        b.fail(td.DialogueError("voice_not_found", code="voice_not_found", config=True))
    assert (b.failures, b.tripped) == (0, False)
    b.fail(ConnectionRefusedError())
    assert b.fail(ConnectionRefusedError()) is True and b.tripped


def test_server_1008_wording_is_classified():
    assert td._as_dialogue_error(Exception("A voice with voice_id 'x' was not found.")).config
    assert td._as_dialogue_error(Exception("Model does not support language 'zz'.")).config
    assert not td._as_dialogue_error(TimeoutError("open timed out")).config
    bad_key = td._as_dialogue_error(Exception("Invalid API key"))
    assert bad_key.account and not bad_key.config
    assert not td._as_dialogue_error(TimeoutError("open timed out")).account


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_breaker_is_half_open_after_cooldown_not_permanent(monkeypatch):
    """A tripped breaker used to hold until restart: one bad minute degraded the
    process to Flash forever. Now it holds for the cooldown, lets one trial
    through, and reopens straight away if that trial fails too."""
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES", "2")
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_COOLDOWN_S", "60")
    clock = _Clock()
    monkeypatch.setattr(td.time, "monotonic", clock)
    b = td.DialogueBreaker()
    b.fail(ConnectionRefusedError())
    assert b.fail(ConnectionRefusedError()) is True and b.tripped

    clock.now += 59
    assert b.tripped
    clock.now += 2
    assert not b.tripped  # half-open: the next utterance tries the socket
    assert b.fail(ConnectionRefusedError()) is True and b.tripped  # trial failed

    clock.now += 61
    b.ok()  # trial succeeded
    assert (b.failures, b.tripped) == (0, False)
    b.fail(ConnectionRefusedError())
    assert not b.tripped  # back to needing the full run of failures


def test_account_errors_open_the_breaker_at_once(monkeypatch):
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES", "3")
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_COOLDOWN_S", "60")
    clock = _Clock()
    monkeypatch.setattr(td.time, "monotonic", clock)
    b = td.DialogueBreaker()
    quota = td.DialogueError("quota", code="quota_exceeded", account=True)
    assert b.fail(quota) is True and b.tripped and b.reason == "quota_exceeded"
    clock.now += 61
    assert not b.tripped


def test_quota_exceeded_stops_paying_a_socket_per_utterance(monkeypatch, pns):
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES", "3")
    err = json.dumps({"error": "quota_exceeded", "message": "This request exceeds your quota."})
    server = _FakeServer(frames=[err]).install(monkeypatch)

    for _ in range(3):
        asyncio.run(pns._speak("Hello there."))

    assert len(server.urls) == 1  # one attempt, then held off for the cooldown
    assert pns._dialogue_ws_tripped is True
    assert len(_StubHTTP.calls) == 3  # the fallback's business from here on


def test_config_error_codes_are_not_account_errors(monkeypatch, pns):
    err = json.dumps({"error": "unsupported_language", "message": "nope"})
    server = _FakeServer(frames=[err]).install(monkeypatch)

    for _ in range(2):
        asyncio.run(pns._speak("Hello there."))

    assert len(server.urls) == 2 and pns._dialogue_ws_tripped is False


# ── the brain's own voice (PNS._speak) ───────────────────────────────────────


def test_v4t_is_the_default_and_performs_tags(monkeypatch, pns):
    server = _FakeServer().install(monkeypatch)

    asyncio.run(pns._speak("[mood:excited]We did it![/mood] Now the next part."))

    assert len(server.urls) == 1
    assert "model_id=eleven_v4_turbo" in server.urls[0]
    first, text_frame = server.sent[0], server.sent[1]
    assert first == {"voices": ["voice-test"]}  # v4 ignores the sliders: none sent
    assert "[excited]" in text_frame["inputs"][0]["text"]  # the mood span is performed
    assert {"flush": True} in server.sent and {"close_socket": True} in server.sent
    assert PCM in _played(pns)
    assert _StubHTTP.calls == []


def test_v4t_failure_falls_back_to_flash_without_tags(monkeypatch, pns):
    _FakeServer(fail=True).install(monkeypatch)

    asyncio.run(pns._speak("[mood:excited]We did it![/mood] Now the next part."))

    assert _StubHTTP.calls, "Flash must speak when the socket can't"
    assert all(c["model_id"] == "eleven_flash_v2_5" for c in _StubHTTP.calls)
    assert all("[" not in c["text"] for c in _StubHTTP.calls)  # tags would be read aloud
    assert HTTP_PCM in _played(pns)
    assert pns._dialogue_ws_failures == 1


def test_config_error_falls_back_without_tripping(monkeypatch, pns):
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES", "2")
    err = json.dumps(
        {"error": "voice_not_found", "message": "A voice was not found.", "code": 1008}
    )
    _FakeServer(frames=[err]).install(monkeypatch)

    for _ in range(4):
        asyncio.run(pns._speak("Hello there."))

    assert len(_StubHTTP.calls) == 4  # every utterance still spoke
    assert pns._dialogue_ws_failures == 0 and pns._dialogue_ws_tripped is False


def test_kill_switch_routes_v4t_to_flash(monkeypatch, pns):
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS", "0")
    server = _FakeServer().install(monkeypatch)

    asyncio.run(pns._speak("Hello there."))

    assert server.urls == []
    assert _StubHTTP.calls and _StubHTTP.calls[0]["model_id"] == "eleven_flash_v2_5"


def test_prewarmed_socket_is_claimed(monkeypatch, pns):
    server = _FakeServer().install(monkeypatch)

    async def turn():
        pns.prewarm_tts()  # turn start
        await asyncio.sleep(0.01)  # the brain thinks; handshake happens meanwhile
        assert len(server.urls) == 1
        await pns._speak("Hello there.")

    asyncio.run(turn())
    assert len(server.urls) == 1  # the speech reused the turn-start socket
    assert server.sent[0] == {"voices": ["voice-test"]}
    assert PCM in _played(pns)


def test_prewarm_for_another_voice_is_dropped(monkeypatch, pns):
    server = _FakeServer().install(monkeypatch)

    async def turn():
        pns.prewarm_tts()
        await asyncio.sleep(0.01)
        pns._voice_id = "persona-voice"  # a persona switch mid-turn
        await pns._speak("Hello there.")
        await asyncio.sleep(0.01)

    asyncio.run(turn())
    assert len(server.urls) == 2
    assert server.closes >= 1  # the stale socket was closed, not leaked
    assert {"voices": ["persona-voice"]} in server.sent


def test_prewarm_dropped_by_the_server_is_not_claimed(monkeypatch, pns):
    """The server (idle timeout) or the network closed the turn-start socket
    while the brain thought. It used to look usable because only our own close()
    marked it closed; feeding it then counted a breaker failure and the reply
    fell back to Flash."""
    server = _FakeServer().install(monkeypatch)

    async def turn():
        pns.prewarm_tts()
        await asyncio.sleep(0.01)
        pns._tts_prewarmed._ws.closed.set()  # the server hung up
        await asyncio.sleep(0.01)
        assert not td.usable(pns._tts_prewarmed, "eleven_v4_turbo", "voice-test", "pcm_22050")
        await pns._speak("Hello there.")

    asyncio.run(turn())
    assert len(server.urls) == 2  # a fresh socket carried the reply
    assert PCM in _played(pns) and _StubHTTP.calls == []
    assert pns._dialogue_ws_failures == 0


def test_usable_reads_the_websocket_state(monkeypatch):
    """websockets >= 13 has no ``closed`` on the connection, only ``state``."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    _FakeServer(hold_open=True).install(monkeypatch)

    async def run():
        s = td.DialogueSession(model="eleven_v4_turbo", voice_id="v", expire_after=90).start()
        await asyncio.sleep(0.01)
        before = td.usable(s, "eleven_v4_turbo", "v", "pcm_22050")
        s._ws.state = types.SimpleNamespace(name="CLOSED")
        after = td.usable(s, "eleven_v4_turbo", "v", "pcm_22050")
        await s.close()
        return before, after

    assert asyncio.run(run()) == (True, False)


def test_prewarm_that_dies_unnoticed_is_retried_once_uncounted(monkeypatch, pns):
    """The drop is only discovered when the first text is sent: reopen once on
    a fresh socket, replay, and don't charge the breaker for it."""
    server = _FakeServer(first_dead=True).install(monkeypatch)

    async def turn():
        pns.prewarm_tts()
        await asyncio.sleep(0.01)
        await pns._speak("Hello there.")

    asyncio.run(turn())
    assert len(server.urls) == 2
    assert PCM in _played(pns) and _StubHTTP.calls == []
    assert pns._dialogue_ws_failures == 0
    texts = [m for m in server.sent if "inputs" in m]
    assert len(texts) == 1 and {"close_socket": True} in server.sent


def test_fresh_socket_failure_is_not_retried(monkeypatch, pns):
    """Only a prewarmed socket gets the free retry; a socket opened for the
    utterance that fails still counts, once."""
    server = _FakeServer(first_dead=True).install(monkeypatch)

    asyncio.run(pns._speak("Hello there."))

    assert len(server.urls) == 1
    assert pns._dialogue_ws_failures == 1 and _StubHTTP.calls


def test_prewarm_skipped_when_muted_or_not_dialogue(monkeypatch, pns):
    server = _FakeServer().install(monkeypatch)
    pns._tts_muted = True
    pns.prewarm_tts()
    pns._tts_muted = False
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    pns.prewarm_tts()
    assert server.urls == [] and pns._tts_prewarmed is None


# ── session lifecycle ────────────────────────────────────────────────────────


def test_keepalive_holds_an_idle_socket(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_KEEPALIVE_S", "0.05")
    server = _FakeServer(hold_open=True).install(monkeypatch)

    async def run():
        s = td.DialogueSession(model="eleven_v4_turbo", voice_id="v").start()
        await asyncio.sleep(0.2)
        await s.close()

    asyncio.run(run())
    assert {"keep_alive": True} in server.sent


def test_unfed_prewarm_expires(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_KEEPALIVE_S", "0.05")
    server = _FakeServer(hold_open=True).install(monkeypatch)

    async def run():
        s = td.DialogueSession(model="eleven_v4_turbo", voice_id="v", expire_after=0.05).start()
        await asyncio.sleep(0.3)
        return s.closed

    assert asyncio.run(run()) is True
    assert server.closes == 1


def test_release_closes_an_unclaimed_prewarm(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    server = _FakeServer(hold_open=True).install(monkeypatch)

    async def run():
        s = td.prewarm("eleven_v4_turbo", "v", "pcm_22050")
        await asyncio.sleep(0.01)
        await td.release()
        return s.closed

    assert asyncio.run(run()) is True
    assert server.closes == 1


def test_prewarm_refuses_non_dialogue_models(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert td.prewarm("eleven_flash_v2_5", "v") is None
    monkeypatch.setenv("BRAIN_TTS_DIALOGUE_WS", "0")
    assert td.prewarm("eleven_v4_turbo", "v") is None


# ── the engine API (/v1/tts, SSE, realtime WS) ───────────────────────────────


def _engine_env(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
    monkeypatch.delenv("BRAIN_TTS_DIALOGUE_WS", raising=False)
    monkeypatch.delenv("TTS_PROVIDER", raising=False)


def _aligned_frames(raw: str) -> list[str]:
    """Two audio frames whose alignment covers the shaped text split at the
    mood span, with per-frame (resetting) start times like the real server."""
    _display, tts_text, _ = PNS._tagged_tts_text(raw, {})
    cut = tts_text.index("[angrily]")
    frames = []
    for part in (tts_text[:cut], tts_text[cut:]):
        chars = list(part)
        frames.append(
            _audio_frame(
                {
                    "chars": chars,
                    "char_start_times_ms": [i * 10 for i in range(len(chars))],
                    "char_durations_ms": [10] * len(chars),
                }
            )
        )
    return frames + [json.dumps({"is_final": True})]


def test_engine_streams_v4t_frames_attributed_to_mood_segments(monkeypatch):
    import brain.api.audio as audio

    _engine_env(monkeypatch)
    raw = "Hi there. [mood:angry]No way![/mood]"
    server = _FakeServer(frames=_aligned_frames(raw)).install(monkeypatch)

    async def collect():
        return [ev async for ev in audio.synthesize_stream(raw, fmt="pcm_22050")]

    events = asyncio.run(collect())
    kinds = [k for k, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "end"
    assert events[0][1]["model"] == "eleven_v4_turbo"
    assert "sync_alignment=true" in server.urls[0]
    assert "[angrily]" in server.sent[1]["inputs"][0]["text"]  # hosted gets the tags too
    chunks = [p for k, p in events if k == "chunk"]
    assert [c["segment"] for c in chunks] == [0, 1]
    assert [c["mood"] for c in chunks] == [None, "angry"]
    assert "[" not in "".join(c["text"] for c in chunks)  # display text, tags skipped
    # Alignment blocks restart at 0 on the wire; the API makes them absolute
    # (block 2 starts where block 1 ended) and they never leak the wire's "chars".
    n1 = len(chunks[0]["alignment"]["chars"])
    assert chunks[1]["alignment"]["char_start_times_ms"][0] == n1 * 10
    assert all("chars" not in c for c in chunks)
    # Quota meters the display text exactly once.
    assert events[-1][1]["chars"] == len(PNS._strip_all_tags(PNS._parse_mood_markup(raw)[0]))


def test_engine_synthesize_keeps_one_entry_per_segment(monkeypatch):
    import brain.api.audio as audio

    _engine_env(monkeypatch)
    raw = "Hi there. [mood:angry]No way![/mood]"
    _FakeServer(frames=_aligned_frames(raw)).install(monkeypatch)

    out = asyncio.run(audio.synthesize(raw, fmt="pcm_22050"))
    assert out["model"] == "eleven_v4_turbo"
    assert [s["mood"] for s in out["segments"]] == [None, "angry"]
    assert base64.b64decode(out["data"]) == PCM * 2
    assert out["chars"] == len("Hi there. No way!")


def test_engine_falls_back_to_flash_before_first_audio(monkeypatch):
    import brain.api.audio as audio

    _engine_env(monkeypatch)
    _FakeServer(fail=True).install(monkeypatch)
    sent: list = []

    async def fake_http(chunks, affect, voice_id, model_id, output_format, cancel=None):
        sent.append(model_id)
        yield {"seq": 0, "text": chunks[0][0], "mood": None, "_bytes": HTTP_PCM, "data": ""}

    monkeypatch.setattr(audio, "_iter_elevenlabs", fake_http)
    out = asyncio.run(audio.synthesize("Hello there.", fmt="pcm_22050"))
    assert out["model"] == "eleven_flash_v2_5" and sent == ["eleven_flash_v2_5"]
    assert td.ENGINE_BREAKER.failures == 1


def test_engine_claims_the_turn_start_socket(monkeypatch):
    import brain.api.audio as audio

    _engine_env(monkeypatch)
    server = _FakeServer().install(monkeypatch)

    async def turn():
        audio.prewarm_for({"enabled": True, "format": "pcm_22050"}, "voice-x")
        await asyncio.sleep(0.01)
        return await audio.synthesize("Hello there.", voice_id="voice-x", fmt="pcm_22050")

    out = asyncio.run(turn())
    assert len(server.urls) == 1
    assert out["model"] == "eleven_v4_turbo"


def test_engine_cancel_before_audio_is_silent_not_fallback(monkeypatch):
    import brain.api.audio as audio

    _engine_env(monkeypatch)
    _FakeServer().install(monkeypatch)
    cancel = asyncio.Event()
    cancel.set()

    async def collect():
        return [k async for k, _ in audio.synthesize_stream("Hello.", cancel=cancel)]

    kinds = asyncio.run(collect())
    assert "chunk" not in kinds
    assert td.ENGINE_BREAKER.failures == 0
