"""Project allostatic learning for prod-shaped personas before shipping a setting.

Runs brain/allostasis.py's real learning code against synthetic turn traces shaped
like the 2026-10-19 prod audit (no network, no prod reads, a temp state root):

  analyst_trading  ~1 session/day, 6 turns, tool successes with the odd failure,
                   low stress, the same trading-review trigger every day
  adversary_debate ~2 sessions/day, 8 turns, ends wound up (GABA ≈ 0.75, NE ≈ 0.7),
                   no grounded outcomes, the same debate trigger — predictable
  empath_therapy   ~1 session/day, 5 turns, the bond with one person rising,
                   mild stress the persona resolves (controllable)
  hostile_user     ~1 session/day, 4 turns, unresolved hostility and failures

Prints, per persona, the learned offsets, reward rate, load, the top reactivity
gains and the bond at day 7 / 30 / 90. Usage:
    .venv/bin/python eval/allostasis_sim.py [--days 90]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DAY = 86400.0
T0 = 1_760_000_000.0
TOOL_OK = {"delta": 0.145, "signal_type": "self_graded", "reason": "tool_success"}
TOOL_FAIL = {"delta": -0.087, "signal_type": "self_graded", "reason": "tool_failure"}


def _trace(
    persona, sid, i, *, eu, emissions=(), nm=None, hs=None, key="", imp=None, bond=0.0, aff=0
):
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id=f"{sid}-{i}", session_id=sid, user_input="x")
    t.persona_name, t.end_user_id = persona, eu
    t.reward_emissions = [dict(e) for e in emissions]
    t.neuromod, t.hormonal = dict(nm or {}), dict(hs or {})
    t.trigger_key, t.appraisal_impulses = key, dict(imp or {})
    t.bond, t.affection = bond, aff
    return t


def _day_traces(profile: str, d: int) -> list:
    out = []
    if profile == "analyst_trading":
        for i in range(6):
            em = [TOOL_OK] if i in (1, 3) else ([TOOL_FAIL] if (i == 5 and d % 4 == 0) else [])
            out.append(
                _trace(
                    "the_analyst",
                    f"a{d}",
                    i,
                    eu="russ",
                    emissions=em,
                    nm={"GABA": 0.37, "NE": 0.29},
                    key="request|neutral",
                    imp={"ACh": 0.03, "DA": 0.01},
                )
            )
    elif profile == "adversary_debate":
        for s in range(2):
            for i in range(8):
                out.append(
                    _trace(
                        "the_adversary",
                        f"v{d}{s}",
                        i,
                        eu="russ",
                        nm={"GABA": 0.75, "NE": 0.69},
                        hs={"CORT": 0.4},
                        key="debate|challenging",
                        imp={"GABA": 0.04, "NE": 0.03},
                    )
                )
    elif profile == "empath_therapy":
        b = min(0.9, 0.2 + d * 0.01)
        for i in range(5):
            out.append(
                _trace(
                    "the_empath",
                    f"e{d}",
                    i,
                    eu="client",
                    emissions=[
                        {"delta": 0.04, "signal_type": "external_user", "reason": "sentiment"}
                    ],
                    nm={"GABA": 0.2},
                    key="disclosure|vulnerable",
                    imp={"GABA": 0.02, "DA": 0.02},
                    bond=b,
                    aff=int(60 * b),
                )
            )
    elif profile == "hostile_user":
        for i in range(4):
            out.append(
                _trace(
                    "the_stoic",
                    f"h{d}",
                    i,
                    eu="troll",
                    emissions=[TOOL_FAIL],
                    nm={"GABA": 0.8, "NE": 0.8},
                    hs={"CORT": 0.6},
                    key="hostile|hostile",
                    imp={"GABA": 0.06, "NE": 0.05},
                )
            )
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SECOND_BRAIN_PATH"] = tmp
        os.environ.setdefault("BRAIN_PERSONA_NAME", "the_admin")
        from brain import allostasis as al

        personas = {
            "analyst_trading": ("the_analyst", "russ"),
            "adversary_debate": ("the_adversary", "russ"),
            "empath_therapy": ("the_empath", "client"),
            "hostile_user": ("the_stoic", "troll"),
        }
        checkpoints = {7, 30, args.days}
        for profile, (persona, person) in personas.items():
            print(f"\n== {profile} ({persona})")
            for d in range(args.days):
                now = T0 + d * DAY
                al.learn("sim", _day_traces(profile, d), now=now)
                if d + 1 in checkpoints:
                    s = al.summary(persona, now=now)
                    bond = al.bond_offsets(persona, person, now=now)
                    top = ", ".join(
                        f"{g['trigger']}:{g['channel']}={g['gain']}" for g in s["reactivity"][:3]
                    )
                    print(
                        f"  day {d + 1:>3}: offsets={s['offsets']} reward_rate={s['reward_rate']:+.2f} "
                        f"load={s['load']:.2f} reactivity=[{top}] bond={bond}"
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
