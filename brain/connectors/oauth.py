"""OAuth 2.1 client for third-party MCP servers (the MCP authorization spec).

What happens on "Connect":

  1. discover(mcp_url)      RFC 9728 protected-resource metadata → the server's
                            authorization server → RFC 8414 / OIDC metadata.
  2. register_client(...)   RFC 7591 dynamic client registration when the
                            authorization server offers it (most MCP servers do);
                            otherwise the org supplies a client id from the
                            provider's developer console.
  3. authorize_url(...)     PKCE (S256) + a random `state`; the browser goes to
                            the provider's consent page.
  4. exchange_code(...)     the callback trades the code for tokens, bound to
                            the MCP server with the RFC 8707 `resource` value.
  5. refresh_tokens(...)    before a token lapses the executor rotates it and
                            writes the new pair back to the registry.

Nothing here touches storage: the caller (brain/ui/server.py for the browser
legs, brain/clusters/cma_executor.py for refresh) persists what comes back.
The in-flight consent (`state` → verifier + endpoints) lives in a process-local
store with a short TTL: the org's console is one process, and the callback
lands on the same one.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
PENDING_TTL_S = 15 * 60
CLIENT_NAME = "Elyceum"


class OAuthError(Exception):
    """A discovery / registration / token step failed; the message is user-facing."""


@dataclass
class ASMeta:
    """Where to send the org, and what the server is called on the wire."""

    resource: str  # canonical MCP server URL (RFC 8707 resource indicator)
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    scopes: list[str] = field(default_factory=list)  # from the protected-resource doc
    token_auth_methods: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "resource": self.resource,
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "scopes": list(self.scopes),
            "token_auth_methods": list(self.token_auth_methods),
        }


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: str | None  # ISO-8601 UTC, None = the server did not say
    scope: str | None


# ── http ─────────────────────────────────────────────────────────────────────
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True, transport=_transport)


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url, headers={"accept": "application/json"})
    except Exception as e:
        logger.debug("[oauth] GET %s failed: %s", url, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── canonical URLs ───────────────────────────────────────────────────────────
def canonical_resource(mcp_url: str) -> str:
    """RFC 8707 resource for an MCP server: lowercase scheme + host, path kept,
    no query/fragment, no trailing slash (unless the path is just '/')."""
    p = urlsplit(mcp_url.strip())
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def _well_known_candidates(base: str, suffix: str) -> list[str]:
    """RFC 8414 §3.1: with a path component the well-known segment is inserted
    between host and path; OIDC also allows appending it. Try both."""
    p = urlsplit(base)
    origin = f"{p.scheme}://{p.netloc}"
    path = (p.path or "").rstrip("/")
    out = [f"{origin}/.well-known/{suffix}{path}"]
    if path:
        out.append(f"{origin}{path}/.well-known/{suffix}")
    return out


def _parse_www_authenticate(header: str) -> str | None:
    """Pull resource_metadata="…" out of a Bearer challenge (RFC 9728 §5.1)."""
    if not header:
        return None
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k.strip().lower().endswith("resource_metadata"):
            return v.strip().strip('"') or None
    return None


# ── discovery ────────────────────────────────────────────────────────────────
async def discover(mcp_url: str) -> ASMeta:
    """Locate the authorization server for an MCP endpoint.

    Order: the server's own 401 challenge (it may point at its metadata),
    then the RFC 9728 well-known documents, then — for servers on the older
    draft that have no protected-resource document — the MCP origin itself
    as the authorization server."""
    resource = canonical_resource(mcp_url)
    async with _client() as client:
        prm: dict | None = None
        # 1. Ask the server; an unauthenticated probe should 401 with a pointer.
        try:
            r = await client.get(
                resource, headers={"accept": "application/json, text/event-stream"}
            )
            if r.status_code == 401:
                hint = _parse_www_authenticate(r.headers.get("www-authenticate", ""))
                if hint:
                    prm = await _get_json(client, hint)
        except Exception as e:
            logger.debug("[oauth] probe %s: %s", resource, e)
        # 2. Well-known protected-resource metadata.
        if prm is None:
            for cand in _well_known_candidates(resource, "oauth-protected-resource"):
                prm = await _get_json(client, cand)
                if prm:
                    break
        issuers: list[str] = []
        scopes: list[str] = []
        if prm:
            issuers = [str(u) for u in (prm.get("authorization_servers") or []) if u]
            scopes = [str(s) for s in (prm.get("scopes_supported") or []) if s]
            if prm.get("resource"):
                resource = canonical_resource(str(prm["resource"]))
        if not issuers:
            issuers = [_origin(resource)]
        # 3. Authorization-server metadata.
        meta: dict | None = None
        issuer = issuers[0]
        for iss in issuers:
            for cand in _well_known_candidates(iss, "oauth-authorization-server") + [
                *_well_known_candidates(iss, "openid-configuration")
            ]:
                meta = await _get_json(client, cand)
                if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                    issuer = iss
                    break
                meta = None
            if meta:
                break
    if not meta:
        raise OAuthError(
            "This server does not publish OAuth metadata. If it takes an API key instead, "
            "add it manually with the key; otherwise check the URL with the provider."
        )
    return ASMeta(
        resource=resource,
        issuer=str(meta.get("issuer") or issuer),
        authorization_endpoint=str(meta["authorization_endpoint"]),
        token_endpoint=str(meta["token_endpoint"]),
        registration_endpoint=(
            str(meta["registration_endpoint"]) if meta.get("registration_endpoint") else None
        ),
        scopes=scopes,
        token_auth_methods=[
            str(m) for m in (meta.get("token_endpoint_auth_methods_supported") or [])
        ],
    )


# ── dynamic client registration ──────────────────────────────────────────────
async def register_client(meta: ASMeta, redirect_uri: str) -> dict:
    """RFC 7591: register this console as a public PKCE client. Returns
    {client_id, client_secret|None}."""
    if not meta.registration_endpoint:
        raise OAuthError(
            "This provider does not support automatic client registration. Create an "
            "OAuth app in its developer console (redirect URL: "
            f"{redirect_uri}) and enter the client id under Advanced."
        )
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with _client() as client:
        try:
            r = await client.post(
                meta.registration_endpoint,
                json=body,
                headers={"accept": "application/json"},
            )
        except Exception as e:
            raise OAuthError(f"client registration failed: {e}") from e
    if r.status_code not in (200, 201):
        raise OAuthError(f"client registration was refused ({r.status_code}): {_err_text(r)}")
    try:
        data = r.json()
    except Exception as e:
        raise OAuthError("client registration returned no JSON") from e
    cid = str(data.get("client_id") or "").strip()
    if not cid:
        raise OAuthError("client registration returned no client_id")
    return {"client_id": cid, "client_secret": data.get("client_secret") or None}


# ── PKCE + authorize URL ─────────────────────────────────────────────────────
def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(
    meta: ASMeta,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str | None,
) -> str:
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": meta.resource,
    }
    if scope:
        q["scope"] = scope
    sep = "&" if "?" in meta.authorization_endpoint else "?"
    return f"{meta.authorization_endpoint}{sep}{urlencode(q)}"


# ── token endpoint ───────────────────────────────────────────────────────────
def _err_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            return str(j.get("error_description") or j.get("error") or r.text[:200])
    except Exception:
        pass
    return (r.text or "")[:200]


def _client_auth(
    client_id: str, client_secret: str | None, methods: list[str]
) -> tuple[dict, tuple[str, str] | None]:
    """(extra form fields, basic-auth tuple) for the token request. A public
    client sends client_id in the body; a confidential one uses HTTP Basic
    unless the server only advertises client_secret_post."""
    if not client_secret:
        return {"client_id": client_id}, None
    if "client_secret_post" in methods and "client_secret_basic" not in methods:
        return {"client_id": client_id, "client_secret": client_secret}, None
    return {}, (client_id, client_secret)


def _parse_tokens(r: httpx.Response) -> TokenSet:
    if r.status_code != 200:
        raise OAuthError(f"token request failed ({r.status_code}): {_err_text(r)}")
    try:
        data = r.json()
    except Exception as e:
        raise OAuthError("token endpoint returned no JSON") from e
    tok = str(data.get("access_token") or "").strip()
    if not tok:
        raise OAuthError("token endpoint returned no access_token")
    exp = None
    try:
        ttl = int(data.get("expires_in") or 0)
        if ttl > 0:
            exp = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
    except Exception:
        exp = None
    return TokenSet(
        access_token=tok,
        refresh_token=(str(data["refresh_token"]) if data.get("refresh_token") else None),
        expires_at=exp,
        scope=(str(data["scope"]) if data.get("scope") else None),
    )


async def exchange_code(
    *,
    token_endpoint: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None,
    resource: str,
    token_auth_methods: list[str] | None = None,
) -> TokenSet:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "resource": resource,
    }
    extra, basic = _client_auth(client_id, client_secret, token_auth_methods or [])
    form.update(extra)
    async with _client() as client:
        try:
            r = await client.post(
                token_endpoint, data=form, auth=basic, headers={"accept": "application/json"}
            )
        except Exception as e:
            raise OAuthError(f"token request failed: {e}") from e
    return _parse_tokens(r)


async def refresh_tokens(
    *,
    token_endpoint: str,
    refresh_token: str,
    client_id: str,
    client_secret: str | None,
    resource: str,
    token_auth_methods: list[str] | None = None,
) -> TokenSet:
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "resource": resource}
    extra, basic = _client_auth(client_id, client_secret, token_auth_methods or [])
    form.update(extra)
    async with _client() as client:
        try:
            r = await client.post(
                token_endpoint, data=form, auth=basic, headers={"accept": "application/json"}
            )
        except Exception as e:
            raise OAuthError(f"token refresh failed: {e}") from e
    ts = _parse_tokens(r)
    if not ts.refresh_token:
        ts.refresh_token = refresh_token  # the server kept the old one
    return ts


def expires_within(expires_at: str | None, seconds: float) -> bool:
    """True when an ISO expiry is inside `seconds` from now (or unparseable)."""
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
    except Exception:
        return True
    return (exp - datetime.now(UTC)).total_seconds() <= seconds


# ── in-flight consents ───────────────────────────────────────────────────────
class PendingStore:
    """state → what the callback needs. Process-local, short-lived."""

    def __init__(self, ttl_s: float = PENDING_TTL_S) -> None:
        self._ttl = ttl_s
        self._items: dict[str, dict] = {}

    def _purge(self, now: float) -> None:
        dead = [k for k, v in self._items.items() if now - v["_ts"] > self._ttl]
        for k in dead:
            self._items.pop(k, None)

    def put(self, data: dict) -> str:
        now = time.monotonic()
        self._purge(now)
        state = secrets.token_urlsafe(32)
        self._items[state] = {**data, "_ts": now}
        return state

    def pop(self, state: str | None) -> dict | None:
        now = time.monotonic()
        self._purge(now)
        if not state:
            return None
        item = self._items.pop(state, None)
        if item is None:
            return None
        item.pop("_ts", None)
        return item

    def __len__(self) -> int:
        return len(self._items)


pending = PendingStore()
