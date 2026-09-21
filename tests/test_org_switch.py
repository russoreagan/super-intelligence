"""
One login, several orgs: the console workspace switcher.

`memberships` has been many-to-many since migration 006, but nothing could SAY
which org a session meant — org_id_for_user collapsed every membership to one
value and the gateway cached that per uid. The sb-org cookie is that selection.

The property every test here circles is that the cookie is a REQUEST, not a
grant. It names an org; membership decides whether it is honoured. So the
interesting cases are the negative ones: a forged cookie, a revoked membership,
and a Supabase outage must all land the user on their own default org rather than
anywhere else — and must never 403 them out of a console they are entitled to.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

import brain.gateway.server as gw
import brain.ui.auth as ui_auth

pytestmark = pytest.mark.asyncio

USER = "u1"
# Real uuids, because selected_org() shape-checks the cookie before it can reach a
# query — a non-uuid id would be dropped at the door and never exercise the path.
HOME = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
FOREIGN = "33333333-3333-4333-8333-333333333333"

MEMBERSHIPS = {
    (USER, HOME): True,
    (USER, OTHER): True,
}


class _FakeProv:
    """Never spawns; every org looks already-running so routing is observable."""

    def __init__(self):
        self.touched: list[str] = []

    def status(self, user_id, persona=None):
        return {"port": 9999, "booting": False, "api_port": 9998}

    def touch(self, user_id, persona=None):
        self.touched.append(user_id)

    def keys_for(self, *_a, **_k):
        return []


@contextlib.contextmanager
def _auth_patched(is_member=None, org_for_user=None):
    orig = {
        "disabled": ui_auth.is_disabled,
        "configured": ui_auth.is_configured,
        "auth": ui_auth.authenticate,
        "set": ui_auth.set_session_cookies,
    }
    import brain.org as org

    orig_org, orig_member = org.org_id_for_user, org.is_member
    orig_list = org.orgs_for_user

    ui_auth.is_disabled = lambda: False
    ui_auth.is_configured = lambda: True

    async def _fake_auth(_conn):
        return {"sub": USER}, None

    ui_auth.authenticate = _fake_auth
    ui_auth.set_session_cookies = lambda *a, **k: None
    org.org_id_for_user = org_for_user or (lambda uid: HOME)
    org.is_member = is_member or (lambda uid, org_id: MEMBERSHIPS.get((uid, org_id), False))
    gw._org_cache.clear()
    gw._member_cache.clear()
    try:
        yield
    finally:
        ui_auth.is_disabled = orig["disabled"]
        ui_auth.is_configured = orig["configured"]
        ui_auth.authenticate = orig["auth"]
        ui_auth.set_session_cookies = orig["set"]
        org.org_id_for_user, org.is_member = orig_org, orig_member
        org.orgs_for_user = orig_list
        gw._org_cache.clear()
        gw._member_cache.clear()


def _client(prov=None, cookies=None):
    """An ASGI client. Cookies are set on the CLIENT, not per request — httpx
    deprecated per-request cookies and silently drops them."""
    app = gw.build_gateway_app(prov or _FakeProv(), [])
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", cookies=cookies or {}
    )


# ── resolution ───────────────────────────────────────────────────────────────


async def test_no_cookie_resolves_the_default_org():
    with _auth_patched():
        assert await gw._tenant_for(USER) == HOME
        assert await gw._tenant_for(USER, None) == HOME


async def test_a_valid_selection_is_honoured():
    with _auth_patched():
        assert await gw._tenant_for(USER, OTHER) == OTHER


async def test_a_selection_you_are_not_a_member_of_falls_back():
    """Not a 403: refusing here would lock a user out of a console they ARE
    entitled to, over a cookie they may not even know is set."""
    with _auth_patched():
        assert await gw._tenant_for(USER, FOREIGN) == HOME


async def test_a_lookup_failure_falls_back_rather_than_granting():
    """brain.org.is_member fails closed, and the selection must too — an outage
    is not a reason to hand out a workspace."""

    def _boom(_uid, _org):
        raise RuntimeError("supabase down")

    with _auth_patched(is_member=lambda u, o: False):
        assert await gw._tenant_for(USER, OTHER) == HOME
    with _auth_patched(is_member=_boom), pytest.raises(RuntimeError):
        await gw._tenant_for(USER, OTHER)


async def test_switching_never_poisons_the_default_org_cache():
    """The two caches answer different questions and must stay independent: one
    is 'where does this user live', the other 'may they act here'."""
    with _auth_patched():
        assert await gw._tenant_for(USER, OTHER) == OTHER
        assert await gw._tenant_for(USER) == HOME


async def test_membership_is_cached_then_revalidated(monkeypatch):
    calls: list[tuple] = []

    def _counting(uid, org_id):
        calls.append((uid, org_id))
        return MEMBERSHIPS.get((uid, org_id), False)

    with _auth_patched(is_member=_counting):
        await gw._tenant_for(USER, OTHER)
        await gw._tenant_for(USER, OTHER)
        assert len(calls) == 1, "second call served from cache"
        # The cache TTL is the revocation window, so expiry must re-ask.
        gw._member_cache[(USER, OTHER)] = (True, 0.0)
        await gw._tenant_for(USER, OTHER)
        assert len(calls) == 2


async def test_a_negative_is_cached_too():
    """Otherwise a stale or hostile cookie turns every request into a DB round
    trip — a cheap amplification against the auth backend."""
    calls: list[tuple] = []

    def _counting(uid, org_id):
        calls.append((uid, org_id))
        return False

    with _auth_patched(is_member=_counting):
        await gw._tenant_for(USER, FOREIGN)
        await gw._tenant_for(USER, FOREIGN)
        assert len(calls) == 1


# ── the routes ───────────────────────────────────────────────────────────────


async def test_switch_sets_the_cookie_with_session_flags():
    with _auth_patched():
        async with _client() as c:
            r = await c.post("/__org/switch", json={"org_id": OTHER})
    assert r.status_code == 200 and r.json()["org_id"] == OTHER
    setc = r.headers.get("set-cookie", "")
    assert "sb-org=" + OTHER in setc
    assert "HttpOnly" in setc and "Path=/" in setc and "lax" in setc.lower()


async def test_switch_to_a_foreign_org_is_refused_and_sets_no_cookie():
    with _auth_patched():
        async with _client() as c:
            r = await c.post("/__org/switch", json={"org_id": FOREIGN})
    assert r.status_code == 403 and r.json()["error"] == "not_a_member"
    assert "sb-org" not in r.headers.get("set-cookie", "")


async def test_switch_requires_an_org_id():
    with _auth_patched():
        async with _client() as c:
            assert (await c.post("/__org/switch", json={})).status_code == 400


async def test_list_reports_every_org_and_which_is_current():
    import brain.org as org

    rows = [
        {"org_id": HOME, "name": "Acme", "role": "admin"},
        {"org_id": OTHER, "name": "Acme (staging)", "role": "admin"},
    ]
    with _auth_patched():
        org.orgs_for_user = lambda uid: rows
        async with _client(cookies={"sb-org": OTHER}) as c:
            r = await c.get("/__org/list")
    body = r.json()
    assert r.status_code == 200
    assert body["current"] == OTHER
    assert [o["org_id"] for o in body["orgs"]] == [HOME, OTHER]
    assert [o["current"] for o in body["orgs"]] == [False, True]


async def test_the_routes_are_registered_before_the_catch_all():
    """A regression guard with teeth: /{path:path} is registered last and would
    happily proxy /__org/* to the tenant brain, where it 404s — and the switcher
    would just never appear, with nothing in the logs to say why."""
    paths = {getattr(r, "path", "") for r in gw.build_gateway_app(_FakeProv(), []).routes}
    assert "/__org/list" in paths and "/__org/switch" in paths
    ordered = [getattr(r, "path", "") for r in gw.build_gateway_app(_FakeProv(), []).routes]
    assert ordered.index("/__org/list") < ordered.index("/{path:path}")
    assert ordered.index("/__org/switch") < ordered.index("/{path:path}")


# ── the selection must never leak into the partner-key lane ─────────────────


async def test_v1_ignores_the_org_cookie(monkeypatch):
    """/v1 resolves its org from the BEARER KEY. is_public_path short-circuits the
    auth gate for it, so request.state.tenant is never even set there — a browser
    cookie must not be able to move a partner's traffic to another org."""
    import brain.api.auth as api_auth

    ctx = {"org_id": "org-from-key", "partner_id": "p", "role": "partner", "key_id": "k"}
    monkeypatch.setattr(api_auth, "resolve_key_context", lambda _a: ctx)
    monkeypatch.setattr(gw, "_org_learning_cache", {})
    from brain import org_settings

    monkeypatch.setattr(
        org_settings,
        "read_org_row",
        lambda org, client=None: {"id": org, "name": "From Key"},
    )
    with _auth_patched():
        async with _client(cookies={"sb-org": OTHER}) as c:
            r = await c.get("/v1/whoami", headers={"authorization": "Bearer k"})
    assert r.status_code == 200
    assert r.json()["org_id"] == "org-from-key"


# ── cookie helpers ───────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, cookies):
        self.cookies = cookies


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("f47ac10b-58cc-4372-a567-0e02b2c3d479", "f47ac10b-58cc-4372-a567-0e02b2c3d479"),
        ("", ""),
        ("../../etc/passwd", ""),
        ("org'; drop table memberships;--", ""),
        ("x" * 200, ""),
    ],
)
async def test_selected_org_drops_anything_that_is_not_an_id(raw, expected):
    """Shape-checked before it can reach a query. The gateway would refuse it a
    moment later either way, but not issuing the query at all is the point."""
    assert ui_auth.selected_org(_Conn({"sb-org": raw})) == expected


async def test_logout_clears_the_selection():
    """Otherwise the next person to sign in on this browser inherits a
    pre-selected workspace that was never theirs."""
    deleted: list[str] = []

    class _Resp:
        def delete_cookie(self, name, **_k):
            deleted.append(name)

    ui_auth.clear_session_cookies(_Resp())
    assert ui_auth.ORG_COOKIE in deleted


# ── a selection that stops holding must stop being sent ─────────────────────


async def test_a_rejected_cookie_is_cleared_on_the_way_out():
    """Membership revoked (or the cookie forged): the request still succeeds, on
    the user's default org, and the response deletes sb-org so the browser stops
    re-sending it. Only the middleware can do this — by the time a route has the
    response, clearing it there would mean touching every handler."""
    with _auth_patched():
        async with _client(cookies={"sb-org": FOREIGN}) as c:
            r = await c.get("/__org/list")
    assert r.status_code == 200
    setc = r.headers.get("set-cookie", "")
    assert "sb-org=" in setc
    # A deletion, not a re-set: empty value with an expiry in the past.
    assert 'sb-org=""' in setc or "sb-org=;" in setc or "Max-Age=0" in setc


async def test_a_valid_cookie_is_left_alone():
    with _auth_patched():
        async with _client(cookies={"sb-org": OTHER}) as c:
            r = await c.get("/__org/list")
    assert r.status_code == 200 and "sb-org" not in r.headers.get("set-cookie", "")


# ── /ws drift: a tab left open on the previous workspace ────────────────────


async def test_ws_closes_4001_when_the_page_is_on_a_different_org():
    """_proxy_ws binds its upstream port once at connect and nothing can rebind a
    live socket, so the handshake is the only chance to catch a stale tab. Without
    this it would reconnect onto the NEW org's brain and stream its events into
    the old org's DOM."""
    from starlette.testclient import TestClient

    app = gw.build_gateway_app(_FakeProv(), [])
    with _auth_patched():
        client = TestClient(app)
        client.cookies.set("sb-org", OTHER)
        with pytest.raises(Exception) as err, client.websocket_connect(f"/ws?org={HOME}"):
            pass
    assert getattr(err.value, "code", None) == 4001


async def test_ws_accepts_a_page_that_agrees_with_the_session():
    """The happy path must not be broken by the drift check: a matching claim is
    simply ignored, and an absent one (an older page) is too."""
    with _auth_patched():
        assert await gw._tenant_for(USER, OTHER) == OTHER
