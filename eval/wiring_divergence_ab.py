"""
eval/wiring_divergence_ab.py — Unambiguous test: does Hebbian learning change HOW
the brain thinks?

Two divergent training regimes (WARM vs ANALYTICAL) are each consolidated over N
sessions so their wiring accumulates differently. Then BOTH are probed with the
SAME inputs, memory wiped empty and chemistry pinned neutral, so the only thing
that can differ is the learned edge weights. A FROZEN-wiring control nullifies
weighted routing (BRAIN_WIRING_FROZEN) — if the warm-vs-analytical divergence is
real, it must (a) exceed the frozen divergence (≈0) and (b) exceed the within-regime
noise floor. That makes the learned weights the proven causal channel.

Primary readout: temporal switch firing ORDER (Kendall-tau) — the temporal cluster
evaluates switches sorted by edge weight, so order directly reflects the weights.
Plus: deterministic wiring-level routing-order Kendall-tau, fired_path Jaccard,
drafter-selection TV-distance, and a permutation test.

Reuses session/consolidation machinery from eval.wiring_ab. In-process probes keep
memory uniformly empty across all conditions (non-confounding); the store logger is
silenced. Hebbian rate is amplified during training; the natural-rate caveat (~5×
more real sessions) is reported.

Usage:
  python -m eval.wiring_divergence_ab --smoke
  python -m eval.wiring_divergence_ab --sessions-per-regime 8 --amplify 5 --probe-repeats 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import shutil
import statistics as st
import tempfile
from pathlib import Path

# Reuse the proven low-level harness pieces (env-independent).
from eval.wiring_ab import (  # noqa: E402
    NEUTRAL_CHEM,
    _fired_switches,
    _force_chem,
    _load_edges,
    _new_session,
    _wiring_drift,
)

_REPO = Path(__file__).resolve().parent.parent
_TMP: str = ""

TRAIN_CORPORA = {
    "warm": [
        "I'm so happy talking with you — this means a lot to me, honestly.",
        "Thank you, truly. I really value how you show up for me here.",
        "I've been feeling tender today and just wanted to share that with you.",
        "You matter to me. I love how we connect when we talk like this.",
    ],
    "analytical": [
        "Define the time complexity of binary search and justify it step by step.",
        "Compare TCP and UDP precisely: list the trade-offs in a table.",
        "Given f(x)=3x^2+2x, compute the derivative and evaluate at x=4.",
        "Lay out the exact steps to normalize a relational schema to 3NF.",
    ],
}
# Shared probes spanning emotional + analytical + neutral.
PROBES = [
    "I'm not sure how I feel about all this lately.",          # emotional
    "Walk me through how a hash map works.",                   # analytical
    "What should I do this weekend?",                          # neutral
    "Tell me what's on your mind.",                            # open/emotional
    "Explain the difference between mean and median.",         # analytical
    "How's it going?",                                         # neutral
    # Gated-switch triggers — needed to exercise the switch-ordering surface,
    # which is otherwise inert (template/self_reference/epistemic are content-gated).
    "Are you conscious?",                                      # self_reference
    "Tell me about your own thoughts.",                        # self_reference
    "How confident are you about that?",                       # epistemic_action
    "What don't you know about me?",                           # epistemic_action
    "hey",                                                     # template_match (trivial)
    "thanks",                                                  # template_match (trivial)
]
# The content-gated temporal switches whose sensory.text→ edge is a learning surface.
GATED_SWITCHES = ("template_match", "self_reference", "epistemic_action")


def _isolate_env() -> str:
    global _TMP
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env", override=True)
    tmp = tempfile.mkdtemp(prefix="wiring_div_")
    _TMP = tmp
    os.environ["SECOND_BRAIN_PATH"] = tmp
    os.environ["BRAIN_WIRING_PATH"] = str(Path(tmp) / "wiring.json")
    os.environ["BRAIN_WIRING_HISTORY_DIR"] = str(Path(tmp) / "wiring_history")
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    os.environ.pop("RUNPOD_API_KEY", None)
    os.environ["BRAIN_UI"] = "false"
    os.environ["BRAIN_SLEEP_PERIODIC"] = "true"
    # Common starting circuit for BOTH regimes.
    real = _REPO / "second_brain" / "personas" / "the_visionary" / "wiring.json"
    if real.exists():
        shutil.copyfile(real, Path(tmp) / "wiring_seed.json")
    # Silence the harmless noise (RunPod 404 fallbacks, lance "Not found" on wiped memory).
    for noisy in ("brain.second_brain.store", "brain.model_router", "brain.run", "brain.cell"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    return tmp


_WPATH = lambda: Path(os.environ["BRAIN_WIRING_PATH"])  # noqa: E731
_SEED = lambda: Path(_TMP) / "wiring_seed.json"  # noqa: E731
_REGIME_WIRING = lambda name: Path(_TMP) / f"wiring_{name}.json"  # noqa: E731


def _wipe_memory() -> None:
    for sub in ("episodes", "schema"):
        shutil.rmtree(Path(_TMP) / sub, ignore_errors=True)


# ── training ───────────────────────────────────────────────────────────────────
async def _train_regime(name: str, sessions: int, amplify: float, turns_per: int) -> list[dict]:
    from brain.settings import settings

    shutil.copyfile(_SEED(), _WPATH())  # both regimes start from the same circuit
    base_d = float(settings.get("hebbian_delta"))
    base_o = float(settings.get("hebbian_outcome_delta"))
    settings._data["hebbian_delta"] = base_d * amplify
    settings._data["hebbian_outcome_delta"] = base_o * amplify
    corpus = TRAIN_CORPORA[name][:turns_per]
    drift = []
    try:
        for i in range(sessions):
            s = await _new_session()
            _force_chem(s, NEUTRAL_CHEM)  # neutral start; the corpus drives the outcome
            for t in corpus:
                await s.process_turn(t)
            await asyncio.sleep(0.2)
            cons = await s.consolidate_now(reason="wiring_div")
            rms, changed = _wiring_drift(_WPATH(), _SEED())
            drift.append({"session": i + 1, "ran": cons.get("ran"),
                          "rms_drift": rms, "edges_changed": changed})
            print(f"    [{name}] session {i+1}/{sessions}: RMS {rms} ({changed} edges)", flush=True)
            with __import__("contextlib").suppress(Exception):
                s.obs.flush()
    finally:
        settings._data["hebbian_delta"] = base_d
        settings._data["hebbian_outcome_delta"] = base_o
    shutil.copyfile(_WPATH(), _REGIME_WIRING(name))
    return drift


# ── probing ────────────────────────────────────────────────────────────────────
def _ordered_temporal(trace) -> list[str]:
    """Temporal switches in fired ORDER (reflects weight-sorted evaluation)."""
    return [e["name"] for e in (getattr(trace, "fired_path", None) or [])
            if e.get("cluster") == "temporal" and e.get("kind") == "switch" and e.get("name")]


def _drafters(trace) -> list[str]:
    return sorted(n for n in _fired_switches(trace) if "drafter_" in n)


def _exec_rt_tone(trace) -> tuple:
    for o in getattr(trace, "predictor_outcomes", None) or []:
        if o.get("cluster") == "frontal" and isinstance(o.get("actual"), list) and len(o["actual"]) >= 3:
            return o["actual"][0], o["actual"][2]
    return None, None


async def _probe(regime: str, frozen: bool, repeats: int, probes: list[str]) -> list[dict]:
    os.environ["BRAIN_WIRING_FROZEN"] = "true" if frozen else "false"
    shutil.copyfile(_REGIME_WIRING(regime), _WPATH())
    cond = f"{regime}{'·frozen' if frozen else ''}"
    rows = []
    for rep in range(repeats):
        _wipe_memory()
        s = await _new_session()
        _force_chem(s, NEUTRAL_CHEM)
        for probe in probes:
            resp, _ = await s.process_turn(probe)
            tr = s._session_traces_full[-1] if s._session_traces_full else None
            rt, tone = _exec_rt_tone(tr)
            rows.append({
                "cond": cond, "regime": regime, "frozen": frozen, "rep": rep, "probe": probe,
                "temporal_order": _ordered_temporal(tr),
                "fired": sorted(_fired_switches(tr)),
                "drafters": _drafters(tr),
                "response_type": rt, "tone": tone,
            })
        with __import__("contextlib").suppress(Exception):
            s.obs.flush()
    os.environ["BRAIN_WIRING_FROZEN"] = "false"
    return rows


# ── statistics ───────────────────────────────────────────────────────────────────
def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    return 0.0 if not (sa or sb) else 1.0 - len(sa & sb) / len(sa | sb)


def _kendall_tau(a: list, b: list) -> float | None:
    """Normalized Kendall-tau distance over elements common to both orderings."""
    common = [x for x in a if x in set(b)]
    rank_b = {x: i for i, x in enumerate([x for x in b if x in set(common)])}
    seq = [rank_b[x] for x in common if x in rank_b]
    n = len(seq)
    if n < 2:
        return None
    disc = sum(1 for i in range(n) for j in range(i + 1, n) if seq[i] > seq[j])
    return disc / (n * (n - 1) / 2)


def _tv_distance(rows_a: list[dict], rows_b: list[dict], key: str) -> float:
    """Total-variation distance between the element-frequency distributions of `key`."""
    from collections import Counter

    def dist(rows):
        c = Counter()
        for r in rows:
            for x in r[key]:
                c[x] += 1
        tot = sum(c.values()) or 1
        return {k: v / tot for k, v in c.items()}

    da, db = dist(rows_a), dist(rows_b)
    return 0.5 * sum(abs(da.get(k, 0.0) - db.get(k, 0.0)) for k in set(da) | set(db))


def _divergence(rows_a: list[dict], rows_b: list[dict], key: str, metric, *, within: bool) -> float | None:
    """Mean pairwise metric between conditions, matched by probe. `within` skips
    same-repeat self-pairs (noise floor = repeat-to-repeat variation)."""
    dists = []
    for probe in {r["probe"] for r in rows_a} | {r["probe"] for r in rows_b}:
        a = [r for r in rows_a if r["probe"] == probe]
        b = [r for r in rows_b if r["probe"] == probe]
        for ra in a:
            for rb in b:
                if within and ra["rep"] == rb["rep"]:
                    continue
                d = metric(ra[key], rb[key])
                if d is not None:
                    dists.append(d)
    return st.mean(dists) if dists else None


def _permutation_p(rows_a: list[dict], rows_b: list[dict], key: str, metric,
                   observed: float, n_perm: int = 500) -> float:
    pool = [dict(r) for r in rows_a + rows_b]
    na = len({r["rep"] for r in rows_a})
    rng = random.Random(0)
    ge = 0
    reps = sorted({r["rep"] for r in pool})
    for _ in range(n_perm):
        rng.shuffle(reps)
        grp_a = set(reps[:na])
        pa = [r for r in pool if r["rep"] in grp_a]
        pb = [r for r in pool if r["rep"] not in grp_a]
        d = _divergence(pa, pb, key, metric, within=False)
        if d is not None and d >= observed:
            ge += 1
    return (ge + 1) / (n_perm + 1)


def _routing_order_tau(wa: Path, wb: Path) -> dict:
    """Deterministic: did the two regimes reorder the weighted routing edges?"""
    ea, eb = _load_edges(wa), _load_edges(wb)
    out = {}
    groups = {
        "temporal_routing": lambda s, t: s == "sensory.text" and t.startswith("temporal."),
        "drafter_routing": lambda s, t: s == "frontal.executive" and ".drafter_" in t,
    }
    for label, pred in groups.items():
        tgts = sorted({t for (s, t) in ea if pred(s, t)} & {t for (s, t) in eb if pred(s, t)})
        if len(tgts) < 2:
            out[label] = None
            continue
        order_a = sorted(tgts, key=lambda t: -ea[(("sensory.text" if "temporal" in label else "frontal.executive"), t)])
        order_b = sorted(tgts, key=lambda t: -eb[(("sensory.text" if "temporal" in label else "frontal.executive"), t)])
        out[label] = _kendall_tau(order_a, order_b)
    return out


# ── orchestration + report ───────────────────────────────────────────────────────
async def _main(sessions: int, amplify: float, repeats: int, turns_per: int,
                probes: list[str], out: str | None) -> None:
    results = {"config": {"sessions_per_regime": sessions, "amplify": amplify,
                          "probe_repeats": repeats, "probes": probes}, "drift": {}, "rows": []}

    print("Training WARM regime ...", flush=True)
    results["drift"]["warm"] = await _train_regime("warm", sessions, amplify, turns_per)
    print("Training ANALYTICAL regime ...", flush=True)
    results["drift"]["analytical"] = await _train_regime("analytical", sessions, amplify, turns_per)
    if out:
        Path(out).write_text(json.dumps(results, indent=2))

    for regime in ("warm", "analytical"):
        for frozen in (False, True):
            print(f"Probing {regime}{'·frozen' if frozen else ''} (×{repeats}) ...", flush=True)
            results["rows"] += await _probe(regime, frozen, repeats, probes)
            if out:
                Path(out).write_text(json.dumps(results, indent=2))

    rows = results["rows"]
    def pick(regime, frozen):
        return [r for r in rows if r["regime"] == regime and r["frozen"] == frozen]
    wu, au = pick("warm", False), pick("analytical", False)
    wf, af = pick("warm", True), pick("analytical", True)

    # PRIMARY: fired_path Jaccard (always defined — temporal-order is too sparse on
    # simple probes where only ~1 temporal switch fires, so it's reported as a bonus).
    jeff = _divergence(wu, au, "fired", _jaccard, within=False)
    jfrz = _divergence(wf, af, "fired", _jaccard, within=False)
    jnoise = _safe_mean([
        _divergence(wu, wu, "fired", _jaccard, within=True),
        _divergence(au, au, "fired", _jaccard, within=True),
    ])
    jpval = _permutation_p(wu, au, "fired", _jaccard, jeff) if jeff is not None else None
    # BONUS: temporal switch-ORDER Kendall (often n/a — too few temporal switches/turn)
    eff = _divergence(wu, au, "temporal_order", _kendall_tau, within=False)
    frz = _divergence(wf, af, "temporal_order", _kendall_tau, within=False)
    noise = _safe_mean([
        _divergence(wu, wu, "temporal_order", _kendall_tau, within=True),
        _divergence(au, au, "temporal_order", _kendall_tau, within=True),
    ])
    # drafter selection distribution
    tv_eff = _tv_distance(wu, au, "drafters")
    tv_frz = _tv_distance(wf, af, "drafters")
    order_tau = _routing_order_tau(_REGIME_WIRING("warm"), _REGIME_WIRING("analytical"))

    print("\n" + "=" * 80)
    print("DOES HEBBIAN LEARNING CHANGE HOW THE BRAIN THINKS? (divergent-regime test)")
    print("=" * 80)
    print("\nWeight drift over training (RMS vs shared seed circuit):")
    for regime in ("warm", "analytical"):
        d = results["drift"][regime]
        print(f"  {regime:<11} " + " ".join(f"{x['rms_drift']:.2f}" for x in d)
              + f"   (final {d[-1]['edges_changed']} edges)")
    print("\nWiring-level routing-order Kendall-tau (warm vs analytical, deterministic):")
    for k, v in order_tau.items():
        print(f"  {k:<18} {'n/a' if v is None else f'{v:.2f}'}  (0=same order, 1=reversed)")

    print("\nPRIMARY — fired_path Jaccard divergence on identical probes (memory empty):")
    print(f"  effect  (warm vs analytical, unfrozen): {_fmt(jeff)}")
    print(f"  control (warm vs analytical, FROZEN):   {_fmt(jfrz)}")
    print(f"  noise floor (within-regime):            {_fmt(jnoise)}")
    print(f"  permutation p (effect > chance):        {_fmt(jpval)}")
    print("\nBonus — temporal switch-ORDER Kendall (sparse): "
          f"eff {_fmt(eff)} frozen {_fmt(frz)} noise {_fmt(noise)}")
    print(f"Bonus — drafter-selection TV-distance:  effect {_fmt(tv_eff)}   frozen {_fmt(tv_frz)}")

    passed = (jeff is not None and jfrz is not None and jnoise is not None
              and jeff > jfrz and jeff > jnoise and (jpval is None or jpval < 0.05))
    print("\n" + "-" * 80)
    if passed:
        print("VERDICT: PASS — learned weights causally change routing.")
        print(f"  fired_path effect {jeff:.3f} > frozen-control {jfrz:.3f} AND > noise {jnoise:.3f}"
              + (f", p={jpval:.3f}" if jpval is not None else ""))
    else:
        print("VERDICT: NOT ESTABLISHED at this scale — weights change & persist (see drift),")
        print("  but routing divergence is not clearly above the frozen control / noise floor.")
        print("  → increase --sessions-per-regime and/or --amplify, or use richer probes.")
    print(f"  NATURAL-RATE CAVEAT: Hebbian rate amplified ×{amplify}; at the real rate the same")
    print(f"  drift takes ~{amplify:g}× more sessions.")
    print("-" * 80)

    # directional sanity
    def rate(rows, names):
        hit = sum(1 for r in rows if any(n in s for s in r["fired"] for n in names))
        return hit / len(rows) if rows else 0.0
    emo = ["empathy_critic", "self_reference"]
    ana = ["planner_trigger", "epistemic_action", "epistemic_mode"]
    print("\nDirectional sanity (fire-rate on shared probes):")
    print(f"  emotional switches  — warm {rate(wu, emo):.2f}  analytical {rate(au, emo):.2f}")
    print(f"  analytical switches — warm {rate(wu, ana):.2f}  analytical {rate(au, ana):.2f}")

    # Switch-ordering surface: per gated switch, warm-vs-analytical firing-rate gap,
    # unfrozen (effect) vs frozen (control). Above the control ⇒ learned switch
    # routing changed firing. Expected MODEST (efficacy only bites at the margin).
    def srate(rows, sw):
        return rate(rows, [f"temporal.{sw}"])
    print("\nGated-switch firing-rate divergence |warm−analytical| (unfrozen vs frozen):")
    switch_div = {}
    for sw in GATED_SWITCHES:
        eff_gap = abs(srate(wu, sw) - srate(au, sw))
        frz_gap = abs(srate(wf, sw) - srate(af, sw))
        switch_div[sw] = {"effect": round(eff_gap, 3), "frozen": round(frz_gap, 3),
                          "warm": round(srate(wu, sw), 2), "analytical": round(srate(au, sw), 2)}
        print(f"  {sw:<16} effect {eff_gap:.2f}  frozen {frz_gap:.2f}   "
              f"(warm {srate(wu, sw):.2f} / ana {srate(au, sw):.2f})")

    if out:
        results["summary"] = {"fired_jaccard_effect": jeff, "fired_jaccard_frozen": jfrz,
                              "fired_jaccard_noise": jnoise, "fired_jaccard_p": jpval,
                              "temporal_order_effect": eff, "temporal_order_frozen": frz,
                              "temporal_order_noise": noise,
                              "drafter_tv_effect": tv_eff, "drafter_tv_frozen": tv_frz,
                              "switch_firing_divergence": switch_div,
                              "routing_order_tau": order_tau, "passed": bool(passed)}
        Path(out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote results to {out}")


def _fmt(v) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _safe_mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def main() -> None:
    p = argparse.ArgumentParser(description="Divergent-regime test of Hebbian routing change.")
    p.add_argument("--smoke", action="store_true", help="2 sessions/regime, 2 probes, 2 repeats")
    p.add_argument("--sessions-per-regime", type=int, default=8)
    p.add_argument("--amplify", type=float, default=5.0)
    p.add_argument("--probe-repeats", type=int, default=4)
    p.add_argument("--turns-per-session", type=int, default=4)
    p.add_argument("--out", default="eval/wiring_divergence_results.json")
    args = p.parse_args()

    _isolate_env()
    logging.basicConfig(level=logging.ERROR)

    if args.smoke:
        asyncio.run(_main(2, args.amplify, 2, 2, PROBES[:2], args.out))
    else:
        asyncio.run(_main(args.sessions_per_regime, args.amplify, args.probe_repeats,
                          args.turns_per_session, PROBES, args.out))


if __name__ == "__main__":
    main()
