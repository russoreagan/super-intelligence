"""Embedding fallback flap (audit 2026-09-13).

Both tenants logged "Ollama embedding service unreachable — switching to Google"
minutes after boot with the gateway's CPU sidecar up, and a keyless tenant then
logged "Google embedding API failed — GOOGLE_API_KEY not set" on EVERY call for
the whole 10 min cooldown: no memory search, no diagnosable cause. Now: a Google
failure retries the local chain on the next call (a 30 s probe cadence once both
are down), the flip names the failing host and exception once, the Google warning
is rate-limited and says when the tenant simply has no key, a rejected Google key
arms the provider breaker, and a keepalive keeps the sidecar warm.
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


class _Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


def _mk_router(monkeypatch, t):
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._embed_backend = "ollama"
    r._embed_local_retry_at = 0.0
    r._embed_cache = OrderedDict()
    r._provider_outage = {}
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://sidecar:11500")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 0)
    monkeypatch.setitem(settings._data, "embed_local_retry_s", 600.0)
    monkeypatch.setitem(settings._data, "embed_sidecar_keepalive_s", 60.0)
    return r


def test_google_none_retries_local_next_call_then_probes_every_30s(monkeypatch):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    local_calls: list[str] = []
    local_ok = [False]

    async def _ollama(text):
        local_calls.append(text)
        return VEC if local_ok[0] else None

    async def _google(text):
        return None

    monkeypatch.setattr(r, "_embed_ollama", _ollama)
    monkeypatch.setattr(r, "_embed_google", _google)

    assert asyncio.run(r.embed("a")) is None
    # First Google failure after the flip: the cooldown is abandoned immediately.
    assert r._embed_backend == "ollama" and r._embed_local_retry_at == 0.0
    assert asyncio.run(r.embed("b")) is None
    assert local_calls == ["a", "b"]
    # Both down now: local is probed on a 30 s cadence, not on every call.
    assert r._embed_backend == "google"
    assert r._embed_local_retry_at == t[0] + mr._EMBED_BOTH_DOWN_RETRY_S
    assert asyncio.run(r.embed("c")) is None
    assert local_calls == ["a", "b"]
    t[0] += mr._EMBED_BOTH_DOWN_RETRY_S + 1
    local_ok[0] = True
    assert asyncio.run(r.embed("d")) == VEC
    assert local_calls == ["a", "b", "d"]
    assert r._embed_backend == "ollama" and r._embed_both_down is False


def test_sidecar_back_after_google_failure_is_used_on_the_next_call(monkeypatch):
    """The prod case: a transient sidecar timeout at boot flipped a keyless tenant
    to Google for 10 min. Now the very next embed goes local again."""
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    seq = iter([None, VEC])

    async def _ollama(text):
        return next(seq)

    async def _google(text):
        return None

    monkeypatch.setattr(r, "_embed_ollama", _ollama)
    monkeypatch.setattr(r, "_embed_google", _google)
    assert asyncio.run(r.embed("a")) is None
    assert asyncio.run(r.embed("b")) == VEC
    assert r._embed_backend == "ollama"


def test_flip_logs_the_failing_host_and_reason_once(monkeypatch, caplog):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    http = _Http(lambda url: ConnectionError("boom"))
    monkeypatch.setattr(r, "_get_http", lambda: http)

    async def _google(text):
        return [0.2] * mr.EMBEDDING_DIM

    monkeypatch.setattr(r, "_embed_google", _google)
    caplog.set_level(logging.INFO, logger="brain.model_router")
    asyncio.run(r.embed("a"))
    asyncio.run(r.embed("b"))  # inside the cooldown: no local attempt, no second flip
    flips = [rec for rec in caplog.records if "switching to Google" in rec.getMessage()]
    assert len(flips) == 1
    msg = flips[0].getMessage()
    assert "http://sidecar:11500" in msg and "ConnectionError" in msg and "boom" in msg
    assert flips[0].levelno == logging.INFO
    # Every host in the chain was tried and named.
    assert "http://localhost:11434" in msg
    assert http.calls == [
        "http://sidecar:11500/api/embeddings",
        "http://localhost:11434/api/embeddings",
    ]


def test_google_warning_is_rate_limited_and_names_the_missing_key(monkeypatch, caplog):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)

    def _no_key():
        raise RuntimeError("GOOGLE_API_KEY not set — Gemini/vision unavailable")

    monkeypatch.setattr(r, "_get_google", _no_key)
    caplog.set_level(logging.WARNING, logger="brain.model_router")
    for _ in range(5):
        assert asyncio.run(r._embed_google("x")) is None
    warns = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "no GOOGLE_API_KEY" in warns[0].getMessage()
    assert "local embeddings" in warns[0].getMessage()
    # A missing key is configuration, not a provider outage: no breaker.
    assert r.provider_blocked("google") is None
    t[0] += mr._EMBED_GOOGLE_WARN_S + 1
    asyncio.run(r._embed_google("y"))
    assert len([rec for rec in caplog.records if rec.levelno == logging.WARNING]) == 2


def test_flip_resets_the_google_warning_cadence(monkeypatch, caplog):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    r._embed_google_warned_at = t[0]  # warned just now

    async def _ollama(text):
        return None

    def _no_key():
        raise RuntimeError("GOOGLE_API_KEY not set")

    monkeypatch.setattr(r, "_embed_ollama", _ollama)
    monkeypatch.setattr(r, "_get_google", _no_key)
    caplog.set_level(logging.WARNING, logger="brain.model_router")
    asyncio.run(r.embed("a"))
    assert any("no GOOGLE_API_KEY" in rec.getMessage() for rec in caplog.records)


class _Embedding:
    def __init__(self, values):
        self.values = values


class _GoogleClient:
    def __init__(self, exc=None, values=None):
        self._exc, self._values = exc, values
        self.aio = self
        self.models = self

    async def embed_content(self, **kw):
        if self._exc:
            raise self._exc
        return type("R", (), {"embeddings": [_Embedding(self._values)]})()


def test_rejected_google_key_arms_the_breaker_and_success_clears_it(monkeypatch):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    monkeypatch.setattr(r, "_get_google", lambda: _GoogleClient(exc=_Err("payment required", 402)))
    assert asyncio.run(r._embed_google("x")) is None
    o = r.provider_blocked("google")
    assert o and o["kind"] == "billing"
    monkeypatch.setattr(r, "_get_google", lambda: _GoogleClient(values=VEC))
    assert asyncio.run(r._embed_google("y")) == VEC
    assert r.provider_blocked("google") is None
    # A retryable error (timeout, 5xx) never arms it.
    r2 = _mk_router(monkeypatch, t)
    monkeypatch.setattr(r2, "_get_google", lambda: _GoogleClient(exc=TimeoutError("slow")))
    assert asyncio.run(r2._embed_google("x")) is None
    assert r2.provider_blocked("google") is None


def test_sidecar_keepalive_warms_only_the_sidecar_and_ends_the_cooldown(monkeypatch):
    t = [1_000_000.0]
    r = _mk_router(monkeypatch, t)
    http = _Http(lambda url: _Resp(vec=VEC))
    monkeypatch.setattr(r, "_get_http", lambda: http)
    r._embed_backend = "google"
    r._embed_local_retry_at = t[0] + 600.0
    r._embed_both_down = True
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is True
    assert http.calls == ["http://sidecar:11500/api/embeddings"]
    assert r._embed_backend == "ollama"
    assert r._embed_local_retry_at == 0.0 and r._embed_both_down is False
    # Off switch and unset host: no request at all.
    monkeypatch.setitem(settings._data, "embed_sidecar_keepalive_s", 0)
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is False
    monkeypatch.setitem(settings._data, "embed_sidecar_keepalive_s", 60)
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "")
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is False
    assert len(http.calls) == 1
    # A dead sidecar leaves the state alone.
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://sidecar:11500")
    monkeypatch.setattr(r, "_get_http", lambda: _Http(lambda url: ConnectionError("down")))
    r._embed_backend = "google"
    assert asyncio.run(r.embed_sidecar_keepalive_once()) is False
    assert r._embed_backend == "google"


def test_keepalive_setting_is_declared():
    from brain.settings import DEFAULTS

    assert DEFAULTS["embed_sidecar_keepalive_s"] == 60.0
