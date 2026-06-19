"""
Gateway / Hub — the multi-tenant front door (JupyterHub pattern).

Responsibilities (and ONLY these — it holds no per-user brain state):
  • Auth: reuse brain.ui.auth (login / reset / admission / cookie gate).
  • Key vault: serve the keys page + /api/keys, backed by Supabase Vault via the
    user's own JWT (write-only set/delete, booleans-only status — never decrypts).
  • Spawn + route: on an authed request, ensure the user's brain process is up
    (provisioner), show a booting interstitial while it boots, then reverse-proxy
    HTTP + WebSocket to that user's localhost port.

The per-user brain process re-verifies the forwarded cookie and pins to its
BRAIN_USER_ID, so the gateway↔brain hop is defense-in-depth, not the only gate.

Run:  python -m brain.gateway   (binds 0.0.0.0:$PORT on Railway)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from html import escape as html_escape
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from brain.provisioner import Provisioner
from brain.ui import auth as ui_auth

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
_GW_DIR = Path(__file__).resolve().parent
LOGIN_HTML = _UI_DIR / "login.html"
RESET_HTML = _UI_DIR / "reset.html"
KEYS_HTML = _GW_DIR / "keys.html"
INTERSTITIAL_HTML = _GW_DIR / "interstitial.html"

# Hop-by-hop headers must not be forwarded through a proxy.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _access_token(request: Request) -> str:
    # Prefer a token refreshed during this request (set in the auth gate), else
    # the access cookie.
    tok = getattr(request.state, "access_token", "") or ""
    return tok or request.cookies.get(ui_auth.ACCESS_COOKIE, "") or ""


# Cache uid → org (the tenant unit) so routing isn't a DB query per request.
# TTL'd so membership changes (user added to / removed from an org) take effect
# within minutes rather than only on gateway restart.
_ORG_CACHE_TTL_S = 30 * 60
_org_cache: dict[str, tuple[str, float]] = {}


def _tenant_for(uid: str) -> str:
    """Resolve an authenticated user to their org id (the tenant the brain process
    and all data key on). Falls back to the uid itself when there's no membership
    (pre-migration / dev) — which for a personal org is the same value, so this is
    behavior-preserving."""
    hit = _org_cache.get(uid)
    if hit is not None and time.time() - hit[1] < _ORG_CACHE_TTL_S:
        return hit[0]
    from brain import org

    t = org.org_id_for_user(uid) or uid
    _org_cache[uid] = (t, time.time())
    return t


def build_gateway_app(provisioner: Provisioner, runpod_holder: list | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    # ── Auth gate ───────────────────────────────────────────────────────────
    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if ui_auth.is_disabled() or ui_auth.is_public_path(path):
            return await call_next(request)
        if not ui_auth.is_configured():
            return ui_auth.config_error_response(request)
        claims, refreshed = await ui_auth.authenticate(request)
        if claims is None:
            return ui_auth.unauthorized_response(request)
        request.state.user = claims
        request.state.access_token = (
            (refreshed or {}).get("access_token") if refreshed else
            request.cookies.get(ui_auth.ACCESS_COOKIE, "")
        )
        response = await call_next(request)
        if refreshed:
            ui_auth.set_session_cookies(response, refreshed, remember=ui_auth.remembered(request))
        return response

    # ── HTTPS upgrade + HSTS ──────────────────────────────────────────────
    # Mirrors the brain UI server: registered after the auth gate so it wraps
    # OUTERMOST — an http request redirects to https before auth runs, and
    # every response (including 301/401) carries HSTS so the browser pins
    # https and never attempts plain http again. Railway terminates TLS at
    # the edge and forwards the real scheme in x-forwarded-proto; localhost
    # has no proxy header, so local dev is untouched (and never HSTS-pinned).
    _HSTS_MAX_AGE = os.environ.get("BRAIN_HSTS_MAX_AGE", "31536000")  # 1 year

    @app.middleware("http")
    async def _https_and_hsts(request: Request, call_next):
        from fastapi.responses import RedirectResponse

        proto = request.headers.get("x-forwarded-proto", "")
        if proto == "http":
            url = request.url.replace(scheme="https")
            return RedirectResponse(str(url), status_code=301)
        response = await call_next(request)
        if proto == "https":
            response.headers["Strict-Transport-Security"] = (
                f"max-age={_HSTS_MAX_AGE}; includeSubDomains"
            )
        return response

    # ── Public auth routes (reused from the brain UI) ───────────────────────
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/auth/logout")
    @app.get("/auth/logout")
    async def auth_logout():
        resp = RedirectResponse("/login", status_code=303)
        ui_auth.clear_session_cookies(resp)
        return resp

    @app.get("/login")
    async def login_page():
        return HTMLResponse(LOGIN_HTML.read_text(encoding="utf-8"))

    @app.post("/auth/login")
    async def auth_login(request: Request):
        if not ui_auth.is_configured():
            return JSONResponse({"ok": False, "error": "Authentication is not configured."}, status_code=503)
        body = await request.json()
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return JSONResponse({"ok": False, "error": "Email and password are required."}, status_code=400)
        session = await ui_auth.password_login(email, password)
        if not session or not session.get("access_token"):
            return JSONResponse({"ok": False, "error": "Invalid email or password."}, status_code=401)
        remember = bool(body.get("remember", True))
        resp = JSONResponse({"ok": True, "next": ui_auth.safe_next(body.get("next"))})
        ui_auth.set_session_cookies(resp, session, remember=remember)
        return resp

    @app.post("/auth/forgot")
    async def auth_forgot(request: Request):
        if ui_auth.is_configured():
            body = await request.json()
            reset_url = str(request.base_url).rstrip("/") + "/auth/reset"
            await ui_auth.request_password_reset(str(body.get("email", "")).strip(), redirect_to=reset_url)
        return JSONResponse({"ok": True})

    @app.get("/auth/reset")
    async def reset_page():
        html = RESET_HTML.read_text(encoding="utf-8")
        html = html.replace("__SUPABASE_URL__", os.environ.get("SUPABASE_URL", "").rstrip("/"))
        html = html.replace("__SUPABASE_ANON_KEY__", os.environ.get("SUPABASE_ANON_KEY", ""))
        return HTMLResponse(html)

    @app.post("/auth/admission")
    async def auth_admission(request: Request):
        from brain.ui import mailer

        body = await request.json()
        applicant = str(body.get("email", "")).strip()
        note = str(body.get("note", "")).strip()
        if not applicant:
            return JSONResponse({"ok": False, "error": "An email is required."}, status_code=400)
        to = os.environ.get("ADMISSION_NOTIFY_EMAIL", "").strip() or "admin@thegaim.app"
        safe_applicant = html_escape(applicant)
        note_html = (
            f"<p style='margin:16px 0 0;color:#52525b'><strong>Note:</strong> {html_escape(note)}</p>"
            if note else ""
        )
        html_body = (
            "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;"
            "margin:0 auto;color:#18181b\"><h2 style='font-weight:600'>New Elyceum admission request</h2>"
            f"<p style='color:#52525b'><strong>{safe_applicant}</strong> has requested admission to "
            f"Elyceum.</p>{note_html}<p style='margin-top:24px;color:#71717a;font-size:13px'>Provision via "
            "<code>scripts/create_user.py</code> if approved.</p></div>"
        )
        text_body = f"New Elyceum admission request from {applicant}." + (f"\n\nNote: {note}" if note else "")
        await mailer.send_email(to, "Elyceum — new admission request", html_body, text=text_body)
        return JSONResponse({"ok": True})

    @app.post("/auth/logout")
    async def auth_logout():
        resp = JSONResponse({"ok": True})
        ui_auth.clear_session_cookies(resp)
        return resp

    # ── Keys page + Vault API (authed) ──────────────────────────────────────
    @app.get("/keys")
    async def keys_page():
        return HTMLResponse(KEYS_HTML.read_text(encoding="utf-8"))

    @app.get("/api/keys")
    async def api_keys_status(request: Request):
        from brain import vault

        try:
            status = vault.get_status(_access_token(request))
        except Exception as e:
            logger.error("[gateway] key status failed: %s", e)
            return JSONResponse({"error": "status unavailable"}, status_code=502)
        return JSONResponse(status)

    @app.post("/api/keys")
    async def api_keys_set(request: Request):
        from brain import vault

        body = await request.json()
        provider = str(body.get("provider", "")).strip()
        value = str(body.get("value", "")).strip()
        if provider not in vault.VALID_PROVIDERS:
            return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=400)
        if not value:
            # Blank = leave unchanged (mirror the brain's settings convention).
            return JSONResponse({"ok": True, "unchanged": True})
        try:
            vault.set_key(_access_token(request), provider, value)
        except Exception as e:
            logger.error("[gateway] set key failed: %s", e)
            return JSONResponse({"ok": False, "error": "could not store key"}, status_code=502)
        return JSONResponse({"ok": True})

    @app.delete("/api/keys/{provider}")
    async def api_keys_delete(request: Request, provider: str):
        from brain import vault

        if provider not in vault.VALID_PROVIDERS:
            return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=400)
        try:
            vault.delete_key(_access_token(request), provider)
        except Exception as e:
            logger.error("[gateway] delete key failed: %s", e)
            return JSONResponse({"ok": False, "error": "could not delete key"}, status_code=502)
        return JSONResponse({"ok": True})

    # ── Readiness poll for the interstitial ─────────────────────────────────
    @app.get("/__brain_status")
    async def brain_status(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:  # public-path fall-through / auth-disabled edge
            return JSONResponse({"ready": False, "state": "unauthorized"}, status_code=401)
        tenant = _tenant_for(user["sub"])
        st = provisioner.status(tenant)
        if st is None:
            asyncio.create_task(_safe_ensure(provisioner, tenant))
            return JSONResponse({"ready": False, "state": "starting"})
        return JSONResponse({"ready": (not st["booting"]), "state": "booting" if st["booting"] else "ready"})

    # ── WebSocket proxy ─────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def ws_proxy(client_ws: WebSocket):
        if not ui_auth.is_disabled():
            if not ui_auth.is_configured():
                await client_ws.close(code=1008)
                return
            claims, _ = await ui_auth.authenticate(client_ws)
            if claims is None:
                await client_ws.close(code=1008)
                return
            uid = claims["sub"]
        else:
            uid = os.environ.get("BRAIN_USER_ID", "dev")
        tenant = _tenant_for(uid)
        st = provisioner.status(tenant)
        if not st or st["booting"]:
            # Not ready yet — tell the client to retry (the page is on the interstitial anyway).
            await client_ws.close(code=1013)
            return
        provisioner.touch(tenant)  # a live client connection counts as activity
        await _proxy_ws(client_ws, st["port"], on_activity=lambda: provisioner.touch(tenant))

    # ── Sleep (shutdown brain + pause pod) ─────────────────────────────────
    @app.post("/shutdown")
    async def sleep_brain(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        tenant = _tenant_for(user["sub"])
        # Forward to the brain subprocess so it can run sleep consolidation.
        st = provisioner.status(tenant)
        if st and not st["booting"]:
            try:
                async with httpx.AsyncClient(timeout=5.0) as _c:
                    await _c.post(f"http://127.0.0.1:{st['port']}/shutdown")
            except Exception:
                pass
        # Stop the shared RunPod pod — the brain subprocess is a consumer and
        # cannot do this itself. Schedule it so the brain gets a moment to
        # save state (consolidation uses Anthropic API, not the pod) before
        # the pod stops. The watcher task is cancelled so it won't restart it.
        runpod = runpod_holder[0] if runpod_holder else None
        if runpod is not None and not getattr(runpod, "_consumer", False):
            async def _pause_pod():
                await asyncio.sleep(5.0)
                try:
                    if getattr(runpod, "_watcher_task", None):
                        runpod._watcher_task.cancel()
                        runpod._watcher_task = None
                    pod_id = getattr(runpod, "_pod_id", None)
                    if pod_id:
                        await runpod._stop_pod(pod_id)
                        runpod._pod_id = None
                        logger.info("[gateway] shared RunPod pod paused on sleep")
                except Exception as _e:
                    logger.warning("[gateway] pod pause failed: %s", _e)
            asyncio.create_task(_pause_pod())
        return JSONResponse({"ok": True})

    # ── HTTP catch-all → ensure + proxy (authed) ────────────────────────────
    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy(request: Request, path: str):
        # Public paths without a dedicated gateway route (and non-GET methods on
        # routed paths, e.g. HEAD /login) fall through to this catch-all with no
        # auth state — send them to login instead of crashing on state.user.
        user = getattr(request.state, "user", None)
        if user is None:
            if _wants_html(request):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        tenant = _tenant_for(user["sub"])
        st = provisioner.status(tenant)
        if st and not st["booting"]:
            provisioner.touch(tenant)  # activity → reset the idle backstop timer
            return await _proxy_http(request, st["port"])
        # Brain not running yet: require an Anthropic key before spawning.
        if not await _has_anthropic(request):
            if _wants_html(request):
                return RedirectResponse("/keys", status_code=303)
            return JSONResponse({"error": "no_anthropic_key"}, status_code=403)
        if st is None:
            asyncio.create_task(_safe_ensure(provisioner, tenant))
        if _wants_html(request):
            return HTMLResponse(INTERSTITIAL_HTML.read_text(encoding="utf-8"), status_code=200)
        return JSONResponse({"status": "booting"}, status_code=503)

    return app


# ── helpers ─────────────────────────────────────────────────────────────────
async def _has_anthropic(request: Request) -> bool:
    from brain import vault

    try:
        status = vault.get_status(_access_token(request))
        return bool(status.get("anthropic"))
    except Exception as e:
        logger.error("[gateway] anthropic-key check failed: %s", e)
        return False


async def _safe_ensure(provisioner: Provisioner, uid: str) -> None:
    try:
        await provisioner.ensure(uid)
    except Exception as e:
        logger.error("[gateway] ensure failed for %s: %s", uid[:8], e)


async def _proxy_http(request: Request, port: int) -> Response:
    url = f"http://127.0.0.1:{port}{request.url.path}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            up = await client.request(
                request.method, url, headers=headers, content=body, follow_redirects=False
            )
    except Exception as e:
        logger.error("[gateway] http proxy error: %s", e)
        return JSONResponse({"error": "bad_gateway"}, status_code=502)
    resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in _HOP_BY_HOP}
    return Response(content=up.content, status_code=up.status_code, headers=resp_headers)


async def _proxy_ws(client_ws: WebSocket, port: int, on_activity=None) -> None:
    import websockets

    await client_ws.accept()
    cookie = client_ws.headers.get("cookie", "")
    upstream_url = f"ws://127.0.0.1:{port}/ws"
    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers={"Cookie": cookie} if cookie else None,
            max_size=None,
            open_timeout=20,
        )
    except Exception as e:
        logger.error("[gateway] ws upstream connect failed: %s", e)
        with _suppress():
            await client_ws.close(code=1011)
        return

    async def client_to_upstream():
        try:
            while True:
                msg = await client_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if on_activity is not None:
                    on_activity()  # inbound client frame = activity → reset idle timer
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            pass

    upstream_failed = False

    async def upstream_to_client():
        nonlocal upstream_failed
        try:
            async for message in upstream:
                if isinstance(message, (bytes, bytearray)):
                    await client_ws.send_bytes(bytes(message))
                else:
                    await client_ws.send_text(message)
        except Exception as e:
            # An abnormal upstream close means the brain process died or hung —
            # tell the client (1011) so it retries, and tell the operator.
            upstream_failed = True
            logger.warning("[gateway] ws upstream closed abnormally on :%d: %s", port, e)

    try:
        done, pending = await asyncio.wait(
            {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        with _suppress():
            await upstream.close()
        with _suppress():
            await client_ws.close(code=1011 if upstream_failed else 1000)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(override=True)
    logging.basicConfig(level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"))

    provisioner = Provisioner()
    runpod_holder: list = [None]
    app = build_gateway_app(provisioner, runpod_holder)

    # The gateway is the SINGLE owner of the shared RunPod pod. Tenant children
    # run in consumer mode (BRAIN_MULTITENANT + RUNPOD_HOST → no lifecycle), so
    # something has to keep the shared pod alive and recover it when it 502s.
    # That's the gateway: one RunPodManager, one watcher, no per-tenant races.

    @app.on_event("startup")
    async def _startup():
        await provisioner.start()
        logger.info("[gateway] provisioner started")
        try:
            from brain.runpod_manager import RunPodManager

            runpod = RunPodManager()
            ok = await runpod.start()
            # Publish the resolved pod host so every tenant the provisioner spawns
            # inherits it via os.environ.copy() and enters consumer mode. (Stable
            # across resume; a brand-new pod id would need a gateway restart.)
            from brain.settings import settings as _s

            host = str(_s.get("runpod_host") or "").strip()
            if ok and host and "localhost" not in host:
                os.environ["RUNPOD_HOST"] = host
                runpod_holder[0] = runpod
                logger.info("[gateway] shared RunPod pod ready — RUNPOD_HOST=%s", host)
            else:
                logger.warning("[gateway] no shared RunPod pod — tenants will lack local inference")
        except Exception as e:
            logger.warning("[gateway] RunPod manager failed to start (non-fatal): %s", e)

    @app.on_event("shutdown")
    async def _shutdown():
        await provisioner.stop()
        # Leave the shared pod RUNNING across gateway redeploys (warm restart);
        # just cancel the watcher task so it doesn't outlive the process.
        runpod = runpod_holder[0]
        if runpod is not None and getattr(runpod, "_watcher_task", None):
            runpod._watcher_task.cancel()

    port = int(os.environ.get("PORT", "8765"))
    host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    logger.info("[gateway] listening on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
