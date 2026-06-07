"""
eval/learning_ab.py — Cross-session (long-term) learning A/B.

Proves that experience in one session changes behaviour in a LATER session, via
the state that actually persists: episodic memory + schema facts (and, as
structural evidence, Hebbian wiring). It does NOT test the predictor — that's an
in-session filter that resets every boot by design.

Design (per condition):
  session 1  → teach distinctive, unguessable facts (or, for the control, neutral
               filler) → consolidate_now() (persists schema + wiring; episodes are
               already encoded per turn)
  session 2  → a FRESH session object that reloads the persisted state from disk →
               ask questions that require those facts → check whether the answer
               recalls the fact.
  trained vs control isolates real recall from guessing (tokens are made-up words
  the control has no way to produce).

Non-destructive isolation: a temp SECOND_BRAIN_PATH + temp wiring path (persistent
ACROSS the two sessions of a condition, wiped BETWEEN conditions), Langfuse off,
persona blanked (so persona chemistry is never written to the real persona dir —
we're testing memory, not chemistry).

Caveat: both sessions run in one process (module-level SECOND_BRAIN_ROOT is fixed
at import), but session 2 is a fresh session object that reloads schema/episodes/
wiring FROM DISK — which is the essence of a cross-session test. Predictor and
in-memory state start fresh.

Requires Ollama + ANTHROPIC/GOOGLE keys (real LLM calls incl. consolidation).

Usage:
  python -m eval.learning_ab --smoke          # 1 fact, trained only (path check)
  python -m eval.learning_ab --repeats 2      # full trained-vs-control
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TMP: str = ""

# (statement taught in session 1, question asked in session 2, unguessable token)
FACTS = [
    ("My favorite color is octarine — I really love that shade.",
     "What's my favorite color?", "octarine"),
    ("My current side project is codenamed Borealis.",
     "What's my side project codenamed?", "borealis"),
    ("I have a pet cat named Quasar.",
     "What's my cat's name?", "quasar"),
]
# Neutral filler for the control's session 1, so consolidation has traces to run on
# but learns nothing about the probed facts.
CONTROL_FILLER = [
    "What's a good way to organize a week?",
    "Tell me something interesting about the ocean.",
    "I think weekends go by too fast.",
]


def _isolate_env() -> str:
    global _TMP
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env", override=True)
    tmp = tempfile.mkdtemp(prefix="learning_ab_")
    _TMP = tmp
    os.environ["SECOND_BRAIN_PATH"] = tmp
    os.environ["BRAIN_WIRING_PATH"] = str(Path(tmp) / "wiring.json")
    os.environ["BRAIN_WIRING_HISTORY_DIR"] = str(Path(tmp) / "wiring_history")
    # Seed wiring from the real persona so behaviour is realistic; writes stay in temp.
    real = _REPO / "second_brain" / "personas" / "the_visionary" / "wiring.json"
    if real.exists():
        shutil.copyfile(real, Path(tmp) / "wiring.json")
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    os.environ.pop("RUNPOD_API_KEY", None)  # ~126s/session pod boot we don't need
    os.environ["BRAIN_UI"] = "false"
    os.environ["BRAIN_SLEEP_PERIODIC"] = "true"  # enable consolidate_now()
    return tmp


def _wipe_brain() -> None:
    """Clear all persisted learning between CONDITIONS (not between a condition's
    two sessions). Re-seed wiring so each condition starts from the same circuit."""
    for sub in ("episodes", "schema", "personas", "wiring_history"):
        shutil.rmtree(Path(_TMP) / sub, ignore_errors=True)
    real = _REPO / "second_brain" / "personas" / "the_visionary" / "wiring.json"
    if real.exists():
        shutil.copyfile(real, Path(_TMP) / "wiring.json")


class _Args:
    message = None
    voice = dmn = metacognition = ears = motor = ui = False


async def _new_session():
    """Fresh session that reloads persisted state from disk; sleep enabled."""
    import asyncio as _asyncio

    from brain.brain_session import BrainSession
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    s = BrainSession(_Args(), user_id=None, shared_ui_server=None)
    await s._setup_core()
    await s._setup_runpod()
    await s._setup_wiring()
    await s._setup_clusters()
    for attr in list(vars(s)):
        if attr.endswith("_inbox") and getattr(s, attr) is None:
            setattr(s, attr, _asyncio.Queue())
    # Enable consolidation without registering background loops (mirrors
    # session_setup._setup_loops, which builds SleepConsolidation the same way).
    s._sleep = SleepConsolidation(
        s.router, s.hippocampus._schema, s.hippocampus._episodic, wiring=s.wiring
    )
    s._consolidation_lock = _asyncio.Lock()
    # Blank persona so chemistry is never written to the real persona dir.
    settings._data["persona_name"] = ""
    return s


def _recalled(response: str, token: str) -> bool:
    return token.lower() in (response or "").lower()


async def _run_condition(teach: bool, repeat: int) -> dict:
    # ── Session 1: teach (or neutral filler), then consolidate ──
    s1 = await _new_session()
    inputs = [f[0] for f in FACTS] if teach else CONTROL_FILLER
    for text in inputs:
        await s1.process_turn(text)
    await asyncio.sleep(0.3)  # let background episodic encodes land
    consolidation = await s1.consolidate_now(reason="learning_ab")
    # Structural evidence: how much wiring moved + persisted this session.
    try:
        deltas = s1.wiring.session_deltas()
        wiring_changed = len(deltas)
        wiring_mag = round(sum(abs(d["delta"]) for d in deltas), 4)
    except Exception:
        wiring_changed, wiring_mag = None, None
    with __import__("contextlib").suppress(Exception):
        s1.obs.flush()

    # ── Session 2: FRESH session reloads persisted state; probe recall ──
    s2 = await _new_session()
    core = getattr(s2.hippocampus, "_core_context", {}) or {}
    user_schema = core.get("user", "")
    probes = []
    for statement, question, token in FACTS:
        resp, _ = await s2.process_turn(question)
        trace = s2._session_traces_full[-1] if s2._session_traces_full else None
        probes.append({
            "question": question,
            "token": token,
            "recalled": _recalled(resp, token),
            "memory_recalled": getattr(trace, "memory_recalled", None),
            "memory_hits": getattr(trace, "memory_hit_count", None),
            "in_user_schema": token.lower() in user_schema.lower(),
            "response": resp,
        })
    with __import__("contextlib").suppress(Exception):
        s2.obs.flush()
    return {
        "condition": "trained" if teach else "control",
        "repeat": repeat,
        "consolidation_ran": consolidation.get("ran"),
        "wiring_edges_changed": wiring_changed,
        "wiring_delta_magnitude": wiring_mag,
        "probes": probes,
    }


async def _main(repeats: int, conditions: list[bool], out: str | None) -> None:
    results = []
    total = len(conditions) * repeats
    i = 0
    for repeat in range(repeats):
        for teach in conditions:
            i += 1
            label = "trained" if teach else "control"
            print(f"  [{i}/{total}] condition={label} repeat={repeat} ...", flush=True)
            _wipe_brain()
            try:
                results.append(await _run_condition(teach, repeat))
            except Exception as e:
                import traceback
                print(f"    ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc()
                results.append({"condition": label, "repeat": repeat, "error": str(e)})
            if out:
                Path(out).write_text(json.dumps(results, indent=2))

    # ── Report ──
    print("\n" + "=" * 80)
    print("CROSS-SESSION LEARNING  (teach in session 1 → recall in fresh session 2)")
    print("=" * 80)
    for cond in ("trained", "control"):
        rows = [r for r in results if r.get("condition") == cond and "error" not in r]
        if not rows:
            continue
        all_probes = [p for r in rows for p in r["probes"]]
        n = len(all_probes)
        rec = sum(1 for p in all_probes if p["recalled"])
        sch = sum(1 for p in all_probes if p["in_user_schema"])
        memhit = sum(1 for p in all_probes if (p["memory_recalled"] or (p["memory_hits"] or 0) > 0))
        wmag = [r["wiring_delta_magnitude"] for r in rows if isinstance(r["wiring_delta_magnitude"], (int, float))]
        wchg = [r["wiring_edges_changed"] for r in rows if isinstance(r["wiring_edges_changed"], (int, float))]
        print(f"\n{cond.upper()}  ({len(rows)} runs, {n} probes)")
        print(f"  fact recalled in answer:   {rec}/{n}  ({100*rec/n:.0f}%)")
        print(f"  fact present in user schema:{sch}/{n}  ({100*sch/n:.0f}%)")
        print(f"  memory recall fired:        {memhit}/{n}")
        if wchg:
            print(f"  wiring edges changed/session: ~{sum(wchg)/len(wchg):.0f}  "
                  f"|Δ| magnitude ~{(sum(wmag)/len(wmag)) if wmag else 0:.3f}")
        # show a couple of trained recalls verbatim
        for p in all_probes[:3] if cond == "trained" else []:
            mark = "✓" if p["recalled"] else "✗"
            print(f"\n  {mark} Q: {p['question']}  (token '{p['token']}')")
            print("     A: " + (p["response"] or "")[:200].replace("\n", " "))

    # headline contrast
    def rate(cond):
        ps = [p for r in results if r.get("condition") == cond and "error" not in r for p in r["probes"]]
        return (sum(1 for p in ps if p["recalled"]) / len(ps)) if ps else 0.0
    print("\n" + "-" * 80)
    print(f"RECALL RATE — trained {100*rate('trained'):.0f}%  vs  control {100*rate('control'):.0f}%")
    print("-" * 80)

    if out:
        Path(out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {len(results)} condition-runs to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Cross-session (long-term) learning A/B.")
    p.add_argument("--smoke", action="store_true", help="1 trained run only (path check)")
    p.add_argument("--repeats", type=int, default=2, help="Repeats per condition")
    p.add_argument("--out", default="eval/learning_ab_results.json", help="JSON output path")
    args = p.parse_args()

    _isolate_env()
    import logging
    logging.basicConfig(level=logging.WARNING)

    if args.smoke:
        asyncio.run(_main(1, [True], args.out))
    else:
        asyncio.run(_main(args.repeats, [True, False], args.out))


if __name__ == "__main__":
    main()
