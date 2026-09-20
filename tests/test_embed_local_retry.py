"""An unreachable embedding chain is a cooldown, not a life sentence — and never a
second vector space. The GPU pod is an embed host while it is resident.

History: the process used to flip to Google embeddings permanently on the first
local failure. On Railway that meant the CPU sidecar (whose installer was failing
silently) never got a second chance, every tenant embedded on the platform's
Google key for the life of the process, and — worse than the cost — those rows
landed in Google's vector space alongside nomic's, where neither can be compared
to the other. Since migration 044 there is ONE model: when the local chain is
down the embed is skipped, the row is stored unembedded, and the repair script
fills it in later.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

import brain.model_router as mr
from brain.settings import settings


def _mk_router():
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._embed_local_retry_at = 0.0
    r._embed_down_logged = False
    r._embed_cache = OrderedDict()
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


def test_chain_down_is_a_cooldown_then_local_is_retried(monkeypatch):
    monkeypatch.setitem(settings._data, "embed_local_retry_s", 100.0)
    r = _mk_router()
    t = [5_000_000.0]
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    local_calls = []
    local_ok = [False]

    async def _ollama(text):
        local_calls.append(text)
        return [0.1] * mr.EMBEDDING_DIM if local_ok[0] else None

    monkeypatch.setattr(r, "_embed_ollama", _ollama)

    assert asyncio.run(r.embed("a")) is None  # chain down → no vector, no cloud
    assert asyncio.run(r.embed("b")) is None  # inside cooldown: skipped fast
    assert local_calls == ["a"]  # the per-host timeout is paid once, not per call
    t[0] += 101.0
    local_ok[0] = True
    assert asyncio.run(r.embed("c"))[0] == 0.1  # cooldown over → local retried and wins
    assert r._embed_local_retry_at == 0.0
    assert local_calls == ["a", "c"]


def test_zero_cooldown_retries_the_chain_on_every_call(monkeypatch):
    """embed_local_retry_s = 0 means no skip window: each embed tries the chain."""
    monkeypatch.setitem(settings._data, "embed_local_retry_s", 0.0)
    r = _mk_router()
    t = [5_000_000.0]
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    calls = []

    async def _ollama(text):
        calls.append(text)
        return None

    monkeypatch.setattr(r, "_embed_ollama", _ollama)
    assert asyncio.run(r.embed("a")) is None
    assert asyncio.run(r.embed("b")) is None
    assert calls == ["a", "b"]


def test_there_is_no_cloud_embedding_path():
    """The single-space rule is structural, not a setting: no Google embed method,
    no second model constant. Google remains a GENERATION provider."""
    assert not hasattr(mr.ModelRouter, "_embed_google")
    assert not hasattr(mr, "GOOGLE_EMBED_MODEL")
    assert mr.ModelRouter.embed_model_name() == mr.OLLAMA_EMBED_MODEL
