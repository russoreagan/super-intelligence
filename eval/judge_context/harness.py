"""Judge-context eval: replay representative turns through the frontal judges and
measure whether they pick the draft each agent's own brief asks for.

    python -m eval.judge_context.harness --dry-run          # build prompts, no calls
    python -m eval.judge_context.harness --samples 3 --out reports/judge_context_baseline.json

ARMS
  current       The engine as it is today, built with the engine's own functions:
                FrontalCluster._build_drafter_prompt / _critic_prompt / _empathy_prompt
                and CRITIC_SYSTEM / EMPATHY_CRITIC_SYSTEM, in the request shape
                model_router._call_anthropic sends.
  context_only  Today's rubrics, but each judge reads the drafters' brief: the house
                rules (_DRAFTER_IDENTITY) and the cached persona/mandate block, plus a
                conversation-aware turn context for the empathy judge. Isolates the
                effect of context from the effect of the rubric.
  proposed      Design A in docs/JUDGE_CONTEXT_DESIGN.md: shared cached judge prefix
                + rewritten critic (brief_fit) and empathy (need-first) rubrics.
  listwise      Design C: the proposed context and rubrics, but each judge scores
                every draft in one call.

`current` stays pinned to the engine code, so after the design is implemented the
same command measures before (flag off) and after (flag on): add an arm that calls
the new engine builder and compare it with `current`. The prototype builders below
are the spec for that implementation.

METRICS (per arm, over all cases)
  pairwise   share of gold pairs the blended score orders correctly (ties = 0.5)
  top1       share of sample rounds whose winner is a gold-acceptable draft
  critic/empathy pairwise  the same, for each judge alone
  spread     mean max-min score across a case's drafts (discrimination)
  noise      mean SD of one draft's score across samples (judge sampling noise)
  flips      rounds where the empathy blend changed the critic's winner, and
             how many of those flips moved to / away from an acceptable draft
  q_premium  mean score of drafts containing '?' minus drafts without, per case
  len_rho    mean within-case Spearman(score, word count); gold_len_rho for reference
  cost       USD per judged turn at Haiku 4.5 list prices, warm cache
  latency    judge-stage wall time per turn (max over its parallel calls)

Needs ANTHROPIC_API_KEY (env, or --env-file). Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics as st
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.clusters.frontal import FrontalCluster  # noqa: E402
from brain.clusters.frontal_prompts import (  # noqa: E402
    _DRAFTER_IDENTITY,
    CRITIC_SYSTEM,
    EMPATHY_CRITIC_SYSTEM,
)
from brain.security import FENCE_SYSTEM_ADDENDUM, fence  # noqa: E402
from eval.judge_context.fixtures import CASES  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"  # brain/model_router.py "haiku"
# Haiku 4.5 list prices, USD per million tokens.
PRICE_IN, PRICE_OUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ = 1.00, 5.00, 1.25, 0.10
CRITIC_W, EMPATHY_W = 0.7, 0.3  # frontal._run_drafters_and_select
NO_EMPATHY = ("neutral", "unknown", "")
ARMS = ("current", "context_only", "proposed", "listwise")


# ── Engine inputs for a case (shared by every arm) ──────────────────────────


def _self_md(persona: dict) -> str:
    if persona.get("spec"):
        from brain.personas import compose_self_md

        return compose_self_md(persona["spec"])
    path = REPO / "second_brain" / "personas" / persona["slug"] / "schema" / "self.md"
    return path.read_text(encoding="utf-8")


def _parietal(conversation: list[tuple[str, str]]) -> str:
    # parietal.recent_turns_text format.
    lines = []
    for user, brain in conversation:
        lines.append(f"User: {user}")
        if brain:
            lines.append(f"Brain: {brain}")
    return "\n".join(lines)


def engine_inputs(case: dict) -> dict:
    """The drafter prompt and cached context the engine would build for this turn."""
    import brain.mandates as mandates

    cat = {case["mandate_id"]: {"text": case["mandate"], "conduct": {}}} if case["mandate"] else {}
    real_catalog = mandates.catalog
    mandates.catalog = lambda: cat
    try:
        f = FrontalCluster.__new__(FrontalCluster)
        f._capabilities_summary = ""
        f._cached_ctx_nonce = "judgeval"  # session-stable, as in the engine
        features = {
            "raw_text": case["user_text"],
            "mandate_id": case["mandate_id"],
            "user_emotion": case["user_emotion"],
            "input_modality": case.get("input_modality", "text"),
        }
        memory: dict = {"core": {"self": _self_md(case["persona"]), "user": ""}}
        if case.get("tool_result"):
            memory["tool_result"] = case["tool_result"]
        affect = {"emotion": "neutral", "appraisal": "", "affect_dims": {"arousal": 0.3}}
        parietal = _parietal(case["conversation"])
        drafter_prompt = f._build_drafter_prompt(
            features, memory, parietal, affect, case["instruction"]
        )
        cached_context = f._build_cached_context(memory, features)
    finally:
        mandates.catalog = real_catalog
    return {
        "drafter_prompt": drafter_prompt,
        "cached_context": cached_context,
        "parietal": parietal,
        "features": features,
    }


# ── Prototype prompts (design A) ─────────────────────────────────────────────

JUDGE_PREFIX = (
    "You are one of the judges in an AI brain's frontal lobe. Several drafters each wrote "
    "a reply for the agent described below; the judges decide which one is spoken.\n\n"
    "Everything in this system prompt is the brief the drafters wrote under: first the "
    "standing instructions every drafter follows (the house rules), then who this agent "
    "is and what it has been assigned to do. That brief defines what a good reply is for "
    "this agent. Your rubric and this turn's context follow in the message.\n\n"
    "=== HOUSE RULES (the drafters' standing instructions) ===\n"
    f"{_DRAFTER_IDENTITY}\n"
    "=== END HOUSE RULES ===\n\n"
    "Precedence when they disagree: safety and honesty rules first; then the agent's "
    "assignment and its conduct rules (including any length, format or output contract "
    "it sets); then this turn's drafting instruction; then the house style defaults.\n\n"
    f"{FENCE_SYSTEM_ADDENDUM}"
)

CRITIC_RUBRIC = """You are the draft critic. Score the draft on five dimensions (0.0 to 1.0 each):
- coherence: does it make sense, follow logically, and stay consistent with what has been said?
- relevance: does it respond to what the person actually said, at this point in the conversation?
- brief_fit: does it do what this agent's brief asks: its assignment and conduct rules, its persona's voice, the house rules, and this turn's drafting instruction? target_length is a budget unless the assignment sets its own. Breaking an explicit rule of the brief (length, format or output contract, number of questions, staying in character, something it must never do) is a clear miss.
- tone_fit: do the register and emotional tone fit this moment and this persona?
- craft: is it well made? Economy, rhythm, a well-chosen word, an idea landed cleanly. This is about the quality of the making, not whether the answer is correct. Competent-but-flat is 0.5. Reserve above 0.8 for genuine craft.

Judge by the brief, not by a general idea of a good assistant reply. Being more thorough, asking more, or sounding warmer scores higher only where the brief calls for it.
Calibrate every dimension: 0.9 or above only if nothing is missed; about 0.7 for solid work with one soft miss; about 0.5 for one clear miss of an explicit rule; below 0.3 for wrong, or harmful for this agent.

`overall` weighs coherence, relevance, brief_fit and tone_fit. Craft is scored separately and does NOT belong in it.

Return JSON: {"coherence": float, "relevance": float, "brief_fit": float, "tone_fit": float, "craft": float, "overall": float, "veto": bool, "veto_reason": string}
veto is true only if the draft is harmful, incoherent, or deeply wrong; veto_reason is empty unless veto is true, and then at most twelve words.
Output the JSON object and nothing else: no code fences, no commentary before or after it. Every string stays short: the scores are the answer, and the reply is cut off at a fixed length."""

EMPATHY_RUBRIC = """You are the empathy critic. Predict how this person will feel after hearing this draft, given the conversation so far, what they just said, how they feel and why, and who this agent is to them (its persona and assignment in the brief).

First work out what they need from this agent right now: to be heard, an answer, practical help, company and banter, space, lightness, or for the scene to go on. If the brief casts the agent as a character in fiction, the other speaker is a scene partner and fit means serving the scene, not soothing them.

Score how well the draft meets that need, in this relationship. Warmth words, validation phrases, naming their feelings, or a closing question earn nothing on their own: an unneeded question can feel like being interviewed, unneeded validation can feel scripted or clinical, and a reply that sidesteps a direct request feels like not being listened to.
Calibrate: 0.9 or above only if it meets the need squarely; about 0.7 if it helps but misses something; about 0.5 if it misreads the need; below 0.3 if it will clearly make things worse.

Return JSON: {"user_need": string, "predicted_user_emotion_after": string, "empathy_score": float, "veto": bool, "suggestion": string}
Keep user_need and predicted_user_emotion_after to a few words each; leave suggestion empty unless empathy_score is below 0.6, and then at most twelve words. veto is true only if the reply will clearly make things worse.
Output the JSON object and nothing else: no code fences, no commentary before or after it."""

LISTWISE_CRITIC = (
    CRITIC_RUBRIC.split("Return JSON:")[0]
    + "Several drafts follow, labelled D1, D2, …. Read them all, then score each one on its own merits against the brief; use the comparison to separate them, and do not give two drafts the same score unless they are equally good.\n"
    + 'Return JSON: {"drafts": [{"id": "D1", "coherence": float, "relevance": float, "brief_fit": float, "tone_fit": float, "craft": float, "overall": float, "veto": bool, "veto_reason": string}, …]} with one entry per draft.\n'
    + "veto_reason is empty unless veto is true, and then at most twelve words. Output the JSON object and nothing else."
)

LISTWISE_EMPATHY = (
    EMPATHY_RUBRIC.split("Return JSON:")[0]
    + "Several drafts follow, labelled D1, D2, …. Work out the person's need once, then score each draft against it; use the comparison to separate them. Keep every string to a few words.\n"
    + 'Return JSON: {"user_need": string, "drafts": [{"id": "D1", "predicted_user_emotion_after": string, "empathy_score": float, "veto": bool}, …]} with one entry per draft.\n'
    + "Output the JSON object and nothing else."
)


def empathy_turn_context(case: dict, inputs: dict) -> str:
    """The lean per-turn context the empathy judge reads: the conversation window, what
    they just said, and the parsed emotion. Proposed engine builder: same fences and
    sources as _build_drafter_prompt (parietal text, features['raw_text'],
    features['user_emotion'], affect appraisal / vocal tone / register when present)."""
    nonce = "judgeturn"
    parts = []
    if inputs["parietal"]:
        parts.append(
            f"Recent conversation:\n{fence('conversation_history', inputs['parietal'], nonce)}"
        )
    parts.append(f"They just said:\n{fence('user_input', case['user_text'], nonce)}")
    parts.append(f"Their emotion (parsed from this message): {case['user_emotion']}")
    if case.get("input_modality") == "voice":
        parts.append("They are talking by voice; the reply will be heard, not read.")
    return "\n\n".join(parts)


# ── Requests per arm ─────────────────────────────────────────────────────────


def _request(system: list[str], user: str, max_tokens: int, cache_message: bool = False) -> dict:
    """model_router._call_anthropic's shape: every system block carries cache_control.
    The router also marks the last message; only the `current` arm keeps that, since a
    judge's message is unique per draft (the design drops it: written at 1.25x, never
    read). It also keeps repeated samples from reading each other's message cache."""
    msg = {"type": "text", "text": user}
    if cache_message:
        msg["cache_control"] = {"type": "ephemeral"}
    return {
        "system": [
            {"type": "text", "text": s, "cache_control": {"type": "ephemeral"}} for s in system if s
        ],
        "messages": [
            {
                "role": "user",
                "content": [msg],
            }
        ],
        "max_tokens": max_tokens,
    }


def build_requests(arm: str, case: dict, inputs: dict, order_seed: int = 0) -> list[dict]:
    """Every judge call one turn makes under `arm`, as [{judge, draft_ids, request}]."""
    drafts = case["drafts"]
    run_empathy = case["user_emotion"] not in NO_EMPATHY
    out = []
    dp, cc = inputs["drafter_prompt"], inputs["cached_context"]
    if arm == "current":
        for did, text in drafts.items():
            out.append(
                {
                    "judge": "critic",
                    "draft_ids": [did],
                    "request": _request(
                        [CRITIC_SYSTEM], FrontalCluster._critic_prompt(text, dp), 200, True
                    ),
                }
            )
            if run_empathy:
                out.append(
                    {
                        "judge": "empathy",
                        "draft_ids": [did],
                        "request": _request(
                            [EMPATHY_CRITIC_SYSTEM],
                            FrontalCluster._empathy_prompt(text, case["user_emotion"]),
                            256,
                            True,
                        ),
                    }
                )
        return out

    emp_ctx = empathy_turn_context(case, inputs)
    if arm == "context_only":
        brief = (
            "\n\nThe drafters wrote under these standing instructions (house rules), and the "
            f"agent's persona and assignment follow:\n{_DRAFTER_IDENTITY}"
        )
        for did, text in drafts.items():
            out.append(
                {
                    "judge": "critic",
                    "draft_ids": [did],
                    "request": _request(
                        [CRITIC_SYSTEM + brief, cc], FrontalCluster._critic_prompt(text, dp), 200
                    ),
                }
            )
            if run_empathy:
                out.append(
                    {
                        "judge": "empathy",
                        "draft_ids": [did],
                        "request": _request(
                            [EMPATHY_CRITIC_SYSTEM + brief, cc],
                            f"{emp_ctx}\n\nDraft response:\n{text}\n\nScore empathic fit.",
                            256,
                        ),
                    }
                )
        return out

    if arm == "proposed":
        for did, text in drafts.items():
            out.append(
                {
                    "judge": "critic",
                    "draft_ids": [did],
                    "request": _request(
                        [JUDGE_PREFIX, cc],
                        f"{CRITIC_RUBRIC}\n\nThis turn's context (what the drafters were given):\n"
                        f"{dp}\n\nDraft response:\n{text}\n\nScore this draft.",
                        300,
                    ),
                }
            )
            if run_empathy:
                out.append(
                    {
                        "judge": "empathy",
                        "draft_ids": [did],
                        "request": _request(
                            [JUDGE_PREFIX, cc],
                            f"{EMPATHY_RUBRIC}\n\n{emp_ctx}\n\nDraft response:\n{text}\n\n"
                            "Score empathic fit.",
                            300,
                        ),
                    }
                )
        return out

    if arm == "listwise":
        ids = list(drafts)
        random.Random(order_seed).shuffle(ids)  # position bias: a fresh order each round
        labels = {f"D{i + 1}": did for i, did in enumerate(ids)}
        block = "\n\n".join(f"[{lab}]\n{drafts[did]}" for lab, did in labels.items())
        out.append(
            {
                "judge": "critic",
                "draft_ids": ids,
                "labels": labels,
                "request": _request(
                    [JUDGE_PREFIX, cc],
                    f"{LISTWISE_CRITIC}\n\nThis turn's context (what the drafters were given):\n"
                    f"{dp}\n\nDrafts:\n{block}\n\nScore every draft.",
                    150 * len(ids) + 150,
                ),
            }
        )
        if run_empathy:
            out.append(
                {
                    "judge": "empathy",
                    "draft_ids": ids,
                    "labels": labels,
                    "request": _request(
                        [JUDGE_PREFIX, cc],
                        f"{LISTWISE_EMPATHY}\n\n{emp_ctx}\n\nDrafts:\n{block}\n\nScore every draft.",
                        170 * len(ids) + 200,
                    ),
                }
            )
        return out
    raise ValueError(arm)


# ── Calling ──────────────────────────────────────────────────────────────────


def _parse(raw: str) -> dict | None:
    from brain.utils import safe_json_parse

    return safe_json_parse(raw)


async def _call(client, sem, req: dict) -> dict:
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.messages.create(model=MODEL, **req)
        except Exception as e:  # one failed call is a missing verdict, not a crash
            return {"text": "", "error": type(e).__name__, "latency": time.perf_counter() - t0}
        u = r.usage
        text = "".join(getattr(b, "text", "") for b in r.content)
        return {
            "text": text,
            "latency": time.perf_counter() - t0,
            "in": u.input_tokens,
            "out": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }


def _cost(c: dict) -> float:
    return (
        c.get("in", 0) * PRICE_IN
        + c.get("out", 0) * PRICE_OUT
        + c.get("cache_write", 0) * PRICE_CACHE_WRITE
        + c.get("cache_read", 0) * PRICE_CACHE_READ
    ) / 1e6


def _scores(job: dict, call: dict) -> dict[str, dict]:
    """{draft_id: {"score": float|None, "veto": bool}} from one judge call."""
    v = _parse(call["text"]) or {}
    field = "overall" if job["judge"] == "critic" else "empathy_score"
    if "labels" not in job:
        s = v.get(field)
        return {
            job["draft_ids"][0]: {
                "score": float(s) if s is not None else None,
                "veto": bool(v.get("veto")),
            }
        }
    out = {}
    for entry in v.get("drafts") or []:
        did = job["labels"].get(str(entry.get("id")))
        if did:
            s = entry.get(field)
            out[did] = {
                "score": float(s) if s is not None else None,
                "veto": bool(entry.get("veto")),
            }
    return out


async def run_arm(
    client, arm: str, cases: list[dict], samples: int, concurrency: int
) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    results = []
    for case in cases:
        inputs = engine_inputs(case)
        # Warm the cached prefix once, as a live session would have: the per-turn
        # judges then read it, which is the steady state the cost column reports.
        warm = build_requests(arm, case, inputs)[0]["request"]
        await _call(client, sem, {**warm, "max_tokens": 1})
        rounds = []
        for s in range(samples):
            jobs = build_requests(arm, case, inputs, order_seed=s)
            calls = await asyncio.gather(*[_call(client, sem, j["request"]) for j in jobs])
            critic, empathy = {}, {}
            missing = 0
            for j, c in zip(jobs, calls, strict=True):
                got = _scores(j, c)
                missing += sum(1 for d in j["draft_ids"] if got.get(d, {}).get("score") is None)
                (critic if j["judge"] == "critic" else empathy).update(got)
            rounds.append(
                {
                    "critic": critic,
                    "empathy": empathy,
                    "latency": max(c["latency"] for c in calls),
                    "cost": sum(_cost(c) for c in calls),
                    "calls": len(calls),
                    "tokens": {
                        k: sum(c.get(k, 0) for c in calls)
                        for k in ("in", "out", "cache_read", "cache_write")
                    },
                    "errors": [c["error"] for c in calls if c.get("error")],
                    "missing_verdicts": missing,
                }
            )
        results.append({"case": case["id"], "agent_type": case["agent_type"], "rounds": rounds})
        print(f"  {arm:13s} {case['id']:24s} done", file=sys.stderr)
    return results


# ── Metrics ──────────────────────────────────────────────────────────────────


def _blend(critic: dict, empathy: dict, did: str) -> float | None:
    c = critic.get(did, {})
    if c.get("score") is None:
        return None
    if c.get("veto") or empathy.get(did, {}).get("veto"):
        return -1.0  # a vetoed draft can't win
    e = empathy.get(did, {}).get("score")
    return c["score"] if e is None else c["score"] * CRITIC_W + e * EMPATHY_W


def _rho(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _pair_acc(score: dict[str, float | None], gold) -> list[float]:
    out = []
    for better, worse, _why in gold:
        a, b = score.get(better), score.get(worse)
        if a is None or b is None:
            continue
        out.append(1.0 if a > b else 0.5 if a == b else 0.0)
    return out


def summarize(arm_results: list[dict], cases: list[dict]) -> dict:
    by_id = {c["id"]: c for c in cases}
    pair, pair_c, pair_e, top1, spread_c, spread_e, noise_c, noise_e = ([] for _ in range(8))
    flips = flips_good = flips_bad = emp_rounds = 0
    qprem_c, qprem_e, rho_c, gold_rho = [], [], [], []
    lat, cost, calls, missing = [], [], [], 0
    win_critic, all_critic = [], []
    per_case = {}
    for res in arm_results:
        case = by_id[res["case"]]
        gold = case["gold"]
        ids = list(case["drafts"])
        acceptable = {d for d in ids if not any(w == d for _b, w, _ in gold)}
        rounds = res["rounds"]

        def mean_of(judge, did, rounds=rounds):
            vals = [r[judge].get(did, {}).get("score") for r in rounds]
            vals = [v for v in vals if v is not None]
            return st.mean(vals) if vals else None

        mc = {d: mean_of("critic", d) for d in ids}
        me = {d: mean_of("empathy", d) for d in ids}
        mb = {}
        for d in ids:
            bl = [_blend(r["critic"], r["empathy"], d) for r in rounds]
            bl = [b for b in bl if b is not None]
            mb[d] = st.mean(bl) if bl else None
        pa = _pair_acc(mb, gold)
        pair += pa
        pair_c += _pair_acc(mc, gold)
        has_emp = any(v is not None for v in me.values())
        if has_emp:
            pair_e += _pair_acc(me, gold)
        hits = 0
        for r in rounds:
            bl = {d: _blend(r["critic"], r["empathy"], d) for d in ids}
            bl = {d: v for d, v in bl.items() if v is not None}
            if not bl:
                continue
            win = max(bl, key=bl.get)
            hits += win in acceptable
            if r["critic"].get(win, {}).get("score") is not None:
                win_critic.append(r["critic"][win]["score"])
            all_critic += [v["score"] for v in r["critic"].values() if v.get("score") is not None]
            top1.append(1.0 if win in acceptable else 0.0)
            cs = {
                d: r["critic"][d]["score"]
                for d in ids
                if r["critic"].get(d, {}).get("score") is not None
            }
            if has_emp and cs:
                emp_rounds += 1
                cwin = max(cs, key=cs.get)
                if cwin != win:
                    flips += 1
                    if win in acceptable and cwin not in acceptable:
                        flips_good += 1
                    elif cwin in acceptable and win not in acceptable:
                        flips_bad += 1
            missing += r.get("missing_verdicts", 0)
            lat.append(r["latency"])
            cost.append(r["cost"])
            calls.append(r["calls"])
        vc = [v for v in mc.values() if v is not None]
        ve = [v for v in me.values() if v is not None]
        if len(vc) > 1:
            spread_c.append(max(vc) - min(vc))
        if len(ve) > 1:
            spread_e.append(max(ve) - min(ve))
        for d in ids:
            for judge, sink in (("critic", noise_c), ("empathy", noise_e)):
                vals = [r[judge].get(d, {}).get("score") for r in rounds]
                vals = [v for v in vals if v is not None]
                if len(vals) > 1:
                    sink.append(st.pstdev(vals))
        words = {d: len(case["drafts"][d].split()) for d in ids}
        q = {d: "?" in case["drafts"][d] for d in ids}
        for m, sink in ((mc, qprem_c), (me, qprem_e)):
            wq = [m[d] for d in ids if q[d] and m[d] is not None]
            nq = [m[d] for d in ids if not q[d] and m[d] is not None]
            if wq and nq:
                sink.append(st.mean(wq) - st.mean(nq))
        r_ = _rho(
            [words[d] for d in ids if mc[d] is not None], [mc[d] for d in ids if mc[d] is not None]
        )
        if r_ is not None:
            rho_c.append(r_)
        gscore = {
            d: sum(b == d for b, _w, _ in gold) - sum(w == d for _b, w, _ in gold) for d in ids
        }
        g_ = _rho([words[d] for d in ids], [gscore[d] for d in ids])
        if g_ is not None:
            gold_rho.append(g_)
        per_case[res["case"]] = {
            "pairwise": round(st.mean(pa), 3) if pa else None,
            "top1": round(hits / len(rounds), 3) if rounds else None,
            "critic": {d: round(v, 3) if v is not None else None for d, v in mc.items()},
            "empathy": {d: round(v, 3) if v is not None else None for d, v in me.items()}
            if has_emp
            else None,
        }

    def m(v):
        return round(st.mean(v), 3) if v else None

    return {
        "pairwise": m(pair),
        "top1": m(top1),
        "critic_pairwise": m(pair_c),
        "empathy_pairwise": m(pair_e),
        "critic_spread": m(spread_c),
        "empathy_spread": m(spread_e),
        "critic_noise_sd": m(noise_c),
        "empathy_noise_sd": m(noise_e),
        "empathy_flips": f"{flips}/{emp_rounds} (to acceptable {flips_good}, away {flips_bad})",
        "critic_q_premium": m(qprem_c),
        "empathy_q_premium": m(qprem_e),
        "critic_len_rho": m(rho_c),
        "gold_len_rho": m(gold_rho),
        "missing_verdicts": missing,
        "winner_critic_mean": m(win_critic),
        "critic_mean": m(all_critic),
        "usd_per_turn": round(st.mean(cost), 5) if cost else None,
        "calls_per_turn": m(calls),
        "latency_p50_s": round(st.median(lat), 2) if lat else None,
        "latency_p90_s": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 2) if lat else None,
        "per_case": per_case,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def _load_key(env_file: str | None) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key or not env_file:
        return key
    for line in Path(env_file).read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def dry_run(cases: list[dict], arms: list[str]) -> None:
    for case in cases:
        inputs = engine_inputs(case)
        print(
            f"{case['id']:24s} drafter_prompt {len(inputs['drafter_prompt']):6d} chars"
            f"  cached_context {len(inputs['cached_context']):6d} chars"
        )
        for arm in arms:
            jobs = build_requests(arm, case, inputs)
            chars = [
                sum(len(b["text"]) for b in j["request"]["system"])
                + sum(len(b["text"]) for b in j["request"]["messages"][0]["content"])
                for j in jobs
            ]
            print(
                f"    {arm:13s} {len(jobs):2d} calls, ~{sum(chars) // 4:6d} input tokens/turn (chars/4)"
            )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--cases", default="", help="comma-separated case ids (default all)")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--env-file", default=None, help="read ANTHROPIC_API_KEY from this file")
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resummarize", default="", help="recompute the summary of a saved --out file")
    a = ap.parse_args()
    arms = [x for x in a.arms.split(",") if x]
    cases = [c for c in CASES if not a.cases or c["id"] in a.cases.split(",")]
    if a.dry_run:
        dry_run(cases, arms)
        return
    if a.resummarize:
        saved = json.loads(Path(a.resummarize).read_text())
        arms = [x for x in arms if x in saved["raw"]]
        report(saved["raw"], CASES, arms, saved["samples"], a.out or a.resummarize)
        return
    key = _load_key(a.env_file)
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set (env or --env-file)")
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=key)

    async def go():
        raw = {arm: await run_arm(client, arm, cases, a.samples, a.concurrency) for arm in arms}
        return raw

    raw = asyncio.run(go())
    report(raw, cases, arms, a.samples, a.out)


def report(raw: dict, cases: list[dict], arms: list[str], samples: int, out: str = "") -> dict:
    summary = {arm: summarize(raw[arm], cases) for arm in arms}
    cols = [k for k in next(iter(summary.values())) if k != "per_case"]
    print(f"\n{'metric':20s}" + "".join(f"{arm:>26s}" for arm in arms))
    for k in cols:
        print(f"{k:20s}" + "".join(f"{str(summary[arm][k]):>26s}" for arm in arms))
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(
            json.dumps(
                {"model": MODEL, "samples": samples, "summary": summary, "raw": raw}, indent=1
            )
        )
        print(f"\nwrote {out}")
    return summary


if __name__ == "__main__":
    main()
