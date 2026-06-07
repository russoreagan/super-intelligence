"""eval/reward_divergence.py — Per-persona reward/punishment asymmetry table.

Demonstrates the deliverable: different personalities get DIFFERENT amounts of satisfaction
from being right and different amounts of sadness/frustration from being wrong, even given
identical events. It exercises the REAL reinforcement code path (DMN._resolve_pending_
conclusion, the verified-correctness signal) with a recording neuromod bus, and reports the
intrinsic draft-quality deltas using the same formula session_turn.py applies. No LLM / Ollama
required — this is a deterministic chemistry-delta report, not a generation eval.

Usage:
  python -m eval.reward_divergence
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import brain.open_threads as ot
from brain.dmn import DefaultModeNetwork
from brain.neuron import accomplishment_factor, reward_weight
from brain.settings import settings

PERSONAS = ["The Analyst", "The Empath", "The Visionary", "The Poet", "The Sage"]


class _RecordingNeuromod:
    def __init__(self):
        self.deltas: dict[str, float] = {}

    def add(self, channel, delta):
        self.deltas[channel] = self.deltas.get(channel, 0.0) + delta

    def snapshot(self):
        return dict(self.deltas)


def _make_dmn(persona: str):
    settings._data["persona_name"] = persona
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    dmn._hippocampus = hip
    dmn._session_id = "eval"
    dmn._open_threads = []
    dmn._recent_conclusions = deque(maxlen=5)
    dmn._save_threads = AsyncMock()
    nm = _RecordingNeuromod()
    dmn._bus = MagicMock()
    dmn._bus.neuromod = nm
    return dmn, nm


async def _verdict_deltas(persona: str, verdict: str) -> dict:
    dmn, nm = _make_dmn(persona)
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?")
    dmn._open_threads, t = ot.mark_pending(dmn._open_threads, t.id)
    t.pending_conclusion = "Gating reduces tokens-per-response."
    await dmn._resolve_pending_conclusion(t, verdict, "…")
    return nm.deltas


def _draft_deltas(persona: str) -> dict:
    """Mirror of session_turn.py draft-quality reward/penalty (the intrinsic self-judged path)."""
    w = reward_weight(persona, "correctness")
    er = float(settings.get("emotional_reactivity_scale"))
    high_da = float(settings.get("correctness_self_base")) * w * er
    low_da = -float(settings.get("correctness_penalty_base")) * w * er
    low_5ht = -float(settings.get("correctness_5ht_drain")) * w * er
    return {"high_DA": high_da, "low_DA": low_da, "low_5HT": low_5ht}


async def main() -> None:
    rows = []
    for p in PERSONAS:
        affirm = await _verdict_deltas(p, "affirm")
        reject = await _verdict_deltas(p, "reject")
        draft = _draft_deltas(p)
        rows.append(
            {
                "persona": p,
                "w_correctness": reward_weight(p, "correctness"),
                "verified_right_DA": affirm.get("DA", 0.0),
                "verified_wrong_DA": reject.get("DA", 0.0),
                "verified_wrong_5HT": reject.get("5HT", 0.0),
                "self_good_DA": draft["high_DA"],
                "self_short_5HT": draft["low_5HT"],
            }
        )
    settings._data.pop("persona_name", None)

    hdr = (
        f"{'persona':<14}{'w_corr':>8}{'right→DA':>10}{'wrong→DA':>10}"
        f"{'wrong→5HT':>11}{'good→DA':>9}{'short→5HT':>11}"
    )
    print("\nPer-persona reward / punishment from being right vs wrong (identical events):\n")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['persona']:<14}{r['w_correctness']:>8.2f}{r['verified_right_DA']:>+10.4f}"
            f"{r['verified_wrong_DA']:>+10.4f}{r['verified_wrong_5HT']:>+11.4f}"
            f"{r['self_good_DA']:>+9.4f}{r['self_short_5HT']:>+11.4f}"
        )
    print(
        "\nRead: the Analyst is buoyed most by being right and stung most by being wrong;\n"
        "the Empath (who draws reward from connection, not correctness) is moved least.\n"
        "The lingering sting (5HT drain) is then colored by each persona's resting chemistry\n"
        "downstream — low-5HT Poet broods, high-AEA/5HT Sage shrugs it off.\n"
    )

    # ── Stage 6: accomplishment / mastery — difficulty scaling + expectation-gap ──
    base = float(settings.get("accomplishment_base"))
    er = float(settings.get("emotional_reactivity_scale"))
    exp_med = float(settings.get("accomplishment_expected_medium"))
    scenarios = [
        ("trivial (eff=1)", 1.0),
        ("met brace (eff=6)", 6.0),
        ("modest over (eff=9)", 9.0),
        ("big over (eff=30)", 30.0),
    ]
    print("Stage 6 — accomplishment DA by difficulty (expected_medium=%.0f), per persona:\n" % exp_med)
    hdr2 = f"{'scenario':<20}" + "".join(f"{p.split()[-1]:>10}" for p in PERSONAS)
    print(hdr2)
    print("-" * len(hdr2))
    for label, eff in scenarios:
        diff, mod = accomplishment_factor(eff, exp_med)
        row = f"{label:<20}"
        for p in PERSONAS:
            da = base * diff * mod * reward_weight(p, "mastery") * er
            row += f"{da:>+10.4f}"
        print(row)
    print(
        "\nRead: DA rises with effort overcome, but 'big over' (r=5) is LOWER than 'modest over'\n"
        "(r=1.5) — the expectation-gap curve: a task blowing past its brace breeds frustration\n"
        "that erodes the payoff. Sage/Analyst/Poet (high mastery valuation) feel it most.\n"
    )

    # ── Stage 5: self-verified correctness — prediction confirmed by reality ──
    from brain.neuron import prediction_reward

    pbase = float(settings.get("prediction_reward_base"))
    print("Stage 5 — self-verified prediction DA (confident=0.8, informative=0.7), per persona:\n")
    for label, correct in [("confirmed", True), ("refuted", False)]:
        pr = prediction_reward(0.8, correct, 0.7)
        row = f"{label:<20}"
        for p in PERSONAS:
            row += f"{pr * pbase * reward_weight(p, 'correctness') * er:>+10.4f}"
        print(row)
    print(
        "\nRead: being proven right by reality (no user needed) rewards the Analyst most; a\n"
        "confident prediction reality refutes dips DA. Trivial/low-confidence predictions earn 0.\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
