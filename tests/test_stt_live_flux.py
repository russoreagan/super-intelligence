"""
Flux (Listen v2) live-STT session: TurnInfo event handling.

brain/api/stt_live.py runs on Deepgram Flux, whose model-based turn detection
replaced the hand-rolled v1 endpointing/UtteranceEnd assembly. These tests pin
the event→callback contract WsSession depends on: in-progress events forward
as non-final hints, EndOfTurn fires the single authoritative final with a
duration for the STT quota meter, and speechless force-ended turns are dropped.

They also pin the session lifetime: ONE connection spans many turns (Flux's
turn_index just increments), and a dropped socket reconnects rather than going
silently deaf — the two properties that stopped being free when the session
stopped being one-per-utterance.
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.api.stt_live import DeepgramLiveSession


def _word(word: str, start: float, end: float) -> SimpleNamespace:
    return SimpleNamespace(word=word, start=start, end=end, confidence=0.99)


def _turn(
    event: str,
    transcript: str,
    words: list | None = None,
    audio_window_start: float = 0.0,
    audio_window_end: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        type="TurnInfo",
        event=event,
        transcript=transcript,
        words=words or [],
        audio_window_start=audio_window_start,
        audio_window_end=audio_window_end,
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, float]] = []

    async def __call__(self, text: str, is_final: bool, duration_s: float) -> None:
        self.calls.append((text, is_final, duration_s))


def _session() -> tuple[DeepgramLiveSession, _Recorder]:
    session = DeepgramLiveSession()
    recorder = _Recorder()
    session._on_transcript = recorder
    return session, recorder


async def test_update_events_forward_as_interim():
    session, recorder = _session()
    await session._handle_turn_info(_turn("StartOfTurn", "hello"))
    await session._handle_turn_info(_turn("Update", "hello there"))
    assert recorder.calls == [("hello", False, 0.0), ("hello there", False, 0.0)]


async def test_end_of_turn_meters_audio_fed_not_word_span():
    """duration_s is the STT quota meter, and must mean the same thing on this
    transport as on the batch one (brain/api/audio.py reads Deepgram's
    metadata.duration — input audio length). Metering the word span instead
    silently discounted every turn's leading and trailing silence, so the same
    clip billed a partner differently depending on which route it took."""
    session, recorder = _session()
    session._turn_bytes = 16_000 * 2 * 2  # 2.0 s of PCM16 @ 16 kHz
    await session._handle_turn_info(
        _turn(
            "EndOfTurn",
            "Hello there.",
            words=[_word("Hello", 1.0, 1.4), _word("there.", 1.5, 2.25)],
        )
    )
    assert recorder.calls == [("Hello there.", True, 2.0)]


async def test_turn_meter_resets_between_turns():
    session, recorder = _session()
    session._turn_bytes = 16_000 * 2  # 1.0 s
    await session._handle_turn_info(_turn("EndOfTurn", "one"))
    session._turn_bytes = 16_000  # 0.5 s
    await session._handle_turn_info(_turn("EndOfTurn", "two"))
    assert recorder.calls == [("one", True, 1.0), ("two", True, 0.5)]


async def test_speechless_forced_end_of_turn_is_dropped_but_still_metered():
    """eot_timeout_ms can force-end a turn that never contained speech — no
    callback (WsSession would otherwise dispatch an empty brain turn), but the
    audio still cost us, so the meter must not carry it into the next turn."""
    session, recorder = _session()
    session._turn_bytes = 16_000 * 2
    await session._handle_turn_info(_turn("EndOfTurn", "  "))
    assert recorder.calls == []
    assert session._turn_bytes == 0


async def test_empty_interim_events_are_dropped():
    session, recorder = _session()
    await session._handle_turn_info(_turn("StartOfTurn", ""))
    await session._handle_turn_info(_turn("Update", "   "))
    assert recorder.calls == []


# ── session lifetime: one connection, many turns ─────────────────────────────


class _FakeSocket:
    """Yields a scripted message list, then ends (as a dropped socket would)."""

    def __init__(self, messages, raise_after: int | None = None):
        self.messages = messages
        self.raise_after = raise_after
        self.sent: list[bytes] = []

    async def __aiter__(self):
        for i, m in enumerate(self.messages):
            if self.raise_after is not None and i == self.raise_after:
                raise ConnectionResetError("socket dropped")
            yield m

    async def send_media(self, data: bytes) -> None:
        self.sent.append(data)


async def test_one_session_spans_multiple_turns():
    """The session used to close on every final and let WsSession reopen. Flux's
    state machine is multi-turn on one connection, and the reopen cost a full
    handshake in the gap — during which send() silently dropped audio, taking
    the first words of a fast follow-up with it."""
    session, recorder = _session()
    session._socket = _FakeSocket(
        [
            _turn("StartOfTurn", "first"),
            _turn("EndOfTurn", "First turn."),
            _turn("StartOfTurn", "second"),
            _turn("EndOfTurn", "Second turn."),
        ]
    )
    await session._read_loop()

    finals = [c for c in recorder.calls if c[1]]
    assert [c[0] for c in finals] == ["First turn.", "Second turn."]
    # Still connected: nothing tore the socket down between the two turns.
    assert session._socket is not None
    assert not session._closed


async def test_send_is_metered_and_forwarded():
    session, _ = _session()
    sock = _FakeSocket([])
    session._socket = sock
    await session.send(b"\x00" * 3200)
    assert sock.sent == [b"\x00" * 3200]
    assert session._turn_bytes == 3200


async def test_send_after_close_is_a_noop():
    session, _ = _session()
    sock = _FakeSocket([])
    session._socket = sock
    session._closed = True
    await session.send(b"\x00" * 100)
    assert sock.sent == []


async def test_supervisor_reconnects_while_audio_is_flowing():
    """A persistent session that lost its socket no longer self-heals via the
    next utterance's reopen — send() suppresses its own exceptions, so without
    the supervisor the connection would go deaf for the rest of the session with
    no error anywhere."""
    import time

    session, _ = _session()
    session._socket = _FakeSocket([_turn("Update", "hi")], raise_after=1)
    session._last_media_ts = time.monotonic()  # actively speaking

    reconnects = []

    async def fake_connect():
        reconnects.append(1)
        session._socket = _FakeSocket([])  # reconnected, then ends cleanly
        if len(reconnects) >= 2:
            session._closed = True

    session._connect = fake_connect
    await session._supervisor()

    assert reconnects, "supervisor did not reconnect during an active conversation"
    assert not session._dormant


async def test_supervisor_goes_dormant_when_client_is_idle():
    """Reconnecting on a drop while nobody is speaking would churn a connection
    every few seconds for as long as the client stays parked. Go dormant and let
    the next send() reopen instead."""
    session, _ = _session()
    session._socket = _FakeSocket([])
    session._last_media_ts = 0.0  # nothing sent in a long time

    reconnects = []

    async def fake_connect():
        reconnects.append(1)

    session._connect = fake_connect
    await session._supervisor()

    assert reconnects == []
    assert session._dormant is True


async def test_dormant_session_reopens_lazily_on_next_audio():
    session, _ = _session()
    session._dormant = True
    session._socket = None
    sock = _FakeSocket([])

    async def fake_connect():
        session._socket = sock

    session._connect = fake_connect
    session._supervisor = _noop_supervisor
    await session.send(b"\x00" * 640)

    assert sock.sent == [b"\x00" * 640]
    assert session._dormant is False


async def _noop_supervisor() -> None:
    return None
