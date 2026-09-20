"""Embedding outage behaviour (audit 2026-09-13, reworked 2026-09-20).

Both tenants used to log "switching to Google embeddings" minutes after boot with
the gateway's CPU sidecar up, and a keyless tenant then logged a Google failure on
EVERY call for the whole cooldown: no memory search, no diagnosable cause. The
cloud fallback is gone (one vector space — migration 044); what remains must still
be diagnosable and must not cost a timeout per call: the outage names the failing
host and reason ONCE, recovery says so, and the keepalive ends the cooldown early.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

import brain.model_router as mr
from brain.settings import settings

VEC = [0.1] * mr.EMBEDDING_DIM


class _Resp:
    def __init__(self, vec=None, exc=None):
        self._vec, self._exc = vec, exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return {"embedding": self._vec}


class _Http:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append(url)
        out = self.handler(url)
        if isinstance(out, BaseException):
            raise out
        return out


def _mk_router(monkeypatch, t):
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._embed_local_retry_at = 0.0
    r._embed_down_logged = False
    r._embed_cache = OrderedDict()
    r._provider_outage = {}
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://sidecar:11500")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 0)
    monkeypatch.setitem(settings._data, "embed_local_retry_s", 600.0)
    monkeypatch.setitem(settings._data, "embed_sidecar_keepalive_s", 60.0)
    return r


def test_outage_names_the_failing_host_and_reason_once(monkeypatch, caplog):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    monkeypatch.setattr(r, "_get_http", lambda: _Http(lambda url: ConnectionError("refused")))
    with caplog.at_level(logging.WARNING, logger="brain.model_router"):
        assert asyncio.run(r.embed("a")) is None
        t[0] += 601.0
        assert asyncio.run(r.embed("b")) is None
    outage = [rec for rec in caplog.records if "Embedding chain unreachable" in rec.getMessage()]
    assert len(outage) == 1, "one line per outage, not per call"
    msg = outage[0].getMessage()
    assert "sidecar:11500" in msg and "ConnectionError" in msg
    assert mr.OLLAMA_EMBED_MODEL in msg  # says what to pull to fix it


def test_recovery_is_logged_and_clears_the_cooldown(monkeypatch, caplog):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    up = [False]
    monkeypatch.setattr(
        r,
        "_get_http",
        lambda: _Http(lambda url: _Resp(vec=VEC) if up[0] else ConnectionError("down")),
    )
    with caplog.at_level(logging.INFO, logger="brain.model_router"):
        assert asyncio.run(r.embed("a")) is None
        assert r._embed_local_retry_at == t[0] + 600.0
        t[0] += 601.0
        up[0] = True
        assert asyncio.run(r.embed("b")) == VEC
    assert r._embed_local_retry_at == 0.0
    assert any("reachable again" in rec.getMessage() for rec in caplog.records)
    # ...and the next outage logs again (the once-per-outage latch reset).
    assert r._embed_down_logged is False


def test_sidecar_keepalive_warms_only_the_sidecar_and_ends_the_cooldown(monkeypatch):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    http = _Http(lambda url: _Resp(vec=VEC))
    monkeypatch.setattr(r, "_get_http", lambda: http)
    r._embed_local_retry_at = t[0] + 600.0
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is True
    assert http.calls == ["http://sidecar:11500/api/embeddings"]  # never the pod
    assert r._embed_local_retry_at == 0.0
    # Off switch and unset host: no request at all.
    monkeypatch.setitem(settings._data, "embed_sidecar_keepalive_s", 0)
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is False
    monkeypatch.setitem(settings._data, "embed_sidecar_keepalive_s", 60)
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "")
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is False
    assert len(http.calls) == 1
    # A dead sidecar leaves the cooldown alone.
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://sidecar:11500")
    monkeypatch.setattr(r, "_get_http", lambda: _Http(lambda url: ConnectionError("down")))
    r._embed_local_retry_at = t[0] + 600.0
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is False
    assert r._embed_local_retry_at == t[0] + 600.0


def test_keepalive_setting_is_declared():
    from brain.settings import DEFAULTS

    assert DEFAULTS["embed_sidecar_keepalive_s"] == 60.0
