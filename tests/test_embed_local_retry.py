"""Embedding backend flip is a cooldown, not a life sentence; the GPU pod is an
embed host while it is resident.

The old behaviour flipped a process to Google embeddings permanently on the first
local failure. On Railway that meant: the CPU sidecar was not running (its
installer had been failing silently), OLLAMA_HOST pointed at nothing, and every
tenant embedded on the platform's Google key for the life of the process — even
after the sidecar or the pod came up.
"""

from __future__ import annotations

import asyncio

import brain.model_router as mr
from brain.settings import settings


def _mk_router():
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._embed_backend = "ollama"
    r._embed_local_retry_at = 0.0
    r._embed_cache = __import__("collections").OrderedDict()
    return r


def test_pod_is_an_embed_host_only_when_resident(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://127.0.0.1:11500")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setitem(settings._data, "runpod_host", "https://pod.example/")
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 0)
    assert "https://pod.example/" not in mr.ModelRouter._embed_hosts()
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 1)
    assert mr.ModelRouter._embed_hosts() == [
        "http://127.0.0.1:11500",
        "https://pod.example/",
        "http://localhost:11434",
    ]
    # The 'off' sentinel is never a host.
    monkeypatch.setitem(settings._data, "runpod_host", "off")
    assert "off" not in mr.ModelRouter._embed_hosts()


def test_flip_to_google_is_a_cooldown_then_local_is_retried(monkeypatch):
    monkeypatch.setitem(settings._data, "embed_local_retry_s", 100.0)
    r = _mk_router()
    t = [5_000_000.0]
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    local_calls = []
    local_ok = [False]

    async def _ollama(text):
        local_calls.append(text)
        return [0.1] * mr.EMBEDDING_DIM if local_ok[0] else None

    async def _google(text):
        return [0.2] * mr.EMBEDDING_DIM

    monkeypatch.setattr(r, "_embed_ollama", _ollama)
    monkeypatch.setattr(r, "_embed_google", _google)

    assert asyncio.run(r.embed("a"))[0] == 0.2  # local down → google
    assert r._embed_backend == "google"
    assert asyncio.run(r.embed("b"))[0] == 0.2  # inside cooldown: no local attempt
    assert local_calls == ["a"]
    t[0] += 101.0
    local_ok[0] = True
    assert asyncio.run(r.embed("c"))[0] == 0.1  # cooldown over → local retried and wins
    assert r._embed_backend == "ollama"
    assert local_calls == ["a", "c"]


def test_zero_retry_keeps_the_permanent_flip(monkeypatch):
    monkeypatch.setitem(settings._data, "embed_local_retry_s", 0.0)
    r = _mk_router()
    t = [5_000_000.0]
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    calls = []

    async def _ollama(text):
        calls.append(text)
        return None

    async def _google(text):
        return [0.2] * mr.EMBEDDING_DIM

    monkeypatch.setattr(r, "_embed_ollama", _ollama)
    monkeypatch.setattr(r, "_embed_google", _google)
    asyncio.run(r.embed("a"))
    t[0] += 10_000.0
    asyncio.run(r.embed("b"))
    assert calls == ["a"]
