"""
Flux (Listen v2) live-STT session: TurnInfo event handling.

brain/api/stt_live.py runs on Deepgram Flux, whose model-based turn detection
replaced the hand-rolled v1 endpointing/UtteranceEnd assembly. These tests pin
the event→callback contract WsSession depends on: in-progress events forward
as non-final hints, EndOfTurn fires the single authoritative final with a
duration for the STT quota meter, and speechless force-ended turns are dropped.
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


async def test_end_of_turn_is_final_with_word_span_duration():
    session, recorder = _session()
    await session._handle_turn_info(
        _turn(
            "EndOfTurn",
            "Hello there.",
            words=[_word("Hello", 1.0, 1.4), _word("there.", 1.5, 2.25)],
        )
    )
    assert recorder.calls == [("Hello there.", True, 1.25)]


async def test_end_of_turn_falls_back_to_audio_window():
    session, recorder = _session()
    await session._handle_turn_info(
        _turn("EndOfTurn", "ok", audio_window_start=3.0, audio_window_end=4.5)
    )
    assert recorder.calls == [("ok", True, 1.5)]


async def test_speechless_forced_end_of_turn_is_dropped():
    """eot_timeout_ms can force-end a turn that never contained speech —
    no callback (WsSession would otherwise dispatch an empty brain turn)."""
    session, recorder = _session()
    await session._handle_turn_info(_turn("EndOfTurn", "  "))
    assert recorder.calls == []


async def test_empty_interim_events_are_dropped():
    session, recorder = _session()
    await session._handle_turn_info(_turn("StartOfTurn", ""))
    await session._handle_turn_info(_turn("Update", "   "))
    assert recorder.calls == []
