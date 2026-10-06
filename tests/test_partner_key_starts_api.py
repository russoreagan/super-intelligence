"""A brain only starts its engine API at boot when its org already has a key. A
partner org's FIRST key is minted while the brain is running, so /v1 calls with it
hit a closed port until a respawn (prod 2026-10-06, a fresh test org). Minting a
key from the console must start the API in the running brain."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain.api import auth as _auth  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402


@pytest.fixture
def console(monkeypatch):
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: True)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(
        _auth, "mint_partner_key", lambda pid, label=None, **kw: {"key": "sk_x", "partner": pid}
    )
    started: list[int] = []

    async def _start():
        started.append(1)

    from brain.ui.server import UIServer

    server = UIServer(emitter_queue=asyncio.Queue(), on_partner_key_minted=_start)
    return TestClient(server._build_app()), started


def test_minting_a_key_starts_the_engine_api(console):
    client, started = console
    r = client.post("/partner_keys", json={"partner_id": "acme"})
    assert r.status_code == 200 and r.json()["key"] == "sk_x"
    assert started == [1]


def test_a_failed_mint_does_not_start_it(console, monkeypatch):
    client, started = console

    def _boom(*a, **kw):
        raise ValueError("bad partner")

    monkeypatch.setattr(_auth, "mint_partner_key", _boom)
    assert client.post("/partner_keys", json={"partner_id": "acme"}).status_code == 400
    assert started == []


def test_ensure_api_started_is_idempotent_and_serialized():
    from brain.session_setup import _SetupMixin

    calls: list[int] = []

    class _S(_SetupMixin):
        def __init__(self):
            self._api_server = None

        async def _setup_api(self):
            calls.append(1)
            await asyncio.sleep(0.01)
            self._api_server = object()

    s = _S()

    async def run():
        await asyncio.gather(s._ensure_api_started(), s._ensure_api_started())
        await s._ensure_api_started()

    asyncio.run(run())
    assert calls == [1]
