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
import contextlib
import logging
import os
import shutil
import subprocess
import threading
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
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# How long to let a brain consolidate (end-of-session memory write) on Sleep
# before force-reaping it. Consolidation uses the cloud LLM and usually finishes
# in a few seconds, but a long session can take longer — don't cut it short.
SLEEP_CONSOLIDATE_WAIT_S = float(os.environ.get("BRAIN_SLEEP_CONSOLIDATE_WAIT_S", "90"))

# Multi-persona routing (Path A). When on, the /v1 engine API routes each request to
# the persona named in the X-Brain-Persona header, so one tenant can run several
# persona processes at once (e.g. a six-persona debate). Off → every request uses the
# tenant's single process (original behavior) and the header is ignored.
_MULTI_PERSONA = os.environ.get("BRAIN_MULTI_PERSONA", "").lower() in ("1", "true", "yes")
_PERSONA_HEADER = "x-brain-persona"


def _persona_header(headers) -> str | None:
    """The target persona for a request, or None to use the tenant's default process.
    Honored only when multi-persona routing is enabled, so the default deployment is
    byte-for-byte unchanged."""
    if not _MULTI_PERSONA:
        return None
    p = (headers.get(_PERSONA_HEADER) or "").strip()
    return p or None


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


async def _tenant_for(uid: str) -> str:
    """Resolve an authenticated user to their org id (the tenant the brain process
    and all data key on). Falls back to the uid itself when there's no membership
    (pre-migration / dev) — which for a personal org is the same value, so this is
    behavior-preserving.

    The underlying Supabase query is synchronous (supabase-py sync client). Runs in
    a thread on cache miss to avoid blocking the event loop."""
    hit = _org_cache.get(uid)
    if hit is not None and time.time() - hit[1] < _ORG_CACHE_TTL_S:
        return hit[0]
    from brain import org

    t = (await asyncio.to_thread(org.org_id_for_user, uid)) or uid
    _org_cache[uid] = (t, time.time())
    return t


def build_gateway_app(provisioner: Provisioner, runpod_holder: list | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    # Per-tenant sleep progress (tenant → {state, since, ...}), surfaced via
    # /__sleep_status. Cleared when the tenant's brain (re)spawns = it woke.
    sleep_status: dict = {}

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
            (refreshed or {}).get("access_token")
            if refreshed
            else request.cookies.get(ui_auth.ACCESS_COOKIE, "")
        )
        response = await call_next(request)
        if refreshed:
            ui_auth.set_session_cookies(response, refreshed, remember=ui_auth.remembered(request))
        return response

    # ── API-host gate ─────────────────────────────────────────────────────
    # BRAIN_API_HOST names a dedicated hostname for the engine API (e.g.
    # api.elyceum.app) pointed at THIS service. Routing here is path-based and
    # never inspects Host, so merely attaching a second domain would also serve
    # the login page and the cookie-authed UI proxy on it — a partner who typos a
    # path would get an HTML login redirect instead of JSON. This gate makes the
    # API host serve /v1 (+ /health) and nothing else.
    #
    # It narrows ONE hostname; the app host is untouched and keeps serving /v1 for
    # backwards compatibility, so existing integrations never break. Unset (the
    # default) → no host is special and behaviour is byte-for-byte unchanged.
    #
    # Registered BETWEEN the auth gate and the HTTPS layer, so the final wrapping
    # is: https/HSTS (outermost) → host gate → cookie auth → routes. An http
    # request is still upgraded before anything else runs, and a rejected path on
    # the API host never reaches the cookie gate.
    #
    # HTTP only: Starlette does not run http middleware for WebSocket scopes. The
    # one WS route is /v1/... — allowed on the API host anyway — so there is
    # nothing to gate.
    _API_HOST = os.environ.get("BRAIN_API_HOST", "").strip().lower()
    if _API_HOST:
        logger.info("[gateway] engine API host: %s (serves /v1 only)", _API_HOST)

    def _is_api_host(request: Request) -> bool:
        if not _API_HOST:
            return False
        # Strip the port: a Host header legitimately carries one (localhost:8080),
        # and Railway's edge does not, so compare on the name alone.
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        return host == _API_HOST

    @app.middleware("http")
    async def _api_host_gate(request: Request, call_next):
        if _is_api_host(request):
            path = request.url.path
            # /health stays reachable so the API hostname can be probed on its own
            # (Railway's own healthcheck is internal and never sees this).
            if path != "/health" and not (path == "/v1" or path.startswith("/v1/")):
                return JSONResponse(
                    {"detail": f"not found — {_API_HOST} serves the /v1 engine API only"},
                    status_code=404,
                )
        return await call_next(request)

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
        # Aggregate tenant footprint only — this route is unauthenticated, so no
        # per-org detail here (that's in the reconcile-tick log lines).
        # `commit` = the code this container is actually running (Railway injects
        # RAILWAY_GIT_COMMIT_SHA) — the only external way to tell whether a push
        # has reached prod, since tenant processes inherit the container's code.
        commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:12]
        try:
            stats = provisioner.tenant_stats()
            return {
                "status": "ok",
                "commit": commit,
                "tenants": len(stats),
                "tenants_booting": sum(1 for s in stats if s["booting"]),
                "rss_total_mb": round(sum(s["rss_mb"] or 0 for s in stats)),
            }
        except Exception:
            return {"status": "ok", "commit": commit}

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
            return JSONResponse(
                {"ok": False, "error": "Authentication is not configured."}, status_code=503
            )
        body = await request.json()
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return JSONResponse(
                {"ok": False, "error": "Email and password are required."}, status_code=400
            )
        session = await ui_auth.password_login(email, password)
        if not session or not session.get("access_token"):
            return JSONResponse(
                {"ok": False, "error": "Invalid email or password."}, status_code=401
            )
        remember = bool(body.get("remember", True))
        resp = JSONResponse({"ok": True, "next": ui_auth.safe_next(body.get("next"))})
        ui_auth.set_session_cookies(resp, session, remember=remember)
        return resp

    @app.post("/auth/forgot")
    async def auth_forgot(request: Request):
        if ui_auth.is_configured():
            body = await request.json()
            # external_base_url, not request.base_url: the latter is http:// behind
            # Railway's edge, and GoTrue silently drops an unlisted redirect_to.
            reset_url = ui_auth.external_base_url(request) + "/auth/reset"
            await ui_auth.request_password_reset(
                str(body.get("email", "")).strip(), redirect_to=reset_url
            )
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
            if note
            else ""
        )
        html_body = (
            '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;'
            "margin:0 auto;color:#18181b\"><h2 style='font-weight:600'>New Elyceum admission request</h2>"
            f"<p style='color:#52525b'><strong>{safe_applicant}</strong> has requested admission to "
            f"Elyceum.</p>{note_html}<p style='margin-top:24px;color:#71717a;font-size:13px'>Provision via "
            "<code>scripts/create_user.py</code> if approved.</p></div>"
        )
        text_body = f"New Elyceum admission request from {applicant}." + (
            f"\n\nNote: {note}" if note else ""
        )
        await mailer.send_email(to, "Elyceum — new admission request", html_body, text=text_body)
        return JSONResponse({"ok": True})

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

    def _kick_pod() -> None:
        """Fire-and-forget: start resuming the shared pod NOW (don't wait for the
        reconciler's next tick) so its boot overlaps the brain boot and the UI
        shows progress immediately. Idempotent — ensure_running() no-ops if alive.

        Tier-aware: a lite brain never uses the pod, so don't eagerly spin a GPU for
        one. We can't know an as-yet-unbooted brain's tier here (it's resolved inside
        the process and reported on /health), so warm eagerly only when we have positive
        reason to believe a full brain needs it:
          • BRAIN_TIER=full — the operator's authoritative override (the current hosted
            default; preserves the boot-overlap warm exactly as before), or
          • a full brain is already alive (full_count>0) — the pod is likely already up,
            so this is a cheap no-op that also covers a 2nd persona/tab.
        BRAIN_TIER=lite never warms. When tier is per-tenant (BRAIN_TIER unset) a brand-new
        full brain isn't warmed here — the reconciler brings the pod up once it reports
        full on /health. That trades a little cold-start latency for never letting a
        lite-only tenant spin the GPU pod; a tier-aware eager warm is deferred work."""
        runpod = runpod_holder[0] if runpod_holder else None
        if runpod is None:
            return
        tier_env = os.environ.get("BRAIN_TIER", "").strip().lower()
        if tier_env == "lite":
            return
        if tier_env == "full" or provisioner.full_count() > 0:
            asyncio.create_task(_safe_pod_ensure(runpod))

    # ── Readiness poll for the interstitial ─────────────────────────────────
    @app.get("/__brain_status")
    async def brain_status(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:  # public-path fall-through / auth-disabled edge
            return JSONResponse({"ready": False, "state": "unauthorized"}, status_code=401)
        tenant = await _tenant_for(user["sub"])
        st = provisioner.status(tenant)
        if st is None:
            sleep_status.pop(tenant, None)  # respawning = waking up
            asyncio.create_task(_safe_ensure(provisioner, tenant))
            _kick_pod()  # warm the shared pod in parallel with the brain boot
            return JSONResponse({"ready": False, "state": "starting"})
        return JSONResponse(
            {"ready": (not st["booting"]), "state": "booting" if st["booting"] else "ready"}
        )

    # ── Shared-pod boot status (polled by the in-app banner) ────────────────
    @app.get("/__pod_status")
    async def pod_status(request: Request):
        if getattr(request.state, "user", None) is None:
            return JSONResponse({"state": "unknown"}, status_code=401)
        runpod = runpod_holder[0] if runpod_holder else None
        if runpod is None:
            # No pod manager (no RunPod key / local-only) — nothing to show.
            return JSONResponse({"state": "off", "detail": "", "elapsed_s": 0})
        return JSONResponse(runpod.status())

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
        tenant = await _tenant_for(uid)
        st = provisioner.status(tenant)
        if not st or st["booting"]:
            # Not ready yet — tell the client to retry (the page is on the interstitial anyway).
            await client_ws.close(code=1013)
            return
        provisioner.touch(tenant)  # a live client connection counts as activity
        await _proxy_ws(client_ws, st["port"], on_activity=lambda: provisioner.touch(tenant))

    # ── Sleep (shutdown brain + pause pod when last one sleeps) ─────────────
    # Per-tenant sleep progress, polled by the UI's /__sleep_status so a user can
    # confirm shutdown is progressing (not stuck). Phases:
    #   consolidating → stopping → pausing_pod → asleep   (or error)
    def _set_sleep(tenant: str, state: str, **extra) -> None:
        sleep_status[tenant] = {"state": state, "since": time.time(), **extra}

    async def _consolidate_and_stop(org: str, persona: str | None) -> None:
        """Gracefully shut ONE instance (default or dedicated persona): POST its
        /shutdown (SIGTERM handler runs end-of-session consolidation), wait for a
        clean exit, then reap. Waiting matters — force-killing mid-consolidation
        loses the Hebbian/narrator pass for whatever traces that instance holds."""
        st = provisioner.status(org, persona)
        if st and not st["booting"]:
            try:
                async with httpx.AsyncClient(timeout=10.0) as _c:
                    await _c.post(f"http://127.0.0.1:{st['port']}/shutdown")
            except Exception:
                pass
            deadline = time.time() + SLEEP_CONSOLIDATE_WAIT_S
            while time.time() < deadline and provisioner.is_running(org, persona):
                await asyncio.sleep(1.0)
        await provisioner.stop_user(org, persona)

    async def _do_sleep(tenant: str) -> None:
        """Sleep one ORG — every instance of it: each dedicated persona brain first,
        the default (shared) instance last, then pause the shared pod if no other
        org's brain needs it. Sweeping ALL instances is what guarantees the org's
        learning consolidates regardless of how personas were placed: every trace
        buffer lives in exactly one instance, and each instance's shutdown runs its
        own per-persona-grouped consolidation. Shared by the UI Sleep button and
        the engine API POST /v1/sleep, with progress in sleep_status."""
        phase = "consolidating"  # tracked so an error names the step that failed
        try:
            # 1+2. Consolidate + stop every live instance of this org. keys_for()
            # orders dedicated instances first, the default (fallback) last.
            _set_sleep(tenant, "consolidating")
            keys = provisioner.keys_for(tenant) or [tenant]
            for key in keys:
                org, _, persona = key.partition("::")
                if persona:
                    logger.info(
                        "[gateway] sleep sweep: consolidating dedicated instance %s::%s",
                        org[:8],
                        persona,
                    )
                await _consolidate_and_stop(org, persona or None)
            phase = "stopping"
            _set_sleep(tenant, "stopping")

            # 3. Pause the shared pod — only if NO other FULL-tier brain still needs it.
            # A lingering lite brain runs entirely on cloud and never touches the pod,
            # so it shouldn't keep a GPU alive. The brain subprocess is a consumer and
            # can't stop the pod itself.
            phase = "pausing_pod"
            runpod = runpod_holder[0] if runpod_holder else None
            if runpod is None:
                pod = "none"
            elif provisioner.full_count() == 0:
                _set_sleep(tenant, "pausing_pod")
                await runpod.pause()
                logger.info("[gateway] last full-tier brain slept — shared pod paused")
                pod = "paused"
            else:
                pod = "kept"  # other full-tier sessions still using the pod

            _set_sleep(tenant, "asleep", pod=pod)
        except Exception as e:
            logger.error("[gateway] sleep failed for %s at %s: %s", tenant[:8], phase, e)
            _set_sleep(tenant, "error", at=phase, detail=str(e)[:120])

    @app.post("/shutdown")
    async def sleep_brain(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        tenant = await _tenant_for(user["sub"])
        asyncio.create_task(_do_sleep(tenant))
        return JSONResponse({"ok": True})

    # ── Sleep progress (polled by the UI sleep panel) ───────────────────────
    @app.get("/__sleep_status")
    async def sleep_status_ep(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse({"state": "awake"}, status_code=401)
        tenant = await _tenant_for(user["sub"])
        s = sleep_status.get(tenant)
        if not s:
            return JSONResponse({"state": "awake"})
        return JSONResponse(
            {
                "state": s["state"],
                "detail": s.get("detail", ""),
                "pod": s.get("pod", ""),
                "at": s.get("at", ""),
                "elapsed_s": round(max(0.0, time.time() - s["since"]), 1),
            }
        )

    # ── Engine API cost control: sleep + status (partner-key authed) ─────────
    # A partner can turn off the cost-generating parts (brain process + GPU pod) for
    # their org, and inspect cost state, without the UI. Registered BEFORE the /v1
    # catch-all proxy so they're handled at the gateway (which owns the pod), not
    # forwarded to the brain (a pod consumer that can't pause it). Waking is implicit
    # — any other /v1 call respawns the brain + kicks the pod on demand.
    @app.post("/v1/sleep")
    async def engine_api_sleep(request: Request):
        from brain.api import auth as _api_auth

        org = _api_auth.resolve_partner_org(request.headers.get("authorization"))
        if org is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        asyncio.create_task(_do_sleep(org))
        return JSONResponse({"ok": True, "state": "sleeping"})

    @app.get("/v1/status")
    async def engine_api_status(request: Request):
        from brain.api import auth as _api_auth

        org = _api_auth.resolve_partner_org(request.headers.get("authorization"))
        if org is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        st = provisioner.status(org)
        awake = bool(st and not st["booting"])
        runpod = runpod_holder[0] if runpod_holder else None
        sl = sleep_status.get(org)
        return JSONResponse(
            {
                # Is this org's brain process running (the per-request compute)?
                "brain": "awake" if awake else ("booting" if st else "asleep"),
                # Shared GPU pod state (the main cost): off/resuming/warming/ready/...
                "pod": (runpod.status() if runpod else {"state": "off"}),
                # Last sleep transition for this org, if any (asleep/consolidating/...).
                "sleep": ({"state": sl["state"], "pod": sl.get("pod", "")} if sl else None),
            }
        )

    # ── OpenAPI schema + Swagger UI (no tenant needed) ───────────────────────
    # The engine app serves Swagger at /v1/docs on its own port, but the schema it
    # fetches lives at /openapi.json on the origin ROOT — which the gateway's
    # cookie-authed catch-all bounces to login (and, on a dedicated API host, 404s).
    # So Swagger has never actually worked through the gateway.
    #
    # Serve both HERE instead. build_api_router takes any callable as its turn
    # runner, so the route table can be introspected with a dummy and NO tenant
    # process — the same trick brain/api/reference.py uses. The schema is identical
    # for every tenant (routes are static), so one cached document serves everyone
    # and this path never spawns a brain or touches the pod.
    #
    # Deliberately unauthenticated: this is the partner-facing surface already
    # published in the app's API docs, it contains no tenant data, and Swagger UI cannot
    # attach a bearer key to its own schema fetch — requiring one would just put
    # the page back to broken. Kill with BRAIN_PUBLIC_API_DOCS=0.
    #
    # Registered BEFORE the /v1 catch-all so the proxy doesn't swallow them.
    #
    # Caveat: OpenAPI has no WebSocket concept, so WS /v1/sessions/{id}/stream is
    # absent from the schema by construction. The Documentation section of the API
    # workspace (brain/api/api_guide.md §10) is its reference.
    _PUBLIC_API_DOCS = os.environ.get("BRAIN_PUBLIC_API_DOCS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    _openapi_cache: dict = {}

    def _engine_openapi() -> dict:
        """The engine API's OpenAPI document, built once per process."""
        if "doc" not in _openapi_cache:
            from fastapi.openapi.utils import get_openapi

            from brain.api.server import build_api_router

            async def _dummy(*a, **k):  # never called — we only read the route table
                return {}

            probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
            probe.include_router(build_api_router(_dummy))
            _openapi_cache["doc"] = get_openapi(
                title="Elyceum Engine API",
                version="v1",
                description=(
                    "Bearer-authed engine API. Full developer reference — request/response "
                    "shapes, the SSE and WebSocket transports, error semantics and quotas — "
                    "is the Documentation section of the API workspace."
                ),
                routes=probe.routes,
            )
        return _openapi_cache["doc"]

    if _PUBLIC_API_DOCS:

        @app.get("/v1/openapi.json")
        async def engine_openapi():
            return JSONResponse(_engine_openapi())

        @app.get("/v1/docs")
        async def engine_docs():
            from fastapi.openapi.docs import get_swagger_ui_html

            return get_swagger_ui_html(
                openapi_url="/v1/openapi.json", title="Elyceum Engine API — v1"
            )

    # ── Engine API (/v1) → partner-key routing + on-demand spawn + pod kick ──
    # Bearer key → org (cross-org lookup), then spawn the org's brain (which runs
    # its API server) and warm the pod, exactly like the UI path does. This is what
    # makes partner API traffic spin the pod up. Streamed so SSE turns pass through.
    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def engine_api_proxy(request: Request, path: str):
        from brain.api import auth as _api_auth

        org = _api_auth.resolve_partner_org(request.headers.get("authorization"))
        if org is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        persona = _persona_header(request.headers)  # None unless multi-persona is on
        st = provisioner.status(org, persona)
        if st and not st["booting"] and st.get("api_port"):
            provisioner.touch(org, persona)
            return await _proxy_http_stream(request, st["api_port"])
        # Brain not up yet: spawn it (starts its API server) + warm the pod, and tell
        # the partner to retry. Idempotent — concurrent calls await one spawn.
        if st is None:
            sleep_status.pop(org, None)
            asyncio.create_task(_safe_ensure(provisioner, org, persona))
            _kick_pod()
        return JSONResponse({"status": "booting"}, status_code=503)

    @app.websocket("/v1/sessions/{session_id}/stream")
    async def engine_api_ws(client_ws: WebSocket, session_id: str):
        from brain.api import auth as _api_auth

        org = _api_auth.resolve_partner_org(client_ws.headers.get("authorization"))
        if org is None:
            await client_ws.close(code=1008)
            return
        persona = _persona_header(client_ws.headers)  # None unless multi-persona is on
        st = provisioner.status(org, persona)
        if not st or st["booting"] or not st.get("api_port"):
            if st is None:
                asyncio.create_task(_safe_ensure(provisioner, org, persona))
                _kick_pod()
            await client_ws.close(code=1013)  # not ready — partner retries
            return
        provisioner.touch(org, persona)
        await _proxy_ws(
            client_ws,
            st["api_port"],
            upstream_path=f"/v1/sessions/{session_id}/stream",
            extra_headers={"Authorization": client_ws.headers.get("authorization", "")},
            on_activity=lambda: provisioner.touch(org),
        )

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
        tenant = await _tenant_for(user["sub"])
        st = provisioner.status(tenant)
        if st and not st["booting"]:
            provisioner.touch(tenant)  # activity → reset the idle backstop timer
            return await _proxy_http(request, st["port"])
        # Brain not running yet: require an Anthropic key before spawning.
        if not await _has_anthropic(request, tenant):
            if _wants_html(request):
                return RedirectResponse("/keys", status_code=303)
            return JSONResponse({"error": "no_anthropic_key"}, status_code=403)
        if st is None:
            sleep_status.pop(tenant, None)  # respawning = waking up
            asyncio.create_task(_safe_ensure(provisioner, tenant))
            _kick_pod()  # warm the shared pod in parallel with the brain boot
        if _wants_html(request):
            return HTMLResponse(INTERSTITIAL_HTML.read_text(encoding="utf-8"), status_code=200)
        return JSONResponse({"status": "booting"}, status_code=503)

    return app


# ── event-loop watchdog (self-heal) ──────────────────────────────────────────
# The gateway is the single public front door; if its asyncio loop ever wedges
# (a stray synchronous/blocking call), every request — including /health — goes
# dark, and Railway's healthcheck runs only at deploy time so a SUCCESS deployment
# is never auto-restarted. This watchdog makes a wedge self-healing: an async
# heartbeat stamps a timestamp each second, a daemon thread watches it, and if the
# loop stops ticking for BRAIN_GW_WATCHDOG_S the process force-exits so Railway's
# restartPolicyType=on_failure brings up a fresh container. A correctly-offloaded
# spawn never trips this; it's the backstop for the next blocker we haven't found.
_loop_heartbeat: list[float] = [0.0]
_WATCHDOG_THRESHOLD_S = float(os.environ.get("BRAIN_GW_WATCHDOG_S", "60"))


async def _loop_heartbeat_task() -> None:
    import time as _t

    while True:
        _loop_heartbeat[0] = _t.monotonic()
        await asyncio.sleep(1.0)


# ── CPU embedding sidecar ─────────────────────────────────────────────────────
# Embeddings are the highest-volume model call (10-15/turn: recall, DMN dedup)
# but need no GPU — nomic-embed-text runs on CPU in tens of ms. Without this,
# hosted tenants embed against OLLAMA_HOST (nothing local on Railway) and flip
# permanently to Google — cost + latency + memory content leaving the box. The
# gateway runs ONE CPU Ollama for the whole host (the model loads once, not per
# brain) and points every tenant at it via OLLAMA_EMBED_HOST (env-inherited at
# spawn; running brains pick it up on their next respawn).
_EMBED_SIDECAR = os.environ.get("BRAIN_EMBED_SIDECAR", "1").lower() not in ("0", "false")
_EMBED_SIDECAR_PORT = int(os.environ.get("BRAIN_EMBED_SIDECAR_PORT", "11500"))


def _start_embed_sidecar() -> subprocess.Popen | None:
    """Start the CPU Ollama embed sidecar; returns the process or None (skipped).

    No-ops when disabled, when OLLAMA_EMBED_HOST is already pointed somewhere,
    or when the image has no ollama binary (dev machines run their own). The
    embed model pull happens in a background thread — embeds fall through the
    existing chain (OLLAMA_HOST → Google) until the sidecar is warm."""
    if not _EMBED_SIDECAR or os.environ.get("OLLAMA_EMBED_HOST"):
        return None
    binary = shutil.which("ollama")
    if binary is None:
        logger.info("[gateway] embed sidecar skipped — no ollama binary in image")
        return None
    listen = f"127.0.0.1:{_EMBED_SIDECAR_PORT}"
    env = os.environ.copy()
    env["OLLAMA_HOST"] = listen
    env["OLLAMA_MAX_LOADED_MODELS"] = "1"
    env["OLLAMA_NUM_PARALLEL"] = "2"
    env["OLLAMA_KEEP_ALIVE"] = "-1m"  # ~0.3 GB model — keep resident
    try:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv from shutil.which
            [binary, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning("[gateway] embed sidecar failed to start: %s", e)
        return None
    os.environ["OLLAMA_EMBED_HOST"] = f"http://{listen}"
    logger.info("[gateway] embed sidecar starting on %s (pid %d)", listen, proc.pid)

    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    def _pull():
        time.sleep(3)  # let serve bind
        try:
            r = subprocess.run(  # noqa: S603
                [binary, "pull", embed_model],
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if r.returncode == 0:
                logger.info("[gateway] embed sidecar ready — %s pulled", embed_model)
            else:
                logger.warning(
                    "[gateway] embed sidecar pull failed (%s): %s",
                    embed_model,
                    (r.stderr or "")[-300:],
                )
        except Exception as e:
            logger.warning("[gateway] embed sidecar pull error: %s", e)

    threading.Thread(target=_pull, daemon=True, name="embed-sidecar-pull").start()
    return proc


def _start_loop_watchdog() -> None:
    import time as _t

    _loop_heartbeat[0] = _t.monotonic()
    asyncio.ensure_future(_loop_heartbeat_task())

    def _watch() -> None:
        while True:
            _t.sleep(5.0)
            last = _loop_heartbeat[0]
            if last <= 0.0:
                continue
            lag = _t.monotonic() - last
            if lag > _WATCHDOG_THRESHOLD_S:
                logger.critical(
                    "[gateway] event loop wedged for %.0fs (>%.0fs) — force-exiting so "
                    "Railway restarts the container",
                    lag,
                    _WATCHDOG_THRESHOLD_S,
                )
                os._exit(1)

    threading.Thread(target=_watch, daemon=True, name="gw-loop-watchdog").start()
    logger.info("[gateway] loop watchdog armed (threshold %.0fs)", _WATCHDOG_THRESHOLD_S)


# ── helpers ─────────────────────────────────────────────────────────────────
async def _has_anthropic(request: Request, tenant: str | None = None) -> bool:
    # Check via the SERVICE ROLE keyed by the tenant id — the same path the
    # provisioner uses to inject the tenant's keys (vault.fetch_user_keys). The
    # earlier user-token status RPC (get_my_api_key_status) could report no key
    # even when one is on file (auth.uid() edge cases under asymmetric tokens),
    # which silently blocked every spawn and left the UI stuck on the interstitial.
    # `tenant` may be pre-resolved by the caller to avoid a second _tenant_for call.
    user = getattr(request.state, "user", None)
    if not user:
        return False
    from brain import vault

    try:
        tid = tenant or (await _tenant_for(user["sub"]))
        # fetch_user_keys is a synchronous Supabase RPC (+ decrypt); run it off the
        # event loop so the key check never blocks the gateway from serving requests.
        keys = await asyncio.to_thread(vault.fetch_user_keys, tid)
        return bool((keys or {}).get("anthropic"))
    except Exception as e:
        logger.error("[gateway] anthropic-key check failed: %s", e)
        return False


async def _safe_ensure(provisioner: Provisioner, uid: str, persona: str | None = None) -> None:
    try:
        await provisioner.ensure(uid, persona)
    except Exception as e:
        from brain.provisioner import CapacityError

        if isinstance(e, CapacityError):
            # Deliberate refusal, not a fault: the host is at its configured brain
            # budget. The user keeps seeing "booting"; this line is the diagnosis.
            logger.warning("[gateway] AT CAPACITY — %s", e)
        else:
            logger.error("[gateway] ensure failed for %s/%s: %s", uid[:8], persona or "-", e)


async def _safe_pod_ensure(runpod) -> None:
    try:
        await runpod.ensure_running()
    except Exception as e:
        logger.warning("[gateway] pod ensure failed: %s", e)


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


async def _proxy_http_stream(request: Request, port: int) -> Response:
    """Stream a proxied response through unbuffered — required for SSE turn streams
    (POST /v1/.../turns/stream) so the partner gets inner-life events as they happen,
    not all at once at the end. Works for plain JSON too (just one chunk)."""
    from fastapi.responses import StreamingResponse

    url = f"http://127.0.0.1:{port}{request.url.path}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    body = await request.body()
    # Reverse proxy: upstream responses (SSE/long polls) are intentionally unbounded.
    client = httpx.AsyncClient(timeout=None)  # nosec B113
    try:
        up = await client.send(
            client.build_request(request.method, url, headers=headers, content=body),
            stream=True,
        )
    except Exception as e:
        logger.error("[gateway] /v1 proxy error: %s", e)
        await client.aclose()
        return JSONResponse({"error": "bad_gateway"}, status_code=502)
    resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in _HOP_BY_HOP}

    async def _body():
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose()
            await client.aclose()

    return StreamingResponse(_body(), status_code=up.status_code, headers=resp_headers)


async def _proxy_ws(
    client_ws: WebSocket,
    port: int,
    on_activity=None,
    upstream_path: str = "/ws",
    extra_headers: dict | None = None,
) -> None:
    import websockets

    await client_ws.accept()
    hdrs = dict(extra_headers or {})
    cookie = client_ws.headers.get("cookie", "")
    if cookie and "Cookie" not in hdrs:
        hdrs["Cookie"] = cookie
    upstream_url = f"ws://127.0.0.1:{port}{upstream_path}"
    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers=hdrs or None,
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
    embed_sidecar_holder: list = [None]
    app = build_gateway_app(provisioner, runpod_holder)

    # The gateway is the SINGLE owner of the shared RunPod pod. Tenant children run
    # in consumer mode (BRAIN_MULTITENANT + RUNPOD_HOST → no lifecycle). The pod's
    # lifecycle is DEMAND-DRIVEN: a reconciler loop keeps it running only while ≥1
    # tenant brain is alive, and pauses it once the last brain is slept or reaped.
    # This is what prevents an orphaned pod from burning money with no consumer.
    reconciler_task: list = [None]

    # How long the live-brain count must stay at zero before the pod is paused.
    # Small grace absorbs a user logging out and back in without a resume cycle.
    pod_idle_grace_s = float(os.environ.get("BRAIN_POD_IDLE_GRACE_S", "600"))
    reconcile_interval_s = float(os.environ.get("BRAIN_POD_RECONCILE_S", "60"))

    def _sync_runpod_host(runpod):
        """Keep RUNPOD_HOST pointed at the live pod so every NEW tenant spawn inherits
        the right host, AND publish it to the shared host file so brains ALREADY
        running pick it up without a respawn (they poll BRAIN_RUNPOD_HOST_FILE). Without
        the latter, a host change (new pod after a churn/crash) left running tenants
        calling a dead pod until they were respawned."""
        host = runpod.published_host()
        if host and "localhost" not in host:
            if os.environ.get("RUNPOD_HOST") != host:
                os.environ["RUNPOD_HOST"] = host
                logger.info("[gateway] RUNPOD_HOST synced → %s", host)
            _publish_host_file(host)

    def _publish_host_file(host: str) -> None:
        """Write the live pod host to the shared file running consumer brains poll
        (empty = pod off). Delegates to the provisioner helper — atomic, idempotent
        — which the RunPodManager's terminate paths also use to unpublish."""
        from brain.provisioner import publish_runpod_host

        publish_runpod_host(host)

    def _log_tenant_stats():
        """One RSS line per reconcile tick — the sizing ground truth for how many
        brains this Railway plan actually fits (per-brain footprint was never
        measured on the hosted image; the dev-Mac figure includes Ollama)."""
        try:
            stats = provisioner.tenant_stats()
            if not stats:
                return
            total = sum(s["rss_mb"] or 0 for s in stats)
            detail = " ".join(
                f"{s['key'][:16]}={s['rss_mb'] or '?'}MB/{s['tier']}"
                f"{'(booting)' if s['booting'] else ''}"
                for s in stats
            )
            logger.info("[gateway] tenants=%d rss_total=%.0fMB %s", len(stats), total, detail)
        except Exception as e:
            logger.debug("[gateway] tenant stats failed: %s", e)

    async def _pod_reconciler(runpod):
        zero_since: float | None = None
        while True:
            try:
                await asyncio.sleep(reconcile_interval_s)
                _log_tenant_stats()
                # Publish per-org placement (which personas run dedicated) so each
                # org's SHARED instance drops them from its DMN roster. Derived
                # from live procs → self-heals when a dedicated instance dies.
                from brain.provisioner import write_placement_files

                write_placement_files(provisioner)
                # Gate on FULL-tier brains, not all live brains: a lite brain remaps
                # every local/runpod route to cloud and never uses the pod, so spinning
                # a GPU for a lite-only host is pure waste. full_count() reads each
                # brain's tier (reported on /health, captured at boot).
                full = provisioner.full_count()
                if full > 0:
                    zero_since = None
                    await runpod.ensure_running()
                    _sync_runpod_host(runpod)
                else:
                    if zero_since is None:
                        zero_since = time.time()
                    elif time.time() - zero_since >= pod_idle_grace_s and runpod._pod_id:
                        logger.info(
                            "[gateway] no full-tier brains for %.0fs — pausing shared pod",
                            time.time() - zero_since,
                        )
                        await runpod.pause()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[gateway] pod reconciler error: %s", e)

    @app.on_event("startup")
    async def _startup():
        _start_loop_watchdog()  # self-heal: force-restart if the event loop ever wedges
        # Before the provisioner: the sidecar sets OLLAMA_EMBED_HOST, which tenant
        # spawns inherit via os.environ.copy().
        embed_sidecar_holder[0] = _start_embed_sidecar()
        await provisioner.start()
        logger.info("[gateway] provisioner started")
        try:
            from brain.runpod_manager import RunPodManager

            runpod = RunPodManager()
            # Publish the stable pod host WITHOUT resuming — tenant spawns inherit it
            # via os.environ.copy() and enter consumer mode. The reconciler resumes
            # the pod lazily when a brain actually needs it.
            host = await runpod.discover_and_publish_host()
            if host and "localhost" not in host:
                os.environ["RUNPOD_HOST"] = host
                logger.info("[gateway] shared pod host published — RUNPOD_HOST=%s", host)
                _publish_host_file(host)  # seed the file so running brains can sync
            else:
                # No live pod to point at. Clear any baked-in RUNPOD_HOST (e.g. a stale
                # Railway var for a terminated pod) so tenants don't inherit a dead host
                # — they use cloud until the reconciler brings a pod up and re-syncs.
                if os.environ.pop("RUNPOD_HOST", None):
                    logger.warning("[gateway] cleared stale RUNPOD_HOST (no pod discovered)")
                else:
                    logger.warning("[gateway] no shared RunPod pod discovered")
            runpod_holder[0] = runpod
            reconciler_task[0] = asyncio.create_task(_pod_reconciler(runpod))
        except Exception as e:
            logger.warning("[gateway] RunPod manager failed to start (non-fatal): %s", e)

    @app.on_event("shutdown")
    async def _shutdown():
        await provisioner.stop()
        # Stop the reconciler. Leave the shared pod's RUNNING state alone here
        # (warm restart across redeploys): the next gateway's reconciler will pause
        # it within pod_idle_grace_s if no brains reconnect. Just cancel the manager's
        # liveness watcher so it doesn't outlive the process.
        if reconciler_task[0]:
            reconciler_task[0].cancel()
        runpod = runpod_holder[0]
        if runpod is not None:
            runpod._cancel_watcher()
        sidecar = embed_sidecar_holder[0]
        if sidecar is not None:
            with contextlib.suppress(Exception):
                sidecar.terminate()

    port = int(os.environ.get("PORT", "8765"))
    host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"  # nosec B104
    logger.info("[gateway] listening on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
