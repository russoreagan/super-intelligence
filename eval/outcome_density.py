"""
Outcome-density diagnostic — how much learning signal do real turns carry?

Replays eval/turns.jsonl through the same composite-outcome formula the Hebbian
pass uses (hebbian._composite_outcome: 0.5·ΔDA + 0.3·critic + 0.2·user-emotion)
and reports the distribution. The motivating question: what fraction of turns
contribute ~zero outcome, i.e. the wiring update is mostly decay + noise?

Run before and after the SNR changes (surprise-triggered critic, engagement
term, eligibility traces) to prove the signal density actually moved.

Usage: uv run python eval/outcome_density.py [path/to/turns.jsonl]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict


def composite_outcome(turn: dict, prior_da: float | None) -> tuple[float, dict]:
    da = float((turn.get("neuromod") or {}).get("DA", 0.5))
    da_delta = 0.0
    if prior_da is not None:
        da_delta = max(-1.0, min(1.0, (da - prior_da) * 4.0))

    critic_term = 0.0
    critic_ran = False
    for d in turn.get("draft_scores") or []:
        if d.get("selected") and d.get("critic_ran"):
            critic_ran = True
            critic_term = (float(d.get("overall", 0.5)) - 0.5) * 2.0
            break

    user_term = 0.0
    ue = str(turn.get("user_emotion", "") or "")
    has_user_emotion = ue not in ("", "neutral", "unknown")
    if has_user_emotion:
        user_term = (
            0.6
            if ue in ("happy", "pleased", "excited", "grateful", "warm")
            else (-0.6 if ue in ("angry", "frustrated", "annoyed", "sad", "disappointed") else 0.0)
        )

    outcome = 0.5 * da_delta + 0.3 * critic_term + 0.2 * user_term
    return outcome, {
        "da_delta": da_delta,
        "critic_ran": critic_ran,
        "has_user_emotion": has_user_emotion,
    }


def main(path: str) -> None:
    by_session: dict[str, list[dict]] = defaultdict(list)
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "turn":
                by_session[rec.get("session_id", "?")].append(rec)

    outcomes: list[float] = []
    critic_count = 0
    emotion_count = 0
    n = 0
    for turns in by_session.values():
        prior_da: float | None = None
        for t in turns:
            o, parts = composite_outcome(t, prior_da)
            outcomes.append(o)
            critic_count += parts["critic_ran"]
            emotion_count += parts["has_user_emotion"]
            prior_da = float((t.get("neuromod") or {}).get("DA", 0.5))
            n += 1

    if not n:
        print("no turns found")
        return

    def frac_below(thresh: float) -> float:
        return sum(1 for o in outcomes if abs(o) < thresh) / n

    mean_abs = sum(abs(o) for o in outcomes) / n
    print(f"turns analysed:            {n} ({len(by_session)} sessions)")
    print(f"critic coverage:           {critic_count / n:.1%} of turns")
    print(f"user-emotion coverage:     {emotion_count / n:.1%} of turns")
    print(f"mean |outcome|:            {mean_abs:.4f}")
    print(f"turns with |outcome|<0.02: {frac_below(0.02):.1%}   (≈ zero learning signal)")
    print(f"turns with |outcome|<0.05: {frac_below(0.05):.1%}")
    print(f"turns with |outcome|>0.20: {sum(1 for o in outcomes if abs(o) > 0.20) / n:.1%}   (strong signal)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "eval/turns.jsonl")
