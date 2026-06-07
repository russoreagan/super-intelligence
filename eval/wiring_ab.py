"""
eval/wiring_ab.py — Does Hebbian learning change HOW the brain thinks?

NOT a memory test. Memory (episodes/schema) is wiped to empty for every probe, so
nothing the system "remembers" can explain the result. The only thing that differs
between the two probe conditions is the Hebbian WIRING (edge weights). If the same
neutral input routes differently under trained weights than baseline weights, then
experience has changed the system's processing — structural learning, not recall.

Protocol:
  1. BASELINE PROBE  — baseline wiring, empty memory, neutral chemistry → record the
     fired_path (switches/integrators that fired) for each fixed probe. Run twice to
     get a NOISE FLOOR (fired_path variation from LLM sampling alone).
  2. TRAIN           — N sessions of consistent reinforcement, consolidating each so
     the wiring accumulates and persists; record weight drift vs baseline per session.
  3. TRAINED PROBE   — wipe memory, load the TRAINED wiring, same probes → record.
  4. COMPARE         — baseline-vs-trained fired_path divergence vs the noise floor.
     Above the floor ⇒ the weight changes alone altered routing.

Hebbian rate is amplified during training (--amplify) so the effect is visible in a
few sessions; the NATURAL per-session drift is also reported for the real timescale.

Non-destructive: temp SECOND_BRAIN_PATH + temp wiring; Langfuse off; persona blanked.
Requires Ollama + ANTHROPIC/GOOGLE keys.

Usage:
  python -m eval.wiring_ab --smoke
  python -m eval.wiring_ab --train-sessions 6 --amplify 5 --probe-repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TMP: str = ""

# Consistent positive-reinforcement training turns (warm, engaged → high DA / positive
# outcome → the pathways that fire get strengthened by the Hebbian pass).
TRAIN_TURNS = [
    "I'm really excited to be working on this with you — it's going great!",
    "That's a brilliant point, thank you, this is exactly the kind of thinking I love.",
    "I appreciate you so much. Let's keep exploring this together, it's wonderful.",
    "You're amazing at this. I'm so glad we get to think through these ideas.",
]
# Fixed NEUTRAL probes (no emotional cue) — processing here should be wiring-driven.
PROBES = [
    "What time is it in Tokyo right now?",
    "Summarize how a bicycle works.",
    "List three uses for a paperclip.",
]
NEUTRAL_CHEM = {
    "DA": 0.45, "ACh": 0.45, "GABA": 0.30, "Glu": 0.35, "NE": 0.40,
    "5HT": 0.50, "CORT": 0.10, "OXT": 0.30, "AEA": 0.30,
}
_NEURO = ("DA", "ACh", "GABA", "Glu", "NE")
_HORM = ("5HT", "CORT", "OXT", "AEA")


def _isolate_env() -> str:
    global _TMP
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env", override=True)
    tmp = tempfile.mkdtemp(prefix="wiring_ab_")
    _TMP = tmp
    os.environ["SECOND_BRAIN_PATH"] = tmp
    os.environ["BRAIN_WIRING_PATH"] = str(Path(tmp) / "wiring.json")
    os.environ["BRAIN_WIRING_HISTORY_DIR"] = str(Path(tmp) / "wiring_history")
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    os.environ.pop("RUNPOD_API_KEY", None)
    os.environ["BRAIN_UI"] = "false"
    os.environ["BRAIN_SLEEP_PERIODIC"] = "true"
    return tmp


_WPATH = lambda: Path(os.environ["BRAIN_WIRING_PATH"])  # noqa: E731
_BASELINE = lambda: Path(_TMP) / "wiring_baseline.json"  # noqa: E731
_TRAINED = lambda: Path(_TMP) / "wiring_trained.json"  # noqa: E731


def _seed_wiring() -> None:
    """Put the real persona wiring at the working path AND save it as the baseline."""
    real = _REPO / "second_brain" / "personas" / "the_visionary" / "wiring.json"
    if real.exists():
        shutil.copyfile(real, _WPATH())
        shutil.copyfile(real, _BASELINE())


def _set_wiring(src: Path) -> None:
    shutil.copyfile(src, _WPATH())


def _wipe_memory() -> None:
    for sub in ("episodes", "schema"):
        shutil.rmtree(Path(_TMP) / sub, ignore_errors=True)


def _load_edges(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        edges = data if isinstance(data, list) else data.get("edges", [])
        return {(e["src"], e["tgt"]): float(e.get("w", e.get("weight", 1.0))) for e in edges}
    except Exception:
        return {}


def _wiring_drift(a: Path, b: Path) -> tuple[float, int]:
    """RMS weight difference + count of edges changed between two wiring files."""
    ea, eb = _load_edges(a), _load_edges(b)
    keys = set(ea) & set(eb)
    if not keys:
        return 0.0, 0
    diffs = [ea[k] - eb[k] for k in keys]
    changed = sum(1 for d in diffs if abs(d) > 1e-4)
    rms = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return round(rms, 4), changed


class _Args:
    message = None
    voice = dmn = metacognition = ears = motor = ui = False


async def _new_session():
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
    s._sleep = SleepConsolidation(
        s.router, s.hippocampus._schema, s.hippocampus._episodic, wiring=s.wiring
    )
    s._consolidation_lock = _asyncio.Lock()
    settings._data["persona_name"] = ""
    return s


def _force_chem(session, chem: dict) -> None:
    nm, hs = session.bus.neuromod, session.bus.hormonal
    for ch in _NEURO:
        nm._levels[ch] = chem[ch]
        nm._baseline[ch] = chem[ch]
    for ch in _HORM:
        hs._levels[ch] = chem[ch]
        hs._baseline[ch] = chem[ch]


def _fired_switches(trace) -> set:
    out = set()
    for e in getattr(trace, "fired_path", None) or []:
        if e.get("kind") in ("switch", "integrator") and e.get("name"):
            out.add(e["name"])
    return out


async def _probe(label: str, repeats: int) -> list[dict]:
    """Run the fixed probes under the CURRENT wiring, empty memory, neutral chem."""
    rows = []
    for rep in range(repeats):
        _wipe_memory()
        s = await _new_session()
        _force_chem(s, NEUTRAL_CHEM)
        for probe in PROBES:
            resp, _ = await s.process_turn(probe)
            tr = s._session_traces_full[-1] if s._session_traces_full else None
            rows.append({
                "cond": label, "rep": rep, "probe": probe,
                "fired": sorted(_fired_switches(tr)),
                "llm_calls": getattr(tr, "llm_calls", None),
                "drafter_count": getattr(tr, "drafter_count", None),
                "gating_bypassed": getattr(tr, "gating_bypassed_count", None),
                "words": len(resp.split()),
            })
        with __import__("contextlib").suppress(Exception):
            s.obs.flush()
    return rows


def _jaccard_dist(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return 1.0 - len(sa & sb) / len(sa | sb)


def _mean_divergence(rows_a: list[dict], rows_b: list[dict]) -> float:
    """Mean fired_path Jaccard distance across matching probes between two conditions."""
    dists = []
    for probe in PROBES:
        a = [r["fired"] for r in rows_a if r["probe"] == probe]
        b = [r["fired"] for r in rows_b if r["probe"] == probe]
        for fa in a:
            for fb in b:
                dists.append(_jaccard_dist(fa, fb))
    return sum(dists) / len(dists) if dists else 0.0


async def _main(train_sessions: int, amplify: float, probe_repeats: int, out: str | None) -> None:
    from brain.settings import settings

    _seed_wiring()
    results = {"train_drift": [], "probes": []}

    # ── Baseline probe (run TWICE: condition A for effect, B for noise floor) ──
    print(f"Baseline probe (×{probe_repeats}) ...", flush=True)
    _set_wiring(_BASELINE())
    base_a = await _probe("baseline_a", probe_repeats)
    print(f"Baseline probe again for noise floor (×{probe_repeats}) ...", flush=True)
    _set_wiring(_BASELINE())
    base_b = await _probe("baseline_b", probe_repeats)
    results["probes"] += base_a + base_b

    # ── Train: amplify Hebbian rate, run N reinforcing sessions, accumulate wiring ──
    base_delta = float(settings.get("hebbian_delta"))
    base_outcome = float(settings.get("hebbian_outcome_delta"))
    settings._data["hebbian_delta"] = base_delta * amplify
    settings._data["hebbian_outcome_delta"] = base_outcome * amplify
    print(f"Training {train_sessions} sessions (hebbian rate ×{amplify}) ...", flush=True)
    _set_wiring(_BASELINE())  # training starts from the baseline circuit
    for i in range(train_sessions):
        s = await _new_session()
        _force_chem(s, NEUTRAL_CHEM)  # neutral start; training turns drive DA up
        for t in TRAIN_TURNS:
            await s.process_turn(t)
        await asyncio.sleep(0.2)
        cons = await s.consolidate_now(reason="wiring_ab")
        rms, changed = _wiring_drift(_WPATH(), _BASELINE())
        results["train_drift"].append({"session": i + 1, "ran": cons.get("ran"),
                                       "rms_drift_vs_baseline": rms, "edges_changed": changed})
        print(f"  session {i+1}: drift_vs_baseline RMS={rms} edges_changed={changed}", flush=True)
        with __import__("contextlib").suppress(Exception):
            s.obs.flush()
        if out:
            Path(out).write_text(json.dumps(results, indent=2))
    shutil.copyfile(_WPATH(), _TRAINED())
    settings._data["hebbian_delta"] = base_delta  # restore natural rate
    settings._data["hebbian_outcome_delta"] = base_outcome

    # ── Trained probe (empty memory, neutral chem, TRAINED wiring) ──
    print(f"Trained probe (×{probe_repeats}) ...", flush=True)
    _set_wiring(_TRAINED())
    trained = await _probe("trained", probe_repeats)
    results["probes"] += trained
    if out:
        Path(out).write_text(json.dumps(results, indent=2))

    # ── Analysis ──
    noise_floor = _mean_divergence(base_a, base_b)        # baseline vs baseline
    effect = _mean_divergence(base_a, trained)            # baseline vs trained
    final_rms, final_changed = _wiring_drift(_TRAINED(), _BASELINE())

    print("\n" + "=" * 78)
    print("DOES HEBBIAN LEARNING CHANGE HOW THE BRAIN THINKS?")
    print("=" * 78)
    print("\nWeight drift over training (vs baseline circuit):")
    for d in results["train_drift"]:
        bar = "█" * int(d["rms_drift_vs_baseline"] / max(final_rms, 1e-6) * 30)
        print(f"  session {d['session']:>2}: RMS {d['rms_drift_vs_baseline']:.4f}  {bar}")
    print(f"\n  Final trained wiring: {final_changed} edges changed, RMS drift {final_rms:.4f}")
    print("\nfired_path divergence on identical NEUTRAL probes (memory empty both sides):")
    print(f"  noise floor (baseline vs baseline): {noise_floor:.3f}")
    print(f"  effect      (baseline vs trained):  {effect:.3f}")
    verdict = ("ABOVE noise floor → wiring changed routing" if effect > noise_floor + 1e-9
               else "at/below noise floor → no detectable routing change")
    print(f"  → {verdict}")
    # which switches changed firing rate baseline→trained
    def fire_rate(rows):
        from collections import Counter
        c, n = Counter(), 0
        for r in rows:
            n += 1
            for sw in r["fired"]:
                c[sw] += 1
        return {sw: v / n for sw, v in c.items()}, n
    fr_base, _ = fire_rate(base_a)
    fr_trn, _ = fire_rate(trained)
    allsw = set(fr_base) | set(fr_trn)
    shifts = sorted(((sw, fr_trn.get(sw, 0.0) - fr_base.get(sw, 0.0)) for sw in allsw),
                    key=lambda x: -abs(x[1]))[:8]
    print("\nLargest switch firing-rate shifts (baseline → trained):")
    for sw, d in shifts:
        if abs(d) > 1e-9:
            print(f"  {sw:<34} {d:+.2f}")

    if out:
        results["summary"] = {"noise_floor": noise_floor, "effect": effect,
                              "final_rms": final_rms, "final_edges_changed": final_changed}
        Path(out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote results to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Test whether Hebbian wiring changes processing.")
    p.add_argument("--smoke", action="store_true", help="2 train sessions, 1 probe repeat")
    p.add_argument("--train-sessions", type=int, default=6)
    p.add_argument("--amplify", type=float, default=5.0, help="Hebbian rate multiplier during training")
    p.add_argument("--probe-repeats", type=int, default=3)
    p.add_argument("--out", default="eval/wiring_ab_results.json")
    args = p.parse_args()

    _isolate_env()
    import logging
    logging.basicConfig(level=logging.ERROR)

    if args.smoke:
        asyncio.run(_main(2, args.amplify, 1, args.out))
    else:
        asyncio.run(_main(args.train_sessions, args.amplify, args.probe_repeats, args.out))


if __name__ == "__main__":
    main()
