"""Regression test for the UI websocket audio buffer.

`_receive_loop` is the producer of raw mic audio; `_start_deepgram`'s
`_listen` closure is the consumer that joins the buffered bytes into a full
utterance and publishes it to the auditory bus. The buffer must therefore be
shared between the two — it lives on the `DGSession` handle as `.audio_chunks`.

Previously `_receive_loop` referenced a closure-local `_audio_chunks` that only
existed inside `_start_deepgram`'s scope. On the first audio chunk this raised
`NameError`, which the loop's blanket `except` swallowed — silently breaking the
receive loop and killing voice input. This test pins the corrected behavior.
"""

from __future__ import annotations

import asyncio
import json

from brain.ui.server import UIServer


class _FakeSession:
    """Stand-in for the DGSession handle returned by _start_deepgram."""

    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.sent: list[bytes] = []
        self._task = None

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def finish(self) -> None:  # pragma: no cover - not exercised here
        pass


class _FakeWebSocket:
    """Yields queued messages, then raises to break the receive loop."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.sent_text: list[str] = []

    async def receive(self) -> dict:
        if self._messages:
            return self._messages.pop(0)
        raise RuntimeError("websocket closed")  # → loop's except → break

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)


def test_receive_loop_buffers_audio_on_session_handle():
    server = UIServer(asyncio.Queue())

    fake_session = _FakeSession()

    async def _fake_start(_ws):
        return fake_session

    server._start_deepgram = _fake_start  # type: ignore[assignment]

    ws = _FakeWebSocket(
        [
            {"text": json.dumps({"type": "voice_start"})},
            {"bytes": b"chunk-1"},
            {"bytes": b"chunk-2"},
        ]
    )

    asyncio.run(server._receive_loop(ws))

    # Raw chunks buffered on the session handle (the pre-fix code raised
    # NameError here and broke the loop before the first chunk landed).
    assert fake_session.audio_chunks == [b"chunk-1", b"chunk-2"]
    # ...and each chunk was also forwarded to Deepgram.
    assert fake_session.sent == [b"chunk-1", b"chunk-2"]
