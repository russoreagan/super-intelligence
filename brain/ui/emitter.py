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
        # Live partner-webhook deliveries (proactive outbound). Held so the
        # fire-and-forget POST tasks aren't garbage-collected before they finish.
        self._webhook_tasks: set[asyncio.Task] = set()

    def get_queue(self) -> asyncio.Queue:
        return self._queue

    def add_tap(self, q: asyncio.Queue) -> None:
        self._taps.add(q)

    def remove_tap(self, q: asyncio.Queue) -> None:
        self._taps.discard(q)

    def _put(self, event: dict) -> None:
        """Enqueue to the UI broadcast queue and fan out to any taps. Best-effort
        per sink (a full queue drops the event rather than blocking the turn)."""
        self._stamp_lane(event)
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)
        for q in list(self._taps):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)
        # Mirror self-directed work (task_* events) to the owning partner so its UI can
        # show a live "what it's working on" tray. No-op on the owner lane / unconfigured.
        self._maybe_forward_work(event)

    def _maybe_forward_work(self, event: dict) -> None:
        """Forward a motor-cortex work event (task_planning/start/step_*/complete) to a
        partner's work webhook so its UI can show a live "what it's working on" tray.
        Generic and best-effort; only fires when AGENT_WORK_WEBHOOK_URL is configured.

        Attribution: an agent-lane turn (a tenant drove the work) carries the end-user on
        the event. But the brain's OWN self-directed/idle jobs run on the *owner* lane with
        no end-user (execute_internal_job is not wrapped in bind_turn), so we fall back to
        AGENT_WORK_DEFAULT_END_USER_ID — the single tenant that should see autonomous work.
        Without that env set, owner-lane work isn't forwarded (stays private to the brain's
        own UI). Crucially we DON'T re-lane the event: leaving channel="owner" keeps the job
        visible in the brain's own Tasks panel while still mirroring it to the partner."""
        import os

        if not str(event.get("type", "")).startswith("task_"):
            return
        url = os.environ.get("AGENT_WORK_WEBHOOK_URL", "").strip()
        secret = os.environ.get("AGENT_WEBHOOK_SECRET", "").strip()
        if not url or not secret:
            return
        target = (event.get("end_user_id") or "").strip() or os.environ.get(
            "AGENT_WORK_DEFAULT_END_USER_ID", ""
        ).strip()
        if not target:
            return
        # Copy so the other sinks (owner UI, engine taps) keep reading the unmodified event;
        # stamp the resolved target end-user without touching the event's lane.
        payload = dict(event)
        payload["end_user_id"] = target
        with contextlib.suppress(RuntimeError):  # no running loop → nothing to schedule
            task = asyncio.create_task(self._post_partner_webhook(url, secret, payload))
            self._webhook_tasks.add(task)
            task.add_done_callback(self._webhook_tasks.discard)

    @staticmethod
    def _stamp_lane(event: dict) -> None:
        """Tag the event with the current turn's routing lane so downstream sinks
        can keep agents apart: ``channel`` always, plus the agent session
        (``route_sid``) / agent_id / end_user_id for the agent lane. Owner-lane
        events (interactive UI + idle inner life) carry only channel="owner".
        Uses distinct keys so it never collides with the cosmetic process
        ``session_id`` that turn_start/turn_end already carry."""
        with contextlib.suppress(Exception):
            from brain.turn_ctx import current_turn

            ctx = current_turn()
            event.setdefault("channel", ctx["channel"])
            if ctx["channel"] == "agent":
                event.setdefault("route_sid", ctx["session_id"])
                if ctx["agent_id"]:
                    event.setdefault("agent_id", ctx["agent_id"])
                if ctx["end_user_id"]:
                    event.setdefault("end_user_id", ctx["end_user_id"])

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

    async def emit_emotion(self, emotion: str, intensity: float | None = None) -> None:
        event = {"type": "emotion", "emotion": emotion}
        if intensity is not None:
            event["intensity"] = round(float(intensity), 3)
        with contextlib.suppress(asyncio.QueueFull):
            self._put(event)

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

    @staticmethod
    def _is_json_blob(text: str | None) -> bool:
        """A raw JSON object/array rather than spoken prose. Proactive speech is
        always natural language headed for TTS and (in engine mode) a customer's
        channel; a degenerate local model sometimes emits an echoed tool output or
        a confabulated response schema (e.g. ``{"has_signal": ...}``) instead.
        Spoken text never starts with a brace/bracket — catch that here so no raw
        JSON reaches the UI or the partner webhook, whatever the source."""
        if not text:
            return False
        t = text.strip()
        if t.startswith("```"):
            t = t[3:].lstrip()
            # Drop an optional language label (e.g. "json") on the fence's first line.
            nl = t.find("\n")
            if nl != -1 and t[:nl].strip().isalpha():
                t = t[nl + 1 :]
            t = t.lstrip()
        return t.startswith("{") or t.startswith("[")

    async def emit_proactive_speech(self, text: str, *, partner_target: str = "") -> None:
        # Last-line guard: never surface a raw JSON blob as proactive speech. The
        # source paths (result reporter, planner clarification) already filter it;
        # this covers every other proactive caller too.
        if self._is_json_blob(text):
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._put({"type": "proactive_speech", "text": text, "ts": time.time()})
        # Durable delivery to the owning partner's callback (works with the partner's
        # client closed). No-op on the owner lane or when no callback is configured —
        # unless a self-directed job-result caller supplies partner_target (the owning
        # tenant), which reaches the partner without re-laning the local UI event above.
        self._dispatch_partner_proactive(text, partner_target=partner_target)

    def _dispatch_partner_proactive(
        self,
        text: str,
        *,
        kind: str = "proactive",
        urgency: str = "normal",
        partner_target: str = "",
    ) -> None:
        """Schedule a best-effort webhook POST to the owning partner's callback for a
        proactive message. Generic — the brain knows nothing about the partner's domain;
        it just forwards "agent emitted a message for end-user X".

        Agent-lane turns deliver to the end-user that drove them. Owner-lane callers (the
        brain's own self-directed jobs) carry no end-user on the context, so the result
        would otherwise be dropped — a job-result caller may pass ``partner_target`` (the
        owning tenant) to reach the partner anyway, mirroring _maybe_forward_work's
        owner-lane fallback. Crucially this does NOT re-lane the local UI event, so the
        summary still shows in the brain's own feed. No-op when unconfigured, or on the
        owner lane with no partner_target (the brain's private musings stay private)."""
        import os

        url = os.environ.get("AGENT_WEBHOOK_URL", "").strip()
        secret = os.environ.get("AGENT_WEBHOOK_SECRET", "").strip()
        if not url or not secret:
            return
        from brain.turn_ctx import current_turn

        ctx = current_turn()
        if ctx.get("channel") == "agent" and ctx.get("end_user_id"):
            target = ctx["end_user_id"]
        else:
            target = (partner_target or "").strip()
        if not target:
            return
        import uuid

        payload = {
            "event_id": uuid.uuid4().hex,
            "agent_id": ctx.get("agent_id") or None,
            "session_id": ctx.get("session_id") or "",
            "end_user_id": target,
            "kind": kind,
            "text": text,
            "urgency": urgency,
            "ts": time.time(),
        }
        with contextlib.suppress(RuntimeError):  # no running loop → nothing to schedule
            task = asyncio.create_task(self._post_partner_webhook(url, secret, payload))
            self._webhook_tasks.add(task)
            task.add_done_callback(self._webhook_tasks.discard)

    @staticmethod
    async def _post_partner_webhook(url: str, secret: str, payload: dict) -> None:
        """One best-effort webhook POST. Never raises — a dead partner callback must
        not affect the brain."""
        with contextlib.suppress(Exception):
            import httpx

            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                await client.post(
                    url, json=payload, headers={"Authorization": f"Bearer {secret}"}
                )

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
