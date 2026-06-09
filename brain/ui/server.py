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
from html import escape as html_escape
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
LOGIN_HTML_PATH = Path(__file__).parent / "login.html"
RESET_HTML_PATH = Path(__file__).parent / "reset.html"


class UIServer:
    def __init__(
        self,
        emitter_queue: asyncio.Queue,
        on_user_message: Callable[[str], None] | None = None,
        on_voice_change: Callable[[str], None] | None = None,
        on_eval_mode: Callable[[bool], None] | None = None,
        on_mic_toggle: Callable[[], bool] | None = None,
        on_mic_ptt: Callable[[bool], None] | None = None,
        is_muted_fn: Callable[[], bool] | None = None,
        mic_status_fn: Callable[[], str] | None = None,
        on_interrupt: Callable[[], None] | None = None,
        wiring=None,
        bus=None,
    ) -> None:
        self._queue = emitter_queue
        self._on_user_message = on_user_message
        self._on_voice_change = on_voice_change
        self._on_eval_mode = on_eval_mode
        self._on_mic_toggle = on_mic_toggle  # () -> is_muted (bool) — toggles
        self._on_mic_ptt = on_mic_ptt  # (down: bool) -> None — push-to-talk hold
        self._is_muted_fn = is_muted_fn  # () -> is_muted (bool) — read-only; None = no Python voice
        # () -> 'off'|'muted'|'active'. Authoritative status that knows whether a
        # server-side mic exists at all. 'off' tells the browser to self-capture
        # (hosted, no audio device). Preferred over _is_muted_fn when provided.
        self._mic_status_fn = mic_status_fn
        self._on_interrupt = on_interrupt
        self._clients: set = set()
        self._last_neuromod: dict = {}
        self._last_hormonal: dict = {}
        self._last_emotion: str = ""
        self._last_thoughts: list[dict] = []
        self._chat_history: list[dict] = []  # completed turns for page-refresh replay
        self._pending_turn: dict | None = None  # turn_start awaiting its turn_end
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
        'off' means no server-side mic (browser self-captures). 'muted'/'active'
        reflect a present server-side mic's live state."""
        if self._mic_status_fn is not None:
            return self._mic_status_fn()
        # Legacy fallback: bool-only predicate can't express "off", so it always
        # reported "muted"/"active" even with no mic — the bug this replaces.
        if self._is_muted_fn is None:
            return "off"
        return "muted" if self._is_muted_fn() else "active"

    def set_message_handler(self, fn: Callable[[str], None]) -> None:
        self._on_user_message = fn

    def set_voice_change_handler(self, fn: Callable[[str], None]) -> None:
        self._on_voice_change = fn

    def _build_app(self):
        app = FastAPI(docs_url=None, redoc_url=None)

        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

        from brain.ui import auth as ui_auth

        # ── Auth gate ─────────────────────────────────────────────────────────
        # Nothing below /login is reachable without a valid Supabase session.
        # Browser navigations bounce to /login; API calls get 401. Fail closed
        # when Supabase isn't configured. Disable with BRAIN_AUTH_DISABLED=true.
        @app.middleware("http")
        async def _auth_gate(request: Request, call_next):
            path = request.url.path
            if ui_auth.is_disabled() or ui_auth.is_public_path(path):
                return await call_next(request)
            if not ui_auth.is_configured():
                return ui_auth.config_error_response(request)
            claims, refreshed = await ui_auth.authenticate(request)
            if claims is None or ui_auth.owner_mismatch(claims):
                return ui_auth.unauthorized_response(request)
            request.state.user = claims
            # Expose the EFFECTIVE access token (the freshly-refreshed one when the
            # middleware just rotated it, else the cookie). Handlers that call out
            # to Supabase as the user — e.g. the key-vault endpoints — must use this
            # instead of request.cookies, which still holds the stale/expired token
            # until the browser receives the new cookie set on the response below.
            request.state.access_token = (
                refreshed.get("access_token")
                if refreshed
                else request.cookies.get(ui_auth.ACCESS_COOKIE)
            )
            response = await call_next(request)
            if refreshed:
                ui_auth.set_session_cookies(
                    response, refreshed, remember=ui_auth.remembered(request)
                )
            return response

        # ── HTTPS upgrade + HSTS ──────────────────────────────────────────────
        # Defined AFTER the auth gate so it wraps outermost: it can redirect an
        # http request to https BEFORE auth runs, and it stamps HSTS on every
        # response (including 301s and 401s). Railway terminates TLS at the edge
        # and forwards over http, so the client's real scheme is in
        # x-forwarded-proto. On localhost (no proxy header, http) both branches
        # are skipped, so local dev is untouched. HSTS is omitted by design when
        # the connection isn't secure — never pin localhost to https-only.
        _HSTS_MAX_AGE = os.environ.get("BRAIN_HSTS_MAX_AGE", "31536000")  # 1 year

        @app.middleware("http")
        async def _https_and_hsts(request: Request, call_next):
            from fastapi.responses import RedirectResponse

            fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
            is_secure = fwd_proto == "https" or request.url.scheme == "https"

            # Client reached us over plain http through a proxy → bounce to https.
            # (Railway's edge already does this; this covers any other front door
            # and makes the guarantee app-level rather than infra-dependent.)
            if fwd_proto == "http":
                https_url = request.url.replace(scheme="https")
                return RedirectResponse(str(https_url), status_code=301)

            response = await call_next(request)
            if is_secure and _HSTS_MAX_AGE != "0":
                response.headers.setdefault(
                    "Strict-Transport-Security", f"max-age={_HSTS_MAX_AGE}"
                )
            return response

        @app.get("/login")
        async def login_page():
            return HTMLResponse(LOGIN_HTML_PATH.read_text(encoding="utf-8"))

        @app.post("/auth/login")
        async def auth_login(request: Request):
            if not ui_auth.is_configured():
                return JSONResponse(
                    {"ok": False, "error": "Authentication is not configured."},
                    status_code=503,
                )
            body = await request.json()
            email = str(body.get("email", "")).strip()
            password = str(body.get("password", ""))
            if not email or not password:
                return JSONResponse(
                    {"ok": False, "error": "Email and password are required."},
                    status_code=400,
                )
            session = await ui_auth.password_login(email, password)
            if not session or not session.get("access_token"):
                return JSONResponse(
                    {"ok": False, "error": "Invalid email or password."},
                    status_code=401,
                )
            remember = bool(body.get("remember", True))
            resp = JSONResponse({"ok": True, "next": ui_auth.safe_next(body.get("next"))})
            ui_auth.set_session_cookies(resp, session, remember=remember)
            return resp

        @app.post("/auth/forgot")
        async def auth_forgot(request: Request):
            # Always answer ok — never disclose whether the address has an
            # account (email-enumeration defense). The recovery email only
            # actually sends if Supabase auth + email templates are configured.
            if ui_auth.is_configured():
                body = await request.json()
                # Land the email's link on our own reset page, derived from the
                # request so it's correct on localhost and on Railway alike.
                reset_url = str(request.base_url).rstrip("/") + "/auth/reset"
                await ui_auth.request_password_reset(
                    str(body.get("email", "")).strip(), redirect_to=reset_url
                )
            return JSONResponse({"ok": True})

        @app.get("/auth/reset")
        async def reset_page():
            # Supabase bounces the recovery link here with the token in the URL
            # hash. The page reads it client-side and sets the new password via
            # GoTrue. Inject the public Supabase URL + anon key (both publishable).
            html = RESET_HTML_PATH.read_text(encoding="utf-8")
            html = html.replace("__SUPABASE_URL__", os.environ.get("SUPABASE_URL", "").rstrip("/"))
            html = html.replace("__SUPABASE_ANON_KEY__", os.environ.get("SUPABASE_ANON_KEY", ""))
            return HTMLResponse(html)

        @app.post("/auth/admission")
        async def auth_admission(request: Request):
            # Route admission requests through thegaim.app's Resend mail service
            # to a real inbox (configurable; never the user's hardcoded address).
            from brain.ui import mailer

            body = await request.json()
            applicant = str(body.get("email", "")).strip()
            note = str(body.get("note", "")).strip()
            if not applicant:
                return JSONResponse(
                    {"ok": False, "error": "An email is required."}, status_code=400
                )
            to = os.environ.get("ADMISSION_NOTIFY_EMAIL", "").strip() or "admin@thegaim.app"
            safe_applicant = html_escape(applicant)
            safe_note = html_escape(note) if note else ""
            note_html = (
                f"<p style='margin:16px 0 0;color:#52525b'><strong>Note:</strong> {safe_note}</p>"
                if safe_note
                else ""
            )
            html_body = (
                "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
                "max-width:520px;margin:0 auto;color:#18181b\">"
                "<h2 style='font-weight:600'>New Elyceum admission request</h2>"
                f"<p style='color:#52525b'><strong>{safe_applicant}</strong> "
                "has requested admission to Elyceum.</p>"
                f"{note_html}"
                "<p style='margin-top:24px;color:#71717a;font-size:13px'>"
                "Provision the account via <code>scripts/create_user.py</code> if approved.</p>"
                "</div>"
            )
            text_body = (
                f"New Elyceum admission request from {applicant}."
                + (f"\n\nNote: {note}" if note else "")
            )
            await mailer.send_email(
                to,
                "Elyceum — new admission request",
                html_body,
                text=text_body,
            )
            # Always ok: the applicant shouldn't learn whether mail actually sent.
            return JSONResponse({"ok": True})

        @app.post("/auth/logout")
        async def auth_logout():
            resp = JSONResponse({"ok": True})
            ui_auth.clear_session_cookies(resp)
            return resp

        @app.get("/auth/me")
        async def auth_me(request: Request):
            # Gated by the auth middleware, which attaches the verified claims.
            claims = getattr(request.state, "user", None) or {}
            return JSONResponse(
                {"email": claims.get("email"), "is_admin": ui_auth.is_admin(claims)}
            )

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/")
        async def index():
            html = HTML_PATH.read_text(encoding="utf-8")
            return HTMLResponse(html)

        @app.get("/settings")
        async def get_settings(request: Request):
            from brain.settings import API_KEY_ENV, DEFAULTS, settings

            s = settings.all()
            # API-key status: prefer the Supabase Vault (per-user, encrypted) when
            # configured + authenticated; otherwise fall back to local settings.json.
            # Never ship the secret VALUES to the browser — the fields load blank.
            vault_status = None
            token = getattr(request.state, "access_token", None)
            if token and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"):
                try:
                    from brain import vault

                    st = vault.get_status(token)
                    vault_status = {
                        f"api_key_{p}": bool(v)
                        for p, v in st.items()
                        if p in vault.VALID_PROVIDERS
                    }
                except Exception as e:
                    logger.warning("[settings] vault status unavailable: %s", e)
            secrets_set = {}
            for k in API_KEY_ENV:
                secrets_set[k] = (
                    bool(vault_status.get(k))
                    if vault_status is not None
                    else bool(str(s.get(k) or "").strip())
                )
                s[k] = ""
            return {"settings": s, "defaults": DEFAULTS, "secrets_set": secrets_set}

        @app.post("/settings")
        async def save_settings(request: Request):
            from brain.settings import settings

            body = await request.json()
            try:
                # API keys route to the Supabase Vault (per-user, encrypted) when
                # configured; otherwise they persist to local settings.json
                # (single-user dev). Either way they're removed from the
                # settings.json patch, and an empty field leaves the stored key
                # unchanged (never let a blank wipe a saved secret).
                from brain.settings import API_KEY_ENV

                _token = getattr(request.state, "access_token", None)
                _vault_on = bool(
                    _token
                    and os.environ.get("SUPABASE_URL")
                    and os.environ.get("SUPABASE_ANON_KEY")
                )
                for _k in list(body):
                    if _k not in API_KEY_ENV:
                        continue
                    _val = str(body.pop(_k) or "").strip()
                    if not _val:
                        continue  # blank = keep existing
                    if _vault_on:
                        from brain import vault

                        vault.set_key(_token, _k.replace("api_key_", ""), _val)
                        # Apply live so a not-yet-constructed client picks it up
                        # this session too (clients read os.environ lazily).
                        os.environ[API_KEY_ENV[_k]] = _val
                    else:
                        body[_k] = _val  # local dev: persist to settings.json

                prior_persona = str(settings.get("persona_name", ""))
                settings.save(body)
                # A persona SWITCH posts the new persona's chem_baseline/chem_init
                # (= its resting profile) wholesale. Don't treat that as a slider
                # edit — it would overwrite the incoming persona's saved evolved
                # state with its baseline. Boot re-materializes chem from the
                # persona's own file (resting from the table, current preserved).
                is_switch = (
                    "persona_name" in body
                    and str(settings.get("persona_name", "")) != prior_persona
                )
                # Creating a NEW persona arrives as a switch to a name with no
                # chemistry file yet. Seed its file from the posted chem_baseline/
                # chem_init so it persists immediately (and isn't reliant on the
                # boot-time seed reading whatever global chem happens to be set).
                if is_switch and any(k.startswith("chem_baseline_") for k in body):
                    from brain import persona_chem

                    _new = str(settings.get("persona_name", ""))
                    if _new and not persona_chem.exists(_new):
                        persona_chem.save_resting(
                            _new,
                            {ch: float(settings.get(f"chem_baseline_{ch}")) for ch in persona_chem.CHANNELS},
                        )
                        persona_chem.save_current(
                            _new,
                            {ch: float(settings.get(f"chem_init_{ch}")) for ch in persona_chem.CHANNELS},
                            {},
                        )
                # If the user edited resting/boot chemistry sliders, persist them
                # to the ACTIVE persona's own file so the edit sticks per-persona
                # instead of leaking through global settings on the next switch.
                edited_resting = not is_switch and any(k.startswith("chem_baseline_") for k in body)
                edited_boot = not is_switch and any(k.startswith("chem_init_") for k in body)
                if edited_resting or edited_boot:
                    persona = str(settings.get("persona_name", ""))
                    if persona:
                        from brain import persona_chem

                        if edited_resting:
                            persona_chem.save_resting(
                                persona,
                                {
                                    ch: float(settings.get(f"chem_baseline_{ch}"))
                                    for ch in persona_chem.CHANNELS
                                },
                            )
                        if edited_boot:
                            persona_chem.save_current(
                                persona,
                                {
                                    ch: float(settings.get(f"chem_init_{ch}"))
                                    for ch in persona_chem.CHANNELS
                                },
                                {},
                            )
                # Apply a voice change LIVE so the settings-page Voice dropdown
                # (which only writes the setting) takes effect immediately, just
                # like the header pill's set_voice — no restart required.
                if self._on_voice_change and any(k.startswith("persona_voice_") for k in body):
                    from brain.persona_chem import _slug

                    persona = str(settings.get("persona_name", ""))
                    vid = ""
                    if persona:
                        vid = str(settings.get(f"persona_voice_{_slug(persona)}", "")).strip()
                    if not vid:
                        vid = str(settings.get("persona_voice_id", "")).strip()
                    if vid:
                        self._on_voice_change(vid)
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
                return {
                    "voices": [],
                    "reason": "no_elevenlabs_key",
                    "message": "ELEVENLABS_API_KEY not set",
                }
            model_id = (
                os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip()
                or "eleven_flash_v2_5"
            )
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    # Fetch both in parallel — model capabilities + voice list
                    voices_resp, models_resp = await asyncio.gather(
                        client.get(
                            "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": api_key}
                        ),
                        client.get(
                            "https://api.elevenlabs.io/v1/models", headers={"xi-api-key": api_key}
                        ),
                    )
                voices_resp.raise_for_status()
                models_resp.raise_for_status()
                voices_raw = voices_resp.json().get("voices", [])

                # eleven_v3 silently substitutes its own default voice when given
                # a Professional Voice Clone voice_id — hide them to prevent that.
                # All other models (flash, turbo, multilingual) work fine with PVCs.
                is_v3 = model_id == "eleven_v3"

                # Categorize the user's voices
                pro_voices = [v for v in voices_raw if v.get("category") == "professional"]
                custom_voices = [
                    v for v in voices_raw if v.get("category") not in ("premade", "professional")
                ]
                premade_voices = [v for v in voices_raw if v.get("category") == "premade"]

                if is_v3:
                    candidates = custom_voices
                    excluded_pro = len(pro_voices)
                else:
                    candidates = custom_voices + pro_voices
                    excluded_pro = 0

                message = ""
                if not candidates:
                    # Fall back to premade so dropdown isn't empty
                    candidates = premade_voices
                    if excluded_pro:
                        message = (
                            f"{excluded_pro} of your voices are Professional Voice Clones, "
                            f"which {model_id} does not serve. Showing premade voices instead. "
                            "Switch to eleven_flash_v2_5 or another non-v3 model to access them."
                        )
                elif excluded_pro:
                    message = (
                        f"Hiding {excluded_pro} Professional Voice Clones — "
                        f"{model_id} does not support them."
                    )

                voices = [{"voice_id": v["voice_id"], "name": v["name"]} for v in candidates]
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
            # Gate the socket with the same session cookies as the HTTP routes.
            # Reject before accept() so an unauthenticated client never connects.
            if not ui_auth.is_disabled():
                if not ui_auth.is_configured():
                    await websocket.close(code=1008)
                    return
                claims, _ = await ui_auth.authenticate(websocket)
                if claims is None or ui_auth.owner_mismatch(claims):
                    await websocket.close(code=1008)
                    return
            await websocket.accept()
            self._clients.add(websocket)
            logger.info("UI: client connected (%d total)", len(self._clients))

            # Send current mic state so the button reflects reality on connect.
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "mic_state",
                            "status": self._mic_status(),
                        }
                    )
                )

            # Send current neuromod + hormonal state immediately on connect
            if self._last_neuromod:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "neuromod", **self._last_neuromod})
                    )
            if self._last_hormonal:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "hormonal", **self._last_hormonal})
                    )
            if self._last_emotion:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "emotion", "emotion": self._last_emotion})
                    )

            # Tell the client about wiring state (frozen tag in plasticity panel)
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "wiring_status",
                            "frozen": self._wiring_frozen,
                        }
                    )
                )

            # Subsystem health — sent on every connect so status pill is always current
            if self._subsystem_status:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "subsystem_status",
                                **self._subsystem_status,
                            }
                        )
                    )

            # Replay chat history so the conversation survives a page refresh
            if self._chat_history:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "chat_history",
                                "turns": list(self._chat_history),
                            }
                        )
                    )

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
                await websocket.send_text(
                    json.dumps({"type": "transcript_error", "msg": "DEEPGRAM_API_KEY not set"})
                )
            return None

        client = AsyncDeepgramClient()  # picks up DEEPGRAM_API_KEY from env
        audio_queue: asyncio.Queue = asyncio.Queue()

        class DGSession:
            """Thin wrapper so _receive_loop can call .send() and .finish()."""

            def __init__(self) -> None:
                self._task: asyncio.Task | None = None
                # Raw audio bytes for the in-progress utterance. _receive_loop
                # appends incoming chunks here; _listen drains it on each final
                # transcript to publish the full utterance to the auditory bus.
                self.audio_chunks: list[bytes] = []

            async def send(self, data: bytes) -> None:
                await audio_queue.put(data)

            async def finish(self) -> None:
                await audio_queue.put(None)  # sentinel → closes the connection
                if self._task:
                    try:
                        await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
                    except Exception:
                        self._task.cancel()

        session = DGSession()

        # Accumulate diarized words across chunks for one utterance so we can
        # publish to the auditory bus on final. (Raw audio bytes are buffered on
        # the DGSession handle as `.audio_chunks`, written by _receive_loop.)
        _diarized_words: list[dict] = []
        _utterance_start_s: float | None = None

        async def _publish_utterance(
            transcript: str, audio_bytes: bytes, words: list[dict]
        ) -> None:
            """Mirror what streaming_mic publishes so the auditory cortex gets
            the same events regardless of whether voice mode is active."""
            if not (self._bus and audio_bytes):
                return
            duration_s = len(audio_bytes) / (16000 * 2)  # 16kHz, int16 = 2 bytes/sample
            try:
                await self._bus.publish_dict(
                    "auditory.raw_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": 16000,
                        "duration_s": duration_s,
                        "channels": 1,
                        "dtype": "int16",
                    },
                    source="ui",
                )
                await self._bus.publish_dict(
                    "auditory.diarized_audio",
                    {
                        "audio_bytes": audio_bytes,
                        "sample_rate": 16000,
                        "duration_s": duration_s,
                        "dtype": "int16",
                        "diarized_words": words,
                        "transcript": transcript,
                    },
                    source="ui",
                )
                logger.debug(
                    "UI: published utterance to auditory bus (%d bytes, %d words)",
                    len(audio_bytes),
                    len(words),
                )
            except Exception as e:
                logger.debug("UI: auditory publish failed: %s", e)

        async def _run_session() -> None:
            nonlocal _diarized_words, _utterance_start_s
            try:
                async with client.listen.v1.connect(
                    model="nova-3",
                    language="en-US",
                    smart_format=True,
                    punctuate=True,
                    interim_results=True,
                    endpointing=150,  # ms of silence before finalising (was 300)
                    utterance_end_ms=1000,  # also fire on utterance boundary
                    diarize=True,  # enable speaker diarization for auditory cortex
                ) as conn:
                    logger.info("UI: Deepgram live session started for client")

                    async def _listen() -> None:
                        nonlocal _diarized_words
                        async for msg in conn:
                            if isinstance(msg, ListenV1Results):
                                try:
                                    alt = msg.channel.alternatives[0]
                                    if alt.transcript:
                                        await websocket.send_text(
                                            json.dumps(
                                                {
                                                    "type": "transcript",
                                                    "text": alt.transcript,
                                                    "is_final": msg.is_final,
                                                }
                                            )
                                        )
                                    if msg.is_final and alt.transcript:
                                        # Harvest diarized words and flush the
                                        # audio buffer to the auditory bus.
                                        words = []
                                        for w in getattr(alt, "words", []) or []:
                                            words.append(
                                                {
                                                    "word": getattr(w, "word", ""),
                                                    "start": getattr(w, "start", 0.0),
                                                    "end": getattr(w, "end", 0.0),
                                                    "speaker": getattr(w, "speaker", 0),
                                                }
                                            )
                                        audio_bytes = b"".join(session.audio_chunks)
                                        session.audio_chunks.clear()
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
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "transcript_error",
                                        "msg": "Deepgram closed connection",
                                    }
                                )
                            )
            except Exception as e:
                logger.warning("Deepgram session error — voice input unavailable: %s", e)
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        json.dumps({"type": "transcript_error", "msg": str(e)})
                    )

        session._task = asyncio.create_task(_run_session())
        # Give the connection a moment to establish before we declare success
        await asyncio.sleep(0.3)
        if session._task.done():
            return None  # connection failed immediately
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
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "mic_state",
                                        "status": self._mic_status(),
                                    }
                                )
                            )
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
                    dg_conn.audio_chunks.append(msg["bytes"])
                    await dg_conn.send(msg["bytes"])

            except Exception:
                break

        if dg_conn is not None:
            with contextlib.suppress(Exception):
                await dg_conn.finish()

    @property
    def has_listeners(self) -> bool:
        """True if at least one browser client is connected to receive events/audio."""
        return bool(self._clients)

    def attach_tts_queue(self, pns) -> None:
        """Wire PNS TTS queue into this server's broadcast loop.

        Called by session_setup after PNS and UIServer are both ready.
        pns._tts_ws_queue is set here; the _tts_broadcast_loop drains it.
        """
        import asyncio

        pns._tts_ws_queue = asyncio.Queue(maxsize=256)
        asyncio.create_task(self._tts_broadcast_loop(pns._tts_ws_queue, pns))

    async def _tts_broadcast_loop(self, queue: asyncio.Queue, pns) -> None:
        """Drain TTS PCM chunks and broadcast as binary WebSocket frames.

        Frame format: 1-byte type prefix (0x01=audio, 0xFF=interrupt) + PCM bytes.
        Browser detects interrupts by the 0xFF frame and stops playback immediately.
        """
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                continue

            if chunk is None:
                # Sentinel: TTS stream ended — send tts_end event
                if self._clients:
                    payload = json.dumps({"type": "tts_end"})
                    dead = set()
                    for client in list(self._clients):
                        with contextlib.suppress(Exception):
                            await client.send_text(payload)
                continue

            if chunk == b"\xff":
                # Interrupt sentinel
                if self._clients:
                    payload = json.dumps({"type": "tts_interrupt"})
                    dead = set()
                    for client in list(self._clients):
                        with contextlib.suppress(Exception):
                            await client.send_text(payload)
                continue

            # Binary PCM chunk — prefix with 0x01 so browser can distinguish
            frame = b"\x01" + chunk
            if self._clients:
                dead = set()
                for client in list(self._clients):
                    try:
                        await client.send_bytes(frame)
                    except Exception:
                        dead.add(client)
                self._clients -= dead

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
                elif event.get("type") == "turn_start" and event.get("user_input"):
                    self._pending_turn = {
                        "turn_id": event.get("turn_id"),
                        "user_input": event["user_input"],
                    }
                elif (
                    event.get("type") == "turn_end" and event.get("response") and self._pending_turn
                ):
                    self._chat_history.append(
                        {
                            **self._pending_turn,
                            "response": event["response"],
                            "elapsed_s": event.get("elapsed_s"),
                            "llm_calls": event.get("llm_calls", 0),
                        }
                    )
                    self._pending_turn = None
                    if len(self._chat_history) > 50:
                        self._chat_history.pop(0)

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

    async def start(self, host: str | None = None, port: int | None = None) -> None:
        import uvicorn

        # Railway sets PORT env var; default to 0.0.0.0 in production
        _port = port or int(os.environ.get("PORT", "8765"))
        # Bind all interfaces only when hosted (Railway); localhost otherwise.
        _host = host or (
            "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"  # nosec B104
        )

        self._app = self._build_app()

        # Start broadcast loop as a background task
        asyncio.create_task(self._broadcast_loop())

        config = uvicorn.Config(
            self._app,
            host=_host,
            port=_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info("UI server starting at http://%s:%d", _host, _port)
        print(f"\nBrain UI: http://{host}:{port}\n")
        await server.serve()
