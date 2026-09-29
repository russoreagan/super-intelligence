"""Phase 0 probe harness: eleven_v4_turbo over the Text to Dialogue WebSocket.

Answers P1-P12 in docs/ELEVEN_V4_TURBO_PLAN.md. Standalone, no brain changes.
Run manually:

    .venv/bin/python scripts/spike_v4t_probe.py --out DIR
    .venv/bin/python scripts/spike_v4t_probe.py --probes p1,p6 --out DIR

Writes WAVs + results.json under --out. Flash 2.5 over per-chunk HTTP is the
baseline, as in scripts/spike_v3c_ws.py.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import websockets  # noqa: E402

from brain.pns import PNS  # noqa: E402

API = "https://api.elevenlabs.io"
WSS = "wss://api.elevenlabs.io"
SINGLE_PATH = "/v1/text-to-dialogue/stream-input"
MULTI_PATH = "/v1/text-to-dialogue/multi-stream-input"
V4T, V4, V3C, FLASH = "eleven_v4_turbo", "eleven_v4", "eleven_v3_conversational", "eleven_flash_v2_5"
DEFAULT_VOICE = "c6SfcYrb2t09NHXiT80T"  # the-analyst (Jarnathan), designed voice
RATE = 22050

SHORT = "On it."
MEDIUM_TAGS = (
    "[warmly] I went back through the changelog like you asked, and honestly, "
    "there's more here than I expected. [excited] The new realtime model is the big "
    "one, it finally makes the expressive path viable in conversation! "
    "[sighs] It took a while to get here, though. [thoughtfully] So the plan is to "
    "keep the fast model as the floor, and let the expressive one lead whenever it's "
    "healthy. That way nothing breaks while we learn what it can do."
)
MOOD_SPAN_RAW = (
    "So the harness finished its first full pass. "
    "[mood:excited]Every single case came back with clean audio, and the tags "
    "actually landed the way we hoped![/mood] "
    "There's one wrinkle with very short replies. "
    "[mood:calm]Nothing serious, the flush control seems to handle it.[/mood] "
    "I'll write the numbers up before we decide anything."
)
# Emotions that all collapse to [softly] in EMOTION_TAG_MAP today, rendered with
# v4 free-text direction instead, for the listening check.
DIRECTION_SCRIPT = (
    "[wistfully, remembering something] I used to think we'd finish this in a week. "
    "[apologetic, a little embarrassed] Sorry, that's my fault, I undersold it. "
    "[somber, slow] Some of the old work just didn't survive the move. "
    "[brightening] But the new path is better. [laughs softly] Much better."
)
LONG_PARAGRAPH = (
    "The idle loop picked this thread back up because the open question was "
    "still marked unresolved. When the pipeline splits an utterance into "
    "sentence chunks, each request re-initializes prosody from silence, and "
    "the seams are audible no matter how carefully the boundaries are chosen. "
)
BILLING_TEXT = ("The quick brown fox jumps over the lazy dog near the riverbank. " * 16)[:960]


def _wav(path: Path, pcm: bytes, rate: int = RATE) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return str(path)


def _audio_s(nbytes: int, fmt: str) -> float | None:
    if not fmt.startswith("pcm"):
        return None
    return round(nbytes / (int(fmt.split("_")[1]) * 2), 2)


def _connect(url: str, api_key: str):
    return websockets.connect(
        url, additional_headers={"xi-api-key": api_key}, max_size=16 * 1024 * 1024, open_timeout=10
    )


async def ws_session(
    api_key: str,
    *,
    model: str,
    voice: str,
    text: str,
    fmt: str = "pcm_22050",
    flush: bool = True,
    feed: list[str] | None = None,
    feed_delay: float = 0.15,
    voice_settings: dict | None = None,
    query: dict | None = None,
    pre_wait: float = 0.0,
    wav: Path | None = None,
) -> dict:
    """One single-context dialogue session. TTFA is measured from the first
    text send; connect_s separately, so cold = connect_s + ttfa_s."""
    q = {"model_id": model, "output_format": fmt, **(query or {})}
    url = f"{WSS}{SINGLE_PATH}?" + "&".join(f"{k}={v}" for k, v in q.items())
    row: dict = {"model": model, "fmt": fmt, "chars": len(text), "errors": [], "events": []}
    pcm = bytearray()
    alignments: list[dict] = []
    t0 = time.monotonic()
    t_send = t_first = t_last = None
    try:
        async with _connect(url, api_key) as ws:
            row["connect_s"] = round(time.monotonic() - t0, 3)
            first: dict = {"voices": [voice]}
            if voice_settings is not None:
                first["voice_settings"] = voice_settings
            await ws.send(json.dumps(first))

            async def _feed() -> None:
                nonlocal t_send
                if pre_wait:
                    # Hold the socket open like a turn-start open would.
                    end = time.monotonic() + pre_wait
                    while time.monotonic() < end:
                        await asyncio.sleep(min(8.0, end - time.monotonic()))
                        await ws.send(json.dumps({"keep_alive": True}))
                t_send = time.monotonic()
                for piece in feed or [text]:
                    await ws.send(json.dumps({"inputs": [{"text": piece, "voice_id": voice}]}))
                    if feed:
                        await asyncio.sleep(feed_delay)
                if flush:
                    await ws.send(json.dumps({"flush": True}))
                await ws.send(json.dumps({"close_socket": True}))

            async def _drain() -> None:
                nonlocal t_first, t_last
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("audio"):
                        now = time.monotonic()
                        t_first = t_first or now
                        t_last = now
                        pcm.extend(base64.b64decode(msg["audio"]))
                    if msg.get("alignment"):
                        alignments.append(msg["alignment"])
                    rest = {k: v for k, v in msg.items() if k not in ("audio", "alignment", "normalized_alignment")}
                    if rest:
                        row["events"].append(rest)

            drain = asyncio.create_task(_drain())
            await _feed()
            await asyncio.wait_for(drain, timeout=180)
        row["close"] = "clean"
    except Exception as exc:  # noqa: BLE001  harness records everything
        row["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
    row.update(
        ttfa_s=round(t_first - t_send, 3) if t_first and t_send else None,
        gen_s=round(t_last - t_send, 3) if t_last and t_send else None,
        bytes=len(pcm),
        audio_s=_audio_s(len(pcm), fmt),
    )
    if alignments:
        row["alignment"] = _alignment_summary(alignments, row["audio_s"])
    if wav and pcm and fmt.startswith("pcm"):
        row["wav"] = _wav(wav, bytes(pcm), int(fmt.split("_")[1]))
    row["events"] = row["events"][:8]
    return row


def _alignment_summary(frames: list[dict], audio_s: float | None) -> dict:
    chars = [c for f in frames for c in (f.get("chars") or f.get("characters") or [])]
    starts = [s for f in frames for s in (f.get("char_start_times_ms") or [])]
    durs = [d for f in frames for d in (f.get("char_durations_ms") or f.get("durations") or [])]
    end_ms = max((s + d for s, d in zip(starts, durs, strict=False)), default=None)
    return {
        "frames": len(frames),
        "keys": sorted(frames[0].keys()),
        "chars": len(chars),
        "text_head": "".join(chars[:60]),
        "starts_monotonic": starts == sorted(starts),
        "starts_reset_per_frame": bool(starts) and starts != sorted(starts),
        "last_end_ms": end_ms,
        "sum_durations_ms": sum(durs) if durs else None,
        "audio_ms": int(audio_s * 1000) if audio_s else None,
    }


async def flash_http(api_key: str, *, voice: str, text: str, wav: Path | None = None, speed=None) -> dict:
    """Baseline: prod per-chunk HTTP Flash path (tags stripped, stitching on)."""
    from elevenlabs import AsyncElevenLabs
    from elevenlabs.types import VoiceSettings

    client = AsyncElevenLabs(api_key=api_key)
    clean = PNS._strip_all_tags(text)
    chunks = PNS._split_sentences(clean)
    kw = {"stability": 0.5, "similarity_boost": 0.80, "style": 0.35, "use_speaker_boost": True}
    vs = VoiceSettings(**kw, speed=speed) if speed else VoiceSettings(**kw)
    pcm = bytearray()
    errors: list[str] = []
    t0 = time.monotonic()
    t_first = None
    for i, sentence in enumerate(chunks):
        args = {"text": sentence, "voice_id": voice, "model_id": FLASH, "output_format": "pcm_22050", "voice_settings": vs}
        if i > 0:
            args["previous_text"] = chunks[i - 1]
        if i + 1 < len(chunks):
            args["next_text"] = chunks[i + 1]
        try:
            async for chunk in client.text_to_speech.stream(**args):
                if chunk:
                    t_first = t_first or time.monotonic()
                    pcm.extend(chunk)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"chunk {i}: {type(exc).__name__}: {exc}")
        if i < len(chunks) - 1:
            pcm.extend(b"\x00" * (RATE * 2 * 20 // 1000))
    row = {
        "model": FLASH, "transport": "http", "chars": len(clean), "n_chunks": len(chunks),
        "ttfa_s": round(t_first - t0, 3) if t_first else None, "gen_s": round(time.monotonic() - t0, 3),
        "bytes": len(pcm), "audio_s": _audio_s(len(pcm), "pcm_22050"), "errors": errors,
    }
    if wav and pcm:
        row["wav"] = _wav(wav, bytes(pcm))
    return row


async def _get(api_key: str, path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{API}{path}", headers={"xi-api-key": api_key}, params=params)
        try:
            return {"status": r.status_code, "json": r.json()}
        except ValueError:
            return {"status": r.status_code, "text": r.text[:300]}


# ── probes ───────────────────────────────────────────────────────────────────


async def p1_ttfa(k: str, voice: str, out: Path) -> dict:
    """TTFA: v4t vs v3c vs Flash, cold vs pre-opened socket, 3 reps each."""
    mood_tts, _ = PNS._parse_mood_markup(MOOD_SPAN_RAW, "[warmly]")
    res: dict = {}
    for label, text in (("short", SHORT), ("medium", MEDIUM_TAGS)):
        for model in (V4T, V3C):
            runs = [await ws_session(k, model=model, voice=voice, text=text) for _ in range(3)]
            warm = [await ws_session(k, model=model, voice=voice, text=text, pre_wait=3.0) for _ in range(2)]
            res[f"{model}:{label}"] = {
                "cold_connect_s": [r.get("connect_s") for r in runs],
                "cold_ttfa_s": [r["ttfa_s"] for r in runs],
                "warm_ttfa_s": [r["ttfa_s"] for r in warm],
                "errors": [e for r in runs + warm for e in r["errors"]],
            }
        flash = [await flash_http(k, voice=voice, text=text) for _ in range(3)]
        res[f"{FLASH}:{label}"] = {"ttfa_s": [r["ttfa_s"] for r in flash], "errors": [e for r in flash for e in r["errors"]]}
    # Listening pairs (P10) come from these.
    for name, text in (("medium_tags", MEDIUM_TAGS), ("mood_span", mood_tts), ("direction", DIRECTION_SCRIPT)):
        r = await ws_session(k, model=V4T, voice=voice, text=text, wav=out / f"v4t_{name}.wav")
        res[f"wav:v4t:{name}"] = {kk: r.get(kk) for kk in ("ttfa_s", "gen_s", "audio_s", "wav", "errors")}
        f = await flash_http(k, voice=voice, text=text, wav=out / f"flash_{name}.wav")
        res[f"wav:flash:{name}"] = {kk: f.get(kk) for kk in ("ttfa_s", "gen_s", "audio_s", "wav", "errors")}
    r = await ws_session(k, model=V4T, voice=voice, text=LONG_PARAGRAPH * 10, wav=out / "v4t_long.wav")
    res["long_form"] = {kk: r.get(kk) for kk in ("chars", "ttfa_s", "gen_s", "audio_s", "errors")}
    return res


async def p2_billing(k: str, voice: str, out: Path) -> dict:
    """Credits per char: subscription character_count delta around a 960-char run.
    Other traffic on the same key adds noise, so each model runs twice."""
    res: dict = {}

    async def count() -> int | None:
        r = await _get(k, "/v1/user/subscription")
        return (r.get("json") or {}).get("character_count")

    for model in (V4T, FLASH, V4T, FLASH):
        before = await count()
        if model == FLASH:
            await flash_http(k, voice=voice, text=BILLING_TEXT)
        else:
            await ws_session(k, model=model, voice=voice, text=BILLING_TEXT)
        await asyncio.sleep(4)
        after = await count()
        res.setdefault(model, []).append(
            {"chars": len(BILLING_TEXT), "billed": (after - before) if None not in (after, before) else None}
        )
    sub = await _get(k, "/v1/user/subscription")
    j = sub.get("json") or {}
    res["subscription"] = {key: j.get(key) for key in ("tier", "character_limit", "character_count", "status")}
    return res


async def p3_settings(k: str, voice: str, out: Path) -> dict:
    """Which voice_settings v4t accepts, and whether speed changes duration."""
    variants = {
        "none": None,
        "stability_0.0": {"stability": 0.0},
        "stability_0.3": {"stability": 0.3},
        "stability_1.0": {"stability": 1.0},
        "speed_0.75": {"stability": 0.5, "speed": 0.75},
        "speed_1.2": {"stability": 0.5, "speed": 1.2},
        "style_0.8": {"stability": 0.5, "style": 0.8},
        "full_flash_set": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.35, "use_speaker_boost": True, "speed": 1.0},
    }
    res = {}
    for name, vs in variants.items():
        r = await ws_session(k, model=V4T, voice=voice, text=MEDIUM_TAGS, voice_settings=vs, wav=out / f"v4t_vs_{name}.wav")
        res[name] = {kk: r.get(kk) for kk in ("ttfa_s", "audio_s", "errors", "events")}
    return res


async def p4_formats(k: str, voice: str, out: Path) -> dict:
    fmts = ["pcm_22050", "pcm_16000", "pcm_24000", "pcm_44100", "mp3_44100_128", "mp3_22050_32", "ulaw_8000", "opus_48000_64"]
    res = {}
    for fmt in fmts:
        r = await ws_session(k, model=V4T, voice=voice, text="Give me one second, I'm checking now.", fmt=fmt)
        res[fmt] = {"ok": bool(r["bytes"]) and not r["errors"], "bytes": r["bytes"], "ttfa_s": r["ttfa_s"], "errors": r["errors"]}
    return res


async def p5_alignment(k: str, voice: str, out: Path) -> dict:
    r = await ws_session(k, model=V4T, voice=voice, text=MEDIUM_TAGS, query={"sync_alignment": "true"})
    return {kk: r.get(kk) for kk in ("ttfa_s", "audio_s", "alignment", "errors")}


async def p6_cancel(k: str, voice: str, out: Path) -> dict:
    """Barge-in options: (a) close the single-context socket mid-utterance and
    reconnect; (b) close_context on the multi-context socket and start a new
    context on the same connection."""
    res: dict = {}
    long_text = LONG_PARAGRAPH * 4

    # (a) close + reconnect
    url = f"{WSS}{SINGLE_PATH}?model_id={V4T}&output_format=pcm_22050"
    try:
        t0 = time.monotonic()
        async with _connect(url, k) as ws:
            await ws.send(json.dumps({"voices": [voice]}))
            await ws.send(json.dumps({"inputs": [{"text": long_text, "voice_id": voice}]}))
            await ws.send(json.dumps({"flush": True}))
            async for raw in ws:
                if json.loads(raw).get("audio") and time.monotonic() - t0 > 1.5:
                    break
        t_closed = time.monotonic()
        re = await ws_session(k, model=V4T, voice=voice, text="Sorry, go ahead.")
        res["a_close_reconnect"] = {
            "close_s": round(time.monotonic() - t_closed, 3),
            "reconnect_connect_s": re.get("connect_s"),
            "reconnect_ttfa_s": re.get("ttfa_s"),
            "errors": re["errors"],
        }
    except Exception as exc:  # noqa: BLE001
        res["a_close_reconnect"] = {"errors": [f"{type(exc).__name__}: {exc}"]}

    # (b) multi-context
    url = f"{WSS}{MULTI_PATH}?model_id={V4T}&output_format=pcm_22050"
    row: dict = {"events": [], "errors": []}
    try:
        async with _connect(url, k) as ws:
            await ws.send(json.dumps({"context_id": "a", "voices": [voice], "inputs": [{"text": long_text, "voice_id": voice}]}))
            await ws.send(json.dumps({"context_id": "a", "flush": True}))
            t_close = t_b_send = t_b_first = None
            a_after_close = 0
            b_bytes = 0
            t0 = time.monotonic()
            async for raw in ws:
                msg = json.loads(raw)
                ctx = msg.get("context_id") or msg.get("contextId")
                if msg.get("audio"):
                    if ctx == "a" and t_close is None and time.monotonic() - t0 > 1.5:
                        t_close = time.monotonic()
                        await ws.send(json.dumps({"context_id": "a", "close_context": True}))
                        t_b_send = time.monotonic()
                        await ws.send(json.dumps({"context_id": "b", "voices": [voice], "inputs": [{"text": "Sorry, go ahead.", "voice_id": voice}]}))
                        await ws.send(json.dumps({"context_id": "b", "flush": True}))
                    elif ctx == "a" and t_close is not None:
                        a_after_close += len(base64.b64decode(msg["audio"]))
                    elif ctx == "b":
                        t_b_first = t_b_first or time.monotonic()
                        b_bytes += len(base64.b64decode(msg["audio"]))
                rest = {kk: v for kk, v in msg.items() if kk not in ("audio", "alignment", "normalized_alignment")}
                if rest:
                    row["events"].append(rest)
                if ctx == "b" and msg.get("is_final"):
                    await ws.send(json.dumps({"close_socket": True}))
                if time.monotonic() - t0 > 30:
                    break
            row.update(
                a_audio_after_close_s=_audio_s(a_after_close, "pcm_22050"),
                b_ttfa_s=round(t_b_first - t_b_send, 3) if t_b_first and t_b_send else None,
                b_audio_s=_audio_s(b_bytes, "pcm_22050"),
            )
    except Exception as exc:  # noqa: BLE001
        row["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
    row["events"] = row["events"][:10]
    res["b_multi_context"] = row
    return res


async def p7_incremental(k: str, voice: str, out: Path) -> dict:
    """Clause-sized feeding, a tag split across frames, and new_turn reuse."""
    res: dict = {}
    clauses = [c + " " for c in MEDIUM_TAGS.replace("! ", "!|").replace(". ", ".|").replace(", ", ",|").split("|")]
    r = await ws_session(k, model=V4T, voice=voice, text=MEDIUM_TAGS, feed=clauses, feed_delay=0.25, wav=out / "v4t_incremental.wav")
    res["clauses"] = {kk: r.get(kk) for kk in ("ttfa_s", "gen_s", "audio_s", "errors")} | {"n_frames": len(clauses)}
    split = ["[war", "mly] That tag was split across two frames. ", "Did it land as a tag?"]
    r = await ws_session(k, model=V4T, voice=voice, text="".join(split), feed=split, wav=out / "v4t_split_tag.wav")
    res["split_tag"] = {kk: r.get(kk) for kk in ("ttfa_s", "audio_s", "errors", "wav")}

    # Two utterances on one warm socket, second with new_turn.
    url = f"{WSS}{SINGLE_PATH}?model_id={V4T}&output_format=pcm_22050"
    row: dict = {"errors": []}
    try:
        async with _connect(url, k) as ws:
            await ws.send(json.dumps({"voices": [voice]}))
            ttfas = []
            for i, text in enumerate(("[excited] First reply, all done now.", "[calmly] Second reply on the same socket.")):
                t_send = time.monotonic()
                inp = {"text": text, "voice_id": voice}
                if i:
                    inp["new_turn"] = True
                await ws.send(json.dumps({"inputs": [inp]}))
                await ws.send(json.dumps({"flush": True}))
                t_first = None
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    except TimeoutError:
                        break  # utterance drained
                    if json.loads(raw).get("audio"):
                        t_first = t_first or time.monotonic()
                ttfas.append(round(t_first - t_send, 3) if t_first else None)
            await ws.send(json.dumps({"close_socket": True}))
            row["ttfa_s_per_utterance"] = ttfas
    except Exception as exc:  # noqa: BLE001
        row["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
    res["reuse_new_turn"] = row
    return res


async def p8_pool(k: str, voice: str, out: Path, ceiling: int = 15) -> dict:
    """Concurrent dialogue sessions until the first rejection (or ceiling)."""
    res: dict = {"levels": {}}
    for n in (2, 4, 8, 12, ceiling):
        rows = await asyncio.gather(*[ws_session(k, model=V4T, voice=voice, text="Checking in, one moment please.") for _ in range(n)])
        errs = [e for r in rows for e in r["errors"]] + [str(ev) for r in rows for ev in r["events"] if "error" in str(ev).lower()]
        ok = sum(1 for r in rows if r["bytes"])
        res["levels"][n] = {"ok": ok, "median_ttfa_s": statistics.median([r["ttfa_s"] for r in rows if r["ttfa_s"]] or [0]), "errors": errs[:3]}
        if ok < n:
            break
        await asyncio.sleep(2)
    return res


async def p9_pvc(k: str, voice: str, out: Path) -> dict:
    r = await _get(k, "/v2/voices", {"category": "professional", "page_size": 20})
    voices = (r.get("json") or {}).get("voices") or []
    res: dict = {"status": r.get("status"), "pvc_count": len(voices), "pvcs": [(v.get("voice_id"), v.get("name")) for v in voices][:10]}
    if voices:
        vid = voices[0]["voice_id"]
        s = await ws_session(k, model=V4T, voice=vid, text=MEDIUM_TAGS, wav=out / "v4t_pvc_medium_tags.wav")
        res["sample"] = {kk: s.get(kk) for kk in ("ttfa_s", "audio_s", "errors", "wav")} | {"voice": voices[0].get("name")}
    return res


async def p11_languages(k: str, voice: str, out: Path) -> dict:
    samples = {
        "es": "Hola, ya estoy revisando los resultados, dame un segundo.",
        "yue": "你好，我而家睇緊啲結果，等我一陣。",
        "ja": "こんにちは、今結果を確認しています。少々お待ちください。",
        "zz": "Invalid language code test.",
    }
    res = {}
    for code, text in samples.items():
        r = await ws_session(k, model=V4T, voice=voice, text=text, query={"language_code": code}, wav=out / f"v4t_lang_{code}.wav")
        res[code] = {kk: r.get(kk) for kk in ("ttfa_s", "audio_s", "errors", "events")}
    r = await ws_session(k, model=V4T, voice=voice, text=samples["es"])
    res["es_no_code"] = {kk: r.get(kk) for kk in ("ttfa_s", "audio_s", "errors")}
    return res


async def p12_faults(k: str, voice: str, out: Path) -> dict:
    """Idle timeout without keep_alive, and survival with it."""
    res: dict = {}
    url = f"{WSS}{SINGLE_PATH}?model_id={V4T}&output_format=pcm_22050"
    t0 = time.monotonic()
    try:
        async with _connect(url, k) as ws:
            await ws.send(json.dumps({"voices": [voice]}))
            try:
                async for raw in ws:
                    res.setdefault("idle_msgs", []).append(str(raw)[:200])
            except websockets.ConnectionClosed as cc:
                res["idle_close"] = {"after_s": round(time.monotonic() - t0, 1), "code": cc.code, "reason": cc.reason}
            else:
                res["idle_close"] = {"after_s": round(time.monotonic() - t0, 1), "code": ws.close_code, "reason": ws.close_reason}
    except Exception as exc:  # noqa: BLE001
        res["idle_close"] = {"error": f"{type(exc).__name__}: {exc}"}
    r = await ws_session(k, model=V4T, voice=voice, text=SHORT, pre_wait=25.0)
    res["keep_alive_25s"] = {kk: r.get(kk) for kk in ("ttfa_s", "audio_s", "errors", "close")}
    r = await ws_session(k, model=V4T, voice="not_a_real_voice_id", text=SHORT)
    res["bad_voice"] = {kk: r.get(kk) for kk in ("errors", "events")}
    return res


PROBES = {
    "p1": p1_ttfa, "p2": p2_billing, "p3": p3_settings, "p4": p4_formats, "p5": p5_alignment,
    "p6": p6_cancel, "p7": p7_incremental, "p8": p8_pool, "p9": p9_pvc, "p11": p11_languages, "p12": p12_faults,
}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--out", required=True)
    ap.add_argument("--probes", default=",".join(PROBES))
    ap.add_argument("--env-file", default="", help="defaults to the repo .env")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file or Path(__file__).resolve().parent.parent / ".env")
    k = os.environ.get("ELEVENLABS_API_KEY", "")
    if not k:
        sys.exit("ELEVENLABS_API_KEY not set")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else {}
    for name in [p.strip() for p in args.probes.split(",") if p.strip()]:
        print(f"→ {name}", flush=True)
        t0 = time.monotonic()
        try:
            results[name] = await PROBES[name](k, args.voice, out)
        except Exception as exc:  # noqa: BLE001
            results[name] = {"crashed": f"{type(exc).__name__}: {exc}"}
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        print(f"   done in {time.monotonic() - t0:.1f}s", flush=True)
    print(f"Results → {results_path}")


if __name__ == "__main__":
    asyncio.run(main())
