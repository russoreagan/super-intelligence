"""
The key page names the workspace it is about to write into.

One login can hold several orgs (a partner's staging and production), and a
provider key belongs to exactly one of them. Before this the page said only
"Let's wake your brain", so a production credential could be pasted into staging
with nothing on screen to say which. GET /api/keys now returns the org, and the
page labels itself with it.

The property that matters is that the LABEL and the WRITE can never disagree:
both resolve through _tenant_of(request), so whatever org the page names is the
org POST /api/keys stores into. And a forged workspace cookie must never make the
page name an org the user does not belong to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import brain.gateway.server as gw
import brain.org as org
import brain.ui.auth as ui_auth
import brain.vault as vault
from tests.test_org_switch import FOREIGN, HOME, OTHER, USER, _auth_patched, _client

NAMES = {HOME: "Acme (staging)", OTHER: "Acme (prod)", FOREIGN: "Someone Else Inc"}


def _orgs_for_user(uid):
    # Membership-scoped, like the real one: FOREIGN is never in the list.
    return (
        [{"org_id": o, "name": NAMES[o], "role": "admin"} for o in (HOME, OTHER)]
        if uid == USER
        else []
    )


@pytest.fixture
def vault_calls(monkeypatch):
    calls: dict[str, list] = {"status": [], "set": []}

    def _status(org_id, _tok):
        calls["status"].append(org_id)
        return {"anthropic": org_id == HOME, "elevenlabs": False}

    def _set(org_id, _tok, provider, value):
        calls["set"].append((org_id, provider))

    monkeypatch.setattr(vault, "get_status", _status)
    monkeypatch.setattr(vault, "set_key", _set)
    return calls


@pytest.mark.asyncio
async def test_the_page_names_the_default_org(vault_calls):
    with _auth_patched():
        org.orgs_for_user = _orgs_for_user
        async with _client() as c:
            r = await c.get("/api/keys")
    body = r.json()
    assert r.status_code == 200
    assert body["org"] == {"id": HOME, "name": "Acme (staging)"}
    # Provider status is still there, untouched, for render().
    assert body["anthropic"] is True and body["elevenlabs"] is False


@pytest.mark.asyncio
async def test_the_page_names_the_selected_workspace(vault_calls):
    with _auth_patched():
        org.orgs_for_user = _orgs_for_user
        async with _client(cookies={ui_auth.ORG_COOKIE: OTHER}) as c:
            r = await c.get("/api/keys")
    assert r.json()["org"] == {"id": OTHER, "name": "Acme (prod)"}


@pytest.mark.asyncio
async def test_the_label_and_the_write_target_are_the_same_org(vault_calls):
    """The whole point: what the page says is where the key goes."""
    with _auth_patched():
        org.orgs_for_user = _orgs_for_user
        async with _client(cookies={ui_auth.ORG_COOKIE: OTHER}) as c:
            labelled = (await c.get("/api/keys")).json()["org"]["id"]
            await c.post("/api/keys", json={"provider": "anthropic", "value": "sk-test"})
    assert vault_calls["set"] == [(labelled, "anthropic")]
    assert labelled == OTHER


@pytest.mark.asyncio
async def test_a_forged_cookie_never_names_a_foreign_org(vault_calls):
    """A cookie naming an org you are not a member of falls back to your default,
    and the page names your default: it never reveals or targets the other org."""
    with _auth_patched():
        org.orgs_for_user = _orgs_for_user
        async with _client(cookies={ui_auth.ORG_COOKIE: FOREIGN}) as c:
            body = (await c.get("/api/keys")).json()
            await c.post("/api/keys", json={"provider": "anthropic", "value": "sk-test"})
    assert body["org"] == {"id": HOME, "name": "Acme (staging)"}
    assert "Someone Else Inc" not in str(body)
    assert vault_calls["set"] == [(HOME, "anthropic")]


@pytest.mark.asyncio
async def test_a_name_lookup_failure_degrades_to_no_label(vault_calls):
    def _boom(_uid):
        raise RuntimeError("supabase down")

    with _auth_patched():
        org.orgs_for_user = _boom
        async with _client() as c:
            r = await c.get("/api/keys")
    body = r.json()
    assert r.status_code == 200, "a missing label must never break key entry"
    assert body["org"] == {"id": HOME, "name": ""}
    assert body["anthropic"] is True


def test_the_page_renders_the_name_as_text_not_html():
    """This page takes credentials, and an org name is data. It must be set with
    textContent; innerHTML would turn a crafted org name into markup."""
    html = (Path(gw.__file__).parent / "keys.html").read_text(encoding="utf-8")
    assert 'id="org-name"' in html
    assert 'getElementById("org-name").textContent = name' in html
    assert "showOrg(status.org)" in html
    assert "per-user isolation" not in html, "keys belong to the workspace, not the user"
