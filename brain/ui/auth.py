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
verified LOCALLY and offline: asymmetric ES256/RS256 tokens (Supabase's default)
against the project's cached JWKS public key, or legacy HS256 against
SUPABASE_JWT_SECRET. A remote GoTrue /user check is only a last resort for when
local verification is impossible (e.g. JWKS unreachable) — never per request in
the healthy path, so auth latency can't wedge the gateway.

Fail-closed: if Supabase isn't configured, every gated route is denied rather
than served publicly. Set BRAIN_AUTH_DISABLED=true to bypass the gate for local
development only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
    # /v1/* is the engine API: bearer-key authed by the API layer itself, so it must
    # bypass the cookie/session gate (the gateway's /v1 proxy does its own bearer auth).
    if path == "/v1" or path.startswith("/v1/"):
        return True
    return path in PUBLIC_PATHS


def owner_mismatch(claims: dict[str, Any] | None) -> bool:
    """True when this process is pinned to one tenant and the caller isn't entitled.

    Each process serves exactly one ORG (BRAIN_ORG_ID = the org/tenant id). Its UI
    is reachable over a public proxy URL, so a holder of *any* valid session cookie
    could otherwise reach another tenant's process. Entitlement = membership in this
    org. Fast path: a caller whose sub == the org id is the owner (and the only
    member of a personal org), so no DB hit — this is the dev/companion case. Only a
    multi-member org pays a membership lookup. No-op when neither id is set
    (single-user / dev)."""
    org_id = (
        os.environ.get("BRAIN_ORG_ID", "").strip() or os.environ.get("BRAIN_USER_ID", "").strip()
    )
    if not org_id:
        return False
    sub = str((claims or {}).get("sub", "")).strip()
    if not sub:
        return True
    if sub == org_id:
        return False  # owner / personal-org member — fast path, no DB
    try:
        from brain.org import is_member

        return not is_member(sub, org_id)
    except Exception:
        return True  # fail closed


def is_admin(claims: dict[str, Any] | None) -> bool:
    """True if the authenticated user is an app admin.

    Read from Supabase ``app_metadata.is_admin`` — app_metadata is admin-/
    service-role-controlled, so a user cannot self-grant it (unlike the
    user-editable user_metadata). Admins see the full settings surface,
    including the operational/system pages; everyone else gets the curated view.
    Set it with the service role, e.g.:
      update auth.users
        set raw_app_meta_data = raw_app_meta_data || '{"is_admin": true}'
        where email = '<you>';
    The flag lands in the JWT on the user's next login/token refresh.
    """
    app_meta = (claims or {}).get("app_metadata")
    if isinstance(app_meta, dict) and bool(app_meta.get("is_admin")):
        return True
    # Optional env allowlist as a fallback / break-glass (comma-separated emails).
    allow = os.environ.get("BRAIN_ADMIN_EMAILS", "")
    if allow.strip():
        email = str((claims or {}).get("email", "")).strip().lower()
        allowed = {e.strip().lower() for e in allow.split(",") if e.strip()}
        if email and email in allowed:
            return True
    return False


def is_org_admin(claims: dict[str, Any] | None) -> bool:
    """True if the caller may administer THIS process's org — manage its agents,
    roles, connectors, and partner keys (the per-agent narrowing that lives within
    the account ceilings). This is the tenant-scoped admin, distinct from is_admin
    (the platform super-user, who sets the ceilings and gets the cross-org god
    view): an org-admin manages only their own org.

    Resolution, cheapest first:
      • a platform admin is implicitly an org-admin (the super-user can operate any
        org its process is pinned to);
      • the owner of a personal org (sub == org_id) is its admin — the common case,
        no DB hit;
      • a multi-member org pays one membership lookup for the 'admin' role.
    With no org pin (local/dev single-user), returns True to match the unpinned
    posture of owner_mismatch. Fail-closed on a lookup error (denies)."""
    if is_admin(claims):
        return True
    org_id = (
        os.environ.get("BRAIN_ORG_ID", "").strip() or os.environ.get("BRAIN_USER_ID", "").strip()
    )
    if not org_id:
        return True  # unpinned single-user / dev — nothing to scope to
    sub = str((claims or {}).get("sub", "")).strip()
    if not sub:
        return False
    if sub == org_id:
        return True  # personal-org owner — the only member, and its admin
    try:
        from brain.org import membership_role

        return membership_role(sub, org_id) == "admin"
    except Exception:
        return False  # fail closed


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


# ── shared HTTP client ─────────────────────────────────────────────────────────
# One reused AsyncClient for every Supabase call. A *new* client per request (the
# old behaviour) meant each auth check paid a fresh TLS + pool setup, so when the
# verify path fell back to a remote call on every request it could exhaust
# connections and wedge the whole gateway. Reusing one pooled client + short
# timeouts means a slow auth dependency degrades gracefully instead of hanging.
_http: httpx.AsyncClient | None = None
_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _http


# ── JWKS (asymmetric signing keys) ─────────────────────────────────────────────
# Supabase signs access tokens with asymmetric keys (ES256 by default); the
# public keys are published at the JWKS endpoint. We fetch them ONCE and cache, so
# the common verify path is a local crypto check with NO network call per request.
_jwks_lock: asyncio.Lock | None = None
_jwks_cache: dict[str, Any] = {}  # kid -> jwt.PyJWK
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 600.0  # re-fetch at most every 10 min (covers key rotation)


def _jwks_url() -> str:
    return f"{_base()}/auth/v1/.well-known/jwks.json"


async def _refresh_jwks() -> None:
    """Fetch the project's JWKS and rebuild the {kid: PyJWK} cache (single-flight)."""
    global _jwks_lock, _jwks_fetched_at, _jwks_cache
    if _jwks_lock is None:
        _jwks_lock = asyncio.Lock()
    async with _jwks_lock:
        # A waiter that blocked on the lock may find the cache already refreshed.
        if _jwks_cache and time.monotonic() - _jwks_fetched_at < _JWKS_TTL:
            return
        try:
            r = await _client().get(_jwks_url(), headers={"apikey": _anon()})
            r.raise_for_status()
            keys = r.json().get("keys", [])
        except Exception as e:
            logger.warning("[auth] JWKS fetch failed: %s", e)
            return
        import jwt

        cache: dict[str, Any] = {}
        for jwk in keys:
            kid = jwk.get("kid")
            if not kid:
                continue
            try:
                cache[kid] = jwt.PyJWK(jwk)
            except Exception as e:
                logger.warning("[auth] skipping unusable JWK %s: %s", kid, e)
        if cache:
            _jwks_cache = cache
            _jwks_fetched_at = time.monotonic()


async def _jwks_key_for(kid: str | None):
    """Return the cached PyJWK for this key id, refreshing the JWKS if needed."""
    if not kid:
        return None
    if kid in _jwks_cache and time.monotonic() - _jwks_fetched_at < _JWKS_TTL:
        return _jwks_cache[kid]
    await _refresh_jwks()
    return _jwks_cache.get(kid)


def safe_next(value: str | None) -> str:
    """Only allow same-site relative redirects (defends against open-redirect)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def external_base_url(conn: Any) -> str:
    """The ``scheme://host`` this request reached us on from the public internet.

    NOT the same as ``request.base_url``. Railway terminates TLS at the edge and
    forwards to us over plain http; uvicorn only rewrites the scheme from
    x-forwarded-proto when the peer is in ``forwarded_allow_ips`` (default
    127.0.0.1), and the edge never is. So ``request.url.scheme`` — and therefore
    ``request.base_url`` — is "http" in production. Any link handed to the OUTSIDE
    world has to read the forwarded headers itself, exactly as the HTTPS/HSTS
    middleware already does.

    This matters because the failure is silent: GoTrue matches ``redirect_to``
    against the project's redirect allowlist including scheme, and an unlisted URL
    isn't an error — it quietly falls back to SITE_URL. An http:// reset link
    therefore dumped the user on the app root (then /login) with the recovery
    token stranded in the fragment, and the password was never changed.

    Set BRAIN_PUBLIC_URL to pin the canonical origin (e.g. https://elyceum.app)
    when the app answers on more than one domain and only one is allowlisted.
    """
    override = os.environ.get("BRAIN_PUBLIC_URL", "").strip()
    if override:
        return override.rstrip("/")
    headers = getattr(conn, "headers", {}) or {}
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (headers.get("x-forwarded-host") or "").split(",")[0].strip() or (
        headers.get("host") or ""
    ).strip()
    if not proto:
        proto = conn.url.scheme
    if not host:
        host = conn.url.netloc
    return f"{proto}://{host}"


# ── credential login ────────────────────────────────────────────────────────


async def password_login(email: str, password: str) -> dict[str, Any] | None:
    """Exchange email+password for a Supabase session. None on bad credentials."""
    url = f"{_base()}/auth/v1/token?grant_type=password"
    headers = {"apikey": _anon(), "Content-Type": "application/json"}
    try:
        r = await _client().post(url, headers=headers, json={"email": email, "password": password})
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
        await _client().post(url, headers=headers, json={"email": email})
    except httpx.HTTPError as e:
        logger.error("[auth] password recovery request failed: %s", e)


# Single-flight refresh. A page load fires many parallel requests; when the
# access token has just expired they would each POST grant_type=refresh_token
# with the SAME refresh token. GoTrue rotates refresh tokens and rate-limits,
# so the stampede yields 429s and the losers surface as spurious 401s in the
# UI. One request does the refresh; the rest reuse the result via this cache.
_refresh_lock: asyncio.Lock | None = None
_refresh_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_REFRESH_CACHE_TTL = 60.0  # seconds — well under the access token's lifetime


async def _refresh(refresh_token: str) -> dict[str, Any] | None:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()

    cached = _refresh_cache.get(refresh_token)
    if cached and time.monotonic() - cached[0] < _REFRESH_CACHE_TTL:
        return cached[1]

    async with _refresh_lock:
        cached = _refresh_cache.get(refresh_token)
        if cached and time.monotonic() - cached[0] < _REFRESH_CACHE_TTL:
            return cached[1]
        url = f"{_base()}/auth/v1/token?grant_type=refresh_token"
        headers = {"apikey": _anon(), "Content-Type": "application/json"}
        try:
            r = await _client().post(url, headers=headers, json={"refresh_token": refresh_token})
        except httpx.HTTPError as e:
            logger.error("[auth] GoTrue refresh request failed: %s", e)
            return None
        if r.status_code != 200:
            logger.warning("[auth] GoTrue refresh rejected (%s)", r.status_code)
            return None
        session = r.json()
        # Key the result by the OLD token: concurrent requests still carry it in
        # their cookies. Prune expired entries so the dict can't grow unbounded.
        now = time.monotonic()
        for k in [k for k, (ts, _) in _refresh_cache.items() if now - ts >= _REFRESH_CACHE_TTL]:
            _refresh_cache.pop(k, None)
        _refresh_cache[refresh_token] = (now, session)
        return session


# ── token verification ───────────────────────────────────────────────────────


async def _verify_access(token: str) -> dict[str, Any] | None:
    """Return JWT claims if the access token is valid and unexpired, else None.

    Verified LOCALLY in the common case — no network call per request:
      • asymmetric tokens (ES256/RS256, Supabase's default) against the cached
        JWKS public key;
      • legacy symmetric tokens (HS256) against SUPABASE_JWT_SECRET.
    A remote GoTrue /user check is a LAST resort, used only when local verification
    is impossible (e.g. the JWKS endpoint is unreachable) — never per request in
    the healthy path. A token we can verify locally and find invalid is rejected
    outright (no round-trip), since GoTrue would reject it too.
    """
    import jwt

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    alg = header.get("alg", "")

    if alg == "HS256":
        secret = _jwt_secret()
        if secret:
            try:
                return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
            except jwt.ExpiredSignatureError:
                return None
            except jwt.PyJWTError as e:
                # Secret wrong/rotated — let GoTrue judge rather than churning the
                # refresh path (which would drop app_metadata → admin pages vanish).
                logger.warning("[auth] HS256 verify failed (%s) — checking GoTrue.", e)
    elif alg in ("ES256", "ES384", "ES512", "RS256", "RS384", "RS512"):
        key = await _jwks_key_for(header.get("kid"))
        if key is not None:
            try:
                return jwt.decode(token, key.key, algorithms=[alg], audience="authenticated")
            except jwt.ExpiredSignatureError:
                return None
            except jwt.PyJWTError as e:
                logger.warning("[auth] JWKS verify rejected token (%s)", e)
                return None  # definitive — bad signature/claims; no remote fallback
        # JWKS unavailable for this kid → fall through to the remote check.

    return await _verify_remote(token)


async def _verify_remote(token: str) -> dict[str, Any] | None:
    """Last-resort token validation via GoTrue /user. One pooled network hop."""
    url = f"{_base()}/auth/v1/user"
    headers = {"apikey": _anon(), "Authorization": f"Bearer {token}"}
    try:
        r = await _client().get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.error("[auth] GoTrue user lookup failed: %s", e)
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    # GoTrue's /user returns the user object keyed "id"; the JWT path returns the
    # standard "sub" claim. Normalize so callers can always read claims["sub"].
    if isinstance(data, dict) and "sub" not in data and data.get("id"):
        data["sub"] = data["id"]
    return data


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
                # Carry app_metadata through — is_admin lives there, and dropping
                # it silently demotes admins (system settings pages vanish).
                user = session.get("user") or {}
                claims = {
                    "sub": user.get("id"),
                    "email": user.get("email"),
                    "app_metadata": user.get("app_metadata") or {},
                }
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
    common = {"httponly": True, "secure": secure, "samesite": "lax", "path": "/"}
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
