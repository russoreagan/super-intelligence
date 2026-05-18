"""
ActivationEmitter — singleton that bridges brain session events to the UI WebSocket server.
Brain clusters call emit() before/after they fire; the server drains the queue and broadcasts.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class ActivationEmitter:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    def get_queue(self) -> asyncio.Queue:
        return self._queue

    async def emit(self, cluster: str, intensity: float, note: str,
                   turn_id: str = "") -> None:
        event = {
            "type": "activation",
            "cluster": cluster,
            "intensity": round(intensity, 3),
            "note": note,
            "turn_id": turn_id,
            "ts": time.time(),
        }
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def emit_neuromod(self, snapshot: dict[str, float]) -> None:
        event = {"type": "neuromod", **{k: round(v, 3) for k, v in snapshot.items()}}
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def emit_emotion(self, emotion: str) -> None:
        try:
            self._queue.put_nowait({"type": "emotion", "emotion": emotion})
        except asyncio.QueueFull:
            pass

    async def emit_turn_start(self, turn_id: str, user_input: str) -> None:
        try:
            self._queue.put_nowait({
                "type": "turn_start",
                "turn_id": turn_id,
                "user_input": user_input,
                "ts": time.time(),
            })
        except asyncio.QueueFull:
            pass

    async def emit_turn_end(self, turn_id: str, response: str,
                             elapsed_s: float, llm_calls: int) -> None:
        try:
            self._queue.put_nowait({
                "type": "turn_end",
                "turn_id": turn_id,
                "response": response,
                "elapsed_s": round(elapsed_s, 2),
                "llm_calls": llm_calls,
                "ts": time.time(),
            })
        except asyncio.QueueFull:
            pass

    async def emit_stream_thought(self, thought: str) -> None:
        try:
            self._queue.put_nowait({"type": "stream_thought", "thought": thought})
        except asyncio.QueueFull:
            pass


# Module-level singleton — import and use directly
emitter = ActivationEmitter()
