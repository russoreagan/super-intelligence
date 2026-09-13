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
import re
import secrets
import shutil
import subprocess
import threading
import time
from html import escape as html_escape
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from brain.api import rate_limit as _rl
from brain.persona_key import persona_slug
from brain.provisioner import Provisioner, internal_token
from brain.ui import auth as ui_auth


def _rate_limited(retry_after: float) -> JSONResponse:
    # Retry-After is the wait in seconds; X-RateLimit-Reset is the same instant as an
    # epoch second, so a client can pace against the clock without re-deriving it.
    return JSONResponse(
        {"detail": "rate limit exceeded"},
        status_code=429,
        headers={
            "Retry-After": str(int(retry_after)),
            "X-RateLimit-Reset": str(int(time.time() + retry_after)),
        },
    )


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

# Largest single WebSocket frame the gateway will relay. Live audio arrives as
# base64 PCM16 chunks inside JSON, which are small (tens of KB); 8 MB leaves room
# for a whole utterance in one frame without letting a peer allocate unboundedly.
_MAX_WS_FRAME_BYTES = int(os.environ.get("BRAIN_MAX_WS_FRAME_BYTES", str(8 * 1024 * 1024)))

# Multi-persona routing (Path A). When on, the /v1 engine API routes a request to
# the persona named in the X-Brain-Persona header — but ONLY when that persona is
# already running on its own dedicated instance (the org's promoted set); any other
# header value routes to the org's shared instance. The header never spawns a
# process. Off → every request uses the tenant's single process and the header is
# ignored.
# Default ON since the placement controller landed (2026-09-13): the header can only
# reach a persona that already has a dedicated instance, and dedicated instances
# exist only for placement rows, so an org with no placements is unchanged. `0` is
# the kill switch for routing AND the controller (brain/gateway/placement_control).
_MULTI_PERSONA = os.environ.get("BRAIN_MULTI_PERSONA", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# The placement controller's cross-tick state, shared between main()'s reconciler
# (which drives it) and the app's sleep sweep / superadmin view (which read it).
placement_holder: list = [None]
# The event-driven reconciler (brain/gateway/reconciler.py) driving the pool and
# placement ticks; routes wake it, the sleep sweep wakes it, children wake it.
reconciler_holder: list = [None]
# Loopback nudge secret: minted per gateway boot, handed to every tenant spawn as
# BRAIN_GATEWAY_NUDGE_TOKEN (with BRAIN_GATEWAY_NUDGE_URL) via os.environ.copy().
NUDGE_TOKEN = os.environ.get("BRAIN_GATEWAY_NUDGE_TOKEN") or secrets.token_urlsafe(24)
os.environ["BRAIN_GATEWAY_NUDGE_TOKEN"] = NUDGE_TOKEN
_NUDGE_REASONS = ("placement", "budget", "demand", "use", "pressure", "sleep")


def _is_loopback(request: Request) -> bool:
    host = (getattr(request.client, "host", "") or "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def wake_reconciler(reason: str) -> bool:
    """Wake the gateway's reconciler if one is running. False when none."""
    rec = reconciler_holder[0]
    if rec is None:
        return False
    rec.wake(reason)
    return True


async def consolidate_and_stop_instance(provisioner, org: str, persona: str | None) -> None:
    """Gracefully shut ONE instance (default or dedicated persona): POST its
    /shutdown (SIGTERM handler runs end-of-session consolidation), wait for a clean
    exit, then reap. Waiting matters — force-killing mid-consolidation loses the
    Hebbian/narrator pass for whatever traces that instance holds. Shared by the
    sleep sweep and the placement controller's demotion path."""
    st = provisioner.status(org, persona)
    if st and not st["booting"]:
        # The tenant keeps its cookie gate ON (the provisioner strips
        # BRAIN_AUTH_DISABLED), so this hop authenticates with the gateway's
        # internal token (brain/ui/auth.py INTERNAL_PATHS). Anything but a 200
        # means the tenant never received SIGTERM: the wait below would burn the
        # full SLEEP_CONSOLIDATE_WAIT_S for nothing and stop_user's SIGTERM would
        # then cut consolidation short — so the miss is logged loudly.
        label = f"{org[:8]}::{persona}" if persona else org[:8]
        try:
            async with httpx.AsyncClient(timeout=10.0) as _c:
                r = await _c.post(
                    f"http://127.0.0.1:{st['port']}/shutdown",
                    headers={"x-brain-internal-token": internal_token()},
                )
            if r.status_code != 200:
                logger.warning(
                    "[gateway] sleep: tenant %s refused /shutdown (%s) — consolidation "
                    "will be cut short by the reaper's SIGTERM",
                    label,
                    r.status_code,
                )
        except Exception as e:
            logger.warning("[gateway] sleep: /shutdown to tenant %s failed: %s", label, e)
        deadline = time.time() + SLEEP_CONSOLIDATE_WAIT_S
        while time.time() < deadline and provisioner.is_running(org, persona):
            await asyncio.sleep(1.0)
    await provisioner.stop_user(org, persona)


_PERSONA_HEADER = "x-brain-persona"
# The canonical persona slug shape, matching brain/personas.py's own validator.
_PERSONA_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def _persona_header(headers) -> str | None:
    """The target persona for a request, or None to use the tenant's default process.
    Honored only when multi-persona routing is enabled, so the default deployment is
    byte-for-byte unchanged."""
    if not _MULTI_PERSONA:
        return None
    raw = (headers.get(_PERSONA_HEADER) or "").strip()
    if not raw:
        return None
    # Slugify, then insist on the canonical shape. This value becomes a FILESYSTEM
    # PATH SEGMENT in the provisioner (TENANTS_DIR/<tenant>/personas/<persona>, which
    # is then mkdir'd) and part of the process key, so an unvalidated header was a
    # path-traversal primitive: Path joins ".." literally. Slugifying alone already
    # neutralises that (every non-alphanumeric becomes "_"); the explicit shape check
    # is what makes the guarantee auditable rather than incidental.
    #
    # Shape only here; membership is checked by _routable_persona inside the app
    # against the provisioner's live promoted set (no Supabase round trip — it is
    # the gateway's own process table). Before 2026-09-13 a well-formed header was
    # honoured as-is, and a cold status for it went straight to _safe_ensure: any
    # partner key could spawn an arbitrary persona process by naming a slug.
    slug = persona_slug(raw)
    return slug if _PERSONA_SLUG_RE.match(slug) else None


# GET /v1/whoami reads the org's learning mode from the organizations row. One
# cached read per org per minute; the row is tiny and the route is the partner's
# first call, so it must stay cheap and must never spawn anything.
_ORG_LEARNING_TTL_S = 60.0
_org_learning_cache: dict[str, tuple[str, str | None, float]] = {}


async def _org_learning(org: str) -> tuple[str, str | None]:
    """(learning_mode, instance_seed) for an org, from a cached organizations row
    read under the gateway's service role. ("unknown", None) when the row cannot
    be read and nothing is cached — the engine twin's convention (treat unknown
    as isolated); a stale cached value is preferred over unknown."""
    now = time.time()
    hit = _org_learning_cache.get(org)
    if hit and now - hit[2] < _ORG_LEARNING_TTL_S:
        return hit[0], hit[1]
    row = None
    try:
        from brain import org_settings
        from brain.gateway import fleet_orgs

        row = await asyncio.to_thread(org_settings.read_org_row, org, fleet_orgs._client())
    except Exception as e:
        logger.debug("[gateway] org row read failed for %s: %s", org[:8], e)
    if not row:
        return (hit[0], hit[1]) if hit else ("unknown", None)
    mode = str(row.get("learning_mode") or "consolidated")
    seed = str(row.get("instance_seed") or "default")
    _org_learning_cache[org] = (mode, seed, now)
    return mode, seed


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


def pod_pool_enabled() -> bool:
    """BRAIN_POD_POOL — the pod pool (plan §10) is ON by default; set 0/false to fall
    back to the single-pod reconciler and one RunPodManager. Read at call time so a
    test (or an operator flipping the var before a restart) sees the current value."""
    return os.environ.get("BRAIN_POD_POOL", "1").strip().lower() not in ("0", "false", "no", "off")


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

    # ── /v1 abuse control: body cap + rate limit ─────────────────────────────
    # Registered after the auth gate so it wraps OUTSIDE it: a request that is going
    # to be throttled or rejected for size should never reach a database lookup.
    #
    # Scoped to /v1 deliberately. The cookie-authed UI has different traffic shapes
    # (and its own login throttling concerns) and must not share a budget with
    # partner API traffic.
    #
    # NOTE: Starlette does NOT run http middleware for WebSocket scopes, so this
    # cannot be the only enforcement point — see engine_api_ws, which checks the
    # same limiter explicitly. A middleware-only design leaves the WS route open.
    _MAX_BODY_BYTES = int(os.environ.get("BRAIN_MAX_BODY_BYTES", str(10 * 1024 * 1024)))

    @app.middleware("http")
    async def _v1_guard(request: Request, call_next):
        path = request.url.path
        if not (path == "/v1" or path.startswith("/v1/")):
            return await call_next(request)

        # Size first: rejecting a 2 GB upload should not require resolving a key.
        # Content-Length is a claim, not a fact, so _proxy_http also bounds the
        # streamed read; this is the cheap early exit for honest clients.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
            return JSONResponse(
                {"detail": f"request body exceeds {_MAX_BODY_BYTES} bytes"},
                status_code=413,
            )

        ip = _rl.client_ip(request.headers, getattr(request.client, "host", None))
        tok = _rl.token_key(request.headers.get("authorization"))
        retry = _rl.limiter.check("key", tok)
        if retry is not None:
            return _rate_limited(retry)

        response = await call_next(request)

        # An auth failure is counted per IP, not per key: an attacker rotates tokens
        # freely, so a per-key budget would never bind.
        if response.status_code == 401:
            fail_retry = _rl.limiter.check("auth_fail", ip)
            if fail_retry is not None:
                return _rate_limited(fail_retry)
        limit, left = _rl.limiter.remaining("key", tok)
        if limit:
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(left)
            response.headers["X-RateLimit-Reset"] = str(_rl.limiter.reset_at("key", tok))
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
        # With the pool this carries pod 0's boot phase (state/detail/elapsed_s for the
        # banner) PLUS the pool summary: pods[], ready, assignments, max_pods.
        body = runpod.status()
        # The manager's own cost_accrued_usd is per-POD-SESSION: it resets every time
        # the pod restarts, so it can never answer "what has the GPU cost today". Pair
        # it with the durable daily ledger, and with WHY the pod is currently down —
        # "nothing wants it" and "the budget is spent" look identical from the outside,
        # and telling them apart is the difference between a tuning dial and a bug.
        from brain import pod_budget
        from brain.provisioner import pod_demand_age_s

        body["budget"] = pod_budget.status()
        age = pod_demand_age_s()
        body["demand_age_s"] = round(age, 1) if age is not None else None
        return JSONResponse(body)

    # ── Superadmin cross-org fleet view (plan §2.5) ─────────────────────────
    # Platform super-admin ONLY (ui_auth.is_admin — the app_metadata flag, never an
    # org admin): the gateway is the one process that sees every org's brain,
    # sleep state and pod, and holds a service-role client that is not pinned to
    # an org. Content-free by construction — counts, states, costs, timestamps;
    # no persona rows — so it never consults the read policy. See
    # brain/gateway/fleet_orgs.py for the row shape and the caches.
    def _superadmin_or_error(request: Request) -> JSONResponse | None:
        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not ui_auth.is_admin(user):
            return JSONResponse({"error": "platform admin required"}, status_code=403)
        return None

    @app.get("/__fleet/orgs")
    async def fleet_orgs(request: Request):
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain.gateway import fleet_orgs as _fo

        try:
            return JSONResponse(await _fo.build_orgs_view(provisioner, sleep_status))
        except Exception as e:
            logger.warning("[gateway] fleet orgs view failed: %s", e)
            return JSONResponse({"orgs": [], "error": "fleet view unavailable"}, status_code=503)

    @app.get("/__fleet/deploy")
    async def fleet_deploy(request: Request):
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain.gateway import fleet_orgs as _fo

        return JSONResponse(_fo.deploy_view(provisioner))

    # Persona-index rebuild (migration 039) on demand, from the console. The index
    # is filled by every write but an org that predates the migration, or whose
    # volume was restored, holds stale or missing rows until someone runs the
    # owner route POST /v1/personas/reindex — which needs that org's owner key.
    # These two routes let the platform admin do it without one: the gateway asks
    # the org's live tenant over its internal token (brain/gateway/fleet_orgs.py).
    #   POST /__fleet/orgs/{org_id}/reindex?spawn=1   one org; spawn=0 refuses to
    #                                                 boot a dormant org (409)
    #   POST /__fleet/reindex_all?spawn=0             every org; dormant ones are
    #                                                 skipped unless spawn=1
    def _spawn_flag(request: Request, default: bool) -> bool:
        raw = str(request.query_params.get("spawn", "")).strip().lower()
        if not raw:
            return default
        return raw not in ("0", "false", "no", "off")

    @app.post("/__fleet/orgs/{org_id}/reindex")
    async def fleet_reindex_org(org_id: str, request: Request):
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain.gateway import fleet_orgs as _fo

        org_id = org_id.strip()
        if not org_id or "::" in org_id:
            return JSONResponse({"error": "bad org id"}, status_code=400)
        row = await _fo.reindex_org(provisioner, org_id, spawn=_spawn_flag(request, True))
        status = 200 if row["state"] == "reindexed" else (409 if row["state"] == "skipped" else 502)
        logger.info(
            "[gateway] fleet reindex %s → %s (indexed=%s learned=%s spawned=%s%s)",
            org_id[:8],
            row["state"],
            row.get("indexed"),
            row.get("learned"),
            row.get("spawned"),
            f" error={row['error']}" if row.get("error") else "",
        )
        return JSONResponse(row, status_code=status)

    @app.post("/__fleet/reindex_all")
    async def fleet_reindex_all(request: Request):
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain.gateway import fleet_orgs as _fo

        out = await _fo.reindex_all(provisioner, spawn=_spawn_flag(request, False))
        logger.info(
            "[gateway] fleet reindex_all: %d reindexed, %d skipped, %d errors in %.1fs",
            out["reindexed"],
            out["skipped"],
            out["errors"],
            out["elapsed_s"],
        )
        return JSONResponse(out)

    @app.post("/__nudge")
    async def nudge_route(request: Request):
        """Tenant → gateway edge: a placement row changed, a consumer wants its
        pod, output arrived, pressure changed. Loopback only, token-gated
        (BRAIN_GATEWAY_NUDGE_TOKEN, minted per boot). Wakes the reconciler;
        never blocks on it."""
        if not _is_loopback(request):
            return JSONResponse({"error": "loopback only"}, status_code=403)
        token = request.headers.get("x-brain-nudge-token", "")
        if not token or not secrets.compare_digest(token, NUDGE_TOKEN):
            return JSONResponse({"error": "bad token"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = str((body or {}).get("reason") or "")[:32]
        if reason not in _NUDGE_REASONS:
            return JSONResponse({"error": "unknown reason"}, status_code=400)
        key = str((body or {}).get("key") or "")[:64]
        woke = wake_reconciler(f"{reason}:{key}" if key else reason)
        if reason == "demand":
            _kick_pod()  # the old wake path too: no reconciler tick is needed to warm pod 0
        return JSONResponse({"ok": True, "woke": woke})

    @app.get("/__fleet/placement")
    async def fleet_placement(request: Request):
        """Superadmin: the placement controller's view — dedicated pods (kind,
        state, host, consumers, fallback reason), refused spawns, orgs whose GPU
        budget is spent today. Content-free."""
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain.gateway import placement_control as _pc

        pstate = placement_holder[0]
        body = _pc.summary(pstate) if pstate is not None else {"enabled": _pc.enabled(), "pods": []}
        body["instances"] = sorted(k for k in provisioner.keys_for_all() if "::" in k)
        rec = reconciler_holder[0]
        body["reconciler"] = rec.status() if rec is not None else None
        return JSONResponse(body)

    # ── Platform GPU budget (pod_daily_usd_budget at runtime) ───────────────
    # The pool's daily dollar ceiling is enforced HERE, for every org at once, and
    # this process runs with no BRAIN_SETTINGS_PATH — so `brain.settings` hands it
    # the repo-bundled default and no tenant's settings UI can reach it. These two
    # routes read/write the runtime store beside the ledger on the volume
    # (pod_budget.runtime_budget_path(); precedence runtime file > bundled
    # settings). The reconciler re-reads it on its next tick, so an edit is live
    # without a redeploy. Superadmin only: it is a platform-wide spend control.
    _UNCAPPED_WARNING = (
        "0 = UNCAPPED: the pool will hold pods for as long as anything produces "
        "output, with no daily dollar ceiling. The reconciler logs a warning on "
        "every wake while this stands."
    )

    @app.get("/__fleet/pod_budget")
    async def fleet_pod_budget(request: Request):
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain import pod_budget

        view = pod_budget.platform_budget_view()
        if view["usd_budget"] == 0:
            view["warning"] = _UNCAPPED_WARNING
        return JSONResponse(view)

    @app.put("/__fleet/pod_budget")
    async def fleet_pod_budget_put(request: Request):
        err = _superadmin_or_error(request)
        if err is not None:
            return err
        from brain import pod_budget

        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict) or "usd" not in body:
            return JSONResponse(
                {"error": 'body must be a JSON object with "usd" (number ≥ 0; 0 = uncapped)'},
                status_code=400,
            )
        usd, problem = pod_budget.validate_budget_usd(body.get("usd"))
        if problem is not None:
            return JSONResponse({"error": problem}, status_code=400)
        user = request.state.user
        actor = {
            "user": str(user.get("sub") or ""),
            "email": str(user.get("email") or ""),
            "source": "gateway",
        }
        try:
            pod_budget.set_runtime_budget_usd(usd, actor)
        except Exception as e:
            logger.warning("[gateway] pod budget write failed: %s", e)
            return JSONResponse({"error": "could not write the budget file"}, status_code=503)
        view = pod_budget.platform_budget_view()
        if usd == 0:
            view["warning"] = _UNCAPPED_WARNING
        return JSONResponse(view)

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
        await consolidate_and_stop_instance(provisioner, org, persona)

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
            # The org's own pods (standalone / org placements) sleep with it; the
            # controller would pause them after the grace period anyway, but a
            # deliberate Sleep should not bill another ten minutes of GPU.
            pstate = placement_holder[0]
            if pstate is not None:
                from brain.gateway import placement_control as _pc

                with contextlib.suppress(Exception):
                    n = await _pc.pause_org(pstate, tenant)
                    if n:
                        logger.info("[gateway] sleep sweep: paused %d dedicated pod(s)", n)
            wake_reconciler(f"sleep:{tenant[:8]}")

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

    # ── Engine API cost control: sleep + status (OWNER-key authed) ───────────
    # Turn off the cost-generating parts (brain process + GPU pod) for an org, and
    # inspect cost state, without the UI. Registered BEFORE the /v1 catch-all proxy
    # so they're handled at the gateway (which owns the pod), not forwarded to the
    # brain (a pod consumer that can't pause it). Waking is implicit — any other /v1
    # call respawns the brain + kicks the pod on demand.
    #
    # Owner-gated, not partner-gated: _do_sleep sweeps EVERY instance of the org and
    # pauses the shared pod, so a partner key here could kill its siblings' in-flight
    # sessions and force them into a cold start. Cost control is an owner concern.
    def _key_ctx(header: str | None) -> tuple[dict | None, JSONResponse | None]:
        """(context, error-response). Resolves the bearer key across orgs.

        A token that recently resolved to nothing is refused from the negative cache
        without a database round trip — the cross-org lookup is uncached and cannot
        be scoped to an org (finding the org is what it does), so repeat bad keys
        were the cheapest way to generate database load on the whole platform."""
        from brain.api import auth as _api_auth

        tok = _rl.token_key(header)
        if _rl.limiter.is_known_miss(tok):
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            ctx = _api_auth.resolve_key_context(header)
        except _api_auth.AuthBackendError:
            # Do NOT cache: the key may well be valid and the store merely down.
            return None, JSONResponse({"error": "auth backend unavailable"}, status_code=503)
        if ctx is None:
            _rl.limiter.note_miss(tok)
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        return ctx, None

    def _routable_persona(org: str, persona: str | None) -> str | None:
        """The persona a request may be pinned to: the header's persona only when
        the org already runs a dedicated instance for it (provisioner.
        promoted_personas — the same set the org's placement file publishes).
        Anything else routes to the shared instance, so a header can never
        start a process: spawning is the placement's job, not the caller's."""
        if persona is None:
            return None
        fn = getattr(provisioner, "promoted_personas", None)
        try:
            promoted = set(fn(org)) if callable(fn) else set()
        except Exception:
            promoted = set()
        if persona in promoted:
            return persona
        logger.debug(
            "[gateway] X-Brain-Persona %r is not a promoted persona of %s — shared route",
            persona,
            org[:8],
        )
        return None

    @app.post("/v1/sleep")
    async def engine_api_sleep(request: Request):
        ctx, err = _key_ctx(request.headers.get("authorization"))
        if err is not None:
            return err
        if ctx["role"] != "owner":
            return JSONResponse({"error": "owner credential required"}, status_code=403)
        asyncio.create_task(_do_sleep(ctx["org_id"]))
        return JSONResponse({"ok": True, "state": "sleeping"})

    @app.get("/v1/status")
    async def engine_api_status(request: Request):
        ctx, err = _key_ctx(request.headers.get("authorization"))
        if err is not None:
            return err
        org = ctx["org_id"]
        st = provisioner.status(org)
        awake = bool(st and not st["booting"])
        runpod = runpod_holder[0] if runpod_holder else None
        sl = sleep_status.get(org)
        body = {
            # Is this org's brain process running (the per-request compute)?
            "brain": "awake" if awake else ("booting" if st else "asleep"),
            # Last sleep transition for this org, if any (asleep/consolidating/...).
            "sleep": ({"state": sl["state"], "pod": sl.get("pod", "")} if sl else None),
        }
        # The GPU pod is SHARED across orgs, so its state is not this caller's data.
        # Owners see it because they pay for it; partners don't get it at all — even a
        # coarse ready/not-ready boolean is a cross-tenant side channel.
        if ctx["role"] == "owner":
            # Under the pool (default) this is the pool summary — pod 0's state plus
            # pods[], ready, assignments, max_pods (api_guide §27).
            body["pod"] = runpod.status() if runpod else {"state": "off"}
            # Owners pay for the GPU, so they get the daily ledger alongside its state.
            # Cloud spend has always been visible here; GPU spend was not, which is how
            # six days of idle burn went unnoticed while cloud dollars were watched.
            from brain import pod_budget

            body["pod_budget"] = pod_budget.status()
        return JSONResponse(body)

    @app.get("/v1/whoami")
    async def engine_api_whoami(request: Request):
        """Who does this key belong to. Answered at the gateway from the key row
        plus one cached organizations-row read — during a cold start too — and
        never spawns a brain or touches the pod, so a partner can verify a
        credential (and learn its org id and learning mode) before sending
        traffic. Same shape as the engine twin (brain/api/server.py), which
        serves the path directly for self-hosted deployments."""
        ctx, err = _key_ctx(request.headers.get("authorization"))
        if err is not None:
            return err
        mode, seed = await _org_learning(ctx["org_id"])
        return JSONResponse(
            {
                "org_id": ctx["org_id"],
                "partner_id": ctx.get("partner_id"),
                "role": ctx["role"],
                "key_id": ctx.get("key_id"),
                "allowed_agents": ctx.get("allowed_agents"),
                "learning_mode": mode,
                "instance_seed": seed,
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
        """The engine API's OpenAPI document, built once per process, with owner-gated
        operations stripped so the UNAUTHENTICATED schema does not hand out a map of the
        admin surface (key minting, the GDPR purge, the skill review queue, the DMN
        switch) complete with docstrings describing how each works."""
        if "doc" not in _openapi_cache:
            from fastapi.openapi.utils import get_openapi

            from brain.api.server import build_api_router

            async def _dummy(*a, **k):  # never called — we only read the route table
                return {}

            probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
            probe.include_router(build_api_router(_dummy))
            doc = get_openapi(
                title="Elyceum Engine API",
                version="v1",
                description=(
                    "Bearer-authed engine API. Full developer reference — request/response "
                    "shapes, the SSE and WebSocket transports, error semantics and quotas — "
                    "is the Documentation section of the API workspace."
                ),
                routes=probe.routes,
            )
            _openapi_cache["doc"] = _strip_owner_operations(doc)
        return _openapi_cache["doc"]

    def _strip_owner_operations(doc: dict) -> dict:
        """Remove owner-gated operations from a generated OpenAPI document.

        Filtering the produced `paths` (per operation) rather than the route table is
        deliberate: FastAPI can structure `app.routes` so an included router is a single
        opaque object with no walkable per-method sub-routes (this changed under us and
        silently leaked the whole admin surface), whereas the emitted schema is stable.
        Filtering is per METHOD, not per path — org config is partner-READABLE, so a
        path whose GET is public but PUT is owner-only keeps its GET.

        Owner-ness comes from brain.api.reference so the docs chip and this filter cannot
        disagree; a drift test asserts the registry matches what the handlers enforce."""
        from brain.api.reference import is_owner_route

        _http_methods = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
        paths = doc.get("paths", {})
        for path in list(paths):
            item = paths[path]
            for method in [m for m in list(item) if m.lower() in _http_methods]:
                if is_owner_route(method.upper(), path):
                    del item[method]
            # Drop a path once no HTTP operation survives (leaving only shared keys like
            # "parameters" would publish a hollow, meaningless entry).
            if not any(m.lower() in _http_methods for m in item):
                del paths[path]
        return doc

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
        ctx, err = _key_ctx(request.headers.get("authorization"))
        if err is not None:
            return err
        org = ctx["org_id"]
        # None unless multi-persona is on AND the header names a promoted persona.
        persona = _routable_persona(org, _persona_header(request.headers))
        st = provisioner.status(org, persona)
        if st and not st["booting"] and st.get("api_port"):
            provisioner.touch(org, persona)
            return await _proxy_http_stream(request, st["api_port"])
        # Brain not up yet: spawn it (starts its API server) + warm the pod, and tell
        # the partner to retry. Idempotent — concurrent calls await one spawn.
        if st is None:
            # A spawn refused for capacity. Say so instead of "booting": the two need
            # different client behaviour (back off much harder, and alert someone),
            # and they were previously indistinguishable. Checked SYNCHRONOUSLY
            # first (provisioner.capacity_refusal) so the very first over-cap request
            # already says at_capacity; the async record (_safe_ensure) stays as the
            # backstop for a refusal raised inside the spawn itself.
            refusal = _capacity_refusal(org)
            if refusal is None:
                _sync_check = getattr(provisioner, "capacity_refusal", None)
                if callable(_sync_check):
                    refusal = _sync_check(org, persona)
                    if refusal is not None:
                        capacity_refusals[org] = (time.time() + _CAPACITY_TTL_S, refusal)
            if refusal is not None:
                return JSONResponse(
                    {"status": "at_capacity", "detail": refusal},
                    status_code=503,
                    headers={"Retry-After": "30"},
                )
            # Same gate as the UI catch-all: tenants are BYO-key, and a brain spawned
            # for a keyless org only fails on its first cloud call (plan §0.4 #4).
            if not await _org_has_anthropic(org):
                return JSONResponse(
                    {
                        "error": "no_anthropic_key",
                        "detail": "this org has no Anthropic key on file; the owner adds "
                        "one in the console before the brain can be started",
                    },
                    status_code=403,
                )
            sleep_status.pop(org, None)
            asyncio.create_task(_safe_ensure(provisioner, org, persona))
            _kick_pod()
        # Booting: a spawn takes seconds (up to ~1 min on a cold pod), so a short,
        # explicit retry hint beats every client guessing its own backoff.
        return JSONResponse({"status": "booting"}, status_code=503, headers={"Retry-After": "2"})

    @app.websocket("/v1/sessions/{session_id}/stream")
    async def engine_api_ws(client_ws: WebSocket, session_id: str):
        from brain.api import auth as _api_auth

        # Rate-limited HERE, not in middleware: Starlette runs no http middleware for
        # WebSocket scopes, so the _v1_guard above never sees this route. Checked
        # before the key lookup so a connection flood cannot drive database load.
        # check() returns None when allowed, else the Retry-After seconds — compare
        # against None: a 0.0 retry-after is still a refusal, not a pass.
        if (
            _rl.limiter.check("ws", _rl.token_key(client_ws.headers.get("authorization")))
            is not None
        ):
            await client_ws.close(code=1013)  # try again later
            return
        try:
            ctx = _api_auth.resolve_key_context(client_ws.headers.get("authorization"))
        except _api_auth.AuthBackendError:
            # Not 1008 (policy violation) — we never established whether the caller is
            # authorised, so this is an internal error the client should retry.
            await client_ws.close(code=1011)
            return
        if ctx is None:
            await client_ws.close(code=1008)
            return
        org = ctx["org_id"]
        # None unless multi-persona is on AND the header names a promoted persona.
        persona = _routable_persona(org, _persona_header(client_ws.headers))
        st = provisioner.status(org, persona)
        if not st or st["booting"] or not st.get("api_port"):
            if st is None:
                if not await _org_has_anthropic(org):
                    await client_ws.close(code=1008, reason="no_anthropic_key")
                    return
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
            # Touch the instance that is actually serving the stream. touch(org)
            # alone kept the DEFAULT instance alive while the idle reaper took the
            # dedicated persona instance out from under a long-running stream.
            on_activity=lambda: provisioner.touch(org, persona),
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
    # BRAIN_OLLAMA_BIN pins an explicit binary; otherwise PATH, then the installer's
    # default locations (its `>>> Installing ollama to /usr` lands in /usr/bin).
    # Name the probed paths in the skip line: the installer can fail silently in
    # the build (it needs zstd, and nixpacks tolerates the failure), and "no
    # binary" used to be the only clue that every tenant was embedding on Google.
    candidates = [
        os.environ.get("BRAIN_OLLAMA_BIN", "").strip(),
        shutil.which("ollama") or "",
        "/usr/bin/ollama",
        "/usr/local/bin/ollama",
    ]
    binary = next((c for c in candidates if c and os.access(c, os.X_OK)), None)
    if binary is None:
        logger.warning(
            "[gateway] embed sidecar skipped — no ollama binary in image (probed %s); "
            "tenants will embed on the pod when it is up, else on Google",
            ", ".join(c for c in candidates if c) or "PATH",
        )
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


# org -> (expiry, message) for a recent CapacityError. Short-lived: capacity frees
# up as other tenants idle out, so a stale entry must not pin a caller at 503.
_CAPACITY_TTL_S = 60.0
capacity_refusals: dict[str, tuple[float, str]] = {}


async def _org_has_anthropic(org: str) -> bool:
    """The /v1 lane's pre-spawn key gate — the same service-role vault read the
    provisioner injects from (and the UI catch-all's _has_anthropic checks), keyed
    by the org id a partner key resolved to. Fail closed on any error."""
    from brain import vault

    try:
        keys = await asyncio.to_thread(vault.fetch_user_keys, org)
        return bool((keys or {}).get("anthropic"))
    except Exception as e:
        logger.error("[gateway] anthropic-key check failed for %s: %s", str(org)[:8], e)
        return False


async def _safe_ensure(provisioner: Provisioner, uid: str, persona: str | None = None) -> None:
    try:
        await provisioner.ensure(uid, persona)
    except Exception as e:
        from brain.provisioner import CapacityError

        if isinstance(e, CapacityError):
            # Deliberate refusal, not a fault: the host is at its configured brain
            # budget. This runs in a fire-and-forget task, so raising here reaches
            # nobody — record it where the request path can see it, or the caller
            # retries "booting" forever against a host that will never boot them.
            logger.warning("[gateway] AT CAPACITY — %s", e)
            capacity_refusals[uid] = (time.time() + _CAPACITY_TTL_S, str(e))
        else:
            logger.error("[gateway] ensure failed for %s/%s: %s", uid[:8], persona or "-", e)


def _capacity_refusal(org: str) -> str | None:
    """A live capacity refusal for this org, or None."""
    entry = capacity_refusals.get(org)
    if entry is None:
        return None
    if time.time() >= entry[0]:
        capacity_refusals.pop(org, None)
        return None
    return entry[1]


async def _safe_pod_ensure(runpod) -> None:
    # The eager warm is a SECOND route to ensure_running(), independent of the
    # reconciler — so it needs the same ceiling, or the ceiling isn't one. It fires on
    # login/spawn whenever BRAIN_TIER=full, which production sets, so without this check
    # every login would wake the pod no matter how much GPU time the day had already
    # spent. The reconciler would sleep it again a tick later, and under a network
    # volume a wake is a pod CREATE and a sleep is a TERMINATE — so the cost of getting
    # this wrong is churn, not just a little overshoot.
    try:
        from brain import pod_budget

        if pod_budget.exhausted():
            st = pod_budget.status()
            logger.info(
                "[gateway] skipping eager pod warm — GPU budget spent ($%.2f/$%.2f today)",
                st["usd_today"],
                st["usd_budget"],
            )
            return
        # Same for the churn guard: an unproductive session arms a cooldown in the
        # reconciler, and a login or a /__brain_status poll must not re-wake the
        # pod the reconciler just put down (create → terminate → create again).
        cooldown = pod_budget.cooldown_remaining_s()
        if cooldown > 0:
            logger.debug(
                "[gateway] skipping eager pod warm — unproductive-session cooldown, %.0fs left",
                cooldown,
            )
            return
        await runpod.ensure_running()
    except Exception as e:
        logger.warning("[gateway] pod ensure failed: %s", e)


class _BodyTooLarge(Exception):
    """The client sent more bytes than the cap allows."""


async def _read_bounded_body(request: Request) -> bytes:
    """Buffer the request body, aborting past BRAIN_MAX_BODY_BYTES.

    The proxy has to materialise the body to forward it, which means an unbounded
    body is an unbounded allocation in the gateway — the one process every tenant
    depends on. Content-Length is checked earlier in middleware, but it is a claim:
    a chunked request can omit it or lie, so the real bound has to be here, on the
    bytes as they arrive."""
    cap = int(os.environ.get("BRAIN_MAX_BODY_BYTES", str(10 * 1024 * 1024)))
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _BodyTooLarge(cap)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(e: _BodyTooLarge) -> JSONResponse:
    return JSONResponse({"detail": f"request body exceeds {e.args[0]} bytes"}, status_code=413)


async def _proxy_http(request: Request, port: int) -> Response:
    url = f"http://127.0.0.1:{port}{request.url.path}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    try:
        body = await _read_bounded_body(request)
    except _BodyTooLarge as e:
        return _too_large(e)
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
    try:
        body = await _read_bounded_body(request)
    except _BodyTooLarge as e:
        return _too_large(e)
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
            # Bounded, but generously: live voice rides this path as base64 PCM16
            # inside JSON, so a cap set too low breaks audio in a way that looks
            # like a network fault. max_size=None let a peer set the gateway's
            # memory ceiling, which is the thing being fixed.
            max_size=_MAX_WS_FRAME_BYTES,
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
    # This process holds the master secrets (SUPABASE_SERVICE_KEY / JWT_SECRET, RunPod).
    # Install the redacting backstop on the root handler so nothing leaks them to logs.
    from brain.security import install_secret_redaction

    install_secret_redaction()

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
    # Placement desired-state loop (brain/gateway/placement_control): runs inside
    # the reconciler tick, before the pool step, so the pool never assigns a
    # consumer the controller just moved onto its own card.
    from brain.gateway import placement_control as _placement

    placement_state = _placement.PlacementState()
    placement_holder[0] = placement_state

    async def _placement_tick(pool, reasons: list[str] | None = None) -> None:
        try:
            if any(
                r.startswith(("placement", "budget", "sleep", "child_exit", "startup"))
                for r in (reasons or [])
            ):
                _placement.invalidate(placement_state)
            rep = await _placement.placement_tick(
                provisioner,
                placement_state,
                pool=pool,
                stop_instance=lambda org, persona: consolidate_and_stop_instance(
                    provisioner, org, persona
                ),
                grace_s=pod_idle_grace_s,
            )
            if rep.get("actions"):
                logger.info(
                    "[gateway] placement tick: desired=%s pods=%s serving=%s fallback=%s actions=%s",
                    rep.get("desired"),
                    rep.get("pods"),
                    rep.get("serving"),
                    rep.get("fallback", 0),
                    ",".join(rep["actions"]),
                )
        except Exception as e:
            logger.warning("[gateway] placement tick error: %s", e)

    # Retries webhook deliveries the tenant brains enqueue. Lives here, not in the
    # brain, because a brain sleeps and cannot own a multi-hour backoff schedule.
    webhook_task: list = [None]

    # How long the live-brain count must stay at zero before the pod is paused.
    # Small grace absorbs a user logging out and back in without a resume cycle.
    pod_idle_grace_s = float(os.environ.get("BRAIN_POD_IDLE_GRACE_S", "600"))
    # Ticks are event-driven (brain/gateway/reconciler): tenant nudges, child
    # exits, the sleep sweep and each tick's own deadlines wake the loop;
    # BRAIN_RECONCILE_RESYNC_S is the safety net. BRAIN_POD_RECONCILE_S is now an
    # opt-in fixed period on top (0 = off).
    from brain.gateway.reconciler import Reconciler, earliest

    # Tenant spawns inherit the nudge URL/token via os.environ.copy(): the gateway
    # listens on $PORT; children reach it over loopback.
    os.environ["BRAIN_GATEWAY_NUDGE_URL"] = (
        f"http://127.0.0.1:{int(os.environ.get('PORT', '8765'))}/__nudge"
    )
    provisioner.on_child_exit = lambda key: wake_reconciler(f"child_exit:{key[:16]}")

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

    async def _pool_reconciler(pool):
        """The pool version of the loop below (plan §10, PR 3). One tick =
        brain.gateway.pod_reconcile.reconcile_tick: failover, bill per held pod,
        assign, fold pressure, should_hold_pod for pod 0, decide_scale above it,
        publish. RUNPOD_HOST (inherited by NEW spawns) tracks pod 0's stable host;
        the files consumers poll are written by pool.publish() inside the tick."""
        from brain.gateway.pod_reconcile import ReconcileState, next_deadline, reconcile_tick
        from brain.provisioner import write_placement_files

        state = ReconcileState()

        async def _tick(reasons: list[str]) -> None:
            _log_tenant_stats()
            await _placement_tick(pool, reasons)
            write_placement_files(provisioner)
            report = await reconcile_tick(pool, provisioner, state)
            host = pool.published_host()
            if host and "localhost" not in host and os.environ.get("RUNPOD_HOST") != host:
                os.environ["RUNPOD_HOST"] = host
                logger.info("[gateway] RUNPOD_HOST synced → %s", host)
            if report.get("actions"):
                logger.info(
                    "[gateway] pool tick (%s): held=%s ready=%s consumers=%s decision=%s actions=%s",
                    ",".join(reasons),
                    report.get("held"),
                    report.get("ready"),
                    report.get("consumers"),
                    report.get("decision"),
                    ",".join(report["actions"]),
                )

        def _deadline(now: float) -> float | None:
            return earliest(
                next_deadline(pool, state, now),
                _placement.next_deadline(placement_state, now, pod_idle_grace_s),
            )

        rec = Reconciler(_tick, deadline_fn=_deadline, name="pool")
        reconciler_holder[0] = rec
        await rec.run()

    async def _pod_reconciler(runpod):
        """LEGACY single-pod loop, used only when BRAIN_POD_POOL is off (kill switch)."""
        _lst: dict = {"idle_since": None, "pod_up_since": None, "last_tick": time.time()}

        async def _tick(reasons: list[str]) -> None:
            idle_since = _lst["idle_since"]
            pod_up_since = _lst["pod_up_since"]
            last_tick = _lst["last_tick"]
            _log_tenant_stats()
            # Publish per-org placement (which personas run dedicated) so each
            # org's SHARED instance drops them from its DMN roster. Derived
            # from live procs → self-heals when a dedicated instance dies.
            from brain import pod_budget
            from brain.provisioner import pod_demand_age_s, pod_use_age_s, write_placement_files

            await _placement_tick(None, reasons)  # processes only: no pool, no dedicated pods
            write_placement_files(provisioner)

            # Bill first, decide second: charge today's ledger for the wall-clock
            # the pod was actually up over the interval we just slept. RunPod bills
            # uptime, so the ledger must measure uptime — not the inference we
            # managed to get out of it.
            now = time.time()
            elapsed, last_tick = now - last_tick, now
            if runpod._pod_id:
                # Convert the dollar ceiling at what THIS pod costs, not a guess:
                # the manager takes the best GPU under its price ceiling, so the
                # rate differs between pods and a stale rate would mis-size the
                # allowance in whichever direction happened to be wrong.
                pod_budget.set_rate_per_hr(getattr(runpod, "_cost_per_hr", None))
                pod_budget.record_uptime(elapsed)

            # Gate on FULL-tier brains, not all live brains: a lite brain remaps
            # every local/runpod route to cloud and never uses the pod, so spinning
            # a GPU for a lite-only host is pure waste. full_count() reads each
            # brain's tier (reported on /health, captured at boot).
            full = provisioner.full_count()
            # ...but a live full-tier brain is NOT the same question as "does
            # anything need a GPU". A keepalive cron guarantees a live brain, so
            # gating on liveness alone pinned the pod up permanently and made the
            # pause() branch below unreachable. Demand is the honest signal: a
            # runpod-routed cell touches POD_DEMAND_FILE when it actually wants the
            # pod, including when it finds the pod off (that IS the wake request).
            demand_age = pod_demand_age_s()
            # Asking is what wakes the pod; PRODUCING is what keeps it. The DMN asks
            # on every idle tick regardless of what it gets back, so holding on
            # demand would keep a useless pod up all day.
            use_age = pod_use_age_s()
            pod_is_up = bool(runpod._pod_id)
            if pod_is_up and pod_up_since is None:
                pod_up_since = now
            if not pod_is_up:
                pod_up_since = None
            # The ceiling. Without it, gating alone would still run the pod ~24/7,
            # because the DMN wants to think whenever the user is idle.
            over_budget = pod_budget.exhausted()
            hold = pod_budget.should_hold_pod(
                full_tier_brains=full,
                demand_age_s=demand_age,
                grace_s=pod_idle_grace_s,
                over_budget=over_budget,
                pod_is_up=pod_is_up,
                use_age_s=use_age,
                up_for_s=(now - pod_up_since) if pod_up_since else None,
                cooldown_active=pod_budget.cooldown_remaining_s() > 0,
            )

            if hold:
                idle_since = None
                if pod_budget.budget_seconds() == 0 and not runpod._pod_id:
                    logger.warning(
                        "[gateway] waking shared pod with pod_daily_usd_budget=0 "
                        "(UNCAPPED GPU spend — set a ceiling on the Fleet page or "
                        "PUT /__fleet/pod_budget)"
                    )
                await runpod.ensure_running()
                _sync_runpod_host(runpod)
            else:
                if idle_since is None:
                    idle_since = time.time()
                # Budget exhaustion sleeps the pod immediately — the grace period is
                # there to damp demand flapping, and waiting it out would just bill
                # another 10 minutes past a ceiling we already know is breached.
                due = over_budget or time.time() - idle_since >= pod_idle_grace_s
                if due and runpod._pod_id:
                    if over_budget:
                        st = pod_budget.status()
                        logger.warning(
                            "[gateway] GPU budget spent ($%.2f/$%.2f today, %.0f min "
                            "at $%.2f/hr) — sleeping shared pod until UTC rollover",
                            st["usd_today"],
                            st["usd_budget"],
                            st["minutes_used"],
                            st["rate_per_hr"],
                        )
                    else:
                        logger.info(
                            "[gateway] pod idle — no output for %s (demand %s, "
                            "full-tier brains=%d) — sleeping shared pod",
                            f"{use_age:.0f}s" if use_age is not None else "ever",
                            f"{demand_age:.0f}s ago" if demand_age is not None else "none",
                            full,
                        )
                    # Arm the churn guard from whether this session actually produced
                    # anything. Without it: wake → nothing → sleep → demand is still
                    # fresh → wake again, and under a network volume every cycle is a
                    # create+terminate.
                    produced = use_age is not None and use_age <= pod_idle_grace_s
                    pod_budget.record_sleep(produced)
                    if not produced:
                        logger.warning(
                            "[gateway] pod produced nothing this session — "
                            "backing off %.0f min before honouring the next wake",
                            pod_budget.cooldown_remaining_s() / 60.0,
                        )
                    await runpod.pause()
            st["idle_since"], st["pod_up_since"], st["last_tick"] = (
                idle_since,
                pod_up_since,
                last_tick,
            )

        def _deadline(now: float) -> float | None:
            from brain import pod_budget
            from brain.gateway.reconciler import next_utc_midnight

            dls = []
            if _lst["idle_since"] is not None and runpod._pod_id:
                dls.append(_lst["idle_since"] + pod_idle_grace_s)
            if runpod._pod_id and _lst["pod_up_since"]:
                dls.append(_lst["pod_up_since"] + pod_idle_grace_s)
            if pod_budget.exhausted():
                dls.append(next_utc_midnight(now))
            cd = pod_budget.cooldown_remaining_s()
            if cd > 0 and not runpod._pod_id:
                dls.append(now + cd)
            return earliest(*dls, _placement.next_deadline(placement_state, now, pod_idle_grace_s))

        rec = Reconciler(_tick, deadline_fn=_deadline, name="pod")
        reconciler_holder[0] = rec
        await rec.run()

    @app.on_event("startup")
    async def _startup():
        _start_loop_watchdog()  # self-heal: force-restart if the event loop ever wedges
        # Webhook delivery retry loop (migration 032) — cross-org, service-role.
        with contextlib.suppress(Exception):
            from brain.gateway.webhook_delivery import sweeper_loop

            webhook_task[0] = asyncio.create_task(sweeper_loop())
        # Before the provisioner: the sidecar sets OLLAMA_EMBED_HOST, which tenant
        # spawns inherit via os.environ.copy().
        embed_sidecar_holder[0] = _start_embed_sidecar()
        await provisioner.start()
        logger.info("[gateway] provisioner started")
        try:
            if pod_pool_enabled():
                from brain.runpod_pool import RunPodPool

                runpod = RunPodPool()
                # Adopt every pool pod by name and rebuild assignments from the pool
                # file; pod 0's stable host is published exactly as the single
                # manager's was (settings + RUNPOD_HOST + host file).
                host = await runpod.discover()
            else:
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
            if pod_pool_enabled():
                logger.info(
                    "[gateway] pod pool enabled (max_pods=%d, min_pods=%d) — BRAIN_POD_POOL=0 "
                    "restores the single-pod reconciler",
                    runpod.cfg.max_pods,
                    runpod.cfg.min_pods,
                )
                reconciler_task[0] = asyncio.create_task(_pool_reconciler(runpod))
            else:
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
        if webhook_task[0]:
            webhook_task[0].cancel()
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
