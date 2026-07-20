"""Weight-economy calibration harness.

Chooses the Hebbian constants by measurement instead of by feel, and reports what each
weight CONSUMER would actually see. Pure arithmetic — no brain boot, no API keys, so a
full sweep runs in milliseconds and can be re-run whenever the reward signal shifts.

Why this exists: weights are only useful at the handful of places a consumer reads them,
and each consumer has its own threshold. A rate that looks reasonable in isolation can
still leave every threshold permanently out of reach — which is exactly the state the
system was in (only 7 edges had ever left rest; the largest weight ever recorded was
1.0159 against thresholds starting at 1.167).

    uv run python eval/weight_economy_sim.py                 # report at current settings
    uv run python eval/weight_economy_sim.py --sweep         # sweep delta
    uv run python eval/weight_economy_sim.py --measure       # re-derive inputs from eval/turns.jsonl

MODEL. Per turn an edge either receives credit (probability c) or does not:

    gain_per_turn = c * o_bar * delta * m * scale
    w_equilibrium = 1 + gain_per_turn / r_turn          (clamped to [w_min, w_max])
    time constant = 1 / r_turn turns

`scale` is the credit surface the edge sits on: 1.0 for ordinary path credit, 0.5 for the
switch/recall routing helpers, 0.25 for co-activation. Competition edges do not use this
model at all — their delta is driven by the critic MARGIN, not the turn outcome, so they
are computed separately.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TURNS = REPO / "eval" / "turns.jsonl"

# ── Measured inputs ──────────────────────────────────────────────────────────
# o_bar and c come from real logged turns (see --measure). m is the combined
# plasticity modulator; both of its factors sit at their ceilings (1.2 and 1.3) for
# essentially all realistic chemistry, so the product is effectively constant at 1.56.
DEFAULTS = {
    "o_bar": 0.409,  # mean |outcome| on credited turns
    "c": 0.993,  # fraction of turns that receive credit (rest are skipped)
    "m": 1.56,  # plasticity_modulator * turn_plasticity, both at ceiling
    "margin": 0.15,  # typical critic margin, winner over mean loser
    "turns_per_session": 5,  # sleep_min_turns
}

SCALES = {"path": 1.0, "routing": 0.5, "coactivation": 0.25}

# ── What each consumer needs ─────────────────────────────────────────────────
# (label, surface, share, threshold)
#
# `share` matters for the RECALL consumers and is easy to miss. Recall credit is split
# by each strategy's contribution to the turn's hits, so a strategy that returns a
# seventh of them receives a seventh of the credit. Those consumers are therefore
# SHARE-gated, not time-gated: no amount of running crosses the threshold if the
# strategy keeps contributing little, which is the intended behaviour (the structural
# limit should widen only when structural recall is actually pulling its weight).
# Modelling them at a flat routing scale overstates them by ~4x.
CONSUMERS = [
    ("structural_limit 3→4 @ 1/7 share", "routing", 1 / 7, 1.167),
    ("structural_limit 3→4 @ 1/3 share", "routing", 1 / 3, 1.167),
    ("self_reference band top", "routing", 1.0, 1.400),
    ("epistemic_action band top", "routing", 1.0, 1.250),
    ("inhibitor floor bottoms", "path", 1.0, 1.500),
    ("fragment inject analogue", "path", 1.0, 1.300),
]


def equilibrium(delta, r_turn, scale, inp=DEFAULTS, w_min=0.10, w_max=3.00, share=1.0):
    gain = inp["c"] * inp["o_bar"] * delta * inp["m"] * scale * share
    return max(w_min, min(w_max, 1.0 + gain / r_turn))


def turns_to_reach(target, delta, r_turn, scale, inp=DEFAULTS, share=1.0):
    """Turns for an edge starting at rest to reach `target`. None if unreachable."""
    eq = equilibrium(delta, r_turn, scale, inp, share=share)
    if eq <= target:
        return None
    # w(n) = eq - (eq - 1) * exp(-n * r)
    return math.ceil(-math.log(1.0 - (target - 1.0) / (eq - 1.0)) / r_turn)


def competition_equilibrium(delta, r_turn, inp=DEFAULTS):
    """Drafter edges: credited by critic MARGIN, not turn outcome. Under credit purity
    this is now their ONLY input, so it alone sets the sampling spread."""
    bonus_scale = delta * 1.2  # plasticity at ceiling
    winner = inp["c"] * inp["margin"] * bonus_scale * 0.5
    loser = inp["c"] * inp["margin"] * bonus_scale * 0.25
    return 1.0 + winner / r_turn, 1.0 - loser / r_turn


def softmax_odds(w_hi, w_lo, temperature=0.20):
    return math.exp((w_hi - w_lo) / temperature)


def measure_from_log(path=TURNS):
    """Re-derive o_bar and c from logged turns. Returns (inputs, note)."""
    if not path.exists():
        return DEFAULTS, f"{path} not found — using stored defaults"
    applied, skipped = {}, set()
    for line in path.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        d, tid = r.get("decision"), r.get("turn_id")
        if d == "hebbian_update_applied" and tid and r.get("outcome") is not None:
            applied.setdefault(tid, abs(float(r["outcome"])))
        elif d == "hebbian_update_skipped" and tid:
            skipped.add(tid)
    if not applied:
        return DEFAULTS, "no hebbian_update_applied records — using stored defaults"
    out = dict(DEFAULTS)
    out["o_bar"] = statistics.mean(applied.values())
    out["c"] = len(applied) / (len(applied) + len(skipped))
    note = (
        f"measured from {len(applied)} credited + {len(skipped)} skipped turns. "
        "NOTE: m is NOT measured here — the logged deltas predate the current formula, "
        "so back-solving m from them yields a physically impossible ~6.4 (it is bounded "
        "to 1.56). m comes from the current code instead."
    )
    return out, note


def report(delta, r_turn, inp, w_min=0.10, w_max=3.00):
    tps = inp["turns_per_session"]
    print(f"\ninputs: o_bar={inp['o_bar']:.3f}  c={inp['c']:.3f}  m={inp['m']:.2f}")
    print(f"constants: hebbian_outcome_delta={delta}  decay_per_turn={r_turn}")
    print(f"time constant: {1 / r_turn:.0f} turns ≈ {1 / r_turn / tps:.1f} sessions\n")

    print(f"{'surface':<14} {'scale':>6} {'equilibrium':>12} {'at cap?':>9}")
    print("-" * 46)
    for name, scale in SCALES.items():
        eq = equilibrium(delta, r_turn, scale, inp, w_min, w_max)
        print(f"{name:<14} {scale:>6.2f} {eq:>12.3f} {'YES' if eq >= w_max - 0.01 else '':>9}")

    print(f"\n{'consumer':<34} {'needs':>7} {'reached':>9} {'turns':>9} {'sessions':>9}")
    print("-" * 72)
    for label, surface, share, thr in CONSUMERS:
        n = turns_to_reach(thr, delta, r_turn, SCALES[surface], inp, share)
        eq = equilibrium(delta, r_turn, SCALES[surface], inp, w_min, w_max, share)
        ok = "yes" if n is not None else "NEVER"
        turns = f"{n}" if n is not None else f"(eq {eq:.2f})"
        sess = f"{n / tps:.1f}" if n is not None else "—"
        print(f"{label:<34} {thr:>7.3f} {ok:>9} {turns:>9} {sess:>9}")

    win, lose = competition_equilibrium(delta, r_turn, inp)
    win, lose = min(w_max, win), max(w_min, lose)
    print("\ndrafter competition (credit purity makes this the ONLY driver)")
    print(f"  winner {win:.3f}   loser {lose:.3f}   gap {win - lose:.3f}")
    print(f"  softmax odds at T=0.20: {softmax_odds(win, lose):.1f}:1   (uniform = 1.0:1)")
    print(f"  floor-pinned? {'YES — recovery is slow' if lose <= w_min + 1e-9 else 'no'}")


def sweep(r_turn, inp):
    print(f"\n{'delta':>7} {'path eq':>9} {'routing':>9} {'coact':>9} {'draft odds':>11} {'sat?':>5}")
    print("-" * 54)
    for delta in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.14):
        p = equilibrium(delta, r_turn, 1.0, inp)
        rt = equilibrium(delta, r_turn, 0.5, inp)
        co = equilibrium(delta, r_turn, 0.25, inp)
        w, lo = competition_equilibrium(delta, r_turn, inp)
        sat = "YES" if p >= 2.99 else ""
        print(f"{delta:>7.3f} {p:>9.3f} {rt:>9.3f} {co:>9.3f} {softmax_odds(w, lo):>10.1f}:1 {sat:>5}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delta", type=float, default=None)
    ap.add_argument("--decay", type=float, default=None)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--measure", action="store_true")
    args = ap.parse_args()

    inp, note = (measure_from_log() if args.measure else (DEFAULTS, "stored defaults"))
    print(f"# {note}")

    try:
        import sys

        sys.path.insert(0, str(REPO))
        from brain.settings import settings

        delta = args.delta if args.delta is not None else float(settings.get("hebbian_outcome_delta"))
        decay = (
            args.decay
            if args.decay is not None
            else float(settings.get("decay_toward_rest_rate_per_turn", 0.01))
        )
        w_min = float(settings.get("weight_min", 0.10))
        w_max = float(settings.get("weight_max", 3.00))
    except Exception:
        delta, decay, w_min, w_max = args.delta or 0.02, args.decay or 0.01, 0.10, 3.00

    if args.sweep:
        sweep(decay, inp)
    else:
        report(delta, decay, inp, w_min, w_max)


if __name__ == "__main__":
    main()
