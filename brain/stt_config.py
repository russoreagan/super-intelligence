"""
Shared STT keyword-boost configuration for both live-transcription paths.

Two modules open Deepgram live sessions: brain/streaming_mic.py (server mic,
local mode) and brain/api/stt_live.py (engine-API WebSocket transport). Both
read BRAIN_STT_KEYWORDS — this module owns the default so the two paths can
never drift apart again (they did: the API path silently defaulted to no
boosts while the mic path shipped a populated list).

BRAIN_STT_KEYWORDS is a comma-separated list of word:boost pairs, e.g.
'claude:5,chloé:3,ableton:5'. Both nova-3 (Listen v1, mic path) and Flux
(Listen v2, API path) take whole-phrase `keyterm` entries with no boost
weight, so the :boost suffix is stripped before connecting; it is kept in
the env format for compatibility with older models' `keywords` parameter.
"""

from __future__ import annotations

import os

DEFAULT_STT_KEYWORDS = (
    "claude:5,chloé:3,ableton:5,imessage:3,github:3,ollama:3,deepgram:3,elevenlabs:3"
)


def stt_keywords() -> list[str]:
    """Resolve BRAIN_STT_KEYWORDS into a list of 'word:boost' entries."""
    raw = os.environ.get("BRAIN_STT_KEYWORDS", DEFAULT_STT_KEYWORDS)
    return [k.strip() for k in raw.split(",") if k.strip()]


def deepgram_privacy_params() -> dict[str, str]:
    """Query params that keep Deepgram from retaining our audio.

    Deepgram enrols every request in its Model Improvement Program unless it
    sends `mip_opt_out=true`: the audio and transcript are kept and may be used
    to train models, possibly outside our region. Opted out, Deepgram keeps
    nothing once it has responded. Every Deepgram call sends these.
    BRAIN_DEEPGRAM_MIP_OPT_OUT=0 opts back in (Deepgram prices the two differently).
    """
    if os.environ.get("BRAIN_DEEPGRAM_MIP_OPT_OUT", "1") == "0":
        return {}
    return {"mip_opt_out": "true"}


def stt_keyterms() -> list[str]:
    """Keyword list with :boost suffixes stripped, for the `keyterm` param
    (nova-3 on Listen v1, Flux on Listen v2)."""
    return [k.split(":")[0] for k in stt_keywords()]
