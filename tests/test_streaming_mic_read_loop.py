"""
Tests for StreamingMicSession._read_loop message-processing logic.

We stub out the Deepgram socket and asyncio bus so tests run without any
network or hardware access.  Each test drives the async-for loop in
_read_loop by feeding hand-crafted message objects.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

def _word(word: str, start: float, end: float, speaker: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        word=word,
        punctuated_word=word,
        start=start,
        end=end,
        speaker=speaker,
        speaker_confidence=1.0,
    )


def _alt(words: list) -> SimpleNamespace:
    return SimpleNamespace(alternatives=[SimpleNamespace(words=words)])


def _speech_started(timestamp: float) -> SimpleNamespace:
    return SimpleNamespace(type="SpeechStarted", timestamp=timestamp)


def _results(words: list, is_final: bool = True) -> SimpleNamespace:
    return SimpleNamespace(type="Results", is_final=is_final, channel=_alt(words))


def _utterance_end(last_word_end: float) -> SimpleNamespace:
    return SimpleNamespace(type="UtteranceEnd", last_word_end=last_word_end)


class _FakeSocket:
    """Async iterable that yields a fixed sequence of messages."""

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
    """Collects publish_dict calls so tests can assert on them."""

    def __init__(self) -> None:
        self.published: list[tuple] = []  # (topic, payload)

    async def publish_dict(self, topic: str, payload: dict, source: str = "") -> None:
        self.published.append((topic, payload))


def _make_session(messages: list[Any]) -> tuple:
    """Return (session, bus) with _socket wired to a FakeSocket."""
    from brain.streaming_mic import StreamingMicSession

    bus = _FakeBus()
    session = StreamingMicSession(
        bus=bus,
        is_speaking_fn=lambda: False,
        on_user_interrupt=None,
    )
    session._socket = _FakeSocket(messages)
    return session, bus


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_loop_returns_immediately_when_socket_is_none():
    from brain.streaming_mic import StreamingMicSession

    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    # _socket stays None (default)
    # Should return without raising
    await session._read_loop()
    assert session.utterances.empty()


@pytest.mark.asyncio
async def test_full_utterance_ends_up_in_queue():
    """SpeechStarted → Results (final) → UtteranceEnd produces one utterance."""
    msgs = [
        _speech_started(1.0),
        _results([_word("hello", 1.0, 1.3), _word("world", 1.4, 1.7)]),
        _utterance_end(1.7),
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    assert not session.utterances.empty()
    utt = session.utterances.get_nowait()
    assert utt["transcript"] == "hello world"
    assert utt["diarized_words"][0]["word"] == "hello"
    assert utt["diarized_words"][1]["word"] == "world"


@pytest.mark.asyncio
async def test_interim_results_are_ignored():
    """Only is_final=True Results should accumulate words."""
    msgs = [
        _speech_started(0.5),
        _results([_word("hel", 0.5, 0.7)], is_final=False),   # interim — discard
        _results([_word("hello", 0.5, 0.9)], is_final=True),   # final — keep
        _utterance_end(0.9),
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    utt = session.utterances.get_nowait()
    assert utt["transcript"] == "hello"
    assert len(utt["diarized_words"]) == 1


@pytest.mark.asyncio
async def test_missing_speech_started_falls_back_to_first_word_start():
    """If SpeechStarted never fires, utterance start falls back to first word."""
    msgs = [
        _results([_word("hi", 2.0, 2.3)]),
        _utterance_end(2.3),
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    utt = session.utterances.get_nowait()
    assert utt["transcript"] == "hi"
    # duration should be based on 2.0 → 2.3 (± floating point)
    assert abs(utt["duration_s"] - 0.3) < 0.01


@pytest.mark.asyncio
async def test_empty_utterance_no_transcript_no_audio_is_skipped():
    """UtteranceEnd with zero audio and empty transcript must not put to queue."""
    msgs = [
        _utterance_end(0.0),  # no prior SpeechStarted, no Results
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    assert session.utterances.empty()


@pytest.mark.asyncio
async def test_multiple_results_events_before_utterance_end():
    """Words from consecutive final Results should all appear in one utterance."""
    msgs = [
        _speech_started(0.0),
        _results([_word("one", 0.0, 0.3), _word("two", 0.4, 0.6)]),
        _results([_word("three", 0.7, 1.0)]),
        _utterance_end(1.0),
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    utt = session.utterances.get_nowait()
    assert utt["transcript"] == "one two three"
    assert len(utt["diarized_words"]) == 3


@pytest.mark.asyncio
async def test_speech_started_only_records_first_timestamp():
    """Second SpeechStarted (mid-sentence pause) must not overwrite the start time."""
    msgs = [
        _speech_started(1.0),   # original burst — should be the utterance start
        _results([_word("hmm", 1.0, 1.2)]),
        _speech_started(2.5),   # second burst after a pause — MUST be ignored
        _results([_word("okay", 2.5, 2.9)]),
        _utterance_end(2.9),
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    utt = session.utterances.get_nowait()
    assert utt["transcript"] == "hmm okay"
    # Duration should be from 1.0, not from the second SpeechStarted at 2.5
    assert abs(utt["duration_s"] - (2.9 - 1.0)) < 0.01


@pytest.mark.asyncio
async def test_two_utterances_in_sequence():
    """The read loop should handle back-to-back utterances in one socket session."""
    msgs = [
        _speech_started(0.0),
        _results([_word("first", 0.0, 0.4)]),
        _utterance_end(0.4),
        _speech_started(1.0),
        _results([_word("second", 1.0, 1.5)]),
        _utterance_end(1.5),
    ]
    session, bus = _make_session(msgs)

    await session._read_loop()

    assert session.utterances.qsize() == 2
    first = session.utterances.get_nowait()
    second = session.utterances.get_nowait()
    assert first["transcript"] == "first"
    assert second["transcript"] == "second"


@pytest.mark.asyncio
async def test_cancelled_error_propagates():
    """CancelledError from inside the loop must not be swallowed."""

    class _CancelSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncio.CancelledError

    from brain.streaming_mic import StreamingMicSession

    bus = _FakeBus()
    session = StreamingMicSession(bus=bus, is_speaking_fn=lambda: False)
    session._socket = _CancelSocket()

    with pytest.raises(asyncio.CancelledError):
        await session._read_loop()
