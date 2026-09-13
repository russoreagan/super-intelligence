"""Connectors with three auth modes (supabase 043, brain/connectors/*):

• the console routes — catalogue, register (api_key / oauth / shared_secret),
  the OAuth start → provider → callback round trip, rotate, and the org-admin
  gate — against the file registry and a fake provider;
• the registry mapping the executor consumes — a Supabase row becomes an
  identity connector only for shared_secret, an oauth row carries the refresh
  triple, an unconnected oauth row is never declared to the agent;
• the org-vault credential sync, which now tracks each bearer by hash so a
  rotated key or refreshed token is pushed in place instead of skipped forever.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as SN
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain.clusters import cma_executor as ce  # noqa: E402
from brain.clusters.cma_executor import CMAExecutor  # noqa: E402
from brain.connectors import oauth  # noqa: E402
from brain.second_brain import supabase_client  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402
from tests.test_connector_oauth import AS, MCP, FakeProvider  # noqa: E402

CB = "https://app.test/connectors/oauth/callback"


# ── console fixture (file registry, fake provider) ──────────────────────────
@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PUBLIC_URL", "https://app.test")
    for k in ("BRAIN_CMA_MCP_SERVERS", "BRAIN_CMA_MCP_OWNER_ORG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    monkeypatch.setattr(ce, "_MCP_CONFIG_PATH", tmp_path / "cma_mcp.json")
    monkeypatch.setattr(oauth, "pending", oauth.PendingStore())
    provider = FakeProvider()
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(provider))
    reloads: list[str] = []
    from brain.ui.server import UIServer

    srv = UIServer(
        emitter_queue=asyncio.Queue(),
        connector_reload_fn=lambda: reloads.append("reload"),
        connectors_fn=lambda: [s["name"] for s in ce._registry_entries()],
    )
    client = TestClient(srv._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    def registry():
        return {s["name"]: s for s in ce._registry_entries()}

    return SN(client=client, as_role=as_role, provider=provider, reloads=reloads, registry=registry)


# ── gating ───────────────────────────────────────────────────────────────────
def test_members_cannot_touch_the_registry(console):
    console.as_role("member")
    c = console.client
    assert c.get("/connectors/catalog").status_code == 403
    assert c.post("/connectors", json={"name": "x", "url": "https://x/mcp"}).status_code == 403
    assert c.post("/connectors/x/oauth/start").status_code == 403
    assert c.post("/connectors/x/rotate", json={"api_key": "k"}).status_code == 403
    assert c.get("/connectors/oauth/callback?code=a&state=b").status_code == 403
    assert console.registry() == {} and console.reloads == []


# ── catalogue ────────────────────────────────────────────────────────────────
def test_catalog_lists_known_servers_and_the_callback_url(console):
    console.as_role("admin")
    r = console.client.get("/connectors/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["redirect_uri"] == CB
    ids = {e["id"]: e for e in body["catalog"]}
    assert ids["notion"]["auth"] == "oauth" and ids["notion"]["url"].startswith("https://")
    assert ids["github"]["auth"] == "api_key" and ids["github"]["key_hint"]
    assert all(e["auth"] in ("oauth", "api_key") for e in body["catalog"])


# ── api_key ──────────────────────────────────────────────────────────────────
def test_api_key_connector_stores_the_key_and_never_returns_it(console):
    console.as_role("admin")
    c = console.client
    r = c.post(
        "/connectors",
        json={
            "name": "zap",
            "url": "https://mcp.zapier.com/api/mcp/mcp",
            "auth_mode": "api_key",
            "api_key": "zk-secret-123",
            "description": "Zapier actions",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"name": "zap", "auth_mode": "api_key"}
    assert "zk-secret-123" not in r.text
    reg = console.registry()["zap"]
    assert reg["access_token"] == "zk-secret-123" and reg["identity"] is False
    assert reg["auth_mode"] == "api_key" and reg["description"] == "Zapier actions"
    # The UI list carries status + auth, and no secret.
    full = c.get("/connectors?full=1")
    d = {x["name"]: x for x in full.json()["details"]}
    assert d["zap"]["status"] == "ready" and d["zap"]["auth_mode"] == "api_key"
    assert "zk-secret-123" not in full.text
    assert console.reloads == ["reload"]


def test_api_key_connector_requires_a_key(console):
    console.as_role("admin")
    r = console.client.post(
        "/connectors", json={"name": "zap", "url": "https://z/mcp", "auth_mode": "api_key"}
    )
    assert r.status_code == 400 and "API key" in r.json()["detail"]
    assert console.registry() == {}


def test_rotate_replaces_an_api_key_and_mints_a_shared_secret(console):
    console.as_role("admin")
    c = console.client
    c.post(
        "/connectors",
        json={"name": "zap", "url": "https://z/mcp", "auth_mode": "api_key", "api_key": "old"},
    )
    r = c.post("/connectors/zap/rotate", json={"api_key": "new"})
    assert r.status_code == 200 and r.json() == {"name": "zap", "auth_mode": "api_key"}
    assert console.registry()["zap"]["access_token"] == "new"
    assert c.post("/connectors/zap/rotate", json={}).status_code == 400

    r = c.post("/connectors", json={"name": "mine", "url": "https://mine/mcp"})
    first = r.json()["secret"]
    assert r.json()["auth_mode"] == "shared_secret" and len(first) == 64
    assert console.registry()["mine"]["identity"] is True
    r = c.post("/connectors/mine/rotate")
    assert r.status_code == 200
    second = r.json()["secret"]
    assert second != first and console.registry()["mine"]["access_token"] == second
    assert r.json()["app_env_var"] == "MINE_MCP_SECRET"
    assert c.post("/connectors/nope/rotate", json={"api_key": "k"}).status_code == 404


# ── oauth round trip ─────────────────────────────────────────────────────────
def _q(url: str) -> dict:
    from urllib.parse import parse_qs, urlsplit

    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_oauth_connect_round_trip(console):
    console.as_role("admin")
    c = console.client
    r = c.post("/connectors", json={"catalog_id": "notion", "url": MCP, "name": "acme"})
    assert r.status_code == 200
    body = r.json()
    assert body["auth_mode"] == "oauth" and body["status"] == "pending"
    q = _q(body["authorize_url"])
    assert body["authorize_url"].startswith(f"{AS}/authorize?")
    assert q["redirect_uri"] == CB and q["resource"] == MCP and q["scope"] == "read write"
    assert q["client_id"] == "cid-1" and q["code_challenge_method"] == "S256"
    # Registered, pending, client stored, nothing declared to the agent yet.
    reg = console.registry()["acme"]
    assert reg["oauth"]["status"] == "pending" and reg["oauth"]["client_id"] == "cid-1"
    assert reg["oauth"]["token_endpoint"] == f"{AS}/token" and "access_token" not in reg
    assert reg["display_name"] == "Notion" and reg["catalog_id"] == "notion"
    assert console.provider.registrations[0]["redirect_uris"] == [CB]

    # Provider sends the browser back with a code.
    r = c.get(f"/connectors/oauth/callback?code=good&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?connected=acme"
    reg = console.registry()["acme"]
    assert reg["access_token"] == "at-1" and reg["oauth"]["status"] == "connected"
    assert reg["refresh"]["refresh_token"] == "rt-1" and reg["refresh"]["client_id"] == "cid-1"
    assert reg["refresh"]["token_endpoint"] == f"{AS}/token" and reg["expires_at"]
    assert reg["identity"] is False
    call = console.provider.token_calls[0]
    assert call["grant_type"] == "authorization_code" and call["resource"] == MCP
    assert call["redirect_uri"] == CB and len(call["code_verifier"]) >= 43
    # Executor reloaded on register and on callback; the state is single-use.
    assert console.reloads == ["reload", "reload"]
    r = c.get(f"/connectors/oauth/callback?code=good&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303 and "connect_error" in r.headers["location"]
    # The UI list shows it connected with no token material.
    full = c.get("/connectors?full=1")
    d = {x["name"]: x for x in full.json()["details"]}
    assert d["acme"]["status"] == "connected" and d["acme"]["has_client"] is True
    assert "at-1" not in full.text and "rt-1" not in full.text


def test_oauth_denied_at_the_provider_marks_the_connector_error(console):
    console.as_role("admin")
    c = console.client
    body = c.post("/connectors", json={"name": "acme", "url": MCP, "auth_mode": "oauth"}).json()
    st = _q(body["authorize_url"])["state"]
    r = c.get(
        f"/connectors/oauth/callback?error=access_denied&error_description=nope&state={st}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?connector=acme&connect_error=nope"
    reg = console.registry()["acme"]
    assert reg["oauth"]["status"] == "error" and reg["oauth"]["error"] == "nope"
    # Reconnect restarts the flow from the stored client registration (no new DCR).
    r = c.post("/connectors/acme/oauth/start")
    assert r.status_code == 200 and _q(r.json()["authorize_url"])["client_id"] == "cid-1"
    assert len(console.provider.registrations) == 1
    assert console.registry()["acme"]["oauth"]["status"] == "pending"


def test_oauth_register_keeps_the_row_when_the_provider_is_unreachable(console, monkeypatch):
    console.as_role("admin")
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(lambda r: httpx.Response(404)))
    r = console.client.post(
        "/connectors",
        json={"name": "dead", "url": "https://dead.example/mcp", "auth_mode": "oauth"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error" and "OAuth metadata" in body["error"]
    reg = console.registry()["dead"]
    assert reg["oauth"]["status"] == "error" and "access_token" not in reg
    # …and it is not declared to the agent.
    monkeypatch.setattr(ce, "is_env_managed", lambda: False)
    names = [s["name"] for s in CMAExecutor._load_mcp_config(SN())]
    assert "dead" not in names


def test_oauth_with_a_preregistered_client_skips_dcr(console):
    console.as_role("admin")
    r = console.client.post(
        "/connectors",
        json={
            "name": "acme",
            "url": MCP,
            "auth_mode": "oauth",
            "oauth_client_id": "my-app",
            "oauth_client_secret": "s3",
        },
    )
    assert _q(r.json()["authorize_url"])["client_id"] == "my-app"
    assert console.provider.registrations == []
    reg = console.registry()["acme"]
    assert reg["oauth"]["client_id"] == "my-app" and reg["oauth"]["client_secret"] == "s3"


def test_rotate_refuses_oauth_connectors(console):
    console.as_role("admin")
    console.client.post("/connectors", json={"name": "acme", "url": MCP, "auth_mode": "oauth"})
    r = console.client.post("/connectors/acme/rotate", json={"api_key": "k"})
    assert r.status_code == 400 and "Connect" in r.json()["detail"]


# ── registry → executor mapping (Supabase rows) ─────────────────────────────
def _rows():
    return [
        {"name": "mine", "url": "https://mine/mcp", "auth_mode": "shared_secret", "token": "sec"},
        {
            "name": "zap",
            "url": "https://z/mcp",
            "auth_mode": "api_key",
            "token": "zk",
            "description": "Zapier",
            "oauth": {"status": ""},
        },
        {
            "name": "acme",
            "url": MCP,
            "auth_mode": "oauth",
            "token": "at-1",
            "oauth": {
                "client_id": "cid-1",
                "client_secret": None,
                "token_endpoint": f"{AS}/token",
                "refresh_token": "rt-1",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "status": "connected",
            },
        },
        {
            "name": "wait",
            "url": "https://w/mcp",
            "auth_mode": "oauth",
            "token": None,
            "oauth": {"status": "pending"},
        },
    ]


@pytest.fixture
def sb(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return SN(execute=lambda: SN(data=_rows() if name == "get_mcp_connectors" else True))

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _Client())
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-pod")
    for k in ("BRAIN_CMA_MCP_SERVERS", "BRAIN_CMA_MCP_OWNER_ORG"):
        monkeypatch.delenv(k, raising=False)
    return calls


def test_supabase_rows_map_to_executor_entries(sb):
    by = {s["name"]: s for s in ce._load_connectors_from_supabase()}
    assert sb[0] == ("get_mcp_connectors", {"p_org_id": "org-pod"})
    assert by["mine"]["identity"] is True and by["mine"]["access_token"] == "sec"
    assert by["zap"]["identity"] is False and by["zap"]["access_token"] == "zk"
    assert by["acme"]["identity"] is False and by["acme"]["access_token"] == "at-1"
    assert by["acme"]["refresh"] == {
        "refresh_token": "rt-1",
        "client_id": "cid-1",
        "token_endpoint": f"{AS}/token",
    }
    assert by["acme"]["expires_at"] == "2030-01-01T00:00:00+00:00"
    assert "access_token" not in by["wait"] and "refresh" not in by["wait"]

    loaded = {s["name"]: s for s in CMAExecutor._load_mcp_config(SN())}
    assert set(loaded) == {"mine", "zap", "acme"}  # pending oauth is not declared
    assert (
        loaded["zap"]["description"] == "Zapier"
        and loaded["acme"]["refresh"]["client_id"] == "cid-1"
    )

    details = {d["name"]: d for d in ce.list_connector_details()}
    assert details["wait"]["status"] == "pending" and details["acme"]["status"] == "connected"
    assert details["zap"]["status"] == "ready" and details["mine"]["auth_mode"] == "shared_secret"
    assert not any("token" in d or "refresh" in d for d in details.values())


def test_registry_writes_thread_the_org_id_and_mode(sb):
    ce.register_connector(
        "zap", "https://z/mcp", auth_mode="api_key", secret="zk", description="Zapier"
    )
    name, params = sb[-1]
    assert name == "register_mcp_connector"
    assert params["p_auth_mode"] == "api_key" and params["p_secret"] == "zk"
    assert params["p_org_id"] == "org-pod" and params["p_description"] == "Zapier"
    assert ce.register_connector("acme", MCP, auth_mode="oauth") == ""
    assert sb[-1][1]["p_secret"] is None and sb[-1][1]["p_auth_mode"] == "oauth"
    ce.set_connector_oauth(
        "acme", status="connected", access_token="at", refresh_token="rt", expires_at="x"
    )
    name, params = sb[-1]
    assert name == "set_mcp_connector_oauth"
    assert params["p_access_token"] == "at" and params["p_client_id"] is None
    ce.set_connector_secret("zap", "zk2")
    assert sb[-1] == (
        "set_mcp_connector_secret",
        {"p_name": "zap", "p_secret": "zk2", "p_org_id": "org-pod"},
    )
    ce.remove_connector("zap")
    assert sb[-1] == ("delete_mcp_connector", {"p_name": "zap", "p_org_id": "org-pod"})
    with pytest.raises(ValueError):
        ce.register_connector("x", "https://x/mcp", auth_mode="bogus")


# ── org-vault credential sync ────────────────────────────────────────────────
def _exec(servers, state=None):
    exe = CMAExecutor.__new__(CMAExecutor)
    exe._mcp_servers = servers
    exe._state = state if state is not None else {}
    exe._vault_id = "vault_1"
    exe._user_id = ""
    exe._save_state = lambda: None
    creds = MagicMock()
    creds.create = AsyncMock(side_effect=lambda *a, **k: SN(id=f"cred_{creds.create.await_count}"))
    creds.update = AsyncMock()
    exe._client = SN(beta=SN(vaults=SN(credentials=creds)))
    return exe, creds


async def test_vault_sync_updates_a_rotated_bearer_in_place():
    srv = {"name": "zap", "url": "https://z/mcp", "auth_mode": "api_key", "access_token": "k1"}
    exe, creds = _exec([srv])
    await exe._sync_vault_credentials()
    creds.create.assert_awaited_once()
    assert creds.create.await_args.kwargs["auth"]["access_token"] == "k1"
    rec = exe._state["seeded_mcp_creds"]["https://z/mcp"]
    assert rec["cred_id"] == "cred_1"
    # Same bearer again → nothing to do.
    await exe._sync_vault_credentials()
    creds.create.assert_awaited_once() and creds.update.assert_not_awaited()
    # Replaced key → updated in place, no second credential.
    srv["access_token"] = "k2"
    await exe._sync_vault_credentials()
    creds.update.assert_awaited_once()
    args, kwargs = creds.update.await_args
    assert args[0] == "cred_1" and kwargs["vault_id"] == "vault_1"
    assert kwargs["auth"]["access_token"] == "k2"
    creds.create.assert_awaited_once()


async def test_vault_sync_honours_legacy_seeded_urls_without_reseeding():
    srv = {
        "name": "mine",
        "url": "https://m/mcp",
        "auth_mode": "shared_secret",
        "access_token": "s",
    }
    exe, creds = _exec([srv], state={"seeded_mcp": ["https://m/mcp"]})
    await exe._sync_vault_credentials()
    creds.create.assert_not_awaited()
    assert exe._state["seeded_mcp_creds"]["https://m/mcp"]["cred_id"] is None


async def test_vault_sync_refreshes_an_expiring_oauth_token_first(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(provider))
    persisted: list[dict] = []
    monkeypatch.setattr(
        ce, "set_connector_oauth", lambda name, **kw: persisted.append({"name": name, **kw}) or True
    )
    srv = {
        "name": "acme",
        "url": MCP,
        "auth_mode": "oauth",
        "access_token": "at-old",
        "expires_at": "2000-01-01T00:00:00+00:00",
        "refresh": {"refresh_token": "rt-1", "client_id": "cid-1", "token_endpoint": f"{AS}/token"},
    }
    exe, creds = _exec([srv])
    assert exe._oauth_refresh_due() is True
    await exe._sync_vault_credentials()
    assert (
        srv["access_token"] == "at-2" and provider.token_calls[0]["grant_type"] == "refresh_token"
    )
    assert persisted[0]["status"] == "connected" and persisted[0]["access_token"] == "at-2"
    assert creds.create.await_args.kwargs["auth"]["access_token"] == "at-2"
    assert creds.create.await_args.kwargs["auth"]["refresh"]["refresh_token"] == "rt-1"
    assert exe._oauth_refresh_due() is False


async def test_vault_sync_marks_a_failed_refresh_and_skips_the_connector(monkeypatch):
    monkeypatch.setattr(
        oauth,
        "_transport",
        httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})),
    )
    persisted: list[dict] = []
    monkeypatch.setattr(
        ce, "set_connector_oauth", lambda name, **kw: persisted.append({"name": name, **kw}) or True
    )
    srv = {
        "name": "acme",
        "url": MCP,
        "auth_mode": "oauth",
        "access_token": "at-old",
        "expires_at": "2000-01-01T00:00:00+00:00",
        "refresh": {"refresh_token": "rt-1", "client_id": "cid-1", "token_endpoint": f"{AS}/token"},
    }
    exe, creds = _exec([srv])
    await exe._sync_vault_credentials()
    creds.create.assert_not_awaited()
    assert persisted[0]["status"] == "error" and "invalid_grant" in persisted[0]["error"]
    assert srv["oauth"]["status"] == "error"


async def test_user_vault_gets_the_org_bearers_too(monkeypatch):
    """A per-user session attaches only the user vault, so the org's api_key /
    oauth bearers are copied in alongside the minted identity token."""
    exe = CMAExecutor.__new__(CMAExecutor)
    exe._mcp_servers = [
        {
            "name": "mine",
            "url": "https://m/mcp",
            "auth_mode": "shared_secret",
            "identity": True,
            "access_token": "s",
        },
        {
            "name": "zap",
            "url": "https://z/mcp",
            "auth_mode": "api_key",
            "identity": False,
            "access_token": "zk",
        },
        {"name": "wait", "url": "https://w/mcp", "auth_mode": "oauth", "identity": False},
    ]
    exe._user_vault_cache = {}
    exe._user_id = ""
    exe._fetch_end_user_tokens = AsyncMock(return_value=[])
    creds = MagicMock()
    creds.create = AsyncMock(return_value=SN(id="c"))
    exe._client = SN(
        beta=SN(vaults=SN(create=AsyncMock(return_value=SN(id="uv")), credentials=creds))
    )
    vid = await exe._ensure_user_vault("eu-1")
    assert vid == "uv"
    seeded = [
        (c.kwargs["auth"]["type"], c.kwargs["auth"]["mcp_server_url"])
        for c in creds.create.await_args_list
    ]
    assert ("mcp_oauth", "https://z/mcp") in seeded
    assert ("static_bearer", "https://m/mcp") in seeded
    assert not any(u == "https://w/mcp" for _, u in seeded)


def test_file_registry_round_trips_every_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    monkeypatch.setattr(ce, "_MCP_CONFIG_PATH", tmp_path / "cma_mcp.json")
    for k in ("BRAIN_CMA_MCP_SERVERS", "BRAIN_CMA_MCP_OWNER_ORG"):
        monkeypatch.delenv(k, raising=False)
    sec = ce.register_connector("mine", "https://m/mcp")
    ce.register_connector("zap", "https://z/mcp", auth_mode="api_key", secret="zk")
    ce.register_connector("acme", MCP, auth_mode="oauth", description="Acme")
    raw = json.loads((tmp_path / "cma_mcp.json").read_text())
    by = {s["name"]: s for s in raw["servers"]}
    assert by["mine"]["access_token"] == sec and "identity" not in by["mine"]
    assert by["zap"]["identity"] is False and by["acme"]["oauth"] == {"status": "pending"}
    assert ce.set_connector_oauth(
        "acme",
        status="connected",
        access_token="at",
        refresh_token="rt",
        client_id="c",
        token_endpoint="t",
        expires_at="2030-01-01T00:00:00+00:00",
    )
    assert ce.set_connector_oauth("zap", status="connected") is False  # not oauth
    rec = ce.get_connector_record("acme")
    assert rec["access_token"] == "at" and rec["refresh"] == {
        "refresh_token": "rt",
        "client_id": "c",
        "token_endpoint": "t",
    }
    assert rec["oauth"]["connected_ts"]
    assert ce.get_connector_record("nope") is None
    with pytest.raises(ValueError):
        ce.register_connector("zap", "https://z/mcp", auth_mode="api_key", secret="dup")
    assert ce.remove_connector("acme") is True and ce.get_connector_record("acme") is None
