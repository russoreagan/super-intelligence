"""Replay the delivery-tone prototype (brain/delivery_tone.py) over a labelled sample.

EXPLORATION ONLY. Reads a local JSON sample (not committed: it holds a real user's
conversation) with, per listener reply: user text, reply text, stored tag, logged turn
emotion, user_emotion and the episode neuromod snapshot.

    PYTHONPATH=. .venv/bin/python scripts/replay_delivery_tone.py sample.json [--llm]

Embeddings: local Ollama nomic-embed-text (the prod embedder). --llm additionally asks
an LLM to name the reply's tone families (the "critic names the delivery" proxy) using
ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

from brain.delivery_tone import (
    TONE_FAMILIES,
    ToneReader,
    resolve_delivery,
    supported_families,
    tag_for_label,
)

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
SELF_DIRECTED = {"proud", "embarrassed", "relieved", "grateful"}
DISTRESS = {"sad", "anxious", "distressed", "struggling", "hurt", "lonely", "scared", "overwhelmed"}
LLM_MODEL = os.environ.get("TONE_LLM_MODEL", "claude-haiku-5-5")

_cache: dict[str, list[float]] = {}
embed_ms: list[float] = []


async def embed(text: str) -> list[float] | None:
    if text in _cache:
        return _cache[text]
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{OLLAMA}/api/embeddings", json={"model": "nomic-embed-text", "prompt": text}
        )
    embed_ms.append((time.perf_counter() - t0) * 1000)
    v = r.json().get("embedding")
    _cache[text] = v
    return v


def _anthropic_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for p in (".env", "../../../.env"):
        try:
            lines = Path(p).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


LLM_PROMPT = """You label how a spoken reply should SOUND. Families:
{fams}
User said: {user}
Reply to be spoken: {reply}
Return JSON only: {{"families": ["<best>", "<second if it clearly also fits, else omit>"]}}"""


async def llm_families(client, user: str, reply: str) -> tuple[list[str], float, int]:
    fams = "\n".join(f"- {k}: {v['desc']}" for k, v in TONE_FAMILIES.items())
    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=LLM_MODEL,
        max_tokens=400,
        thinking={"type": "disabled"},
        messages=[
            {"role": "user", "content": LLM_PROMPT.format(fams=fams, user=user, reply=reply)}
        ],
    )
    ms = (time.perf_counter() - t0) * 1000
    raw = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        out = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])["families"]
    except Exception:
        out = []
    return [f for f in out if f in TONE_FAMILIES], ms, msg.usage.input_tokens


# the_empath resting hormones (brain/persona_chem.py). Episode snapshots carry only the
# five neuromods, so hormones are approximated at rest — a replay, not a re-run.
EMPATH_HORMONES = {"5HT": 0.70, "CORT": 0.03, "OXT": 0.70, "AEA": 0.45}


def effective(nm: dict, horm: dict = EMPATH_HORMONES) -> tuple[str, dict]:
    """Chemistry -> label with the hypothalamus's effective-value math
    (brain/clusters/hypothalamus.py _effective_emotion), without the override inbox."""
    from brain.bus import HormonalState
    from brain.emotion_vocabulary import apply_hormonal_color, apply_ne_color, name_emotion
    from brain.settings import settings as s

    hs = HormonalState()
    hs.restore(horm)
    h = hs.snapshot()
    ne_s, glu_s = hs.aea_suppress(s.get("aea_ne_suppression"), s.get("aea_glu_suppression"))
    e_ne, e_glu = nm["NE"] * ne_s, nm["Glu"] * glu_s
    off = hs.da_offset(s.get("sht_da_floor_lift"), s.get("oxt_da_lift"), s.get("cort_da_suppress"))
    sup = max(0.0, h["AEA"] - s.get("aea_da_suppress_threshold")) * s.get("aea_da_suppress")
    e_da = min(1.0, max(0.0, nm["DA"] + off + h["AEA"] * s.get("aea_da_lift") - sup))
    e_gaba = nm["GABA"] * hs.gaba_scale(s.get("cort_gaba_amplify"), s.get("oxt_gaba_buffer"))
    e, t = name_emotion(e_da, e_gaba, nm["ACh"], e_glu)
    e, t = apply_ne_color(
        e, t, e_ne, ne_high=s.get("ne_high_threshold"), ne_scatter=s.get("ne_scatter_threshold")
    )
    e, t = apply_hormonal_color(
        e,
        t,
        h,
        oxt_connected=s.get("hormonal_oxt_connected_threshold"),
        cort_withdrawn=s.get("hormonal_cort_withdrawn_threshold"),
        oxt_guarded=s.get("hormonal_oxt_guarded_threshold"),
        sht_dysphoric=s.get("hormonal_sht_dysphoric_threshold"),
        aea_eased=s.get("aea_eased_threshold"),
    )
    return e, {"DA": e_da, "GABA": e_gaba, "ACh": nm["ACh"], "Glu": e_glu, "NE": e_ne}


async def main(path: str, use_llm: bool) -> None:
    from brain.emotion_vocabulary import compute_affect_dims

    rows = json.loads(Path(path).read_text())
    reader = ToneReader(embed)
    client = None
    if use_llm:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=_anthropic_key())
    llm_ms: list[float] = []
    cache_path = os.path.join(os.path.dirname(path), "llm_families_cache.json")
    llm_cache = json.loads(Path(cache_path).read_text()) if os.path.exists(cache_path) else {}
    llm_tok: list[int] = []
    out = []
    for r in rows:
        chem_label, _ = effective(r["nm"])
        dims = compute_affect_dims(r["nm"], EMPATH_HORMONES)
        felt_va = (dims["valence"], dims["arousal"])
        # V1: display rules only (no text read)
        v1_label = chem_label if r["emotion"] in SELF_DIRECTED else r["emotion"]
        if r["user_emotion"] in DISTRESS:
            v1_label = "sympathetic"
        v1 = tag_for_label(v1_label)
        # V2: text-aware (embedding read), felt label as logged
        probs = await reader.read(r["reply"])
        fams = supported_families(probs)
        v2, why2 = resolve_delivery(r["emotion"], fams, felt_va)
        # V3: text-aware (LLM read)
        v3 = v4 = fams3 = None
        if client:
            if r["turn"] in llm_cache:
                fams3 = llm_cache[r["turn"]]
            else:
                fams3, ms, tok = await llm_families(client, r["user"], r["reply"])
                llm_ms.append(ms)
                llm_tok.append(tok)
                llm_cache[r["turn"]] = fams3
            v3, _ = resolve_delivery(r["emotion"], fams3, felt_va)
            # V4: V1 felt label (display rules) + LLM-read text gate
            v4, _ = resolve_delivery(v1_label, fams3, felt_va)
        out.append(
            {
                "t": r["t"],
                "emotion": r["emotion"],
                "chem": chem_label,
                "stored": r["stored_tag"],
                "v1": v1,
                "v2": v2,
                "fams2": fams,
                "v3": v3,
                "v4": v4,
                "fams3": fams3,
                "reply": r["reply"][:70],
            }
        )
    hdr = f"{'time':8} {'logged':17} {'stored':11} {'V1 rules':13} {'V2 embed':13} {'V3 llm':13} {'V4 rules+llm':13} emb-families | llm-families | reply"
    print(hdr)
    for o in out:
        print(
            f"{o['t']:8} {o['emotion']:17} {str(o['stored']):11} {str(o['v1']):13} {str(o['v2']):13} "
            f"{str(o['v3']):13} {str(o['v4']):13} {','.join(o['fams2'])} | {','.join(o['fams3'] or [])} | {o['reply']}"
        )
    if embed_ms:
        s = sorted(embed_ms)
        print(
            f"\nembed calls={len(s)} p50={s[len(s) // 2]:.0f}ms p95={s[int(len(s) * 0.95)]:.0f}ms (local nomic, incl. seed bank)"
        )
    if llm_ms:
        s = sorted(llm_ms)
        print(
            f"llm calls={len(s)} p50={s[len(s) // 2]:.0f}ms p95={s[int(len(s) * 0.95)]:.0f}ms avg_in_tok={sum(llm_tok) / len(llm_tok):.0f}"
        )
    Path(path).with_name("replay_out.json").write_text(json.dumps(out, indent=1))
    if client:
        Path(cache_path).write_text(json.dumps(llm_cache, indent=1))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], "--llm" in sys.argv))
