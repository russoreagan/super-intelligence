"""
ActivationEmitter — singleton that bridges brain session events to the UI WebSocket server.
Brain clusters call emit() before/after they fire; the server drains the queue and broadcasts.
"""

from __future__ import annotations

import asyncio
import contextlib
import time


class ActivationEmitter:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    def get_queue(self) -> asyncio.Queue:
        return self._queue

    async def emit(self, cluster: str, intensity: float, note: str, turn_id: str = "") -> None:
        event = {
            "type": "activation",
            "cluster": cluster,
            "intensity": round(intensity, 3),
            "note": note,
            "turn_id": turn_id,
            "ts": time.time(),
        }
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)

    async def emit_neuromod(self, snapshot: dict[str, float]) -> None:
        event = {"type": "neuromod", **{k: round(v, 3) for k, v in snapshot.items()}}
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)

    async def emit_hormonal(self, snapshot: dict[str, float]) -> None:
        event = {"type": "hormonal", **{k: round(v, 3) for k, v in snapshot.items()}}
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)

    async def emit_emotion(self, emotion: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait({"type": "emotion", "emotion": emotion})

    async def emit_user_emotion(self, emotion: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait({"type": "user_emotion", "emotion": emotion})

    async def emit_turn_start(self, turn_id: str, user_input: str, session_id: str = "") -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(
                {
                    "type": "turn_start",
                    "turn_id": turn_id,
                    "user_input": user_input,
                    "session_id": session_id,
                    "ts": time.time(),
                }
            )

    async def emit_turn_end(
        self, turn_id: str, response: str, elapsed_s: float, llm_calls: int
    ) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(
                {
                    "type": "turn_end",
                    "turn_id": turn_id,
                    "response": response,
                    "elapsed_s": round(elapsed_s, 2),
                    "llm_calls": llm_calls,
                    "ts": time.time(),
                }
            )

    async def emit_stream_thought(
        self, thought: str, chem_delta: dict | None = None, proactive: bool = False
    ) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(
                {
                    "type": "stream_thought",
                    "thought": thought,
                    "chem_delta": chem_delta or {},
                    "proactive": proactive,
                }
            )

    async def emit_proactive_speech(self, text: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait({"type": "proactive_speech", "text": text, "ts": time.time()})

    async def emit_cell(self, cluster: str, cell: str, model: str, turn_id: str = "") -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(
                {
                    "type": "cell_activation",
                    "cluster": cluster,
                    "cell": cell,
                    "model": model,
                    "turn_id": turn_id,
                    "ts": time.time(),
                }
            )

    async def emit_event(self, event: dict) -> None:
        """Emit an arbitrary event dict to the UI WebSocket."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)


# Module-level singleton — import and use directly
emitter = ActivationEmitter()
