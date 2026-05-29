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
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

# FastAPI/WebSocket imports at module level so that `from __future__ import annotations`
# (PEP 563 lazy strings) doesn't prevent FastAPI's dependency injector from resolving
# the `WebSocket` annotation in ws_endpoint. When these are imported only inside
# _build_app(), the string 'WebSocket' can't be found in the module globals and FastAPI
# misclassifies the parameter as a query param, causing an immediate 403 on every
# WebSocket connection attempt.
try:
    from fastapi import FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
except ImportError:
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment]
    WebSocket = None  # type: ignore[assignment]
    WebSocketDisconnect = None  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]
    UploadFile = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

HTML_PATH = Path(__file__).parent / "index.html"


class UIServer:
    def __init__(self, emitter_queue: asyncio.Queue,
                 on_user_message: Callable[[str], None] | None = None,
                 on_voice_change: Callable[[str], None] | None = None,
                 on_eval_mode: Callable[[bool], None] | None = None,
                 on_mic_toggle: Callable[[], bool] | None = None,
                 on_mic_ptt: Callable[[bool], None] | None = None,
                 is_muted_fn: Callable[[], bool] | None = None,
                 on_interrupt: Callable[[], None] | None = None,
                 wiring=None,
                 bus=None) -> None:
        self._queue = emitter_queue
        self._on_user_message = on_user_message
        self._on_voice_change = on_voice_change
        self._on_eval_mode = on_eval_mode
        self._on_mic_toggle = on_mic_toggle      # () -> is_muted (bool) — toggles
        self._on_mic_ptt = on_mic_ptt            # (down: bool) -> None — push-to-talk hold
        self._is_muted_fn = is_muted_fn          # () -> is_muted (bool) — read-only; None = no Python voice
        self._on_interrupt = on_interrupt
        self._clients: set = set()
        self._last_neuromod: dict = {}
        self._last_hormonal: dict = {}
        self._last_emotion: str = ""
        self._last_thoughts: list[dict] = []
        self._wiring_frozen: bool = False
        self._subsystem_status: dict[str, bool] = {}
        self._wiring = wiring
        self._bus = bus  # for publishing to auditory pipeline from press-to-talk
        self._app = None

    def set_wiring_frozen(self, frozen: bool) -> None:
        self._wiring_frozen = bool(frozen)

    def set_subsystem_status(self, status: dict[str, bool]) -> None:
        """Store subsystem up/down flags to broadcast on every connect."""
        self._subsystem_status = dict(status)

    def _mic_status(self) -> str:
        """Single status string for the mic: 'off' | 'muted' | 'active'.
        'off' means no Python voice mode. 'muted'/'active' reflect current state."""
        if self._is_muted_fn is None:
            return "off"
        return "muted" if self._is_muted_fn() else "active"

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

        @app.get("/settings")
        async def get_settings():
            from brain.settings import DEFAULTS, settings
            return {"settings": settings.all(), "defaults": DEFAULTS}

        @app.post("/settings")
        async def save_settings(request: Request):
            from brain.settings import settings
            body = await request.json()
            try:
                settings.save(body)
                return {"ok": True}
            except Exception as e:
                from fastapi.responses import JSONResponse
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        @app.post("/settings/reset")
        async def reset_settings():
            from brain.settings import settings
            settings.reset_to_defaults()
            settings.save()
            return {"ok": True, "settings": settings.all()}

        @app.get("/wiring")
        async def get_wiring():
            w = self._wiring
            if w is None:
                return {"top": [], "deltas": [], "edge_count": 0}
            return {
                "top": w.top_edges(20),
                "deltas": w.session_deltas(),
                "edge_count": w.edge_count(),
            }

        @app.post("/restart")
        async def restart_brain():
            """Re-exec the current process with the same args — restarts the brain."""
            async def _do_restart():
                await asyncio.sleep(0.4)
                cmd = [sys.executable] + sys.argv
                logger.info("Restarting brain: %s", " ".join(cmd))
                os.execv(sys.executable, cmd)
            asyncio.create_task(_do_restart())
            return {"ok": True}

        @app.post("/shutdown")
        async def shutdown_brain():
            """Gracefully shut down the brain process."""
            async def _do_shutdown():
                await asyncio.sleep(0.4)
                os.kill(os.getpid(), __import__("signal").SIGTERM)
            asyncio.create_task(_do_shutdown())
            return {"ok": True}

        @app.get("/voices")
        async def list_voices():
            """Return voices compatible with the configured ElevenLabs model.

            Filtering rules:
              - Always exclude category=premade if any non-premade voices remain
                (the user's own voices are what they're after).
              - If the configured model doesn't serve professional voice clones
                (e.g. eleven_v3 has serves_pro_voices=false), exclude
                category=professional too — calling those with that model
                silently substitutes a default voice.
              - If filtering would yield zero voices, fall back to showing
                premade ones (which work with any model) so the dropdown
                isn't empty.

            Response also includes a `message` field when voices were filtered
            out, so the UI can explain to the user why some are missing.
            """
            import httpx
            api_key = os.environ.get("ELEVENLABS_API_KEY", "")
            if not api_key:
                return {"voices": [], "message": "ELEVENLABS_API_KEY not set"}
            model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_v3").strip() or "eleven_v3"
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    # Fetch both in parallel — model capabilities + voice list
                    voices_resp, models_resp = await asyncio.gather(
                        client.get("https://api.elevenlabs.io/v1/voices",
                                   headers={"xi-api-key": api_key}),
                        client.get("https://api.elevenlabs.io/v1/models",
                                   headers={"xi-api-key": api_key}),
                    )
                voices_resp.raise_for_status()
                models_resp.raise_for_status()
                voices_raw = voices_resp.json().get("voices", [])
                models_raw = models_resp.json()

                # Look up the configured model's capabilities
                model_caps = next(
                    (m for m in models_raw if m.get("model_id") == model_id), {}
                )
                # Default False: if the model lookup fails or the field is missing,
                # assume pro voices are NOT served (safe). Allowing them through
                # when uncertain causes eleven_v3 to silently substitute its own
                # default voice, which is the exact bug we're guarding against.
                serves_pro = bool(model_caps.get("serves_pro_voices", False))

                # Categorize the user's voices
                pro_voices = [v for v in voices_raw if v.get("category") == "professional"]
                custom_voices = [v for v in voices_raw
                                 if v.get("category") not in ("premade", "professional")]
                premade_voices = [v for v in voices_raw if v.get("category") == "premade"]

                if serves_pro:
                    candidates = custom_voices + pro_voices
                    excluded_pro = 0
                else:
                    candidates = custom_voices
                    excluded_pro = len(pro_voices)

                message = ""
                if not candidates:
                    # Fall back to premade so dropdown isn't empty
                    candidates = premade_voices
                    if excluded_pro:
                        message = (
                            f"{excluded_pro} of your voices are Professional Voice Clones, "
                            f"which {model_id} does not serve. Showing premade voices instead. "
                            "Set ELEVENLABS_MODEL_ID to eleven_turbo_v2_5 (or another model "
                            "that supports professional voices) to access them."
                        )
                elif excluded_pro:
                    message = (
                        f"Hiding {excluded_pro} Professional Voice Clones — "
                        f"{model_id} does not serve them."
                    )

                voices = [
                    {"voice_id": v["voice_id"], "name": v["name"]}
                    for v in candidates
                ]
                return {"voices": voices, "model_id": model_id, "message": message}
            except Exception as e:
                logger.warning("Failed to fetch ElevenLabs voices: %s", e)
                return {"voices": [], "message": f"Failed to fetch voices: {e}"}

        ui_dir = HTML_PATH.parent

        @app.post("/upload_image")
        async def upload_image(file: UploadFile):
            import tempfile
            suffix = Path(file.filename or "upload").suffix or ".jpg"
            content = await file.read()
            with tempfile.NamedTemporaryFile(
                suffix=suffix, prefix="brain_ui_img_", delete=False
            ) as tmp:
                tmp.write(content)
            return {"path": tmp.name}

        @app.get("/{filename}")
        async def static_asset(filename: str):
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

            # Send current mic state so the button reflects reality on connect.
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps({
                    "type": "mic_state",
                    "status": self._mic_status(),
                }))

            # Send current neuromod + hormonal state immediately on connect
            if self._last_neuromod:
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(
                        {"type": "neuromod", **self._last_neuromod}
                    ))
            if self._last_hormonal:
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(
                        {"type": "hormonal", **self._last_hormonal}
                    ))
            if self._last_emotion:
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(
                        {"type": "emotion", "emotion": self._last_emotion}
                    ))

            # Tell the client about wiring state (frozen tag in plasticity panel)
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps({
                    "type": "wiring_status",
                    "frozen": self._wiring_frozen,
                }))

            # Subsystem health — sent on every connect so status pill is always current
            if self._subsystem_status:
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps({
                        "type": "subsystem_status",
                        **self._subsystem_status,
                    }))

            # Replay recent thoughts so the feed isn't blank on reconnect
            for thought_event in list(self._last_thoughts):
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(thought_event))

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
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(
                    {"type": "transcript_error", "msg": "DEEPGRAM_API_KEY not set"}
                ))
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

        # Accumulate raw audio bytes and diarized words across chunks for one
        # utterance so we can publish to the auditory bus on final.
        _audio_chunks: list[bytes] = []
        _diarized_words: list[dict] = []
        _utterance_start_s: float | None = None

        async def _publish_utterance(transcript: str, audio_bytes: bytes,
                                     words: list[dict]) -> None:
            """Mirror what streaming_mic publishes so the auditory cortex gets
            the same events regardless of whether voice mode is active."""
            if not (self._bus and audio_bytes):
                return
            duration_s = len(audio_bytes) / (16000 * 2)  # 16kHz, int16 = 2 bytes/sample
            try:
                await self._bus.publish_dict(
                    "auditory.raw_audio",
                    {"audio_bytes": audio_bytes, "sample_rate": 16000,
                     "duration_s": duration_s, "channels": 1, "dtype": "int16"},
                    source="ui",
                )
                await self._bus.publish_dict(
                    "auditory.diarized_audio",
                    {"audio_bytes": audio_bytes, "sample_rate": 16000,
                     "duration_s": duration_s, "dtype": "int16",
                     "diarized_words": words, "transcript": transcript},
                    source="ui",
                )
                logger.debug("UI: published utterance to auditory bus (%d bytes, %d words)",
                             len(audio_bytes), len(words))
            except Exception as e:
                logger.debug("UI: auditory publish failed: %s", e)

        async def _run_session() -> None:
            nonlocal _audio_chunks, _diarized_words, _utterance_start_s
            try:
                async with client.listen.v1.connect(
                    model="nova-3",
                    language="en-US",
                    smart_format=True,
                    punctuate=True,
                    interim_results=True,
                    endpointing=150,         # ms of silence before finalising (was 300)
                    utterance_end_ms=1000,   # also fire on utterance boundary
                    diarize=True,            # enable speaker diarization for auditory cortex
                ) as conn:
                    logger.info("UI: Deepgram live session started for client")

                    async def _listen() -> None:
                        nonlocal _audio_chunks, _diarized_words
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
                                    if msg.is_final and alt.transcript:
                                        # Harvest diarized words and flush the
                                        # audio buffer to the auditory bus.
                                        words = []
                                        for w in getattr(alt, "words", []) or []:
                                            words.append({
                                                "word": getattr(w, "word", ""),
                                                "start": getattr(w, "start", 0.0),
                                                "end": getattr(w, "end", 0.0),
                                                "speaker": getattr(w, "speaker", 0),
                                            })
                                        audio_bytes = b"".join(_audio_chunks)
                                        _audio_chunks = []
                                        _diarized_words = []
                                        asyncio.create_task(
                                            _publish_utterance(alt.transcript, audio_bytes, words)
                                        )
                                except Exception as e:
                                    logger.debug("Deepgram transcript handler: %s", e)

                    listen_task = asyncio.create_task(_listen())
                    dg_closed_early = False
                    try:
                        while True:
                            chunk = await audio_queue.get()
                            if chunk is None:
                                break
                            # If _listen() finished, Deepgram closed the connection
                            # from their end (timeout, server-side error, etc.).
                            # Break immediately so _run_session exits and the
                            # browser's transcript_error handler can restart.
                            if listen_task.done():
                                logger.info("UI: Deepgram closed connection from their end")
                                dg_closed_early = True
                                break
                            await conn.send_media(chunk)
                    finally:
                        listen_task.cancel()
                        logger.info("UI: Deepgram live session closed")
                    # Notify the browser if Deepgram closed from their end (not
                    # from a voice_stop) so it can reopen a fresh session.
                    if dg_closed_early:
                        with contextlib.suppress(Exception):
                            await websocket.send_text(json.dumps({
                                "type": "transcript_error",
                                "msg": "Deepgram closed connection",
                            }))
            except Exception as e:
                logger.warning("Deepgram session error — voice input unavailable: %s", e)
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(
                        {"type": "transcript_error", "msg": str(e)}
                    ))

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
                        self._on_mic_toggle()
                        # Echo new state back so the button updates. (The session
                        # also broadcasts the settled state once any async flush
                        # completes — this echo just makes the click feel instant.)
                        with contextlib.suppress(Exception):
                            await websocket.send_text(json.dumps({
                                "type": "mic_state",
                                "status": self._mic_status(),
                            }))
                    elif t == "mic_ptt" and self._on_mic_ptt:
                        # Push-to-talk: {down:true} on Space keydown (go live),
                        # {down:false} on keyup (flush held phrase + re-mute).
                        # State is broadcast by the session once it settles, so
                        # we don't echo here (release involves an async flush).
                        self._on_mic_ptt(bool(data.get("down", False)))
                    elif t == "set_voice" and self._on_voice_change:
                        vid = data.get("voice_id", "").strip()
                        if vid:
                            self._on_voice_change(vid)
                    elif t == "eval_mode" and self._on_eval_mode:
                        intensive = bool(data.get("intensive", False))
                        self._on_eval_mode(intensive)
                    elif t == "interrupt" and self._on_interrupt:
                        self._on_interrupt()
                    elif t == "voice_start":
                        # If a previous session died silently (task finished without
                        # a voice_stop), clear it so we get a fresh connection.
                        if dg_conn is not None and dg_conn._task.done():
                            dg_conn = None
                        if dg_conn is None:
                            dg_conn = await self._start_deepgram(websocket)
                    elif t == "voice_stop":
                        if dg_conn is not None:
                            with contextlib.suppress(Exception):
                                await dg_conn.finish()
                            dg_conn = None

                elif "bytes" in msg and msg["bytes"] and dg_conn is not None:
                    _audio_chunks.append(msg["bytes"])
                    await dg_conn.send(msg["bytes"])

            except Exception:
                break

        if dg_conn is not None:
            with contextlib.suppress(Exception):
                await dg_conn.finish()

    async def _broadcast_loop(self) -> None:
        """Drain emitter queue and broadcast to all connected clients."""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                # Cache latest neuromod + hormonal + emotion for new clients
                if event.get("type") == "neuromod":
                    self._last_neuromod = {k: v for k, v in event.items() if k != "type"}
                elif event.get("type") == "hormonal":
                    self._last_hormonal = {k: v for k, v in event.items() if k != "type"}
                elif event.get("type") == "emotion" and event.get("emotion"):
                    self._last_emotion = event["emotion"]
                elif event.get("type") == "stream_thought" and event.get("thought"):
                    if not event.get("proactive"):
                        self._last_thoughts.append(event)
                        if len(self._last_thoughts) > 10:
                            self._last_thoughts.pop(0)

                if self._clients:
                    payload = json.dumps(event)
                    dead = set()
                    for client in list(self._clients):
                        try:
                            await client.send_text(payload)
                        except Exception:
                            dead.add(client)
                    self._clients -= dead
            except TimeoutError:
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
