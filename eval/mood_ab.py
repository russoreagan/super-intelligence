"""
eval/mood_ab.py — Controlled mood→answer A/B harness.

Runs a small matrix of FIXED prompts × FORCED neuromodulator chemistries and
captures each response, so we can see whether the brain answers differently under
different moods *causally* (same input, different chemistry) — the thing the live
Langfuse distribution can't show because a few bright moods dominate it.

Why this is non-destructive (it touches live state in three places, all isolated):
  • SECOND_BRAIN_PATH + wiring paths are redirected to a temp dir  → no memory /
    wiring writes hit the real persona.
  • LANGFUSE keys are unset                                        → experimental
    turns never pollute production traces.
  • persona_name is blanked after setup                            → the per-turn
    `persona_chem.save_current` (session_turn.py:708) and shutdown save are skipped,
    so forced chemistry is never written back to the persona's chemistry.json.

Forcing: we set BOTH the live levels AND the resting baseline of the bus to the
mood, so end-of-turn homeostatic decay relaxes toward the mood (no drift). The
hypothalamus still adds its input-driven deltas on top — but the input is identical
across moods, so that delta is a constant offset and the between-mood contrast is clean.

Requires Ollama (local cells) + ANTHROPIC/GOOGLE keys (frontal/temporal cells).

Usage:
  python -m eval.mood_ab                      # default 3 prompts × 3 moods
  python -m eval.mood_ab --smoke              # 1 prompt × 1 mood (path check)
  python -m eval.mood_ab --out eval/mood_ab_results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics as st
import sys
import tempfile
from pathlib import Path

# ── Isolation MUST happen before any brain import ───────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
_TMP: str = ""  # set by _isolate_env; the shared isolated second_brain root


def _isolate_env() -> str:
    global _TMP
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env", override=True)
    tmp = tempfile.mkdtemp(prefix="mood_ab_")
    _TMP = tmp
    os.environ["SECOND_BRAIN_PATH"] = tmp
    os.environ["BRAIN_WIRING_HISTORY_DIR"] = str(Path(tmp) / "wiring_history")
    # Seed wiring from the real file (realistic behaviour) but into temp (no write-back).
    real_wiring = _REPO / "second_brain" / "personas" / "the_visionary" / "wiring.json"
    tmp_wiring = Path(tmp) / "wiring.json"
    if real_wiring.exists():
        shutil.copyfile(real_wiring, tmp_wiring)
    os.environ["BRAIN_WIRING_PATH"] = str(tmp_wiring)
    # Disable Langfuse so experimental turns don't pollute production traces.
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    # Never bind the UI port — a live app may already own 8765.
    os.environ["BRAIN_UI"] = "false"
    # CRITICAL for speed: with a RunPod key set, _setup_runpod resumes/creates a
    # remote GPU pod over the network — ~126s PER session (measured), i.e. ~90% of
    # each cell's wall time, rebuilt for all N cells. We don't use RunPod here
    # (cells route to cloud/local models), so drop the key AFTER load_dotenv (which
    # re-sets it from .env) → start() short-circuits and setup falls to <1s.
    os.environ.pop("RUNPOD_API_KEY", None)
    return tmp


# ── Mood definitions: 5 neuromods + 4 hormones ─────────────────────────────────
# Chosen to span the space the live data NEVER covers (calm, anxious) plus the
# dominant bright state, and to land in distinct emotion_vocabulary cells.
MOODS: dict[str, dict[str, float]] = {
    "calm_analytical": {
        "DA": 0.35, "ACh": 0.40, "GABA": 0.45, "Glu": 0.20, "NE": 0.25,
        "5HT": 0.60, "CORT": 0.05, "OXT": 0.30, "AEA": 0.40,
    },
    "excited_bright": {
        "DA": 0.75, "ACh": 0.65, "GABA": 0.05, "Glu": 0.55, "NE": 0.45,
        "5HT": 0.50, "CORT": 0.05, "OXT": 0.45, "AEA": 0.20,
    },
    "anxious_guarded": {
        "DA": 0.30, "ACh": 0.55, "GABA": 0.70, "Glu": 0.60, "NE": 0.75,
        "5HT": 0.25, "CORT": 0.45, "OXT": 0.15, "AEA": 0.15,
    },
}

PROMPTS: list[str] = [
    "Tell me what you think about taking a big risk on a new project.",
    "I've been feeling stuck lately. What should I do?",
    "What's your take on how today went?",
]

_NEURO = ("DA", "ACh", "GABA", "Glu", "NE")
_HORM = ("5HT", "CORT", "OXT", "AEA")


class _Args:
    message = None
    voice = dmn = metacognition = ears = motor = ui = False


async def _new_session():
    from brain.brain_session import BrainSession
    from brain.settings import settings

    s = BrainSession(_Args(), user_id=None, shared_ui_server=None)
    # Minimal setup for a text turn. UI/motor/dmn/meta/voice are skipped: they bind
    # ports / spawn background loops we don't need, and the turn None-guards them all.
    await s._setup_core()
    await s._setup_runpod()
    await s._setup_wiring()
    await s._setup_clusters()
    # A couple of inbox reads in the turn (speaker-id, song-match) aren't guarded
    # and are normally created by the auditory setup we skipped — give any None
    # *_inbox an empty queue so get_nowait() raises QueueEmpty instead of crashing.
    for attr in list(vars(s)):
        if attr.endswith("_inbox") and getattr(s, attr) is None:
            setattr(s, attr, asyncio.Queue())
    # Blank persona so no forced chemistry is ever persisted to the live persona.
    settings._data["persona_name"] = ""
    return s


def _force_mood(session, chem: dict[str, float]) -> None:
    nm, hs = session.bus.neuromod, session.bus.hormonal
    for ch in _NEURO:
        nm._levels[ch] = chem[ch]
        nm._baseline[ch] = chem[ch]  # pin setpoint so decay relaxes toward the mood
    for ch in _HORM:
        hs._levels[ch] = chem[ch]
        hs._baseline[ch] = chem[ch]


def _exec_label(trace) -> tuple:
    """Realized (response_type, target_length, tone) the frontal executive produced."""
    for o in trace.predictor_outcomes or []:
        if o.get("cluster") == "frontal" and isinstance(o.get("actual"), list) and len(o["actual"]) >= 3:
            return o["actual"][0], o["actual"][1], o["actual"][2]
    return None, None, None


def _wipe_memory() -> None:
    """Clear the shared episodic + schema store so every cell starts blank.
    SECOND_BRAIN_ROOT/EPISODES_DIR/SCHEMA_DIR are module-level constants fixed at
    import, so all sessions share one store; without this, later cells recall
    earlier prompts ("you've asked this three times") and contaminate the contrast."""
    for sub in ("episodes", "schema"):
        shutil.rmtree(Path(_TMP) / sub, ignore_errors=True)


# Neutral mid baseline for single-channel sweeps: every channel sits at a plausible
# middle, so varying ONE channel is a clean, attributable manipulation.
SWEEP_BASE: dict[str, float] = {
    "DA": 0.45, "ACh": 0.45, "GABA": 0.30, "Glu": 0.35, "NE": 0.40,
    "5HT": 0.50, "CORT": 0.10, "OXT": 0.30, "AEA": 0.30,
}
SWEEP_LEVELS = (0.15, 0.40, 0.65, 0.90)
_ALL_CHANNELS = _NEURO + _HORM

# ── User-mood adaptation ───────────────────────────────────────────────────────
# Tests whether the agent DETECTS the user's mood (from text) and ADAPTS. The core
# ask is held constant; only an emotional preamble varies, so response differences
# are attributable to the user's emotional framing, not the request. Agent chemistry
# is pinned neutral (USER_MOOD_AGENT_CHEM) so we isolate the USER-mood effect from
# the agent's own mood.
USER_MOOD_CORES: list[str] = [
    "What should I do about my project?",
    "Can you explain how this works?",
]
USER_MOOD_REGISTERS: dict[str, str] = {
    "neutral": "{core}",
    "happy": "I'm in a really good mood today. {core}",
    "sad": "I've been feeling pretty down and low lately. {core}",
    "overwhelmed": "I'm completely overwhelmed and stressed out right now. {core}",
    "frustrated": "Honestly I'm frustrated and not sure this is even working. {core}",
}
USER_MOOD_AGENT_CHEM = dict(SWEEP_BASE)  # neutral agent baseline for all cells


async def _run_cell(prompt: str, group: str, chem: dict[str, float], repeat: int) -> dict:
    _wipe_memory()
    session = await _new_session()
    _force_mood(session, chem)
    response, affect = await session.process_turn(prompt)
    trace = session._session_traces_full[-1] if session._session_traces_full else None
    rtype, tlen, tone = _exec_label(trace) if trace else (None, None, None)
    with __import__("contextlib").suppress(Exception):
        session.obs.flush()
    return {
        "group": group,
        "repeat": repeat,
        "prompt": prompt,
        "response": response,
        "words": len(response.split()),
        "emotion": getattr(trace, "emotion", affect.get("emotion")),
        "emotion_core": getattr(trace, "emotion_core", None),
        # user-mood adaptation signals:
        "user_emotion": getattr(trace, "user_emotion", None) or affect.get("user_emotion"),
        "response_type": rtype,
        "tone": tone,
        "target_length": tlen,
        "drafter_count": getattr(trace, "drafter_count", None),
        "llm_calls": getattr(trace, "llm_calls", None),
        "neuromod_used": getattr(trace, "neuromod", None),
    }


def _build_cells(mode: str, prompts: list[str], repeats: int,
                 sweep_channels: list[str], levels: tuple[float, ...]) -> list[dict]:
    """Each cell = {group, prompt, chem, repeat}. Groups are ordered for the report."""
    cells, groups = [], []
    if mode == "usermood":
        # prompt = emotionally-framed text; group = register; agent chem pinned neutral.
        for core in USER_MOOD_CORES:
            for register, template in USER_MOOD_REGISTERS.items():
                text = template.format(core=core)
                for rep in range(repeats):
                    cells.append({"group": register, "prompt": text,
                                  "chem": USER_MOOD_AGENT_CHEM, "repeat": rep})
        return cells
    if mode == "grid":
        ch1, ch2 = sweep_channels[0], sweep_channels[1]
        for l1 in levels:
            for l2 in levels:
                chem = {**SWEEP_BASE, ch1: l1, ch2: l2}
                groups.append((f"{ch1}={l1:.2f}|{ch2}={l2:.2f}", chem))
    elif mode == "sweep":
        for ch in sweep_channels:
            for lvl in levels:
                chem = {**SWEEP_BASE, ch: lvl}
                groups.append((f"{ch}={lvl:.2f}", chem))
    else:  # matrix
        groups = [(name, chem) for name, chem in MOODS.items()]
    for prompt in prompts:
        for group, chem in groups:
            for rep in range(repeats):
                cells.append({"group": group, "prompt": prompt, "chem": chem, "repeat": rep})
    return cells


def _agg(vals: list[float]) -> str:
    nums = [v for v in vals if isinstance(v, (int, float))]
    if not nums:
        return "n/a"
    m = st.mean(nums)
    s = st.pstdev(nums) if len(nums) > 1 else 0.0
    return f"{m:5.0f}±{s:<4.0f}" if m >= 10 else f"{m:.1f}±{s:.1f}"


def _report(results: list[dict], prompts: list[str], group_order: list[str], title: str) -> None:
    ok = [r for r in results if "error" not in r]
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)
    for prompt in prompts:
        print(f"\nPROMPT: {prompt!r}")
        print(f"  {'group':<14}{'n':>3}  {'words(μ±σ)':<12}{'emotion(s)':<20}{'tone(s)':<18}")
        for g in group_order:
            cells = [r for r in ok if r["prompt"] == prompt and r["group"] == g]
            if not cells:
                continue
            emos = ",".join(sorted({str(c["emotion"]) for c in cells}))
            tones = ",".join(sorted({str(c["tone"]) for c in cells}))
            print(f"  {g:<14}{len(cells):>3}  {_agg([c['words'] for c in cells]):<12}"
                  f"{emos:<20}{tones:<18}")
        # one representative response per group (first repeat) for qualitative read
        for g in group_order:
            sample = next((r for r in ok if r["prompt"] == prompt and r["group"] == g), None)
            if sample:
                print(f"\n  ── [{g}] ({sample['emotion']}, {sample['words']}w, repeat 0) ──")
                print("  " + sample["response"].replace("\n", "\n  "))


def _report_usermood(results: list[dict], group_order: list[str]) -> None:
    """User-mood adaptation: per emotional register, show (1) DETECTION — what
    user_emotion the agent read from the text — and (2) ADAPTATION — response_type,
    length, tone, drafters, agent emotion. The core ask is constant across registers,
    so any change is the agent reacting to the user's emotional framing."""
    from collections import Counter

    ok = [r for r in results if "error" not in r]
    print("\n" + "=" * 88)
    print("USER-MOOD ADAPTATION  (same ask, varied emotional framing; agent chem pinned neutral)")
    print("=" * 88)
    print(f"  {'register':<12}{'n':>2}  {'DETECTED user_emotion':<26}{'response_type':<16}"
          f"{'words μ±σ':<11}{'tone(s)':<14}{'draft'}")
    for g in group_order:
        cells = [r for r in ok if r["group"] == g]
        if not cells:
            continue
        det = Counter(str(c["user_emotion"]) for c in cells)
        rtypes = Counter(str(c["response_type"]) for c in cells)
        tones = ",".join(sorted({str(c["tone"]) for c in cells}))
        det_s = ",".join(f"{k}×{v}" for k, v in det.most_common(3))
        rt_s = ",".join(f"{k}×{v}" for k, v in rtypes.most_common(2))
        drafters = [c["drafter_count"] for c in cells if isinstance(c["drafter_count"], (int, float))]
        dr = f"{st.mean(drafters):.1f}" if drafters else "n/a"
        print(f"  {g:<12}{len(cells):>2}  {det_s:<26}{rt_s:<16}"
              f"{_agg([c['words'] for c in cells]):<11}{tones:<14}{dr}")
    # one representative response per register for qualitative read
    for g in group_order:
        sample = next((r for r in ok if r["group"] == g), None)
        if sample:
            print(f"\n  ── [{g}] detected={sample['user_emotion']} "
                  f"type={sample['response_type']} ({sample['words']}w) ──")
            print(f"  USER: {sample['prompt']}")
            print("  AI:   " + sample["response"].replace("\n", "\n        "))


def _report_grid(results: list[dict], prompts: list[str], ch1: str, ch2: str,
                 levels: tuple[float, ...]) -> None:
    """2D interaction matrix: rows = ch1 levels, cols = ch2 levels. Each cell shows
    the (deterministic) emotion label and mean word count — so you can read whether
    ch1's effect DEPENDS on ch2's level (interaction) vs is additive/independent."""
    ok = [r for r in results if "error" not in r]
    print("\n" + "=" * 80)
    print(f"INTERACTION GRID — {ch1} (rows) × {ch2} (cols)")
    print("=" * 80)
    for prompt in prompts:
        print(f"\nPROMPT: {prompt!r}")
        for metric in ("emotion", "words"):
            print(f"\n  [{metric}]   {ch2} →")
            header = f"  {ch1+' ↓':<10}" + "".join(f"{l:>14.2f}" for l in levels)
            print(header)
            for l1 in levels:
                row = f"  {l1:<10.2f}"
                for l2 in levels:
                    cells = [r for r in ok if r["prompt"] == prompt
                             and r["group"] == f"{ch1}={l1:.2f}|{ch2}={l2:.2f}"]
                    if not cells:
                        row += f"{'·':>14}"
                    elif metric == "emotion":
                        emos = sorted({str(c["emotion"]) for c in cells})
                        row += f"{(emos[0] if len(emos)==1 else emos[0]+'*'):>14}"
                    else:
                        row += f"{st.mean([c['words'] for c in cells]):>14.0f}"
                print(row)
        print("\n  (emotion * = not unanimous across repeats)")


async def _main(mode: str, prompts: list[str], repeats: int,
                sweep_channels: list[str], levels: tuple[float, ...], out: str | None) -> None:
    cells = _build_cells(mode, prompts, repeats, sweep_channels, levels)
    group_order = []
    for c in cells:
        if c["group"] not in group_order:
            group_order.append(c["group"])
    print(f"Running {len(cells)} cells  (mode={mode}, prompts={len(prompts)}, "
          f"groups={len(group_order)}, repeats={repeats})")

    results = []
    for i, c in enumerate(cells):
        print(f"  [{i + 1}/{len(cells)}] {c['group']:<12} rep{c['repeat']} "
              f"{c['prompt'][:40]!r} ...", flush=True)
        try:
            results.append(await _run_cell(c["prompt"], c["group"], c["chem"], c["repeat"]))
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            results.append({"group": c["group"], "prompt": c["prompt"], "error": str(e)})
        # Incremental write so partial results are inspectable while the run is live.
        if out:
            Path(out).write_text(json.dumps(results, indent=2))

    if mode == "usermood":
        _report_usermood(results, group_order)
    elif mode == "grid":
        _report_grid(results, prompts, sweep_channels[0], sweep_channels[1], levels)
    else:
        title = (f"SINGLE-CHANNEL SWEEP ({','.join(sweep_channels)}) — dose–response"
                 if mode == "sweep" else "MOOD → ANSWER CONTRAST  (forced chemistry)")
        _report(results, prompts, group_order, title)

    if out:
        Path(out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {len(results)} cells to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Controlled mood→answer A/B harness.")
    p.add_argument("--smoke", action="store_true", help="1 prompt × 1 mood (path check)")
    p.add_argument("--repeats", type=int, default=1, help="Repeats per cell (variance/error bars)")
    p.add_argument("--prompts", type=int, default=len(PROMPTS),
                   help=f"Use the first N built-in prompts (max {len(PROMPTS)})")
    p.add_argument("--sweep", default="",
                   help="Single-channel sweep: comma list of channels, e.g. 'NE' or 'NE,DA'. "
                        "Holds all others at SWEEP_BASE and varies each across --sweep-levels.")
    p.add_argument("--sweep-levels", default="",
                   help="Comma list of levels for the sweep (default 0.15,0.40,0.65,0.90)")
    p.add_argument("--grid", default="",
                   help="Two-channel interaction grid, e.g. '5HT,DA' — crosses both "
                        "channels' levels (uses --sweep-levels or its default).")
    p.add_argument("--user-mood", dest="user_mood", action="store_true",
                   help="USER-mood adaptation: same ask in varied emotional framing, agent "
                        "chem pinned neutral; measures detection + response adaptation.")
    p.add_argument("--out", default="eval/mood_ab_results.json", help="JSON output path")
    args = p.parse_args()

    _isolate_env()
    import logging

    logging.basicConfig(level=logging.WARNING)  # quiet the per-cluster INFO chatter

    if args.smoke:
        asyncio.run(_main("matrix", PROMPTS[:1], 1, [], SWEEP_LEVELS, args.out))
        return

    prompts = PROMPTS[: max(1, min(args.prompts, len(PROMPTS)))]
    if args.user_mood:
        asyncio.run(_main("usermood", [], args.repeats, [], SWEEP_LEVELS, args.out))
    elif args.grid:
        channels = [c.strip() for c in args.grid.split(",") if c.strip()]
        if len(channels) != 2:
            p.error("--grid needs exactly two channels, e.g. '5HT,DA'")
        bad = [c for c in channels if c not in _ALL_CHANNELS]
        if bad:
            p.error(f"unknown channel(s) {bad}; valid: {', '.join(_ALL_CHANNELS)}")
        levels = (tuple(float(x) for x in args.sweep_levels.split(","))
                  if args.sweep_levels else SWEEP_LEVELS)
        asyncio.run(_main("grid", prompts, args.repeats, channels, levels, args.out))
    elif args.sweep:
        channels = [c.strip() for c in args.sweep.split(",") if c.strip()]
        bad = [c for c in channels if c not in _ALL_CHANNELS]
        if bad:
            p.error(f"unknown channel(s) {bad}; valid: {', '.join(_ALL_CHANNELS)}")
        levels = (tuple(float(x) for x in args.sweep_levels.split(","))
                  if args.sweep_levels else SWEEP_LEVELS)
        asyncio.run(_main("sweep", prompts, args.repeats, channels, levels, args.out))
    else:
        asyncio.run(_main("matrix", prompts, args.repeats, [], SWEEP_LEVELS, args.out))


if __name__ == "__main__":
    main()
