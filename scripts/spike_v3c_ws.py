"""Phase 0 spike harness: eleven_v3_conversational over the Text to Dialogue WS.

Answers Q1-Q9 in docs/V3_CONVERSATIONAL_SPIKE.md. Standalone — no brain changes.
Run manually:

    .venv/bin/python scripts/spike_v3c_ws.py [--voice VOICE_ID] [--out DIR]
    .venv/bin/python scripts/spike_v3c_ws.py --cases short_flush,medium_tags

Each case writes a WAV + a row in results.json under --out. The Flash 2.5
baseline runs the same texts through the existing per-chunk HTTP path.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import websockets  # noqa: E402  (dotenv must load before brain imports)

from brain.pns import PNS  # noqa: E402

WS_URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
V3C_MODEL = "eleven_v3_conversational"
FLASH_MODEL = "eleven_flash_v2_5"
DEFAULT_VOICE = "c6SfcYrb2t09NHXiT80T"  # the-analyst (Jarnathan) — designed voice, v3-safe
# Q4: preference order; first format the server accepts wins.
OUTPUT_FORMATS = ["pcm_22050", "pcm_24000", "mp3_22050_32"]

MEDIUM_TEXT = (
    "[warmly] I went back through the changelog like you asked, and honestly, "
    "there's more here than I expected. [excited] The realtime model is the big "
    "one — it finally makes the expressive path viable in conversation! "
    "[sighs] The professional clone situation hasn't moved, though. Still stuck "
    "in research preview. [thoughtfully] So the plan is to keep the fast model "
    "as the floor, and let each persona opt into the expressive one when the "
    "voice supports it. That way nothing breaks while we learn what it can do."
)

MOOD_SPAN_RAW = (
    "So the harness finished its first full pass. "
    "[mood:excited]Every single case came back with clean audio, and the tags "
    "actually landed the way we hoped![/mood] "
    "There's one wrinkle with very short replies. "
    "[mood:calm]Nothing serious — the flush control seems to handle it.[/mood] "
    "I'll write the numbers up before we decide anything."
)

LONG_PARAGRAPH = (
    "The idle loop picked this thread back up because the open question was "
    "still marked unresolved. When the pipeline splits an utterance into "
    "sentence chunks, each request re-initializes prosody from silence, and "
    "the seams are audible no matter how carefully the boundaries are chosen. "
    "Stitching hints help, padding helps, but the underlying problem is that "
    "the model never sees the utterance as one continuous performance. A "
    "single streaming session changes that premise entirely. "
)


def _cases(voice: str) -> list[dict]:
    base_tag = "[warmly]"
    _display, mood_span_tts = PNS._parse_mood_markup(MOOD_SPAN_RAW, base_tag)
    return [
        # Q3: does a <40-char input produce audio without flush? (6s wait, then close)
        {"name": "short_noflush", "text": "On it.", "flush": False, "wait_before_close": 6.0},
        {"name": "short_flush", "text": "On it.", "flush": True},
        {"name": "medium_tags", "text": MEDIUM_TEXT, "flush": True},
        {"name": "mood_span", "text": mood_span_tts, "flush": True},
        # Q6: long-form well past the 2,000-char HTTP guidance.
        {"name": "long_form", "text": LONG_PARAGRAPH * 8, "flush": True},
        # Simulated LLM cadence: 80-char sends every 150ms.
        {"name": "incremental", "text": MEDIUM_TEXT, "flush": True, "feed_chars": 80, "feed_delay": 0.15},
    ]


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _rate_of(fmt: str) -> int:
    return int(fmt.split("_")[1])


async def run_ws_case(case: dict, voice: str, api_key: str, out: Path) -> dict:
    """One dialogue-WS session per case; returns a result row."""
    result = {"case": case["name"], "transport": "v3c_ws", "chars": len(case["text"])}
    errors: list[str] = []
    for fmt in OUTPUT_FORMATS:
        url = f"{WS_URL}?model_id={V3C_MODEL}&output_format={fmt}"
        pcm = bytearray()
        events: list[str] = []
        t_first_send = t_first_audio = t_last_audio = None
        try:
            async with websockets.connect(
                url, additional_headers={"xi-api-key": api_key}, max_size=16 * 1024 * 1024
            ) as ws:
                await ws.send(json.dumps({
                    "voices": [voice],
                    "voice_settings": {
                        "stability": PNS._snap_v3_stability(0.5),
                        "similarity_boost": 0.80,
                        "use_speaker_boost": True,
                    },
                }))

                async def _feed() -> float:
                    text, step = case["text"], case.get("feed_chars")
                    first = time.monotonic()
                    if step:
                        for i in range(0, len(text), step):
                            await ws.send(json.dumps({
                                "inputs": [{"text": text[i : i + step], "voice_id": voice}]
                            }))
                            await asyncio.sleep(case.get("feed_delay", 0.15))
                    else:
                        await ws.send(json.dumps({
                            "inputs": [{"text": text, "voice_id": voice}]
                        }))
                    if case.get("flush", True):
                        await ws.send(json.dumps({"flush": True}))
                    if case.get("wait_before_close"):
                        await asyncio.sleep(case["wait_before_close"])
                    await ws.send(json.dumps({"close_socket": True}))
                    return first

                async def _drain() -> None:
                    nonlocal t_first_audio, t_last_audio
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            events.append(f"non-json frame ({len(raw)} bytes)")  # noqa: B023
                            continue
                        if msg.get("audio"):
                            now = time.monotonic()
                            if t_first_audio is None:
                                t_first_audio = now
                            t_last_audio = now
                            pcm.extend(base64.b64decode(msg["audio"]))  # noqa: B023
                        keys = {k: v for k, v in msg.items() if k != "audio"}
                        if keys:
                            events.append(json.dumps(keys))  # noqa: B023

                drain = asyncio.create_task(_drain())
                t_first_send = await _feed()
                await asyncio.wait_for(drain, timeout=120)
        except Exception as exc:  # noqa: BLE001 — harness records everything
            errors.append(f"{fmt}: {type(exc).__name__}: {exc}")
            if not pcm:
                continue  # try the next output format
        result.update({
            "output_format": fmt,
            "ttfa_s": round(t_first_audio - t_first_send, 3) if t_first_audio else None,
            "wall_s": round((t_last_audio or time.monotonic()) - t_first_send, 3),
            "bytes": len(pcm),
            "audio_s": round(len(pcm) / (_rate_of(fmt) * 2), 2) if fmt.startswith("pcm") else None,
            "events": events[:12],
            "errors": errors,
        })
        if pcm and fmt.startswith("pcm"):
            wav = out / f"v3c_{case['name']}.wav"
            _write_wav(wav, bytes(pcm), _rate_of(fmt))
            result["wav"] = str(wav)
        elif pcm:
            (out / f"v3c_{case['name']}.mp3").write_bytes(bytes(pcm))
        return result
    result["errors"] = errors
    return result


async def run_flash_case(case: dict, voice: str, api_key: str, out: Path) -> dict:
    """Baseline: current per-chunk HTTP path (Flash 2.5, tags stripped)."""
    from elevenlabs import AsyncElevenLabs
    from elevenlabs.types import VoiceSettings

    client = AsyncElevenLabs(api_key=api_key)
    text = PNS._strip_all_tags(case["text"])
    chunks = PNS._split_sentences(text)
    vs = VoiceSettings(stability=0.5, similarity_boost=0.80, style=0.35, use_speaker_boost=True)
    pcm = bytearray()
    t0 = time.monotonic()
    t_first = None
    errors: list[str] = []
    for i, sentence in enumerate(chunks):
        kwargs = {
            "text": sentence, "voice_id": voice, "model_id": FLASH_MODEL,
            "output_format": "pcm_22050", "voice_settings": vs,
        }
        if i > 0:
            kwargs["previous_text"] = chunks[i - 1]
        if i + 1 < len(chunks):
            kwargs["next_text"] = chunks[i + 1]
        try:
            async for chunk in client.text_to_speech.stream(**kwargs):
                if chunk:
                    if t_first is None:
                        t_first = time.monotonic()
                    pcm.extend(chunk)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"chunk {i}: {type(exc).__name__}: {exc}")
        if i < len(chunks) - 1:
            pcm.extend(b"\x00" * (22050 * 2 * 20 // 1000))  # 20ms gap, as in prod
    wav = out / f"flash_{case['name']}.wav"
    if pcm:
        _write_wav(wav, bytes(pcm), 22050)
    return {
        "case": case["name"], "transport": "flash_http", "chars": len(text),
        "n_chunks": len(chunks), "output_format": "pcm_22050",
        "ttfa_s": round(t_first - t0, 3) if t_first else None,
        "wall_s": round(time.monotonic() - t0, 3),
        "bytes": len(pcm), "audio_s": round(len(pcm) / (22050 * 2), 2),
        "wav": str(wav) if pcm else None, "errors": errors,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--out", default="")
    ap.add_argument("--cases", default="", help="comma-separated subset of case names")
    ap.add_argument("--skip-flash", action="store_true", help="skip the Flash baseline")
    args = ap.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        sys.exit("ELEVENLABS_API_KEY not set")
    out = Path(args.out or Path(__file__).resolve().parent.parent / "spike_out")
    out.mkdir(parents=True, exist_ok=True)

    cases = _cases(args.voice)
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        cases = [c for c in cases if c["name"] in wanted]

    rows = []
    for case in cases:
        print(f"→ v3c_ws:{case['name']} ({len(case['text'])} chars)", flush=True)
        rows.append(await run_ws_case(case, args.voice, api_key, out))
        print(f"   {json.dumps({k: rows[-1].get(k) for k in ('ttfa_s', 'wall_s', 'audio_s', 'errors')})}", flush=True)
    if not args.skip_flash:
        for case in cases:
            if case["name"] == "short_noflush":
                continue  # flush semantics don't exist on HTTP; short_flush covers it
            print(f"→ flash:{case['name']}", flush=True)
            rows.append(await run_flash_case(case, args.voice, api_key, out))
            print(f"   {json.dumps({k: rows[-1].get(k) for k in ('ttfa_s', 'wall_s', 'audio_s', 'errors')})}", flush=True)

    (out / "results.json").write_text(json.dumps(rows, indent=2))
    print(f"\nResults → {out / 'results.json'}")
    hdr = f"{'case':<16} {'transport':<11} {'ttfa_s':>7} {'wall_s':>7} {'audio_s':>8} {'chars':>6}"
    print(hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{r['case']:<16} {r['transport']:<11} {str(r.get('ttfa_s')):>7} "
              f"{str(r.get('wall_s')):>7} {str(r.get('audio_s')):>8} {r['chars']:>6}")


if __name__ == "__main__":
    asyncio.run(main())
