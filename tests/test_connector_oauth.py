"""brain/connectors/oauth.py — the MCP authorization flow against a fake provider.

Discovery follows the server's 401 pointer to its protected-resource document,
finds the authorization server, registers a public PKCE client, and exchanges /
refreshes tokens with the RFC 8707 `resource` bound to the MCP server."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from brain.connectors import oauth

MCP = "https://mcp.example.com/mcp"
AS = "https://auth.example.com"


class FakeProvider:
    """Routes httpx calls; records the token requests it saw."""

    def __init__(self, *, dcr: bool = True, prm_via_header: bool = True, expires_in: int = 3600):
        self.dcr = dcr
        self.prm_via_header = prm_via_header
        self.expires_in = expires_in
        self.token_calls: list[dict] = []
        self.registrations: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url).split("?")[0]
        if u == MCP and req.method == "GET":
            hdrs = {}
            if self.prm_via_header:
                hdrs["www-authenticate"] = (
                    'Bearer resource_metadata="https://mcp.example.com/.well-known/'
                    'oauth-protected-resource/mcp"'
                )
            return httpx.Response(401, headers=hdrs)
        if u in (
            "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
            "https://mcp.example.com/.well-known/oauth-protected-resource",
        ):
            return httpx.Response(
                200,
                json={
                    "resource": MCP,
                    "authorization_servers": [AS],
                    "scopes_supported": ["read", "write"],
                },
            )
        if u == f"{AS}/.well-known/oauth-authorization-server":
            meta = {
                "issuer": AS,
                "authorization_endpoint": f"{AS}/authorize",
                "token_endpoint": f"{AS}/token",
                "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            }
            if self.dcr:
                meta["registration_endpoint"] = f"{AS}/register"
            return httpx.Response(200, json=meta)
        if u == f"{AS}/register" and req.method == "POST":
            body = json.loads(req.content)
            self.registrations.append(body)
            return httpx.Response(201, json={"client_id": "cid-1"})
        if u == f"{AS}/token" and req.method == "POST":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            form["_auth"] = req.headers.get("authorization", "")
            self.token_calls.append(form)
            if form.get("grant_type") == "authorization_code":
                if form.get("code") != "good":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "expires_in": self.expires_in,
                        "scope": "read write",
                    },
                )
            if form.get("grant_type") == "refresh_token":
                return httpx.Response(
                    200, json={"access_token": "at-2", "expires_in": self.expires_in}
                )
        return httpx.Response(404)


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(p))
    return p


async def test_discover_follows_the_401_pointer_to_the_authorization_server(provider):
    meta = await oauth.discover(MCP)
    assert meta.resource == MCP
    assert meta.authorization_endpoint == f"{AS}/authorize"
    assert meta.token_endpoint == f"{AS}/token"
    assert meta.registration_endpoint == f"{AS}/register"
    assert meta.scopes == ["read", "write"]
    assert "client_secret_basic" in meta.token_auth_methods


async def test_discover_falls_back_to_the_well_known_documents(monkeypatch):
    p = FakeProvider(prm_via_header=False)
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(p))
    meta = await oauth.discover(MCP + "/")  # trailing slash is canonicalised away
    assert meta.resource == MCP
    assert meta.token_endpoint == f"{AS}/token"


async def test_discover_without_any_metadata_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(oauth.OAuthError) as ei:
        await oauth.discover("https://plain.example.com/mcp")
    assert "API key" in str(ei.value)


async def test_register_client_is_a_public_pkce_client_bound_to_the_callback(provider):
    meta = await oauth.discover(MCP)
    reg = await oauth.register_client(meta, "https://app.test/connectors/oauth/callback")
    assert reg == {"client_id": "cid-1", "client_secret": None}
    body = provider.registrations[0]
    assert body["redirect_uris"] == ["https://app.test/connectors/oauth/callback"]
    assert body["token_endpoint_auth_method"] == "none"
    assert "refresh_token" in body["grant_types"]


async def test_register_client_without_dcr_tells_the_org_what_to_do(monkeypatch):
    p = FakeProvider(dcr=False)
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(p))
    meta = await oauth.discover(MCP)
    with pytest.raises(oauth.OAuthError) as ei:
        await oauth.register_client(meta, "https://app.test/cb")
    assert "developer console" in str(ei.value) and "https://app.test/cb" in str(ei.value)


def test_authorize_url_carries_pkce_state_and_resource():
    meta = oauth.ASMeta(
        resource=MCP, issuer=AS, authorization_endpoint=f"{AS}/authorize", token_endpoint="t"
    )
    verifier, challenge = oauth.make_pkce()
    url = oauth.authorize_url(
        meta,
        client_id="cid-1",
        redirect_uri="https://app.test/cb",
        state="st",
        code_challenge=challenge,
        scope="read write",
    )
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert urlsplit(url).path == "/authorize"
    assert q["code_challenge"] == challenge and q["code_challenge_method"] == "S256"
    assert q["resource"] == MCP and q["state"] == "st" and q["scope"] == "read write"
    assert q["client_id"] == "cid-1" and q["response_type"] == "code"
    assert len(verifier) >= 43


async def test_exchange_sends_the_verifier_and_resource_and_parses_expiry(provider):
    ts = await oauth.exchange_code(
        token_endpoint=f"{AS}/token",
        code="good",
        code_verifier="v" * 43,
        redirect_uri="https://app.test/cb",
        client_id="cid-1",
        client_secret=None,
        resource=MCP,
    )
    assert ts.access_token == "at-1" and ts.refresh_token == "rt-1" and ts.scope == "read write"
    assert ts.expires_at and oauth.expires_within(ts.expires_at, 3601)
    assert not oauth.expires_within(ts.expires_at, 60)
    call = provider.token_calls[0]
    assert call["code_verifier"] == "v" * 43 and call["resource"] == MCP
    assert call["client_id"] == "cid-1" and call["_auth"] == ""  # public client


async def test_exchange_failure_surfaces_the_provider_error(provider):
    with pytest.raises(oauth.OAuthError) as ei:
        await oauth.exchange_code(
            token_endpoint=f"{AS}/token",
            code="bad",
            code_verifier="v",
            redirect_uri="https://app.test/cb",
            client_id="cid-1",
            client_secret=None,
            resource=MCP,
        )
    assert "invalid_grant" in str(ei.value)


async def test_confidential_client_uses_basic_auth_and_refresh_keeps_the_old_refresh_token(
    provider,
):
    ts = await oauth.refresh_tokens(
        token_endpoint=f"{AS}/token",
        refresh_token="rt-1",
        client_id="cid-1",
        client_secret="sec",
        resource=MCP,
        token_auth_methods=["client_secret_basic"],
    )
    assert ts.access_token == "at-2"
    assert ts.refresh_token == "rt-1"  # server did not rotate it
    call = provider.token_calls[0]
    assert call["grant_type"] == "refresh_token" and call["resource"] == MCP
    assert call["_auth"].startswith("Basic ") and "client_secret" not in call


def test_expires_within_treats_missing_as_not_expiring_and_garbage_as_expired():
    assert oauth.expires_within(None, 60) is False
    assert oauth.expires_within("not a date", 60) is True
    assert oauth.expires_within("2000-01-01T00:00:00Z", 60) is True


def test_pending_store_is_single_use_and_expires(monkeypatch):
    store = oauth.PendingStore(ttl_s=10)
    now = [1000.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now[0])
    st = store.put({"name": "acme"})
    assert store.pop("nope") is None
    assert store.pop(st) == {"name": "acme"}
    assert store.pop(st) is None  # single use
    st2 = store.put({"name": "late"})
    now[0] += 11
    assert store.pop(st2) is None and len(store) == 0


def test_canonical_resource():
    assert oauth.canonical_resource("HTTPS://MCP.Example.com/mcp/?x=1#f") == MCP
    assert oauth.canonical_resource("https://x.example.com/") == "https://x.example.com/"
    assert oauth.canonical_resource("https://x.example.com") == "https://x.example.com/"
