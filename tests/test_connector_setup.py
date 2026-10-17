"""brain/connectors/setup_check.py + catalog.platform_app — what Connect does for
each directory entry, decided from the live provider rather than assumed."""

from __future__ import annotations

import httpx
import pytest

from brain.connectors import catalog, oauth, setup_check
from tests.test_connector_oauth import MCP, FakeProvider


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(setup_check, "_results", {})
    monkeypatch.setattr(setup_check, "_inflight", None)
    for k in ("BRAIN_OAUTH_GOOGLE_CLIENT_ID", "BRAIN_OAUTH_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)


def _google(monkeypatch):
    monkeypatch.setenv("BRAIN_OAUTH_GOOGLE_CLIENT_ID", "plat-id")
    monkeypatch.setenv("BRAIN_OAUTH_GOOGLE_CLIENT_SECRET", "plat-sec")


def test_platform_app_needs_both_halves(monkeypatch):
    assert catalog.platform_app("google") is None
    monkeypatch.setenv("BRAIN_OAUTH_GOOGLE_CLIENT_ID", "plat-id")
    assert catalog.platform_app("google") is None  # an id alone cannot redeem a code
    monkeypatch.setenv("BRAIN_OAUTH_GOOGLE_CLIENT_SECRET", "plat-sec")
    assert catalog.platform_app("google") == {"client_id": "plat-id", "client_secret": "plat-sec"}
    assert catalog.platform_app(None) is None and catalog.platform_app("") is None


def test_catalog_is_consistent():
    ids = [e["id"] for e in catalog.CATALOG]
    assert len(ids) == len(set(ids))
    for e in catalog.CATALOG:
        assert e["auth"] in ("oauth", "api_key")
        assert e["url"].startswith("https://")
        if e.get("app"):
            assert e["app"] in catalog.APPS and e["auth"] == "oauth"
    cal = catalog.catalog_get("google_calendar")
    assert cal["app"] == "google"
    assert "calendar.events" in cal["scope"] and "calendar.acls" not in cal["scope"]


def test_static_setup_before_any_live_check(monkeypatch):
    assert setup_check.static_setup({"auth": "api_key"}) == "api_key"
    assert setup_check.static_setup({"auth": "oauth"}) == "one_click"
    assert setup_check.static_setup({"auth": "oauth", "app": "google"}) == "own_app"
    _google(monkeypatch)
    assert setup_check.static_setup({"auth": "oauth", "app": "google"}) == "platform"


@pytest.mark.parametrize(
    "dcr, app, platform, expect",
    [
        (True, None, False, "one_click"),
        (False, None, False, "own_app"),
        (False, "google", False, "own_app"),
        (False, "google", True, "platform"),
        (True, "google", False, "one_click"),  # a vendor that adds DCR stops needing an app
    ],
)
async def test_check_entry_reads_the_live_provider(monkeypatch, dcr, app, platform, expect):
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(FakeProvider(dcr=dcr)))
    if platform:
        _google(monkeypatch)
    entry = {"id": "x", "url": MCP, "auth": "oauth", **({"app": app} if app else {})}
    r = await setup_check.check_entry(entry)
    assert r["setup"] == expect and r["checked_ts"]


async def test_check_entry_without_metadata_is_unreachable(monkeypatch):
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(lambda r: httpx.Response(404)))
    r = await setup_check.check_entry({"id": "x", "url": MCP, "auth": "oauth"})
    assert r["setup"] == "unreachable" and "OAuth metadata" in r["detail"]


async def test_api_key_entries_are_never_probed(monkeypatch):
    def boom(req):
        raise AssertionError("no network for api_key entries")

    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(boom))
    r = await setup_check.check_entry({"id": "gh", "url": MCP, "auth": "api_key"})
    assert r["setup"] == "api_key"


async def test_annotate_prefers_the_live_result_and_tracks_the_platform_app(monkeypatch):
    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(FakeProvider(dcr=False)))
    entries = [
        {"id": "plain", "url": MCP, "auth": "oauth"},
        {"id": "goog", "url": MCP, "auth": "oauth", "app": "google"},
    ]
    before = {e["id"]: e for e in setup_check.annotate(entries)}
    assert before["plain"]["setup"] == "one_click" and before["plain"]["checked_ts"] is None
    assert setup_check.is_stale(entries)
    await setup_check.recheck(entries)
    assert not setup_check.is_stale(entries)
    after = {e["id"]: e for e in setup_check.annotate(entries)}
    assert after["plain"]["setup"] == "own_app"  # the static guess was wrong; live wins
    assert after["goog"]["setup"] == "own_app"
    # Setting the platform app takes effect without waiting for the next check.
    _google(monkeypatch)
    assert {e["id"]: e for e in setup_check.annotate(entries)}["goog"]["setup"] == "platform"
