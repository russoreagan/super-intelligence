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
        # Extra sinks that mirror every event — used by the engine API's SSE stream
        # to follow one turn's events (thoughts, mood, final response).
        self._taps: set[asyncio.Queue] = set()

    def get_queue(self) -> asyncio.Queue:
        return self._queue

    def add_tap(self, q: asyncio.Queue) -> None:
        self._taps.add(q)

    def remove_tap(self, q: asyncio.Queue) -> None:
        self._taps.discard(q)

    def _put(self, event: dict) -> None:
        """Enqueue to the UI broadcast queue and fan out to any taps. Best-effort
        per sink (a full queue drops the event rather than blocking the turn)."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)
        for q in list(self._taps):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

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
            self._put(event)

    async def emit_neuromod(self, snapshot: dict[str, float]) -> None:
        event = {"type": "neuromod", **{k: round(v, 3) for k, v in snapshot.items()}}
        with contextlib.suppress(asyncio.QueueFull):
            self._put(event)

    async def emit_hormonal(self, snapshot: dict[str, float]) -> None:
        event = {"type": "hormonal", **{k: round(v, 3) for k, v in snapshot.items()}}
        with contextlib.suppress(asyncio.QueueFull):
            self._put(event)

    async def emit_emotion(self, emotion: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._put({"type": "emotion", "emotion": emotion})

    async def emit_user_emotion(self, emotion: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._put({"type": "user_emotion", "emotion": emotion})

    async def emit_user_prosody(self, energy: float, pace: float) -> None:
        """Raw user-speech prosody for the 'reading the speaker' meters.
        energy = RMS loudness, pace = speech rate (onsets/sec). The UI
        normalizes these to its segmented bars."""
        with contextlib.suppress(asyncio.QueueFull):
            self._put({"type": "user_prosody", "energy": round(energy, 4), "pace": round(pace, 3)})

    async def emit_turn_start(self, turn_id: str, user_input: str, session_id: str = "") -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._put(
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
            self._put(
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
        self,
        thought: str,
        chem_delta: dict | None = None,
        proactive: bool = False,
        ts: float | None = None,
    ) -> None:
        # ts = when the thought was generated (so the UI shows the real time,
        # not render time — important for thoughts replayed on reconnect).
        with contextlib.suppress(asyncio.QueueFull):
            self._put(
                {
                    "type": "stream_thought",
                    "thought": thought,
                    "chem_delta": chem_delta or {},
                    "proactive": proactive,
                    "ts": ts if ts is not None else time.time(),
                }
            )

    async def emit_proactive_speech(self, text: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._put({"type": "proactive_speech", "text": text, "ts": time.time()})

    async def emit_cell(self, cluster: str, cell: str, model: str, turn_id: str = "") -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._put(
                {
                    "type": "cell_activation",
                    "cluster": cluster,
                    "cell": cell,
                    "model": model,
                    "turn_id": turn_id,
                    "ts": time.time(),
                }
            )

    async def emit_table(
        self, turn_id: str, title: str, columns: list[str], rows: list[list], note: str = ""
    ) -> None:
        """Emit a structured data table (trading layer). Rendered as an HTML <table>."""
        with contextlib.suppress(asyncio.QueueFull):
            self._put(
                {
                    "type": "data_table",
                    "turn_id": turn_id,
                    "title": title,
                    "columns": columns,
                    "rows": rows,
                    "note": note,
                    "ts": time.time(),
                }
            )

    async def emit_chart(self, turn_id: str, spec: dict) -> None:
        """Emit a chart spec (trading layer). Rendered via lightweight-charts.

        spec: {title, kind:"candlestick"|"line", series:[...], overlays:[...], markers:[...]}
        """
        with contextlib.suppress(asyncio.QueueFull):
            self._put({"type": "chart", "turn_id": turn_id, **spec, "ts": time.time()})

    async def emit_event(self, event: dict) -> None:
        """Emit an arbitrary event dict to the UI WebSocket."""
        with contextlib.suppress(asyncio.QueueFull):
            self._put(event)


# Module-level singleton — import and use directly
emitter = ActivationEmitter()
