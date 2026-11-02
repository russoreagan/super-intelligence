"""Check that the live chemistry relaxes instead of saturating (2026-11 fix).

Replays the two prod shapes from 2026-11-01 against the real chemistry code
(brain/bus.py, brain/clusters/hypothalamus.py), no network, no prod reads:

  small_talk  mundane chat (sentiment 0.3, salience 0.4) with a once-per-turn
              post-draft reward (DA +0.09, ACh +0.06, NE +0.02: pride, critic,
              accomplishment), at gaps of 25 s (farrah, org 0f3c064f), 60 s and
              180 s. Prod went DA 0.61 → 1.0 and ACh → 1.0 in 8 turns.
  idle_mind   the DMN loop on the resting pair with no turns: a tick every 8 s
              or 45 s, each thought nudging DA +0.03, GABA +0.04, 5HT +0.01
              (inside the ±0.06 chem_delta clamp), the loop's idle relaxation
              between ticks. Prod held DA = GABA = 1.0 from 10-21.

  self_reward small talk again, but the per-turn self-graded DA comes from the
              real self-standard rule (brain/self_standard.py): draft quality
              0.06 x surprise, craft 0.04 x margin over the running craft bar,
              for a persona whose critic scores sit around 0.88 / craft 0.85
              (prod-like: +0.08 DA a turn under the absolute bars).

Each runs with the guards on (default) and off (chem_turn_impulse_settle,
dmn_idle_relax_all, dmn_idle_settle_impulses = 0) and prints where the channels
end and the largest value they reach. Then the time to relax back to baseline
after the burst. Usage:
    .venv/bin/python eval/chem_saturation_sim.py [--turns 30] [--minutes 60]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

GUARDS = ("chem_turn_impulse_settle", "dmn_idle_relax_all", "dmn_idle_settle_impulses")
FEATURES = {
    "sentiment": 0.3,
    "hostility": 0.0,
    "salience": 0.4,
    "surprise_score": 0.2,
    "intent": "statement",
    "topic_summary": "small talk",
}
REWARD = {"DA": 0.09, "ACh": 0.06, "NE": 0.02}
THOUGHT = {"DA": 0.03, "GABA": 0.04, "5HT": 0.01}


def _set_guards(on: bool) -> None:
    from brain.settings import settings

    for k in GUARDS:
        settings._data[k] = 1 if on else 0


def _self_graded_da(i: int, rng, relative: bool) -> float:
    """One turn's draft-quality + craft DA under the absolute or relative bar."""
    from brain import self_standard
    from brain.settings import settings

    settings._data["self_reward_relative"] = 1 if relative else 0
    overall = min(1.0, max(0.0, rng.gauss(0.88, 0.04)))
    craft = min(1.0, max(0.0, rng.gauss(0.85, 0.05)))
    tid = f"sim{i}"
    share = 1.0
    if relative:
        share = self_standard.surprise(
            overall, self_standard.appraise("overall", overall, tid, persona="sim")
        )
    da = 0.06 * share if overall > 0.7 else 0.0
    gate = 0.75
    if relative:
        gate = max(gate, self_standard.appraise("craft", craft, tid, persona="sim"))
    if craft > gate:
        da += 0.04 * (craft - gate) / (1.0 - gate)
    return da


def small_talk(
    gap_s: float, turns: int, on: bool, self_reward: str = ""
) -> tuple[dict, dict, float]:
    """(final levels, peak levels, seconds to relax DA within 0.02 of baseline).
    self_reward "" uses the fixed REWARD; "absolute"/"relative" computes the DA
    part with the self-standard rule, "none" pays no self-graded DA (the user's
    sentiment alone); ACh/NE stay as in REWARD."""
    import random

    from brain import self_standard
    from brain.bus import Bus
    from brain.clusters.hypothalamus import HypothalamusCluster

    _set_guards(on)
    self_standard.reset()
    rng = random.Random(7)
    bus = Bus()
    hyp = HypothalamusCluster(bus)
    nm = bus.neuromod
    peak = dict(nm.snapshot())
    for i in range(turns):
        hyp._last_decay_time = time.monotonic() - gap_s  # the gap before this turn
        asyncio.run(hyp.process(dict(FEATURES)))
        reward = dict(REWARD)
        if self_reward == "none":
            reward["DA"] = 0.0
        elif self_reward:
            reward["DA"] = _self_graded_da(i, rng, self_reward == "relative")
        for ch, d in reward.items():  # post-draft, once per turn whatever the pace
            nm.add(ch, d)
        hyp.decay_turn()
        for ch, v in nm.snapshot().items():
            peak[ch] = max(peak[ch], v)
    final = nm.snapshot()
    return final, peak, _relax_seconds(bus.resting_chem, "DA")


def idle_mind(tick_s: float, minutes: float, on: bool) -> tuple[dict, dict, float]:
    """The DMN loop's order per tick: relax for the elapsed time, then the tick's
    impulses, then settle them (dmn.py _idle_decay / _settle_idle_impulses)."""
    from brain.bus import Bus
    from brain.settings import settings

    _set_guards(on)
    bus = Bus()
    pair = bus.resting_chem
    ref = float(settings.get("decay_reference_interval_s"))
    t, prev = 0.0, None
    pair.decay_clock["relaxed_at"] = t
    peak = {**pair.neuromod.snapshot(), **pair.hormonal.snapshot()}
    while t < minutes * 60:
        t += tick_s
        if on:
            pair.relax_elapsed(t)
        start = {**pair.neuromod.snapshot(), **pair.hormonal.snapshot()}
        for ch, d in THOUGHT.items():
            (pair.hormonal if ch in pair.hormonal.CHANNELS else pair.neuromod).add(ch, d)
        if on:
            pair.settle_impulses(start, 1.0 if prev is None else (t - prev) / ref)
            prev = t
        for ch, v in {**pair.neuromod.snapshot(), **pair.hormonal.snapshot()}.items():
            peak[ch] = max(peak[ch], v)
    final = {**pair.neuromod.snapshot(), **pair.hormonal.snapshot()}
    return final, peak, _relax_seconds(pair, "DA") if on else float("inf")


def _relax_seconds(pair, ch: str, tol: float = 0.02, step_s: float = 15.0) -> float:
    """Idle relaxation after the burst until ``ch`` is within tol of its baseline."""
    layer = pair.neuromod
    t = 0.0
    pair.decay_clock.clear()
    pair.decay_clock["relaxed_at"] = 0.0
    while abs(layer.get(ch) - layer.baseline(ch)) > tol and t < 3 * 3600:
        t += step_s
        pair.relax_elapsed(t)
    return t


def _fmt(levels: dict, chans) -> str:
    return "  ".join(f"{c}={levels[c]:.2f}" for c in chans)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=30)
    ap.add_argument("--minutes", type=float, default=60.0)
    args = ap.parse_args()
    from brain.bus import Bus

    base = Bus().neuromod
    print("baselines: " + "  ".join(f"{c}={base.baseline(c):.2f}" for c in base.CHANNELS))
    chans = ("DA", "ACh", "NE", "GABA")
    print(f"\nsmall_talk, {args.turns} turns + once-per-turn reward {REWARD}")
    for gap in (25.0, 60.0, 180.0):
        for on in (False, True):
            final, peak, relax = small_talk(gap, args.turns, on)
            tag = "guards on " if on else "guards off"
            print(
                f"  gap {gap:>5.0f}s {tag}  end {_fmt(final, chans)}   "
                f"peak DA={peak['DA']:.2f} ACh={peak['ACh']:.2f}   "
                f"DA→baseline {relax / 60:.1f} min"
            )
    print(f"\nself_reward, {args.turns} turns at 25 s, guards on, critic ~0.88 / craft ~0.85")
    for mode in ("absolute", "relative", "none"):
        final, peak, _ = small_talk(25.0, args.turns, True, self_reward=mode)
        print(f"  {mode:<9} bar   end {_fmt(final, chans)}   peak DA={peak['DA']:.2f}")
    print(f"\nidle_mind, {args.minutes:.0f} min of ticks, each thought {THOUGHT}")
    for tick in (8.0, 45.0):
        for on in (False, True):
            final, peak, relax = idle_mind(tick, args.minutes, on)
            tag = "guards on " if on else "guards off"
            relax_s = "never" if relax == float("inf") else f"{relax / 60:.1f} min"
            print(
                f"  tick {tick:>4.0f}s {tag}  end {_fmt(final, ('DA', 'GABA', '5HT'))}   "
                f"peak DA={peak['DA']:.2f} GABA={peak['GABA']:.2f}   DA→baseline {relax_s}"
            )


if __name__ == "__main__":
    main()
