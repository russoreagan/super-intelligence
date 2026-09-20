"""
OLLAMA_EMBED_HOST split — embeddings try the dedicated CPU host before the
general Ollama host (the GPU pod on hosted). The unconfigured case must be
byte-identical to the old single-host behavior; the configured case must fall
through embed-host → general host → (caller falls back to Google).
"""

from __future__ import annotations

import asyncio

import brain.model_router as mr


def _mk_router():
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    return r


def test_embed_hosts_unconfigured_is_single_host(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://pod:11434")
    assert mr.ModelRouter._embed_hosts() == ["http://pod:11434"]


def test_embed_hosts_dedicated_first_then_general(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://127.0.0.1:11500")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://pod:11434")
    assert mr.ModelRouter._embed_hosts() == [
        "http://127.0.0.1:11500",
        "http://pod:11434",
    ]


def test_embed_hosts_deduped_when_same(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://localhost:11434")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://localhost:11434")
    assert mr.ModelRouter._embed_hosts() == ["http://localhost:11434"]


def test_embed_ollama_falls_through_to_second_host(monkeypatch):
    """First host down → second host serves the vector; the caller never flips
    to Google when ANY Ollama host works."""
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://dead:1")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://alive:2")
    router = _mk_router()

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"embedding": [0.1] * mr.EMBEDDING_DIM}

    class _Http:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kw):
            self.calls.append(url)
            if url.startswith("http://dead"):
                raise ConnectionError("down")
            return _Resp()

    http = _Http()
    monkeypatch.setattr(router, "_get_http", lambda: http)
    vec = asyncio.run(router._embed_ollama("hello"))
    assert vec is not None and len(vec) == mr.EMBEDDING_DIM
    assert http.calls == [
        "http://dead:1/api/embeddings",
        "http://alive:2/api/embeddings",
    ]


def test_embed_ollama_none_when_all_hosts_fail(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://dead:1")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://dead:2")
    router = _mk_router()

    class _Http:
        async def post(self, url, **kw):
            raise ConnectionError("down")

    monkeypatch.setattr(router, "_get_http", lambda: _Http())
    assert asyncio.run(router._embed_ollama("hello")) is None


# ── Sidecar thread cap (2026-09-19) ────────────────────────────────────────────
# Unbounded, Ollama sized the CPU runner to the host's cores and the Railway
# sidecar averaged ~11 vCPU. The cap rides as `num_thread` on sidecar requests only.


def test_payload_caps_threads_on_embed_host_only(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://127.0.0.1:11500")
    monkeypatch.setattr(mr, "OLLAMA_EMBED_NUM_THREAD", 2)
    assert mr._embed_payload("http://127.0.0.1:11500", "hi") == {
        "model": mr.OLLAMA_EMBED_MODEL,
        "prompt": "hi",
        "options": {"num_thread": 2},
    }
    # The GPU pod and local Ollama keep their own thread defaults.
    assert "options" not in mr._embed_payload("https://pod-11434.proxy.runpod.net", "hi")
    assert "options" not in mr._embed_payload("http://localhost:11434", "hi")


def test_payload_unchanged_without_cap_or_embed_host(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://127.0.0.1:11500")
    monkeypatch.setattr(mr, "OLLAMA_EMBED_NUM_THREAD", 0)
    assert "options" not in mr._embed_payload("http://127.0.0.1:11500", "hi")
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "")
    monkeypatch.setattr(mr, "OLLAMA_EMBED_NUM_THREAD", 2)
    assert "options" not in mr._embed_payload("", "hi")


def test_embed_and_keepalive_send_the_cap_to_the_sidecar(monkeypatch):
    monkeypatch.setattr(mr, "OLLAMA_EMBED_HOST", "http://127.0.0.1:11500")
    monkeypatch.setattr(mr, "OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setattr(mr, "OLLAMA_EMBED_NUM_THREAD", 2)
    from brain.settings import settings as _settings

    monkeypatch.setitem(_settings._data, "embed_sidecar_keepalive_s", 60.0)
    router = _mk_router()

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embedding": [0.1] * mr.EMBEDDING_DIM}

    bodies = []

    class _Http:
        async def post(self, url, **kw):
            bodies.append((url, kw["json"]))
            return _Resp()

    monkeypatch.setattr(router, "_get_http", lambda: _Http())
    assert asyncio.run(router._embed_ollama("hello")) is not None
    assert asyncio.run(router.embed_sidecar_keepalive_once()) is True
    assert [b.get("options") for _, b in bodies] == [{"num_thread": 2}, {"num_thread": 2}]
    assert all(u == "http://127.0.0.1:11500/api/embeddings" for u, _ in bodies)
