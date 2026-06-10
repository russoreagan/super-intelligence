"""
Flock-dynamics kp-sign validation — the gate for flipping flock_dynamics on.

The controller (brain/observability/criticality.py) nudges modulation_gain by
alpha * kp * (sigma - sigma_star). Whether kp must be negative depends on the
PLANT sign: does raising modulation_gain raise or lower sigma? Gain amplifies
each switch's modulation shift (eff = threshold + shift * gain), so the plant
sign is set by the switch population's NET shift under typical chemistry:
net-negative shift → higher gain lowers thresholds → more firing → sigma up
→ plant positive → kp must be NEGATIVE.

Two parts, both offline (no LLM, no live session):

  1. PLANT SIGN, empirically grounded: every literal `modulators={...}` dict in
     brain/clusters/*.py, evaluated against real recorded chemistry snapshots
     from eval/turns.jsonl, exactly as SwitchNeuron.effective_threshold would.
  2. CLOSED LOOP: the real FlockCriticality.control() driven against a plant of
     the measured sign for 60 steps — the sigma error must shrink. The same
     loop against the OPPOSITE plant sign must diverge (proves the check can
     fail, i.e. it is discriminative).

Usage: uv run python eval/flock_validate.py [path/to/turns.jsonl]
Exit 0 = kp sign validated (safe to set flock_dynamics: 1). Exit 1 otherwise.
"""

from __future__ import annotations

import ast
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

CLUSTERS_DIR = Path(__file__).parent.parent / "brain" / "clusters"
_MOD_RE = re.compile(r"modulators=\s*(\{[^}]*\})", re.S)


def collect_modulator_dicts() -> list[dict[str, float]]:
    """Every literal modulators={...} in the cluster sources — the real switch
    population's coefficients, without having to boot the clusters."""
    dicts: list[dict[str, float]] = []
    for path in sorted(CLUSTERS_DIR.glob("*.py")):
        for match in _MOD_RE.finditer(path.read_text(encoding="utf-8")):
            raw = re.sub(r"#[^\n]*", "", match.group(1))  # strip inline comments
            raw = raw.replace("+", "")  # ast chokes on unary + in dict values pre-3.9 style
            try:
                d = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                continue
            if isinstance(d, dict) and d:
                dicts.append({str(k): float(v) for k, v in d.items()})
    return dicts


def load_snapshots(turns_path: str, limit: int = 5000) -> list[dict[str, float]]:
    snaps: list[dict[str, float]] = []
    with open(turns_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            nm = rec.get("neuromod")
            if isinstance(nm, dict) and nm:
                snaps.append({k: float(v) for k, v in nm.items()})
            if len(snaps) >= limit:
                break
    return snaps


def mean_shift(mods: list[dict[str, float]], snap: dict[str, float]) -> float:
    """Population-mean threshold shift at gain=1 for one chemistry snapshot —
    the same sum effective_threshold computes (absent channels contribute 0)."""
    shifts = []
    for d in mods:
        s = 0.0
        for ch, coeff in d.items():
            level = snap.get(ch)
            if level is not None:
                s += coeff * (level - 0.5)
        shifts.append(s)
    return sum(shifts) / len(shifts)


def closed_loop_converges(plant_slope: float, kp: float, steps: int = 60) -> bool:
    """Drive the REAL controller against sigma = sigma0 + plant_slope*(gain-1).
    Returns True when |sigma - sigma*| shrinks to under half its initial value."""
    from brain.observability.criticality import FlockCriticality
    from brain.settings import settings

    settings.update({"flock_gain_kp": kp, "modulation_gain": 1.0})
    fc = FlockCriticality()
    gain = 1.0
    sigma0 = 1.15  # start super-critical so there is an error to correct
    arousal = 0.5
    err0 = None
    err = None
    for _ in range(steps):
        sigma = sigma0 + plant_slope * (gain - 1.0)
        # feed the smoothed-sigma window directly (bypasses fired-path estimation)
        fc._sigmas.append(sigma)
        ctrl = fc.control(arousal)
        gain = float(settings.get("modulation_gain", 1.0))
        err = abs(sigma - ctrl["sigma_star"])
        if err0 is None:
            err0 = err
    return err is not None and err0 is not None and err < 0.5 * err0


def main(turns_path: str) -> int:
    mods = collect_modulator_dicts()
    snaps = load_snapshots(turns_path)
    if not mods or not snaps:
        print(f"FAIL: insufficient data (switches={len(mods)}, snapshots={len(snaps)})")
        return 1

    # Plant sign in the OPERATING REGION. The whole-population median sits at
    # ~zero (the switch coefficients are deliberately balanced), so the global
    # sign is a coin flip. But the controller only pushes gain meaningfully at
    # high arousal (sigma* rises toward critical with arousal) — so the sign
    # that matters is the high-arousal one. Empirically (2026-06): corr(arousal,
    # shift) ≈ -0.93 and 100% of high-arousal snapshots are net-negative.
    scored = []
    for s in snaps:
        arousal = (s.get("Glu", 0.3) + s.get("NE", s.get("Glu", 0.3))) / 2.0
        scored.append((arousal, mean_shift(mods, s)))
    scored.sort()
    q = max(1, len(scored) // 4)
    hi_shifts = [sh for _, sh in scored[-q:]]
    med_hi = statistics.median(hi_shifts)
    neg_frac_hi = sum(1 for s in hi_shifts if s < 0) / len(hi_shifts)
    # net-negative shift → gain lowers thresholds → more firing → sigma rises
    plant_positive = med_hi < 0
    plant_slope = 0.4 if plant_positive else -0.4

    print(f"switch population:        {len(mods)} modulator dicts")
    print(f"chemistry snapshots:      {len(snaps)} real turns")
    print(f"high-arousal quartile:    median shift {med_hi:+.4f}, {neg_frac_hi:.0%} net-negative")
    print(f"inferred plant sign (operating region): "
          f"{'POSITIVE (gain ↑ → sigma ↑)' if plant_positive else 'NEGATIVE (gain ↑ → sigma ↓)'}")
    if neg_frac_hi < 0.8 and neg_frac_hi > 0.2:
        print("VERDICT: FAIL — plant sign ambiguous even in the operating region")
        return 1

    from brain.settings import settings

    kp = float(settings.get("flock_gain_kp", -0.30))
    saved_gain = float(settings.get("modulation_gain", 1.0))
    try:
        ok = closed_loop_converges(plant_slope, kp)
        discriminative = not closed_loop_converges(-plant_slope, kp)
    finally:
        settings.update({"modulation_gain": saved_gain, "flock_gain_kp": kp})

    print(f"configured kp:            {kp:+.2f}")
    print(f"closed loop converges:    {ok}")
    print(f"check is discriminative:  {discriminative} (opposite plant diverges)")

    if ok and discriminative:
        print("VERDICT: kp sign VALIDATED — safe to set flock_dynamics: 1")
        return 0
    needed = "-" if plant_positive else "+"
    print(f"VERDICT: FAIL — kp should be {needed}|kp| for this plant sign")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "eval/turns.jsonl"))
