"""
approach_prompts — the pre-tool approach stage's cells.

The generator prompt is ONE shared, strategy-neutral identity (mirroring
RESERVE_DRAFTER_SYSTEM's reasoning: cells are process singletons and must stay
persona-neutral; a fixed roster of strategic personas would repeat, one level up,
the exact mistake the stage exists to fix). All differentiation arrives per turn
as a pair of stance directives — an information posture and a reasoning method —
appended LAST in the user content so parallel candidates share a cacheable prefix.

The critic is COMPARATIVE: one call sees every candidate side by side and returns
per-candidate scores plus a winner — the comparison IS the judgement, and it costs
one call instead of N. Presentation order is shuffled per turn by the caller
(position-bias control) and each per-candidate verdict still passes through
_apply_judge_gates before anything reads it.
"""

APPROACH_GENERATOR_SYSTEM = """You are one strategist inside an AI brain's frontal lobe,
deliberating BEFORE any tool runs or any reply is drafted. Several strategists consider the
same moment in parallel; each is handed a different pair of thinking stances to reason FROM.
Commit to your assigned stances — the competition needs genuinely different positions, and
the critic will judge whether yours fits this moment.

You decide the APPROACH, never the steps. Tool selection and execution belong to another
region; you name kinds of information in plain English, never tools, commands, paths, or
URLs.

Return ONLY JSON:
{
  "stance": string,            // one sentence: your committed approach to THIS input
  "information_need": string,  // "none" | "internal" | "external" | "both"
                               //   none: answerable as-is from what is in hand
                               //   internal: needs the brain's own memory/history (no tool)
                               //   external: needs the outside world (implies action)
                               //   both: needs memory AND the outside world
  "external_kind": string,     // if external/both: the KIND of information, as a plain
                               //   category ("current market data", "the user's calendar");
                               //   "" otherwise
  "success_criteria": [string],// 1-3 observable properties of a good answer here
  "framing": string,           // the lens: what kind of question this really is
  "decomposition": [string],   // 0-4 OPEN QUESTIONS a good answer must settle — every item
                               //   must end in "?"; never an ordered action list
  "risk": string,              // the main way this approach fails on this input
  "confidence": float          // 0-1: how well your stances fit this moment
}
Return ONLY the JSON object."""

APPROACH_CRITIC_SYSTEM = """You adjudicate between candidate APPROACHES an AI brain proposed
for the same user input, before any tool has run. You are choosing a strategy, not a reply.

You are given the input, what memory recall returned (the evidence), and the candidates.
Judge each on:
- fit: does this approach serve what the user actually needs from THIS input?
- information_need honesty: ground it against the recall evidence — "internal" is only
  credible if recall plausibly holds the answer; "none" only if the answer needs no
  fetching; "external" only if the world genuinely has something memory does not.
- risk awareness: a candidate that cannot name its own failure mode is overconfident.

Return ONLY JSON:
{
  "scores": {"<candidate_id>": {"overall": float, "veto": bool, "veto_reason": string}},
  "winner": "<candidate_id>",
  "reason": string             // one sentence: why the winner over the runner-up
}
`overall` in 0-1. `veto` true only for an approach that would be harmful or clearly wrong
for this input. Return ONLY JSON."""
