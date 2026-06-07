"""
Supabase-backed auth gate for the brain UI.

Invite-only: accounts are provisioned out-of-band (Supabase dashboard, or
`scripts/create_user.py`). There is no public sign-up path. Nothing in the UI
is reachable without a valid session cookie — including the landing page and
any product description.

Mechanism (mirrors the behaviour of the AI GM NextAuth flow, re-implemented for
FastAPI on top of Supabase GoTrue):

  GET  /login        → the ONLY unauthenticated HTML page (a bare sign-in form)
  POST /auth/login   → verify email+password via GoTrue, set httpOnly cookies
  POST /auth/logout  → clear cookies
  everything else    → requires a valid access token cookie; HTML navigations
                       redirect to /login, API/WS calls get 401 / socket close.

Tokens live in httpOnly, SameSite=Lax cookies (never localStorage), so the
FastAPI gate can validate them and JS can't exfiltrate them. Access tokens are
verified locally via the project JWT secret when SUPABASE_JWT_SECRET is set
(fast, offline); otherwise they're validated against GoTrue's /user endpoint.

Fail-closed: if Supabase isn't configured, every gated route is denied rather
than served publicly. Set BRAIN_AUTH_DISABLED=true to bypass the gate for local
development only.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ACCESS_COOKIE = "sb-access"
REFRESH_COOKIE = "sb-refresh"
# Marks whether the user asked to stay signed in ("Keep me present"). Drives
# whether the session cookies are persistent (survive browser close) or expire
# with the browser session. Read back on token refresh so the choice sticks.
REMEMBER_COOKIE = "sb-remember"

# Refresh cookie outlives the access token so a returning user stays signed in.
_REFRESH_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Paths reachable without a session. Keep this list tiny — it is the entire
# public surface of the app. The login page is fully self-contained (inline CSS,
# no external assets) precisely so nothing else needs whitelisting here.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/login",
        "/auth/login",
        "/auth/logout",
        "/auth/forgot",
        "/auth/admission",
        "/auth/reset",
    }
)


def is_disabled() -> bool:
    """Local-dev escape hatch. Default OFF — the gate is on unless asked off."""
    return os.environ.get("BRAIN_AUTH_DISABLED", "").lower() in ("1", "true", "yes")


def is_configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"))


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS


def _secure_cookies() -> bool:
    # Secure cookies aren't sent over plain http (localhost), so only require
    # them when actually hosted behind TLS (Railway terminates TLS for us).
    return bool(os.environ.get("RAILWAY_ENVIRONMENT"))


def _base() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def _anon() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "")


def _jwt_secret() -> str:
    return os.environ.get("SUPABASE_JWT_SECRET", "")


def safe_next(value: str | None) -> str:
    """Only allow same-site relative redirects (defends against open-redirect)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


# ── credential login ────────────────────────────────────────────────────────


async def password_login(email: str, password: str) -> dict[str, Any] | None:
    """Exchange email+password for a Supabase session. None on bad credentials."""
    url = f"{_base()}/auth/v1/token?grant_type=password"
    headers = {"apikey": _anon(), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                url, headers=headers, json={"email": email, "password": password}
            )
    except httpx.HTTPError as e:
        logger.error("[auth] GoTrue login request failed: %s", e)
        return None
    if r.status_code != 200:
        # 400/401 here is the normal "wrong password" path — don't log the body.
        logger.info("[auth] login rejected (status=%s)", r.status_code)
        return None
    return r.json()


async def request_password_reset(email: str, redirect_to: str | None = None) -> None:
    """Ask GoTrue to email a recovery link. Best-effort and intentionally silent:
    we never reveal whether the address exists (defends against enumeration), so
    the caller always reports the same "if it exists, a link is on its way".

    The email itself is sent by Supabase Auth; configure its SMTP to point at
    Resend (thegaim.app's mail service) so it ships from the verified domain.
    ``redirect_to`` is where GoTrue bounces the user after they click the link —
    our in-app /auth/reset page, which finishes the password change."""
    if not email:
        return
    url = f"{_base()}/auth/v1/recover"
    if redirect_to:
        from urllib.parse import quote

        url = f"{url}?redirect_to={quote(redirect_to, safe='')}"
    headers = {"apikey": _anon(), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, headers=headers, json={"email": email})
    except httpx.HTTPError as e:
        logger.error("[auth] password recovery request failed: %s", e)


async def _refresh(refresh_token: str) -> dict[str, Any] | None:
    url = f"{_base()}/auth/v1/token?grant_type=refresh_token"
    headers = {"apikey": _anon(), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers=headers, json={"refresh_token": refresh_token})
    except httpx.HTTPError as e:
        logger.error("[auth] GoTrue refresh request failed: %s", e)
        return None
    if r.status_code != 200:
        return None
    return r.json()


# ── token verification ───────────────────────────────────────────────────────


async def _verify_access(token: str) -> dict[str, Any] | None:
    """Return JWT claims if the access token is valid and unexpired, else None."""
    secret = _jwt_secret()
    if secret:
        import jwt

        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
        except jwt.PyJWTError:
            return None

    # No local secret configured → ask GoTrue. One network hop, but correct.
    url = f"{_base()}/auth/v1/user"
    headers = {"apikey": _anon(), "Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.error("[auth] GoTrue user lookup failed: %s", e)
        return None
    if r.status_code != 200:
        return None
    return r.json()


async def authenticate(conn: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Authenticate a request or websocket from its cookies.

    Works for both Starlette Request and WebSocket (both expose ``.cookies``).
    Returns ``(claims, refreshed_session)``: ``claims`` is None when unauthenticated;
    ``refreshed_session`` is the new GoTrue session dict when the access token was
    expired but the refresh token renewed it (the caller should persist the new
    cookies), else None.
    """
    cookies = getattr(conn, "cookies", {}) or {}
    access = cookies.get(ACCESS_COOKIE)
    refresh = cookies.get(REFRESH_COOKIE)

    if access:
        claims = await _verify_access(access)
        if claims is not None:
            return claims, None

    if refresh:
        session = await _refresh(refresh)
        if session and session.get("access_token"):
            claims = await _verify_access(session["access_token"])
            if claims is None:
                # Trust the fresh token even if we can't re-verify it offline.
                user = session.get("user") or {}
                claims = {"sub": user.get("id"), "email": user.get("email")}
            return claims, session

    return None, None


# ── cookie helpers ────────────────────────────────────────────────────────────


def remembered(conn: Any) -> bool:
    """Did the user opt to stay signed in? Defaults to True for legacy sessions
    that predate the marker cookie. Read on token refresh so the persistence
    choice carries across renewals."""
    cookies = getattr(conn, "cookies", {}) or {}
    return cookies.get(REMEMBER_COOKIE, "1") != "0"


def set_session_cookies(response: Any, session: dict[str, Any], remember: bool = True) -> None:
    access = session.get("access_token", "")
    refresh = session.get("refresh_token", "")
    expires_in = int(session.get("expires_in", 3600) or 3600)
    secure = _secure_cookies()
    # remember=False → omit max_age entirely so these are session cookies the
    # browser discards on close. The access-token JWT still self-expires either
    # way; this only controls whether the browser keeps it across restarts.
    common = dict(httponly=True, secure=secure, samesite="lax", path="/")
    response.set_cookie(
        ACCESS_COOKIE,
        access,
        max_age=expires_in if remember else None,
        **common,
    )
    if refresh:
        response.set_cookie(
            REFRESH_COOKIE,
            refresh,
            max_age=_REFRESH_MAX_AGE if remember else None,
            **common,
        )
    # Persist the choice itself with the same lifetime as the session it governs.
    response.set_cookie(
        REMEMBER_COOKIE,
        "1" if remember else "0",
        max_age=_REFRESH_MAX_AGE if remember else None,
        **common,
    )


def clear_session_cookies(response: Any) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")
    response.delete_cookie(REMEMBER_COOKIE, path="/")


# ── gate responses ────────────────────────────────────────────────────────────


def _wants_html(request: Any) -> bool:
    accept = request.headers.get("accept", "") if hasattr(request, "headers") else ""
    return "text/html" in accept


def unauthorized_response(request: Any):
    """Redirect browser navigations to the sign-in page; 401 everything else."""
    from fastapi.responses import JSONResponse, RedirectResponse

    if _wants_html(request):
        nxt = safe_next(request.url.path + (("?" + request.url.query) if request.url.query else ""))
        target = "/login" if nxt == "/" else f"/login?next={nxt}"
        return RedirectResponse(target, status_code=303)
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def config_error_response(request: Any):
    """Fail closed: refuse to serve the app when auth isn't configured."""
    from fastapi.responses import JSONResponse, PlainTextResponse

    logger.error(
        "[auth] SUPABASE_URL / SUPABASE_ANON_KEY not set — refusing to serve the "
        "app publicly. Configure Supabase, or set BRAIN_AUTH_DISABLED=true for local dev."
    )
    if _wants_html(request):
        return PlainTextResponse(
            "Authentication is not configured. The app is unavailable.",
            status_code=503,
        )
    return JSONResponse({"error": "Auth not configured"}, status_code=503)
