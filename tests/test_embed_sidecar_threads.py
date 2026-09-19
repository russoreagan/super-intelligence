"""The gateway's CPU embed sidecar runs one slot and tells tenants its thread cap.

Unbounded, Ollama sized the sidecar's CPU runner to the host (48 cores visible on
Railway, 32 in quota) and it averaged ~11 vCPU for a few short embeds a turn.
"""

from __future__ import annotations

import os

import brain.gateway.server as gw


def test_sidecar_one_slot_and_exports_thread_cap(monkeypatch, tmp_path):
    fake = tmp_path / "ollama"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BRAIN_OLLAMA_BIN", str(fake))
    monkeypatch.delenv("OLLAMA_EMBED_HOST", raising=False)
    monkeypatch.delenv("OLLAMA_EMBED_NUM_THREAD", raising=False)
    monkeypatch.setattr(gw, "_EMBED_SIDECAR", True)
    monkeypatch.setattr(gw, "_EMBED_SIDECAR_THREADS", 3)

    seen = {}

    class _Proc:
        pid = 4242

    def _popen(argv, env=None, **kw):
        seen["argv"], seen["env"] = argv, env
        return _Proc()

    class _Thread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(gw.subprocess, "Popen", _popen)
    monkeypatch.setattr(gw.threading, "Thread", _Thread)
    try:
        assert gw._start_embed_sidecar() is not None
        assert seen["argv"] == [str(fake), "serve"]
        assert seen["env"]["OLLAMA_NUM_PARALLEL"] == "1"
        # Spawned tenants inherit os.environ: both the host and the cap reach them.
        assert os.environ["OLLAMA_EMBED_HOST"].endswith(f":{gw._EMBED_SIDECAR_PORT}")
        assert os.environ["OLLAMA_EMBED_NUM_THREAD"] == "3"
    finally:
        os.environ.pop("OLLAMA_EMBED_HOST", None)
        os.environ.pop("OLLAMA_EMBED_NUM_THREAD", None)
