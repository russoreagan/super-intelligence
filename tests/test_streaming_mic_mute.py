"""
Tests for StreamingMicSession mute/unmute behaviour.

Covers the regression where voice input stopped working after muting:
  - Stale _utterance_start_s / _pending_words left over from before mute
    would corrupt the timestamps of the next real utterance.
  - Deepgram events arriving *during* mute (SpeechStarted, Results,
    UtteranceEnd for silence) would corrupt state or queue spurious
    empty/silence utterances.
  - Reconnect after a dropped connection did not reset utterance state,
    causing old words to bleed into the new session.

All tests run without network or hardware: Deepgram and sounddevice are
stubbed out entirely.
"""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ── message factories ─────────────────────────────────────────────────────────

def _word(word: str, start: float, end: float, speaker: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        word=word, punctuated_word=word,
        start=start, end=end,
        speaker=speaker, speaker_confidence=1.0,
    )


def _alt(words: list) -> SimpleNamespace:
    return SimpleNamespace(alternatives=[SimpleNamespace(words=words)])


def _speech_started(timestamp: float) -> SimpleNamespace:
    return SimpleNamespace(type="SpeechStarted", timestamp=timestamp)


def _results(words: list, is_final: bool = True) -> SimpleNamespace:
    return SimpleNamespace(type="Results", is_final=is_final, channel=_alt(words))


def _utterance_end(last_word_end: float) -> SimpleNamespace:
    return SimpleNamespace(type="UtteranceEnd", last_word_end=last_word_end)


# ── socket / bus stubs ────────────────────────────────────────────────────────

class _FakeSocket:
    """Async iterable that yields a fixed sequence of messages then stops."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages

    def __aiter__(self):
        return _FakeSocketIter(iter(self._messages))


class _FakeSocketIter:
    def __init__(self, it) -> None:
        self._it = it

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple] = []

    async def publish_dict(self, topic: str, payload: dict, source: str = "") -> None:
        self.published.append((topic, payload))


# ── session factory ───────────────────────────────────────────────────────────

def _make_session(messages: list[Any] | None = None) -> tuple:
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    if messages is not None:
        session._socket = _FakeSocket(messages)
    return session, bus


# ═══════════════════════════════════════════════════════════════════════════════
# Mute / unmute state management
# ═══════════════════════════════════════════════════════════════════════════════

def test_mute_sets_flag():
    session, _ = _make_session()
    assert not session.is_muted
    session.mute()
    assert session.is_muted


def test_unmute_clears_flag():
    session, _ = _make_session()
    session.mute()
    session.unmute()
    assert not session.is_muted


def test_mute_toggle_returns_new_state():
    session, _ = _make_session()
    assert session.toggle_mute() is True    # muted
    assert session.toggle_mute() is False   # unmuted


def test_mute_resets_in_progress_utterance_start():
    """mute() must clear _utterance_start_s so the next utterance gets a fresh timestamp."""
    session, _ = _make_session()
    session._utterance_start_s = 3.0   # simulates being mid-utterance when muted
    session.mute()
    assert session._utterance_start_s is None


def test_mute_resets_pending_words():
    """mute() must clear _pending_words so stale words don't appear in the next utterance."""
    session, _ = _make_session()
    session._pending_words = [{"word": "hello", "start": 1.0, "end": 1.3, "speaker": 0}]
    session.mute()
    assert session._pending_words == []


def test_double_mute_is_idempotent():
    """Calling mute() twice must not clear state set between the two calls."""
    session, _ = _make_session()
    session.mute()
    # After muting, something sets utterance state (edge case: shouldn't happen but be safe)
    session._utterance_start_s = 99.0
    session.mute()          # second mute — should be a no-op
    # state set between the two mutes is preserved (second call skips the if-branch)
    assert session._utterance_start_s == 99.0


def test_is_user_speaking_false_when_muted():
    session, _ = _make_session()
    session._utterance_start_s = 1.0   # would be True if unmuted
    session.mute()
    assert not session.is_user_speaking


def test_is_user_speaking_true_when_unmuted_with_pending():
    session, _ = _make_session()
    session._utterance_start_s = 1.0
    assert session.is_user_speaking


# ═══════════════════════════════════════════════════════════════════════════════
# _read_loop: events during mute must not corrupt state
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_speech_started_during_mute_does_not_set_start_time():
    """SpeechStarted arriving while muted must be ignored; _utterance_start_s stays None."""
    msgs = [_speech_started(5.0)]
    session, _ = _make_session(msgs)
    session.mute()

    await session._read_loop()

    assert session._utterance_start_s is None


@pytest.mark.asyncio
async def test_speech_started_during_mute_does_not_overwrite_cleared_start():
    """SpeechStarted during mute must not restore a stale timestamp that mute() cleared."""
    msgs = [_speech_started(5.0)]
    session, _ = _make_session(msgs)
    # Simulate: was mid-utterance, user muted (state cleared), then Deepgram fires SpeechStarted
    session._utterance_start_s = 2.0
    session.mute()   # clears _utterance_start_s → None
    assert session._utterance_start_s is None

    await session._read_loop()

    assert session._utterance_start_s is None


@pytest.mark.asyncio
async def test_results_during_mute_do_not_accumulate_words():
    """Final Results arriving while muted must not add words to _pending_words."""
    msgs = [_results([_word("noise", 1.0, 1.3)])]
    session, _ = _make_session(msgs)
    session.mute()

    await session._read_loop()

    assert session._pending_words == []


@pytest.mark.asyncio
async def test_utterance_end_during_mute_not_queued():
    """UtteranceEnd while muted must not put anything into the utterances queue."""
    msgs = [
        _speech_started(1.0),
        _results([_word("hello", 1.0, 1.3)]),
        _utterance_end(1.3),
    ]
    session, _ = _make_session(msgs)
    session.mute()

    await session._read_loop()

    assert session.utterances.empty()


@pytest.mark.asyncio
async def test_utterance_end_during_mute_resets_state():
    """UtteranceEnd while muted must reset utterance state so the next unmuted
    utterance starts clean."""
    msgs = [_utterance_end(3.0)]
    session, _ = _make_session(msgs)
    # Force some pre-existing stale state (bypassing the mute() reset for the test)
    session._muted = True
    session._utterance_start_s = 1.0
    session._pending_words = [{"word": "stale", "start": 1.0, "end": 1.2, "speaker": 0}]

    await session._read_loop()

    assert session._utterance_start_s is None
    assert session._pending_words == []
    assert session.utterances.empty()


# ═══════════════════════════════════════════════════════════════════════════════
# Core regression: second utterance after mute/unmute works correctly
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_second_utterance_after_mute_unmute():
    """Full regression test: first utterance → mute events → unmuted second utterance.

    The second utterance must have correct transcript and a fresh start time —
    not the stale timestamp from before the mute.
    """
    # Phase 1: first real utterance
    phase1 = [
        _speech_started(1.0),
        _results([_word("hello", 1.0, 1.3)]),
        _utterance_end(1.3),
    ]
    session, bus = _make_session(phase1)
    await session._read_loop()

    utt1 = session.utterances.get_nowait()
    assert utt1["transcript"] == "hello"

    # Phase 2: mute, then Deepgram fires noise events (should be ignored)
    session.mute()
    session._socket = _FakeSocket([
        _speech_started(5.0),                              # noise SpeechStarted
        _results([_word("the", 5.0, 5.1)]),               # noise Results
        _utterance_end(5.1),                               # silence UtteranceEnd
    ])
    await session._read_loop()

    # Nothing queued from mute period
    assert session.utterances.empty()
    # State clean after muted UtteranceEnd
    assert session._utterance_start_s is None
    assert session._pending_words == []

    # Phase 3: unmute, user speaks again
    session.unmute()
    session._socket = _FakeSocket([
        _speech_started(10.0),
        _results([_word("world", 10.0, 10.3)]),
        _utterance_end(10.3),
    ])
    await session._read_loop()

    assert not session.utterances.empty()
    utt2 = session.utterances.get_nowait()
    assert utt2["transcript"] == "world"
    # Start time must be from the unmuted SpeechStarted, not any mute-period event
    assert abs(utt2["duration_s"] - 0.3) < 0.01


@pytest.mark.asyncio
async def test_second_utterance_after_mute_mid_utterance():
    """Muting while an utterance is in progress must not corrupt the next utterance."""
    # SpeechStarted fires, then user mutes before UtteranceEnd
    session, _ = _make_session([])
    session._utterance_start_s = 1.0
    session._pending_words = [{"word": "partial", "start": 1.0, "end": 1.2, "speaker": 0}]

    # Mute clears the in-progress state
    session.mute()
    assert session._utterance_start_s is None
    assert session._pending_words == []

    # After unmute, new utterance processes cleanly
    session.unmute()
    session._socket = _FakeSocket([
        _speech_started(5.0),
        _results([_word("fresh", 5.0, 5.4)]),
        _utterance_end(5.4),
    ])
    await session._read_loop()

    utt = session.utterances.get_nowait()
    assert utt["transcript"] == "fresh"
    assert abs(utt["duration_s"] - 0.4) < 0.01


@pytest.mark.asyncio
async def test_multiple_mute_cycles():
    """Three alternating mute/unmute cycles each produce exactly one utterance."""
    session, _ = _make_session([])

    for i, word in enumerate(["alpha", "beta", "gamma"]):
        # Unmute and speak
        session.unmute()
        session._socket = _FakeSocket([
            _speech_started(float(i * 10)),
            _results([_word(word, float(i * 10), float(i * 10 + 0.5))]),
            _utterance_end(float(i * 10 + 0.5)),
        ])
        await session._read_loop()

        # Mute (noise events during mute)
        session.mute()
        session._socket = _FakeSocket([
            _speech_started(float(i * 10 + 1.0)),   # noise
            _utterance_end(float(i * 10 + 1.1)),    # silence UtteranceEnd
        ])
        await session._read_loop()

    assert session.utterances.qsize() == 3
    words = [session.utterances.get_nowait()["transcript"] for _ in range(3)]
    assert words == ["alpha", "beta", "gamma"]


# ═══════════════════════════════════════════════════════════════════════════════
# _enqueue_chunk: mute replaces audio with silence
# ═══════════════════════════════════════════════════════════════════════════════

def test_enqueue_chunk_sends_silence_when_muted():
    """Muted chunks must be replaced with all-zero bytes of the same length."""
    session, _ = _make_session()
    session.mute()

    real_audio = bytes(range(256)) * 4   # 1024 bytes of non-zero data
    session._enqueue_chunk(real_audio)

    queued = session._pcm_in.get_nowait()
    assert len(queued) == len(real_audio)
    assert queued == b"\x00" * len(real_audio)


def test_enqueue_chunk_passes_audio_when_unmuted_above_gate():
    """Unmuted chunks above the noise gate pass through unchanged."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session.NOISE_GATE_RMS = 0.0   # disable gate

    # Build a chunk with high RMS (large int16 values)
    n_samples = 160
    samples = [10000] * n_samples
    loud_audio = struct.pack(f"<{n_samples}h", *samples)

    session._enqueue_chunk(loud_audio)

    queued = session._pcm_in.get_nowait()
    assert queued == loud_audio   # unchanged


def test_noise_gate_replaces_quiet_audio_with_silence():
    """Audio whose RMS is below NOISE_GATE_RMS must be silenced."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session.NOISE_GATE_RMS = 500.0   # high threshold

    # Low-RMS audio: small int16 values
    n_samples = 160
    samples = [10] * n_samples   # RMS ≈ 10, far below 500
    quiet_audio = struct.pack(f"<{n_samples}h", *samples)

    session._enqueue_chunk(quiet_audio)

    queued = session._pcm_in.get_nowait()
    assert queued == b"\x00" * len(quiet_audio)


def test_noise_gate_passes_loud_audio():
    """Audio whose RMS is above NOISE_GATE_RMS must reach Deepgram unchanged."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session.NOISE_GATE_RMS = 100.0

    n_samples = 160
    samples = [2000] * n_samples   # RMS = 2000, well above 100
    loud_audio = struct.pack(f"<{n_samples}h", *samples)

    session._enqueue_chunk(loud_audio)

    queued = session._pcm_in.get_nowait()
    assert queued == loud_audio


def test_enqueue_chunk_drops_oldest_when_full():
    """When the PCM queue is full, the oldest chunk must be dropped to make room."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session.NOISE_GATE_RMS = 0.0

    # Fill the queue to capacity (maxsize=200)
    dummy = b"\x01" * 10
    for _ in range(200):
        session._pcm_in.put_nowait(dummy)

    new_chunk = b"\x02" * 10
    session._enqueue_chunk(new_chunk)   # must not raise, must replace oldest

    assert session._pcm_in.qsize() == 200
    # Drain to find the new chunk at the end
    items = []
    while not session._pcm_in.empty():
        items.append(session._pcm_in.get_nowait())
    assert items[-1] == new_chunk


# ═══════════════════════════════════════════════════════════════════════════════
# _keepalive_loop
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_keepalive_sends_to_socket():
    """_keepalive_loop must call send_keep_alive() on the active socket."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session._running = True

    mock_socket = AsyncMock()
    session._socket = mock_socket

    # Patch sleep in the streaming_mic module so _fake_sleep itself can still
    # call real asyncio.sleep without triggering recursion.
    _real_sleep = asyncio.sleep
    call_count = 0

    async def _fake_sleep(t):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            session._running = False
        await _real_sleep(0)

    with patch("brain.streaming_mic.asyncio.sleep", side_effect=_fake_sleep):
        await session._keepalive_loop()

    mock_socket.send_keep_alive.assert_awaited()


@pytest.mark.asyncio
async def test_keepalive_skips_when_socket_is_none():
    """_keepalive_loop must not crash when _socket is None (reconnect window)."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session._running = True
    session._socket = None

    _real_sleep = asyncio.sleep
    call_count = 0

    async def _fake_sleep(t):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            session._running = False
        await _real_sleep(0)

    with patch("brain.streaming_mic.asyncio.sleep", side_effect=_fake_sleep):
        await session._keepalive_loop()   # must not raise


@pytest.mark.asyncio
async def test_keepalive_survives_send_failure():
    """_keepalive_loop must continue after a send_keep_alive() exception."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session._running = True

    mock_socket = AsyncMock()
    mock_socket.send_keep_alive.side_effect = Exception("ws closed")
    session._socket = mock_socket

    _real_sleep = asyncio.sleep
    call_count = 0

    async def _fake_sleep(t):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            session._running = False
        await _real_sleep(0)

    with patch("brain.streaming_mic.asyncio.sleep", side_effect=_fake_sleep):
        await session._keepalive_loop()   # must not propagate the exception

    # send_keep_alive was called (and raised) but the loop continued
    assert mock_socket.send_keep_alive.await_count >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# _reader_supervisor: state reset on reconnect
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reader_supervisor_resets_state_on_reconnect():
    """When _reader_supervisor opens a new Deepgram session, it must clear
    _utterance_start_s and _pending_words so stale state doesn't bleed through."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session._running = True

    # Prime with stale state (simulates: mid-utterance when the WS dropped)
    session._utterance_start_s = 5.0
    session._pending_words = [{"word": "stale", "start": 5.0, "end": 5.2, "speaker": 0}]

    reconnect_happened = False

    async def _fake_open_deepgram():
        nonlocal reconnect_happened
        reconnect_happened = True
        session._socket = _FakeSocket([])   # new empty socket
        session._running = False            # stop after one cycle

    async def _fake_read_loop():
        raise Exception("simulated 1011 drop")

    async def _fake_close_deepgram():
        session._socket = None

    session._open_deepgram = _fake_open_deepgram
    session._read_loop = _fake_read_loop
    session._close_deepgram = _fake_close_deepgram

    _real_sleep = asyncio.sleep

    async def _noop_sleep(t):
        await _real_sleep(0)

    with patch("brain.streaming_mic.asyncio.sleep", side_effect=_noop_sleep):
        await session._reader_supervisor()

    assert reconnect_happened
    assert session._utterance_start_s is None
    assert session._pending_words == []


@pytest.mark.asyncio
async def test_reader_supervisor_stops_on_cancelled_error():
    """CancelledError from _read_loop must cause _reader_supervisor to return, not reconnect."""
    from brain.streaming_mic import StreamingMicSession
    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session._running = True

    reconnect_count = 0

    async def _fake_read_loop():
        raise asyncio.CancelledError

    async def _fake_open():
        nonlocal reconnect_count
        reconnect_count += 1

    session._read_loop = _fake_read_loop
    session._open_deepgram = _fake_open
    session._close_deepgram = AsyncMock()

    await session._reader_supervisor()

    assert reconnect_count == 0   # must not have tried to reconnect


# ═══════════════════════════════════════════════════════════════════════════════
# _rolling_pcm buffer
# ═══════════════════════════════════════════════════════════════════════════════

def test_rolling_pcm_slice_basic():
    from brain.streaming_mic import BYTES_PER_SAMPLE, SAMPLE_RATE, _RollingPCM
    buf = _RollingPCM(SAMPLE_RATE)
    # Write 1 second of audio (2 bytes per sample × 16000 samples = 32000 bytes)
    one_sec = b"\x01\x02" * SAMPLE_RATE
    buf.append(one_sec)

    sliced = buf.slice(0.0, 1.0)
    assert len(sliced) == SAMPLE_RATE * BYTES_PER_SAMPLE


def test_rolling_pcm_discards_oldest():
    from brain.streaming_mic import BYTES_PER_SAMPLE, SAMPLE_RATE, _RollingPCM
    buf = _RollingPCM(SAMPLE_RATE, max_seconds=1.0)
    one_sec = b"\x01\x02" * SAMPLE_RATE

    buf.append(one_sec)  # t=0–1s
    buf.append(one_sec)  # t=1–2s; first second should be discarded

    # Slicing the first second should return empty (it's been discarded)
    assert buf.slice(0.0, 1.0) == b""
    # Second second is still present
    assert len(buf.slice(1.0, 2.0)) == SAMPLE_RATE * BYTES_PER_SAMPLE


def test_rolling_pcm_empty_slice_returns_empty():
    from brain.streaming_mic import SAMPLE_RATE, _RollingPCM
    buf = _RollingPCM(SAMPLE_RATE)
    assert buf.slice(5.0, 5.0) == b""
    assert buf.slice(5.0, 3.0) == b""   # reversed range
