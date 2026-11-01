"""
Deepgram must not keep our users' audio.

Deepgram enrols every request in its Model Improvement Program (audio and
transcript retained, possibly used for training, possibly out of region) unless
the request sends `mip_opt_out=true`. None of our calls sent it. Every live
Deepgram call now goes through brain.stt_config.deepgram_privacy_params().
"""

import contextlib
from pathlib import Path

import pytest

from brain.stt_config import deepgram_privacy_params

REPO = Path(__file__).resolve().parents[1]
# Every module that opens a Deepgram transcription with the installed SDK or REST.
# (brain/pns.py's PrerecordedOptions path targets an SDK API that is no longer
# installed, so it cannot run.)
DEEPGRAM_CALLERS = [
    REPO / "brain" / "streaming_mic.py",
    REPO / "brain" / "api" / "stt_live.py",
    REPO / "brain" / "api" / "audio.py",
    REPO / "brain" / "ui" / "server.py",
]


def test_opted_out_by_default(monkeypatch):
    monkeypatch.delenv("BRAIN_DEEPGRAM_MIP_OPT_OUT", raising=False)
    assert deepgram_privacy_params() == {"mip_opt_out": "true"}


def test_kill_switch_opts_back_in(monkeypatch):
    monkeypatch.setenv("BRAIN_DEEPGRAM_MIP_OPT_OUT", "0")
    assert deepgram_privacy_params() == {}


@pytest.mark.parametrize("path", DEEPGRAM_CALLERS, ids=lambda p: p.name)
def test_every_deepgram_caller_sends_the_opt_out(path):
    code = "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "**deepgram_privacy_params()" in code, (
        f"{path.name} opens a Deepgram request without mip_opt_out — Deepgram "
        f"would retain the audio"
    )


async def test_rest_transcription_sends_the_opt_out(monkeypatch):
    import httpx

    from brain.api import audio

    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.delenv("BRAIN_DEEPGRAM_MIP_OPT_OUT", raising=False)
    seen: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]}}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, params=None, **kw):
            seen.update(params or {})
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    # Response parsing is not under test; the request params are.
    with contextlib.suppress(Exception):
        await audio._transcribe_deepgram(b"\0\0", mimetype="audio/wav", diarize=False, model=None)
    assert seen.get("mip_opt_out") == "true"
