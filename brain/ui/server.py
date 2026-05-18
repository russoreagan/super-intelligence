"""
UI WebSocket server — serves the brain visualizer HTML and pushes activation events.
Runs as asyncio.create_task alongside the brain session.

GET /        → index.html
WebSocket /ws → bidirectional:
    server → client: activation events, neuromod, emotion, turn start/end
    client → server: {"type": "user_message", "text": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

HTML_PATH = Path(__file__).parent / "index.html"


class UIServer:
    def __init__(self, emitter_queue: asyncio.Queue,
                 on_user_message: Callable[[str], None] | None = None) -> None:
        self._queue = emitter_queue
        self._on_user_message = on_user_message
        self._clients: set = set()
        self._last_neuromod: dict = {}
        self._app = None

    def set_message_handler(self, fn: Callable[[str], None]) -> None:
        self._on_user_message = fn

    def _build_app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse

        app = FastAPI(docs_url=None, redoc_url=None)

        @app.get("/")
        async def index():
            html = HTML_PATH.read_text(encoding="utf-8")
            return HTMLResponse(html)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            self._clients.add(websocket)
            logger.info("UI: client connected (%d total)", len(self._clients))

            # Send current neuromod state immediately on connect
            if self._last_neuromod:
                try:
                    await websocket.send_text(json.dumps(
                        {"type": "neuromod", **self._last_neuromod}
                    ))
                except Exception:
                    pass

            # Run receive + broadcast concurrently for this client
            receive_task = asyncio.create_task(self._receive_loop(websocket))
            try:
                await receive_task
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug("UI: ws error: %s", e)
            finally:
                self._clients.discard(websocket)
                receive_task.cancel()
                logger.info("UI: client disconnected (%d remaining)", len(self._clients))

        return app

    async def _start_deepgram(self, websocket) -> object | None:
        """Open a Deepgram live-transcription session for one browser client."""
        try:
            from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
        except ImportError:
            logger.warning("deepgram-sdk not installed; voice disabled")
            return None

        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            try:
                await websocket.send_text(json.dumps(
                    {"type": "transcript_error", "msg": "DEEPGRAM_API_KEY not set"}
                ))
            except Exception:
                pass
            return None

        client = DeepgramClient(api_key)
        conn = client.listen.asynclive.v("1")

        async def _on_transcript(_conn, result, **kwargs):
            try:
                alt = result.channel.alternatives[0]
                text = alt.transcript
                is_final = result.is_final
                if text:
                    await websocket.send_text(json.dumps({
                        "type": "transcript",
                        "text": text,
                        "is_final": is_final,
                    }))
            except Exception as e:
                logger.debug("Deepgram transcript handler: %s", e)

        conn.on(LiveTranscriptionEvents.Transcript, _on_transcript)

        options = LiveOptions(
            model="nova-3",
            language="en-US",
            smart_format=True,
            punctuate=True,
            interim_results=True,
            endpointing=300,
        )
        started = await conn.start(options)
        if not started:
            try:
                await websocket.send_text(json.dumps(
                    {"type": "transcript_error", "msg": "Deepgram connection failed"}
                ))
            except Exception:
                pass
            return None

        logger.info("UI: Deepgram live session started for client")
        return conn

    async def _receive_loop(self, websocket) -> None:
        dg_conn = None  # per-client Deepgram live connection
        while True:
            try:
                msg = await websocket.receive()

                if "text" in msg and msg["text"]:
                    data = json.loads(msg["text"])
                    t = data.get("type")
                    if t == "user_message" and self._on_user_message:
                        text = data.get("text", "").strip()
                        if text:
                            await self._on_user_message(text)
                    elif t == "voice_start":
                        if dg_conn is None:
                            dg_conn = await self._start_deepgram(websocket)
                    elif t == "voice_stop":
                        if dg_conn is not None:
                            try:
                                await dg_conn.finish()
                            except Exception:
                                pass
                            dg_conn = None
                            logger.info("UI: Deepgram live session closed")

                elif "bytes" in msg and msg["bytes"] and dg_conn is not None:
                    await dg_conn.send(msg["bytes"])

            except Exception:
                break

        if dg_conn is not None:
            try:
                await dg_conn.finish()
            except Exception:
                pass

    async def _broadcast_loop(self) -> None:
        """Drain emitter queue and broadcast to all connected clients."""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                # Cache latest neuromod for new clients
                if event.get("type") == "neuromod":
                    self._last_neuromod = {k: v for k, v in event.items() if k != "type"}

                if self._clients:
                    payload = json.dumps(event)
                    dead = set()
                    for client in list(self._clients):
                        try:
                            await client.send_text(payload)
                        except Exception:
                            dead.add(client)
                    self._clients -= dead
            except asyncio.TimeoutError:
                await asyncio.sleep(0.01)

    async def start(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        import uvicorn
        self._app = self._build_app()

        # Start broadcast loop as a background task
        asyncio.create_task(self._broadcast_loop())

        config = uvicorn.Config(
            self._app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info("UI server starting at http://%s:%d", host, port)
        print(f"\nBrain UI: http://{host}:{port}\n")
        await server.serve()
