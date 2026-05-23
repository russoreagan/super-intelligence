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
from typing import TYPE_CHECKING, Callable

# FastAPI/WebSocket imports at module level so that `from __future__ import annotations`
# (PEP 563 lazy strings) doesn't prevent FastAPI's dependency injector from resolving
# the `WebSocket` annotation in ws_endpoint. When these are imported only inside
# _build_app(), the string 'WebSocket' can't be found in the module globals and FastAPI
# misclassifies the parameter as a query param, causing an immediate 403 on every
# WebSocket connection attempt.
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
except ImportError:
    FastAPI = None  # type: ignore[assignment,misc]
    WebSocket = None  # type: ignore[assignment]
    WebSocketDisconnect = None  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

HTML_PATH = Path(__file__).parent / "index.html"


class UIServer:
    def __init__(self, emitter_queue: asyncio.Queue,
                 on_user_message: Callable[[str], None] | None = None,
                 on_voice_change: Callable[[str], None] | None = None,
                 on_eval_mode: Callable[[bool], None] | None = None,
                 on_mic_toggle: Callable[[], bool] | None = None,
                 python_voice_mode: bool = False) -> None:
        self._queue = emitter_queue
        self._on_user_message = on_user_message
        self._on_voice_change = on_voice_change
        self._on_eval_mode = on_eval_mode
        self._on_mic_toggle = on_mic_toggle      # () -> is_muted (bool)
        self._python_voice_mode = python_voice_mode
        self._clients: set = set()
        self._last_neuromod: dict = {}
        self._wiring_frozen: bool = False
        self._app = None

    def set_wiring_frozen(self, frozen: bool) -> None:
        self._wiring_frozen = bool(frozen)

    def set_message_handler(self, fn: Callable[[str], None]) -> None:
        self._on_user_message = fn

    def set_voice_change_handler(self, fn: Callable[[str], None]) -> None:
        self._on_voice_change = fn

    def _build_app(self):
        app = FastAPI(docs_url=None, redoc_url=None)

        from fastapi.responses import FileResponse

        @app.get("/")
        async def index():
            html = HTML_PATH.read_text(encoding="utf-8")
            return HTMLResponse(html)

        @app.get("/voices")
        async def list_voices():
            """Return the user's custom ElevenLabs voices (non-premade)."""
            import httpx
            api_key = os.environ.get("ELEVENLABS_API_KEY", "")
            if not api_key:
                return {"voices": []}
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    r = await client.get(
                        "https://api.elevenlabs.io/v1/voices",
                        headers={"xi-api-key": api_key},
                    )
                    r.raise_for_status()
                    data = r.json()
                voices = [
                    {"voice_id": v["voice_id"], "name": v["name"]}
                    for v in data.get("voices", [])
                    if v.get("category", "premade") != "premade"
                ]
                return {"voices": voices}
            except Exception as e:
                logger.warning("Failed to fetch ElevenLabs voices: %s", e)
                return {"voices": []}

        ui_dir = HTML_PATH.parent

        @app.get("/{filename}")
        async def static_asset(filename: str):
            from fastapi.responses import FileResponse
            from fastapi import HTTPException
            filepath = ui_dir / filename
            if filepath.is_file() and filepath.parent == ui_dir:
                return FileResponse(str(filepath))
            raise HTTPException(status_code=404)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            self._clients.add(websocket)
            logger.info("UI: client connected (%d total)", len(self._clients))

            # Tell the client whether Python voice mode is active so it
            # switches the mic button from press-to-talk to a persistent toggle.
            try:
                await websocket.send_text(json.dumps({
                    "type": "voice_mode",
                    "active": self._python_voice_mode,
                    "muted": False,
                }))
            except Exception:
                pass

            # Send current neuromod state immediately on connect
            if self._last_neuromod:
                try:
                    await websocket.send_text(json.dumps(
                        {"type": "neuromod", **self._last_neuromod}
                    ))
                except Exception:
                    pass

            # Tell the client about wiring state (frozen tag in plasticity panel)
            try:
                await websocket.send_text(json.dumps({
                    "type": "wiring_status",
                    "frozen": self._wiring_frozen,
                }))
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
        """
        Open a Deepgram live-transcription session for one browser client.
        Compatible with deepgram-sdk v7+ which uses an async context-manager API.
        Returns a DGSession handle with .send(bytes) and .finish() methods.
        """
        try:
            from deepgram import AsyncDeepgramClient
            from deepgram.listen.v1.types import ListenV1Results
        except ImportError:
            logger.warning("deepgram-sdk not installed — mic disabled. Run 'uv sync' to install.")
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

        client = AsyncDeepgramClient()     # picks up DEEPGRAM_API_KEY from env
        audio_queue: asyncio.Queue = asyncio.Queue()

        class DGSession:
            """Thin wrapper so _receive_loop can call .send() and .finish()."""
            def __init__(self) -> None:
                self._task: asyncio.Task | None = None

            async def send(self, data: bytes) -> None:
                await audio_queue.put(data)

            async def finish(self) -> None:
                await audio_queue.put(None)   # sentinel → closes the connection
                if self._task:
                    try:
                        await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
                    except Exception:
                        self._task.cancel()

        session = DGSession()

        async def _run_session() -> None:
            try:
                async with client.listen.v1.connect(
                    model="nova-3",
                    language="en-US",
                    smart_format=True,
                    punctuate=True,
                    interim_results=True,
                    endpointing=150,         # ms of silence before finalising (was 300)
                    utterance_end_ms=1000,   # also fire on utterance boundary
                ) as conn:
                    logger.info("UI: Deepgram live session started for client")

                    async def _listen() -> None:
                        async for msg in conn:
                            if isinstance(msg, ListenV1Results):
                                try:
                                    alt = msg.channel.alternatives[0]
                                    if alt.transcript:
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript",
                                            "text": alt.transcript,
                                            "is_final": msg.is_final,
                                        }))
                                except Exception as e:
                                    logger.debug("Deepgram transcript handler: %s", e)

                    listen_task = asyncio.create_task(_listen())
                    try:
                        while True:
                            chunk = await audio_queue.get()
                            if chunk is None:
                                break
                            await conn.send_media(chunk)
                    finally:
                        listen_task.cancel()
                        logger.info("UI: Deepgram live session closed")
            except Exception as e:
                logger.warning("Deepgram session error — voice input unavailable: %s", e)
                try:
                    await websocket.send_text(json.dumps(
                        {"type": "transcript_error", "msg": str(e)}
                    ))
                except Exception:
                    pass

        session._task = asyncio.create_task(_run_session())
        # Give the connection a moment to establish before we declare success
        await asyncio.sleep(0.3)
        if session._task.done():
            return None   # connection failed immediately
        return session

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
                    elif t == "mic_toggle" and self._on_mic_toggle:
                        is_muted = self._on_mic_toggle()
                        # Echo new state back so the button updates
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "voice_mode",
                                "active": self._python_voice_mode,
                                "muted": is_muted,
                            }))
                        except Exception:
                            pass
                    elif t == "set_voice" and self._on_voice_change:
                        vid = data.get("voice_id", "").strip()
                        if vid:
                            self._on_voice_change(vid)
                    elif t == "eval_mode" and self._on_eval_mode:
                        intensive = bool(data.get("intensive", False))
                        self._on_eval_mode(intensive)
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
