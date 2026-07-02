# How Learning Works: One Request, Traced

**A case study of every learning surface in the system, using a single recurring
request from a partner integration (the trading app, over the engine API):**

> "can you find new items for my watchlist"

The same sentence arrives day after day. This document traces what the system
does with it the first time, what each layer writes down, and what is different
by the tenth time — including the things that deliberately never change.

The core design principle throughout: **compute scales with novelty**. A brain
that has seen something before should spend less on it — but only in the layers
where spending less is safe.

---

## 1. First arrival (cold start)

The request lands on the agent lane (an engine-API session with its own
`end_user_id`, isolated from the owner's chat).

**Sensory → temporal (understanding).** The text is fingerprinted structurally
— length bucket, question mark, memory-reference words — twelve possible
signatures, no LLM involved. The intent predictor has no history for this
signature, so surprise is 1.0: the understanding integrator (an LLM parse) runs
and produces structured features — intent, salience, entities
("watchlist"), affect.

One thing is decided *before* the predictor even votes: "find " matches the
tool-verb lexicon, so this turn is stamped as an action request and the full
parse is mandatory. This matters later.

**Features → motor cortex (execution).** `requires_action=true` routes the turn
into a multi-step internal job: a strategic plan (scan the watchlist → screen
movers → rank candidates → write results), then tactical step-by-step execution
against the trading app's MCP tools. Each step's outcome is checkpointed; the
job ends as a typed `JobOutcome` (completed / failed / deferred — never a
silent empty success) with its own metered cloud cost.

**Turn → hippocampus (memory).** The turn is encoded as an episode: the text,
the parsed features, the full neuromodulator snapshot at that moment, the
surprise score, topic and approach tags, and a content-free *cognitive
signature* — a fingerprint of how the problem was approached, independent of
what it was about.

**Outcome → chemistry (evaluation).** Successful steps release intrinsic
dopamine through the reward funnel: appraisal → DA → ΔDA → synaptic weight
change. The reward is guarded — see §4.

At this point nothing has been "learned" in any deep sense. But six different
surfaces have each written one entry.

## 2. What each surface writes down

| Surface | What it records | Where |
|---|---|---|
| Intent predictor (temporal) | signature → parsed intent, 8-turn window | in-process |
| Episodic memory | full episode + chemistry + cognitive signature | `episodes` table |
| Job store | plan, steps, results, outcome, cost | `agent_jobs` + JSON store |
| Wiring / switch efficacy | which gates fired before good outcomes | wiring edges |
| Chunk candidates | the successful tool sequence, verbatim | `second_brain/jobs/*.json` |
| Reward ledger | intrinsic DA spent, predictions made/confirmed | job record extras |

Two of these do their real work later, offline: chunk mining and wiring
consolidation both run during **sleep**, not during the turn. The turn only
leaves raw material.

## 3. The tenth arrival

The same sentence, days later. Now every layer responds differently.

**The parse still runs — by design.** The tool-verb check fires before the
predictor, every time. The fast path (skipping the understanding LLM when the
predictor is confident) would build heuristic features with
`requires_action=false`, and the watchlist scan would simply never happen. So
action-shaped requests are permanently excluded from the parse-skip, no matter
how familiar they become. Familiarity must never eat the work itself.

**But surprise is now near zero.** The predictor has seen this signature
resolve to the same intent ten times; its prediction is confident and correct,
so the recorded surprise on the episode is low. Every downstream novelty gate
reads that number:

- The recall fan-out treats the turn as routine — no wide associative search.
- Episode encoding can take the cheap path when salience is also low.
- The episode is stored as an *unsurprising* instance, which is itself signal:
  the eval layer can now distinguish routine from genuinely novel turns.

**The plan is no longer planned.** Sleep-time mining has, by now, extracted the
repeated sub-sequence (scan → screen → rank → write) from the job records into
a chunk. The chunk memory is read-only at runtime — sleep owns the writes — and
the motor cortex replays the chunk as a unit instead of re-deriving the plan
step by step. A chunk that diverges from reality mid-replay is suppressed until
the next sleep pass re-evaluates it: automatization is always revocable.

**Prior results are reused.** The planner's `recall_jobs` tool surfaces the
last watchlist jobs from the durable store, so run ten builds on run nine's
findings instead of re-discovering them.

**The approach is structurally recallable.** Because episodes carry a
content-free cognitive signature plus approach tags, a *different* problem with
the same shape ("screen these candidates against criteria and rank them") can
recall how the watchlist problem was solved — cross-domain transfer keyed on
how, not what. This recall is novelty-gated: it fires for the new problem, not
for the eleventh watchlist run.

**The gates themselves have adapted.** Switches that consistently fired on the
path to good outcomes have strengthened their learned efficacy — clamped inside
direction-aware safety bands, so a safety gate can never be learned past its
allowed direction regardless of the raw weight.

**Within the job, context is warm.** The job's steps share one cloud session
(reset at the job boundary so one job's content never bleeds into the next).

Net effect: the tenth request costs one mandatory parse plus the irreducible
work — the market APIs still get called, the screens still run. What repetition
buys is the disappearance of planning overhead, exploratory recall, and
re-derivation. What it can never buy is skipping the work or the safety checks.

## 4. Why the reward can't be farmed

A system that rewards itself for its own predictions has an obvious failure
mode: predict the inevitable, collect dopamine forever. The reward path guards
against this explicitly (`prediction_reward`):

- **Confidence floor** — a low-confidence guess earns nothing.
- **Informativeness gate** — being right about a near-constant outcome earns
  nothing; reward is weighted by how uncertain the outcome was beforehand.
- **Symmetry** — a confident *wrong* prediction earns negative reward. Staking
  confidence has a cost.

So the tenth watchlist run, correctly predicted, earns almost nothing — the
outcome stopped being informative around run three. Learning saturates instead
of compounding; the dopamine goes to the turns that were genuinely uncertain.

## 5. What familiarity never changes

- **Action requests always get the full parse** (the tool-verb veto).
- **Emotional moments always get the full engine** — if the user or the entity
  is in a reactive state, every gate is bypassed; a statistically valid
  prediction is still the wrong move when the moment deserves fresh attention.
- **Budgets and approvals are familiarity-blind** — the spend gate, rate caps,
  and external-side-effect approval ledger evaluate every job the same way on
  run one and run one hundred.
- **Switch efficacy stays inside its bands** — no amount of repetition can
  learn a safety gate open.
- **The fast path self-corrects** — skipped turns are shadow-validated at a
  sample rate: the real LLM parse runs purely for measurement, and a mismatch
  feeds the true intent back into the predictor history, closing the fast path
  for that shape until it re-earns confidence.

## 6. Where to see it happening

- `skip_temporal_integrator` / `skip_encoder` decision logs, and
  `llm_calls_saved` on turn traces — the fast paths actually firing.
- `episodes.surprise_score` — should spread across the range: low on routine
  turns, 0.6+ only on wrong predictions and genuine novelty.
- `agent_jobs` — per-job `cloud_usd`, `stories_completed/stories_total`, and a
  reasoned state for every terminal outcome.
- `second_brain/chunks.json` after sleep passes — the mined automatizations.
- Langfuse traces (`brain-turn`, `dmn-thought`, `brain-job`) — the end-to-end
  record the eval judges score.

## 7. The shape of the whole thing

Learning here is not one mechanism but a stack of them, each with its own
timescale and its own veto:

| Timescale | Mechanism | Signal | Safety bound |
|---|---|---|---|
| ~8 turns | intent predictor | prediction vs. parsed intent | shadow validation, action/emotion vetoes |
| per turn | novelty gates | surprise score | floor conditions (salience, DA, entities) |
| per job | job reuse (`recall_jobs`) | durable job outcomes | planner decides, never forced |
| nightly (sleep) | chunk mining | repeated successful sequences | runtime read-only; divergence suppression |
| continuous | switch efficacy (Hebbian) | ΔDA from guarded rewards | direction-aware bands |
| cross-domain | structural recall | cognitive signature match | novelty-gated |

The common pattern: **every learning surface is paired with the condition under
which it must not apply.** That pairing — not any single mechanism — is what
lets the system get cheaper with experience without getting careless with it.
