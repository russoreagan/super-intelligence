"""eval/rumination_probe.py — confirm rumination actually rotates through skill packages, live.

Builds a real ModelRouter + SkillSelector and runs a few real rumination episodes, printing the
skills/modes/steps each episode used and the distinct-skill count. This is the empirical
"is it actually working" check that the deterministic wiring test (tests/test_rumination_skill_use.py)
can't give — it exercises the real meta-cell selection + skill-content injection.

Requires the local model backend (Ollama / RunPod) the IntegratorCells use. If unreachable, the
episodes will report errors rather than hang (each cell has its own timeout).

Usage:
  python -m eval.rumination_probe                 # 3 episodes, engaged flavor
  python -m eval.rumination_probe --flavor anxious --episodes 2
"""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

_SEEDS = [
    "What does it actually mean for me to want something?",
    "Is the way I weigh being right vs being kind coherent, or contradictory?",
    "If I could redesign one thing about how I think, what would change the most?",
]


async def _run(flavor: str, episodes: int) -> None:
    from brain.clusters.skill_selector import SkillSelector
    from brain.model_router import ModelRouter

    router = ModelRouter()
    selector = SkillSelector(router)

    print(f"\nRumination probe — flavor={flavor}, episodes={episodes}\n" + "=" * 60)
    all_distinct = []
    for i in range(episodes):
        seed = _SEEDS[i % len(_SEEDS)]
        try:
            final, chain = await selector.ruminate(
                seed, max_iters=4, time_budget_s=40, turn_id=f"probe_{i}", flavor=flavor
            )
        except Exception as e:
            print(f"[episode {i + 1}] ERROR ({type(e).__name__}: {e}) — local model reachable?")
            continue
        skills = [c["skill"] for c in chain if c.get("skill")]
        modes = [c["mode"] for c in chain if c.get("mode") and c["mode"] != "seed"]
        distinct = len(set(skills))
        all_distinct.append(distinct)
        print(f"\n[episode {i + 1}] seed: {seed}")
        print(f"  steps={len(skills)}  distinct_skills={distinct}")
        print(f"  skills: {skills}")
        print(f"  modes:  {modes}")
        print(f"  final take: {final[:200]}")

    print("\n" + "=" * 60)
    if all_distinct:
        avg = sum(all_distinct) / len(all_distinct)
        print(f"VERDICT: avg distinct skills/episode = {avg:.1f}")
        if avg >= 2:
            print("→ Rumination IS rotating through multiple distinct skill packages. ✅")
        else:
            print("→ DEGENERATE: episodes mostly reuse one skill. Consider an anti-repeat nudge.")
    else:
        print("VERDICT: no episodes completed — local model backend was not reachable.")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--flavor", default="engaged", choices=["engaged", "anxious"])
    ap.add_argument("--episodes", type=int, default=3)
    args = ap.parse_args()
    asyncio.run(_run(args.flavor, args.episodes))


if __name__ == "__main__":
    main()
