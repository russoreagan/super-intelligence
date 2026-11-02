# Judge context: what the draft judges see, and a design to fix it

Status: **design A approved (2026-11-01) and implemented** behind `judge_brief_context`
(default on). §10 records the implementation and its before/after run. The baseline that
informed the decision is `reports/judge_context_baseline_2026-11-01.json`, and the
before/after run is `reports/judge_context_engine_2026-11-01.json`.

## 1. Summary

Each turn, up to five drafters write a reply. Two judges then score every draft: the
**critic** and the **empathy critic**. The draft with the highest `critic*0.7 + empathy*0.3`
is spoken. Both judges score without the brief the drafters wrote under:

- The **critic** sees the per-turn drafter prompt: the conversation, the drafting
  instruction and the user's message. It never sees the persona, the mandate text, the
  conduct rules, or the house rules (`_DRAFTER_IDENTITY`). Those include the length budget
  the drafters are told to treat as hard. The prompt it does see opens with "follow that
  assignment from the Assignments block above", and that block is missing from its view.
- The **empathy critic** sees two things: the emotion label and the draft. It gets no
  conversation, no message, and no idea who the agent is.

So both judges fall back on a generic picture of a good assistant reply. They score
almost every draft near the ceiling: 84% of logged critic scores are ≥ 0.85, and the
median gap between the top two drafts is 0.02. When the empathy judge does break a tie,
it rewards warmth markers and closing questions. That is the Haven/Farrah failure, and
it isn't specific to Haven. The same blindness misjudges a JSON-only portfolio manager,
a story character who must end every turn with a NOTEBOOK line, and a scheduler that
must never offer extra help. The winner also trains the drafter weights, so a blind
judge teaches the brain the wrong lesson as well as picking the wrong reply.

**Proposal (design A):** the judges read what the drafters read. One cached judge prefix
holds the house rules plus the same cached persona and mandate block the drafters get.
Both judges share it, so one cache entry serves all ~10 calls in a turn. The critic keeps
its current per-turn context and gains a `brief_fit` dimension with calibration anchors.
The empathy critic gets the conversation window and the person's message, and is rewritten to work
out the person's need before scoring fit. Nobody authors anything new per agent: whatever
an operator puts in a mandate or persona becomes the judges' criteria automatically.
A settings flag (default on, per tenant) restores today's exact prompts.

On the harness (11 turns, 5 agent shapes plus a default agent, 3 samples each):
the prototype orders **95%** of gold pairs correctly, against **68%** today. It speaks an
acceptable draft on **every** sample round, against **55%** today. Each judge alone goes
from 79% (critic) and 56% (empathy) to 98% and 96%. The price:

- **+$0.006 per judged turn** with a warm cache ($0.0127 vs $0.0067)
- **+0.34 s** median judge-stage latency (1.53 s vs 1.19 s)

The p90 latency *improves* (1.69 s vs 2.03 s), because the judges' strings are now
bounded (§6).

## 2. Current data flow into each judge

`FrontalCluster._run_drafters_and_select` (brain/clusters/frontal.py) builds the drafters'
inputs. This table shows which of them reach each judge:

| Input | Drafters | Critic | Empathy critic |
|---|---|---|---|
| House rules: `_DRAFTER_IDENTITY` (length budget, voice-first format, style, persona voices, empathy guidance) | system prompt | **no** | **no** |
| Persona self-model (`memory.core.self`, i.e. self.md) | cached block (`_build_cached_context`) | **no** | **no** |
| Mandate catalog: role text + conduct rules | cached block | **no**; only the selector line pointing at a block it can't see | **no** |
| Capabilities, companion-mode user model | cached block | **no** | **no** |
| Mandate selector id | per-turn prompt | yes | no |
| Recent conversation (`parietal.recent_turns_text`, `conversation_window_turns`) | per-turn prompt | yes | **no** |
| Executive plan (`Drafting instruction` JSON: response_type, target_length, tone) and committed approach stance + success criteria | per-turn prompt | yes, but the rubric never says what target_length means | no |
| Understanding: user message, register, acoustic cues, speaker | per-turn prompt | yes | **no**; only `features.user_emotion` |
| Entity affect (feeling, appraisal, [mood] guidance) | per-turn prompt | yes (drafter-directed text) | no |
| Memory: facts, episodes, tool result, task results, inner monologue, open threads, lessons | per-turn prompt | yes | no |
| Active skill bodies (native or partner) | per-turn prompt | yes | no |
| Per-drafter fragment attachments | appended per drafter | no (deliberate: they are the per-drafter variation) | no |
| Per-turn app notes | no API field exists; apps prepend them to the message text (Haven's "Note from the app…") | inside the message | no |

Request shape (`model_router._call_anthropic`): system blocks plus the last message,
each with `cache_control`. The critic's system prompt is ~250 tokens, below Haiku 4.5's
4096-token cache minimum, so judge calls are never cached today.

Code paths. Every reply that goes through selection enters at `session_turn.py:2223`
(`frontal.process`). That covers:

- `POST /v1/sessions/{id}/turns`, `/turns/stream` (SSE) and `WS /v1/sessions/{id}/stream`
- the companion UI `/ws`, server-mic voice, and the CLI
- multi-persona (gateway routes by header) and debate/trading seats (one API session per
  seat), which use the same path

Paths that reply without the judges, and so are unaffected:

- defuse
- canned/template replies
- the critic-skip predictor and the single-draft path
- `/confirm`, `/extract`, `/admin/briefing`
- DMN proactive speech (it has its own speak judge)
- background jobs (`ResultReporter`)
- deferred tool synthesis

No route streams reply text before selection, so judge latency adds directly to
time-to-first-audio. The judge-attachment shadow A/B (`_judge_shadow_pair`) rebuilds the
same judge prompts on the local pod and must change in step.

Downstream readers of judge scores. Their calibration matters for §6:

- brainstem selection
- chemistry: the correctness reward/penalty at `overall < 0.4` (`session_turn.py:2240`)
- Hebbian drafter credit (winner margin, losers at half rate; `hebbian.py:93,280`)
- metacognition pride and self-standard gates (`metacognition.py:431,444`)
- the critic-skip predictor (`critic_avg > 0.8`) and the exec predictor's outcome
- colony satisfaction (≥ 0.6)
- the judge-attachment accuracy grader (`critic_overall`, `empathy_score`)

## 3. Measured biases

**Logged production turns** (`eval/turns.jsonl`, 2026-05-22 → 06-07). I counted 819
multi-draft turns where both judges ran, after dropping the 0.5 placeholder scores from
the old critic-cap bug.

| | critic | empathy |
|---|---|---|
| share of drafts ≥ 0.85 (critic) / ≥ 0.8 (empathy) | 84% | 84% |
| mean score (sd) | 0.885 (0.070) | 0.841 (0.101) |
| mean / median range across a turn's drafts | 0.038 / 0.020 | 0.042 / 0.030 |
| turns where every draft got the same score | — | **46%** |

- The critic's top-2 margin is under 0.03 on 62% of turns. That is about one standard
  deviation of the difference between two critic samples: the critic's sampling sd is
  ~0.013 at Haiku's default temperature (§5). So on those turns the winner is close to a coin flip.
- The empathy blend changed the critic's winner on **14.7%** of turns.
- The empathy judge's own pick matched the critic's on 62% of turns.

So "barely moves outcomes" is half right. On roughly half of turns the empathy judge is
a no-op. On roughly one turn in seven it flips the winner, and nothing that judge can see
tells it whether the flip is right.

**The proxy experiment** from the session that started this one:

- Haven context: the empathy judge added +0.06–0.08 to any draft ending in a question.
- Giving the judges the brief and the conversation put the short, no-question draft first
  (0.947) and the long, question-ending draft last (0.804). The context-blind order was
  the reverse, and spanned only 0.88–0.90.

**Harness baseline** (§5) reproduces this on fixtures written from the tenants' own
mandates, and measures the length and question premiums directly.

The critic rubric's default. Without the brief, `relevance` and `coherence` reward
answering more of what could be answered. `tone_fit` judges against an emotion label
rather than a persona. Nothing tells the critic that `target_length` is a budget. The
house rules call it "a hard budget", but only the drafters see that line. So the critic
rewards thoroughness by default, and the measured length correlation in §5 confirms it.

## 4. The use cases a fix must serve

The cases pull in opposite directions, which is why a per-agent brief written from one
example (the earlier proposal) is the wrong shape:

| Agent | What "good" means | What a blind judge does |
|---|---|---|
| Terse task agent (scheduler) | one-sentence confirmation, no offers, ask only when a field is missing; facts must match the tool result | rewards the friendly "want me to also…?" draft and the thorough one |
| Long-form analyst (mispricing analyst, debate seats, PM) | specific 4–6 sentence analysis naming the divergence; the PM returns **only** a JSON object | can't tell 4–6 sentences from rambling; may treat JSON-only output as a flaw, since the house rules say never output JSON and only the mandate overrides that |
| Spoken companion (Farrah) | 1–3 sentences, ≤ 1 question and often none, shares as much as she asks, not a therapist | rewards long and question-ending drafts; the empathy judge prefers therapist-style reflection |
| Therapist listener (Haven) | reflect first, ≤ 1 open question, no lists, no psychoanalysis; once asked for help, give one concrete thing, not more reflection | the empathy judge rewards continued reflection after a direct request for help: the "drift" the mandate forbids |
| Story character (Mara) | in voice, an action on the other character, never narrate others, never reconcile, **always** end with the NOTEBOOK line | sees the NOTEBOOK line as noise; the empathy judge rewards the conciliatory "I understand, I'm sorry" line the character must never say |
| Default agent (no mandate, built-in persona) | house rules only: target_length, voice-first prose, genuine questions | today's behavior; it must not regress |

## 5. Harness and baseline results

`eval/judge_context/` replays these turns through four arms, using the real Haiku 4.5
judge model and the engine's own prompt builders for the `current` arm:

```bash
python -m eval.judge_context.harness --dry-run
```

```bash
python -m eval.judge_context.harness --samples 3 --env-file .env --out reports/judge_context_<date>.json
```

The arms:

- **current**: the engine today. The prompts are built by `FrontalCluster._build_drafter_prompt`,
  `_critic_prompt` and `_empathy_prompt`. `tests/test_judge_context_harness.py` pins this
  arm to the engine functions.
- **context_only**: today's rubrics, plus the brief (house rules and the cached block) and
  the conversation for the empathy judge.
- **proposed**: design A.
- **listwise**: design C, one call per judge scoring all drafts.

Gold is a set of (better, worse) pairs, each justified by a line of the agent's own brief.
The harness reports:

- **pairwise**: the share of gold pairs the blended score orders correctly
- **top1**: whether the spoken draft is acceptable
- each judge's accuracy alone, spread, sampling noise and empathy flips
- the question premium and the length correlation (with gold's own length correlation for reference)
- USD per turn with a warm cache, and judge-stage latency

Baseline, 2026-11-01. Haiku 4.5, 3 samples per draft, 11 cases, 42 drafts, 40 gold pairs.

| metric | current | context_only | **proposed (A)** | listwise (C) |
|---|---|---|---|---|
| pairwise (blended) | 0.675 | 0.917 | **0.950** | 0.938 |
| top-1 acceptable | 0.545 | 0.909 | **1.000** | 1.000 |
| critic alone, pairwise | 0.787 | 0.944 | **0.975** | 0.950 |
| empathy alone, pairwise | 0.556 | 0.907 | **0.963** | 0.963 |
| critic spread across drafts | 0.32 | 0.26 | **0.49** | 0.51 |
| critic sampling noise (sd) | 0.013 | 0.018 | 0.042 | 0.055 |
| empathy flips of the critic's winner (to acceptable / away) | 7/24 (1 / 4) | 2/24 (0 / 1) | 0/24 | 3/24 (3 / 0) |
| question premium: critic / empathy | +0.08 / **+0.20** | +0.02 / +0.01 | −0.01 / −0.04 | +0.01 / −0.03 |
| length ρ, critic (gold's own: +0.24) | +0.34 | −0.04 | +0.10 | −0.05 |
| missing verdicts (unparseable) | 2 | 7 | 0 | 0 |
| winner's critic score / mean critic score | 0.92 / 0.81 | 0.93 / 0.80 | 0.90 / 0.64 | 0.91 / 0.64 |
| USD per judged turn (warm cache) | 0.0067 | 0.0138 | 0.0127 | 0.0062 |
| calls per turn | 6.5 | 6.5 | 6.5 | 1.7 |
| judge-stage latency p50 / p90 | 1.19 / 2.03 s | 2.11 / 2.80 s | **1.53 / 1.69 s** | 3.85 / 4.28 s |

Per case (pairwise, current → proposed):

| case | current | proposed |
|---|---|---|
| task_confirm | 0.80 | 1.00 |
| task_frustrated | 0.50 | 0.88 |
| analyst_mispricing | 0.88 | 0.75 |
| analyst_pm_json | 0.75 | 1.00 |
| companion_about_you | **0.00** | 1.00 |
| companion_date_bailed | 0.67 | 1.00 |
| listener_failing | 1.00 | 1.00 |
| listener_asks_help | 1.00 | 0.88 |
| story_confrontation | 0.50 | 1.00 |
| default_explain | 1.00 | 1.00 |
| default_vent | 0.50 | 1.00 |

What the numbers say:

- **The Farrah failure reproduces exactly.** On `companion_about_you` today's critic
  scores the long draft that ends in a question highest (0.92). The empathy judge prefers
  the long drafts. Every gold pair is lost. With the brief, the 1–3 sentence drafts win.
- **Today's empathy judge mostly does harm when it matters.** It flips the critic's winner
  on 7 of 24 rounds, 4 of them away from an acceptable draft. It pays +0.20 for a question
  mark. On `listener_asks_help` it prefers more reflection (0.82) over the concrete step
  the person asked for (0.79). That is exactly the drift the Haven mandate forbids. With
  context it scores them 0.88 vs 0.33.
- **The empathy judge stays distinct with context; it doesn't become a copy of the
  critic.** On `task_frustrated` the critic ranks "ask which slot" (0.77) close to "just
  move it" (0.92). The empathy judge splits them hard (0.44 vs 0.92): a person frustrated
  by a repeated failure wants it fixed, not another question. Its zero flips here mean
  the critic was already right on these fixtures, not that the empathy judge is redundant.
- **Context alone gets most of the way** (context_only, 0.917). The rewritten rubrics add
  the rest, and they matter for format contracts. With today's rubric plus the brief, the
  PM's JSON-only verdict still loses top-1 two times in three.
- **No length or question bias remains.** The critic's length correlation drops from
  +0.34 (more than gold's +0.24, so it was rewarding length beyond what the briefs want)
  to +0.10, and the question premium is gone. The one per-case dip, `analyst_mispricing`
  (0.88 → 0.75), is on a pair I labelled loosely: the 9-sentence rambling draft over the
  chatty one. The prototype marks the rambling draft down for breaking the 4–6 sentence
  rule. It still ranks the full analysis first every round. Today's critic scores the
  rambling draft level with the full analysis (0.88 each).
- **The scores spread, as the anchors intend.** The mean critic score falls from 0.81 to
  0.64 while winners stay at about 0.90. Noise rises in absolute terms (0.013 → 0.042) but
  far less than the spread, so the share of the margin that is noise falls.
- **Listwise (C) isn't worth it now.** Quality is no better than A (0.938), it is cheaper
  ($0.0062), but median latency rises by 2.7 s. Its output is ~150 tokens per draft, and
  on a spoken reply that's prohibitive.
- **Where the latency goes.** I streamed the critic calls (`current` vs `proposed`). Time
  to first token is the same (~0.45 s) with the 6–7k-token cached prefix. The difference
  is output length. In a first run with unbounded strings, the prototype's median was
  2.92 s, driven by long `veto_reason` essays. Bounding every string to a few words
  brought it to 1.53 s (run 1 is kept as `raw_run1` in the report).
- **Directional, not definitive:** 11 hand-labelled cases. Proposed's pairwise was 0.973
  in run 1 and 0.950 in run 2, so read differences under ~0.03 as noise.

After implementation, add an `engine` arm that calls the new builder with the flag on and
run it against `current`. That is the before/after check. The prototype prompts in
`harness.py` are the spec for the implementation.

## 6. Design options

### A. The judges read the drafters' brief (recommended)

Prompt layout for both judges:

```
system[0]  JUDGE_PREFIX                     cached, identical for both judges, every tenant
           = judge role + _DRAFTER_IDENTITY (the house rules) + precedence line + fence addendum
system[1]  _build_cached_context(...)       cached, the very string the drafters get
           (persona self-model, mandate catalog + conduct, capabilities, companion user model)
user       rubric (critic or empathy) + per-turn context + draft
```

- **Critic, per-turn context:** unchanged; it stays the drafter prompt. **Rubric:** add
  `brief_fit` (the assignment and conduct rules, the persona's voice, the house rules, the
  turn's drafting instruction). Fold it into `overall` with coherence, relevance and
  tone_fit. Add calibration anchors (0.9 = nothing missed; 0.7 = one soft miss; 0.5 = one
  clear miss of an explicit rule), so the score spreads instead of saturating. Add a
  single line that more thorough, more questions or more warmth score higher only where
  the brief asks for them.
- **Empathy critic, per-turn context:** a new lean builder,
  `_judge_turn_context(features, affect, parietal)`. It holds the conversation window, the
  person's message, the parsed emotion with its appraisal (the "why"), and register/voice
  cues. There's no skill bodies or memory dumps; its job is narrower than the critic's.
  **Rubric:** work out the need first (be heard, an answer, practical help, company,
  space, lightness, the scene continuing). Then score fit to that need *in this
  relationship*. Warmth markers and closing questions earn nothing on their own. The
  output gains `user_need`, which is logged for observability and to give the
  judge-attachment grader a richer claim.
- **Precedence**, stated once in the prefix: safety and honesty first; then the
  assignment and its conduct rules, including any length, format or output contract;
  then the turn's drafting instruction; then house style defaults. That is what lets the
  PM's JSON-only contract and the story NOTEBOOK line count as correct rather than as
  violations of the house rule against JSON.
- **Weights:** keep 0.7 / 0.3 and keep the existing gate (empathy runs only when the
  emotion isn't neutral). Re-tune only after the harness and a week of prod data show the
  empathy judge's flips land on the right side.
- **Caching:**
  - One prefix serves both judges and all their parallel calls. The prefix is ~6–9k
    tokens, above Haiku 4.5's 4096 minimum, and stable for a persona and mandate set:
    both blocks change only when the persona or the mandate catalog changes.
  - Concurrent calls on a cold cache each pay a write. So when the last judge call is
    older than ~4 minutes, fire one `max_tokens=1` warm-up call at the start of drafting.
    It runs concurrently with the drafters, off the critical path. This is infrastructure,
    not a pipeline stage.
  - Drop `cache_control` on the judges' last message. It is unique per draft, so today
    the router writes it to cache at 1.25× and never reads it back.
- **Bounded output:** `veto_reason` and `suggestion` stay empty unless needed, and
  then at most twelve words. `user_need` is a few words. Latency follows output
  length, and an over-long `veto_reason` currently truncates the JSON and loses the
  veto (§9). Bounded, the verdicts peak at 128 tokens, so the cells keep their caps
  (200 and 256).
- **Kill switch:** `judge_brief_context` (settings.py-declared, default **1**,
  per-tenant in settings.json). At 0 the judges build today's prompts byte-for-byte.
- **Shadow A/B (`_judge_shadow_pair`):** build both arms with the same builders, so the
  candidate is still measured against the claim it would influence. The pod's
  `runpod_num_ctx` is 12288. A large mandate catalog (up to 24k chars) plus self.md plus
  house rules can exceed it, so the shadow builder must check the length and skip the
  sample rather than truncate it.

Quality: the biggest gain, and it's generic. Every rule an operator writes in a mandate
or persona becomes a criterion with no new authoring, and the judges and drafters can't
drift apart because they read the same source. Cost and latency: see §5.

### B. A distilled per-mandate rubric

An LLM distills each (persona, mandate) into a ~150-token judge rubric, cached by hash,
with an optional operator override (`conduct.judge_criteria`).

- Cheaper per call, and works on weaker models such as the local shadow judge.
- It is a paraphrase, though. It drifts from the text the drafters follow, goes stale
  as self.md evolves through learning, and adds a generation step plus a stored artifact
  per mandate.
- **Not recommended as the primary fix.** It may be worth adding later, only as the
  compact form for the local shadow A/B if num_ctx becomes a problem.

### C. Listwise (comparative) judging

Each judge scores all drafts in one call, with the order shuffled per turn. The approach
stage already works this way (`APPROACH_CRITIC_SYSTEM`).

- Comparison is how judges discriminate best, and it uses ~2 calls per turn instead of ~10.
- But it changes the cell contract: per-draft verdicts, vetoes, judge-attachment grading
  and the shadow A/B all assume one verdict per call.
- And the output is long (~150–200 tokens per draft). That puts it on the critical path:
  see the latency row in §5.
- **Phase 2 candidate**, to decide on the numbers once A is live. It keeps both stages;
  only the call shape changes.

### D. Re-weighting only

Change 0.7/0.3, or scale empathy by emotional intensity. This is cheap, but it amplifies
a signal that is currently context-free. **Not on its own.**

### E. Merging empathy into the critic

**Rejected.** It removes a stage. The fix gives the empathy judge the job it exists for:
predicting how this person will feel, which the critic's `tone_fit` doesn't do.

## 7. Defaults and regression

- An agent that sets nothing new (no mandate, built-in persona) still changes: its judges
  now read the house rules and its self-model. The `default_*` cases are the regression
  check, so check them in the per-case table before rollout.
- **Calibration shift is intended.** The anchors lower the mean and widen the spread. In
  the baseline, the winner's mean critic score moves from 0.92 to 0.90 (and the mean critic score from 0.81 to 0.64).
  Thresholds that read absolute scores should therefore be checked before rollout:
  - the correctness penalty at < 0.4
  - the pride gate
  - the critic-skip predictor at > 0.8
  - colony satisfaction at 0.6
  - the Hebbian `(overall - 0.5) * 2` term

  Proposal: leave all of them unchanged in phase 1, since winners stay well above 0.4 and
  0.6 in the baseline. Watch `critic_avg` (the skip predictor will skip less, which costs
  calls but never quality). Revisit after a week.
- Per-tenant rollout: the flag defaults on (per the no-dark-shipping rule), and a tenant
  can be pinned off in its settings.json if something regresses.

## 8. Open questions (as put for approval; §10 records how each was settled)

1. **Precedence.** The judges would treat an assignment's explicit length or format rules
   as overriding the house defaults, including `target_length` and the house rule against
   JSON. The drafters' own prompt is ambiguous here: "within your identity and principles,
   which take precedence". Should the drafter wording be aligned in the same change?
2. **Empathy weight.** Keep 0.3 for phase 1 (recommended), or scale it with emotional
   intensity from the start?
3. **Listwise (C).** Leave it for phase 2, after A's numbers are in?
4. **Cold cache.** On a turn more than 5 minutes after the last one, the ~7k-token prefix
   is written once (about $0.009) if the warm-up call ships, or once per parallel call
   (about $0.056) if it doesn't. Ship the warm-up?
5. **Cost.** Design A costs about +$0.006 (+90%; about $0.013 instead of $0.007) per judged turn more than today at Haiku 4.5
   prices, with a warm cache. Is that acceptable?

## 9. Follow-ups found on the way (not part of this change)

- **When every draft is vetoed, a vetoed draft is spoken.** `_run_drafters_and_select`
  falls through to the single-draft path. That path endorses `drafts[0]` at a placeholder
  0.8 and overwrites the veto records. `session_turn.py` then re-adds the same text as an
  un-vetoed `final_` draft. I found this by reading the code, not by running it.
- **A long veto reason loses the veto.** The critic has no limit on `veto_reason`, and
  `max_tokens=200`. A veto explained at length truncates the JSON, and `_score_draft`
  falls back to `{"overall": 0.5, "veto": false}`. This happened on 2 of 132 rounds in
  the `current` arm, both on drafts the critic was vetoing. The one-line fix (bound the
  string) can ship ahead of the design.
- The WS transport's `bind_turn` omits `pinned_skills` (`ws.py:513-520`), so pinned
  partner skills don't apply on WS turns.

## 10. Implementation (2026-11-01)

What shipped, against §6.A:

- **Settings.** `brain/settings.py` declares `judge_brief_context` (1) and
  `judge_cache_warm_idle_s` (240). The toggle "Judges Read the Brief" sits in Attention &
  Routing.
- **Prompts.** `brain/clusters/frontal_prompts.py` adds `JUDGE_PREFIX` (role, the house
  rules, the precedence line, the fence addendum), `CRITIC_RUBRIC` and `EMPATHY_RUBRIC`.
  `CRITIC_SYSTEM` and `EMPATHY_CRITIC_SYSTEM` are untouched and remain the cells' prompts.
- **`brain/clusters/frontal.py`:**
  - `_stamp_judge_brief` stamps the turn's brief, or None when the flag is off, next to
    `_build_cached_context`.
  - `_score_draft` and `_run_empathy_check` read the brief. Their signatures are
    unchanged.
  - The new builders are `_brief_critic_prompt`, `_brief_empathy_prompt` and
    `_judge_turn_context`. `_judge_call_kwargs` passes `system_override=JUDGE_PREFIX`,
    the cached block, and `cache_last_message=False`.
  - Posted draft scores gain `brief_fit` and `user_need`.
  - `_maybe_warm_judge_cache` starts one `max_tokens=1` call alongside the drafters when
    the prefix is likely cold. It is attributed to the cell `judge_cache_warm`.
  - `_judge_shadow_pair` uses the same builders, with the brief captured at claim time.
    It skips a sample whose brief would overflow `runpod_num_ctx`.
- **Plumbing.** `IntegratorCell.call` takes `system_override` and `cache_last_message`,
  and passes them on only when set. `ModelRouter.call` and `_call_anthropic` take
  `cache_last_message`. Every other caller is unchanged.
- **Tests.** `tests/test_judge_brief_context.py` covers flag off (byte-identical), flag
  on, the shared prefix, the warm-up, the shadow pair and the plumbing.
  `tests/test_judge_context_harness.py` pins the harness `current` and `engine` arms to
  the engine code.

How the open questions were settled. Design A was approved as proposed, with §6.A's
recommendations:

1. Precedence applies to the judges as written. The drafters' wording is unchanged and
   still worth aligning.
2. Empathy weight stays at 0.3, with the existing non-neutral gate.
3. Listwise stays deferred.
4. The warm-up shipped.
5. The cost was accepted.

Before/after on the engine code (`--arms current,engine`, 3 samples, the cells' own token
caps):

| metric | current | engine |
|---|---|---|
| pairwise (blended) | 0.700 | **0.975** |
| top-1 acceptable | 0.545 | **1.000** |
| critic / empathy alone, pairwise | 0.825 / 0.519 | 0.963 / 0.963 |
| empathy flips (to acceptable / away) | 7/24 (1 / 6) | 2/24 (2 / 0) |
| question premium, critic / empathy | +0.09 / +0.21 | −0.02 / −0.09 |
| critic length ρ (gold +0.24) | +0.32 | +0.03 |
| missing verdicts | 3 | 0 |
| winner's critic / mean critic | 0.92 / 0.81 | 0.90 / 0.64 |
| USD per judged turn (warm) | 0.0068 | 0.0127 |
| judge latency p50 / p90 | 1.18 / 1.99 s | 1.50 / 1.67 s |

Both of the engine's empathy flips moved the winner to an acceptable draft. Today's judge
moved it away six times.

Not yet verified: production. After deploy, check four things:

- `judge_cache_warm` and the critic/empathy cache-read counts in the router's `[Cache]`
  log lines.
- The `critic_avg` the skip predictor sees.
- The share of turns where the correctness penalty (< 0.4) or the pride gate fires.
- A Haven conversation by voice, against the Farrah symptom.
