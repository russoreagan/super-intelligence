"""
Audio layer for the engine API.

Two concerns, deliberately separable (see the design notes in the API plan):

  1. Affect view (always on, pure, no network) — the turn pipeline returns text
     that still carries ``[mood:X]...[/mood]`` markup + bare reaction tags (the
     TTS-ready form). Partners need clean display text plus the *structured*
     affect that drives prosody, so they can render captions, visualise mood, or
     drive their own TTS — while ours stays strictly better. ``affect_view``
     derives both, reusing the PNS chunking/tag helpers so the API's segments are
     identical to what the brain's own TTS path produces (single source of truth).

  2. Synthesis / transcription (optional, partner-gated, hits paid third parties)
     — ``synthesize`` runs ElevenLabs/OpenAI TTS per mood-segmented chunk and
     ``transcribe`` wraps Deepgram. These are the bodies behind ``POST /v1/tts``
     and ``POST /v1/stt``; the router injects them so it stays decoupled/testable.

Field shapes are chosen for forward-compatibility with a later realtime
WebSocket transport: every chunk carries ``seq``; STT results are a list of
``{transcript, is_final}`` segments (phase 1 emits a single final entry). A
realtime variant reuses these shapes verbatim — it's a transport swap, not a
redesign.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Partner-facing format → ElevenLabs output_format + (pcm sample rate | None).
# The sample rate lets us report duration for raw PCM (bytes / (2 * rate));
# compressed formats report duration=None.
_OUTPUT_FORMATS: dict[str, tuple[str, int | None]] = {
    "mp3_44100_128": ("mp3_44100_128", None),
    "mp3_22050_32": ("mp3_22050_32", None),
    "pcm_16000": ("pcm_16000", 16000),
    "pcm_22050": ("pcm_22050", 22050),
    "pcm_24000": ("pcm_24000", 24000),
    "opus_48000": ("opus_48000_64", None),
}
_DEFAULT_FORMAT = "mp3_44100_128"

# Partner-facing model alias → ElevenLabs model_id. "flash" drives prosody via
# VoiceSettings (stability/style/speed); "v3" drives it via inline audio tags.
_MODEL_ALIASES = {"flash": "eleven_flash_v2_5", "v3": "eleven_v3"}


class AudioError(Exception):
    """Raised for bad audio requests (unknown format/model, missing provider key).

    ``status`` mirrors the HTTP code the router should surface (400 client error,
    503 when a provider key isn't configured)."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ── 1. Affect view (pure) ─────────────────────────────────────────────────────
def affect_view(raw_text: str, affect: dict | None) -> tuple[str, dict]:
    """Return ``(display_text, affect_block)`` for one turn's raw response.

    ``display_text`` has all markup/reaction tags stripped (safe for chat + the
    partner's own captions). ``affect_block`` is::

        {
          "base_tag": "[warm]" | None,        # whole-utterance inflection
          "segments": [                        # 1:1 with the TTS chunk plan
            {"seq": 0, "text": "...", "mood": "angry" | None, "tag": "[angrily]" | None},
            ...
          ],
          "markup": "...",                     # present only when it carries mood spans
        }

    Pure: no network, no brain state. Imported lazily so the router never pulls
    PNS in at module load."""
    from brain.emotion_presets import get_tag
    from brain.pns import PNS

    raw_text = raw_text or ""
    affect = affect or {}

    display = PNS._strip_all_tags(raw_text)
    base_tag = PNS._v3_audio_tag_from_affect(affect)

    segments: list[dict] = []
    for i, (chunk, mood) in enumerate(PNS._mood_segmented_chunks(raw_text)):
        tag = (get_tag(mood) if mood else None) or base_tag
        segments.append({"seq": i, "text": chunk, "mood": mood, "tag": tag})

    block: dict = {"base_tag": base_tag, "segments": segments}
    # Only surface the raw markup when it actually differs from display text
    # (i.e. it carries mood spans / reaction tags worth handing back).
    if raw_text.strip() and raw_text.strip() != display:
        block["markup"] = raw_text.strip()
    return display, block


# ── 2. TTS synthesis ──────────────────────────────────────────────────────────
def _resolve_format(fmt: str | None) -> tuple[str, int | None]:
    key = (fmt or _DEFAULT_FORMAT).strip()
    if key not in _OUTPUT_FORMATS:
        raise AudioError(
            f"unknown audio format {key!r}; supported: {sorted(_OUTPUT_FORMATS)}"
        )
    return _OUTPUT_FORMATS[key]


def _resolve_model(model: str | None) -> str:
    if not model:
        return os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5") or "eleven_flash_v2_5"
    return _MODEL_ALIASES.get(model.strip().lower(), model.strip())


async def synthesize(
    text: str,
    *,
    affect: dict | None = None,
    voice_id: str | None = None,
    model: str | None = None,
    fmt: str | None = None,
    provider: str | None = None,
) -> dict:
    """Synthesise ``text`` to a single (concatenated) clip plus per-segment audio.

    One synth call per mood-segmented chunk so each ``[mood:X]`` span gets its own
    prosody — the same chunk plan the brain's own TTS path uses. ``text`` may
    contain ``[mood:X]...[/mood]`` markup. Returns::

        {
          "format": "mp3_44100_128", "voice_id": "...", "model": "eleven_flash_v2_5",
          "data": "<base64 concatenated clip>",
          "duration_s": 4.2 | None,            # pcm only
          "segments": [
            {"seq": 0, "text": "...", "mood": "angry" | None,
             "voice_settings": {...} | None, "data": "<base64>"},
            ...
          ],
        }
    """
    import base64

    meta: dict = {}
    segments: list[dict] = []
    blob = bytearray()
    async for kind, payload in _segment_stream(
        text, affect=affect, voice_id=voice_id, model=model, fmt=fmt, provider=provider
    ):
        if kind == "meta":
            meta = payload
        else:
            blob.extend(payload.pop("_bytes", b""))
            segments.append(payload)

    rate = meta.get("sample_rate")
    duration = round(len(blob) / (2 * rate), 3) if rate else None
    return {
        "format": meta.get("format"),
        "voice_id": meta.get("voice_id"),
        "model": meta.get("model"),
        "data": base64.b64encode(bytes(blob)).decode("ascii"),
        "duration_s": duration,
        "chars": sum(len(s.get("text") or "") for s in segments),  # provider-billed unit
        "segments": segments,
    }


async def synthesize_stream(
    text: str,
    *,
    affect: dict | None = None,
    voice_id: str | None = None,
    model: str | None = None,
    fmt: str | None = None,
    provider: str | None = None,
):
    """Stream synthesis as ``(kind, payload)`` events for the SSE turn path:

      ("meta",  {"format","voice_id","model","sample_rate"})
      ("chunk", {"seq","text","mood","voice_settings"?,"data"})   # one per segment
      ("end",   {"chunks","duration_s"})

    Audio is produced chunk-by-chunk, so a long reply's first sentence reaches the
    client while later sentences are still synthesising. Same shape a future
    realtime transport reuses — only the framing changes."""
    count = 0
    total_bytes = 0
    total_chars = 0
    rate = None
    async for kind, payload in _segment_stream(
        text, affect=affect, voice_id=voice_id, model=model, fmt=fmt, provider=provider
    ):
        if kind == "meta":
            rate = payload.get("sample_rate")
            yield "meta", payload
        else:
            total_bytes += len(payload.pop("_bytes", b""))
            total_chars += len(payload.get("text") or "")
            count += 1
            yield "chunk", payload
    yield "end", {
        "chunks": count,
        "duration_s": round(total_bytes / (2 * rate), 3) if rate else None,
        "chars": total_chars,  # provider-billed unit (for quota accounting)
    }


async def _segment_stream(
    text: str,
    *,
    affect: dict | None,
    voice_id: str | None,
    model: str | None,
    fmt: str | None,
    provider: str | None,
):
    """Shared core: yield ('meta', {...}) then ('chunk', seg) per mood-segmented
    chunk. Each seg carries ``_bytes`` (raw) + ``data`` (base64); callers drop
    ``_bytes`` after consuming. Provider/format/voice resolved once up front."""
    from brain.pns import PNS

    text = (text or "").strip()
    if not text:
        raise AudioError("text (non-empty string) is required")

    el_format, pcm_rate = _resolve_format(fmt)
    provider = (provider or os.environ.get("TTS_PROVIDER") or "elevenlabs").strip().lower()
    affect = affect or {}
    chunks = PNS._mood_segmented_chunks(text) or [(text, None)]

    if provider == "openai":
        # OpenAI TTS is 24 kHz PCM resampled to 22050 by _pcm_resample.
        yield "meta", {
            "format": "pcm_22050",
            "voice_id": os.environ.get("OPENAI_TTS_VOICE", "alloy"),
            "model": os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            "sample_rate": 22050,
        }
        async for seg in _iter_openai(chunks, affect):
            yield "chunk", seg
        return

    resolved_model = _resolve_model(model)
    resolved_voice = voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"
    yield "meta", {
        "format": el_format,
        "voice_id": resolved_voice,
        "model": resolved_model,
        "sample_rate": pcm_rate,
    }
    async for seg in _iter_elevenlabs(chunks, affect, resolved_voice, resolved_model, el_format):
        yield "chunk", seg


async def _iter_elevenlabs(
    chunks: list[tuple[str, str | None]],
    affect: dict,
    voice_id: str,
    model_id: str,
    output_format: str,
):
    import base64
    import contextlib

    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise AudioError("ELEVENLABS_API_KEY is not configured", status=503)

    from elevenlabs import AsyncElevenLabs
    from elevenlabs.types import VoiceSettings

    from brain.pns import PNS

    client = AsyncElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    base_params = PNS._voice_params_from_affect(affect)
    is_v3 = model_id == "eleven_v3"

    for i, (chunk, mood) in enumerate(chunks):
        vs = PNS._voice_settings_from_emotion(mood, base_params, VoiceSettings=VoiceSettings)
        if is_v3:
            # v3 ignores style/speed (422-rejects them); it honours only stability.
            vs = VoiceSettings(
                stability=PNS._snap_v3_stability(base_params["stability"]),
                similarity_boost=0.80,
                use_speaker_boost=True,
            )
        audio = bytearray()
        stream = client.text_to_speech.stream(
            text=chunk,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
            voice_settings=vs,
        )
        try:
            async for part in stream:
                if part:
                    audio.extend(part)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):  # best-effort connection release
                    await aclose()
        yield {
            "seq": i,
            "text": chunk,
            "mood": mood,
            "voice_settings": _voice_settings_dict(vs),
            "_bytes": bytes(audio),
            "data": base64.b64encode(bytes(audio)).decode("ascii"),
        }


async def _iter_openai(chunks: list[tuple[str, str | None]], affect: dict):
    import base64

    if not os.environ.get("OPENAI_API_KEY"):
        raise AudioError("OPENAI_API_KEY is not configured", status=503)

    import openai

    from brain.pns import PNS

    client = openai.AsyncOpenAI()
    base_instruction = PNS._openai_instruction_from_emotion((affect or {}).get("emotion"))
    model = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    voice = os.environ.get("OPENAI_TTS_VOICE", "alloy")

    for i, (chunk, mood) in enumerate(chunks):
        instructions = (
            PNS._openai_instruction_from_emotion(mood) if mood else base_instruction
        )
        resp = await client.audio.speech.create(
            model=model, voice=voice, input=chunk,
            instructions=instructions, response_format="pcm",
        )
        data = await resp.aread() if hasattr(resp, "aread") else resp.read()
        data = PNS._pcm_resample(data)  # 24 kHz → 22050, matching the playback path
        yield {
            "seq": i,
            "text": chunk,
            "mood": mood,
            "instructions": instructions,
            "_bytes": data,
            "data": base64.b64encode(data).decode("ascii"),
        }


def _voice_settings_dict(vs) -> dict:
    """Best-effort plain-dict view of a VoiceSettings (surfaced so partners can
    render/visualise the prosody we chose)."""
    out = {}
    for k in ("stability", "style", "speed", "similarity_boost", "use_speaker_boost"):
        v = getattr(vs, k, None)
        if v is not None:
            out[k] = v
    return out


# ── 3. STT transcription ──────────────────────────────────────────────────────
async def transcribe(
    audio_bytes: bytes,
    *,
    mimetype: str = "audio/wav",
    diarize: bool = False,
    model: str | None = None,
) -> dict:
    """Transcribe one audio clip via Deepgram. Returns::

        {
          "transcript": "...",
          "words": [{"word","start","end","speaker","speaker_confidence"}, ...],
          "duration_s": 3.4 | None,                                # input length (quota meter)
          "segments": [{"transcript": "...", "is_final": true}],   # realtime-shaped
        }

    The single ``is_final: true`` segment mirrors the shape a realtime stream
    emits (with interim ``is_final: false`` entries), so consumers written
    against phase 1 don't change."""
    if not audio_bytes:
        raise AudioError("audio (non-empty) is required")
    if not os.environ.get("DEEPGRAM_API_KEY"):
        raise AudioError("DEEPGRAM_API_KEY is not configured", status=503)

    from deepgram import DeepgramClient, PrerecordedOptions

    client = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])
    options = PrerecordedOptions(
        model=model or "nova-3",
        smart_format=True,
        utterances=True,
        punctuate=True,
        **({"diarize": True, "diarize_model": "latest"} if diarize else {}),
    )
    response = await client.listen.asyncprerecorded.v("1").transcribe_file(
        {"buffer": audio_bytes, "mimetype": mimetype}, options
    )
    alt = response.results.channels[0].alternatives[0]
    transcript = (alt.transcript or "").strip()
    # Deepgram reports the input audio length in metadata — the unit STT bills on
    # and the quota meters. Best-effort: None if the SDK shape ever changes.
    duration_s = None
    try:
        duration_s = float(response.metadata.duration)
    except Exception:  # noqa: BLE001
        duration_s = None

    words: list[dict] = []
    if diarize and getattr(alt, "words", None):
        for w in alt.words:
            words.append(
                {
                    "word": getattr(w, "word", ""),
                    "start": float(getattr(w, "start", 0)),
                    "end": float(getattr(w, "end", 0)),
                    "speaker": int(getattr(w, "speaker", 0)),
                    "speaker_confidence": float(getattr(w, "speaker_confidence", 1.0)),
                }
            )
    return {
        "transcript": transcript,
        "words": words,
        "duration_s": duration_s,  # input audio length (quota meter for STT)
        "segments": [{"transcript": transcript, "is_final": True}],
    }
