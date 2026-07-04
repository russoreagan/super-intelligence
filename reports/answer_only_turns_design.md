# Answer-only turns — design + decision record (2026-07-04)

## The incident

The Scheduler App runs a three-seat trading debate by sending plain conversational
turns to per-seat Elyceum agents (persona×mandate pairs like
`the_visionary.trading_bull`). In the 2026-07-03 22:28–22:37 UTC AAPL debate
(Railway deployment 04aae956), every debate turn triggered brain-side work far
beyond answering:

1. **Motor cortex** ran `cloud_action` tool calls on nearly every turn — seats
   re-fetched quotes/news/indicators the prompt already contained (13–19 LLM calls,
   57–73 s minimum per turn).
2. **Muscle memory** locked this in: the recurring "Round 2 (audit their claims)"
   prompt matched a learned open-loop plan (sim≈0.95, uses 15–17) whose step 1 is
   always `cloud_action get_quote/...`, executed verbatim with the planner bypassed
   (`motor_memory.recall_procedure` → `motor_cortex._execute_open_loop`).
3. **FollowThrough** enqueued a background multi-story internal job after nearly
   every completed turn ("AAPL stock price outlook", ralph=True, 45–120 s/story).
   These ran concurrently on the same brain process, starved the live debate turns,
   and cascaded everything past the app's per-turn timeout.

App-side mitigations landed first (Scheduler App 45a47d4 + e77263d: collective
data pre-fetch + prompts/mandates forbidding tool use), but that is prompt-level
prohibition only. Muscle memory and FollowThrough never read prompt text, so no
wording can reach them.

## Why the mechanisms are general (not debate-specific)

The harm pattern needs a caller shape, not a debate: high-volume,
latency-sensitive, repetitive turns whose wording *sounds like* a work request
("analyze", "audit", "evaluate claims") with the data already in the prompt. The
2026-06-28 Haiku bill spike (trading_ingest: 888 session-turns/day) was the same
class. Any embedding partner doing agent-panel or pipeline orchestration will
reproduce it.

## Options considered

| Option | Verdict |
|---|---|
| (a) Per-turn API flag | **Chosen** (as part of the shared gate) — surgical, zero effect unflagged |
| (g) Session-level API flag | **Chosen** — set once per debate seat session |
| (b) Agent permission `answer_only` | **Chosen** — durable backstop, no caller cooperation needed |
| (c) Suppress FollowThrough only | Rejected alone — leaves the 57–73 s in-turn motor/open-loop work intact |
| (d1) Invalidate stale debate procedures | **Chosen** — one-off hygiene (`scripts/invalidate_procedures.py`) |
| (d2) Retune muscle-memory matching thresholds | Deferred — touches live learning for all tenants; separate project |
| (e) Honor in-message "do NOT use tools" text | Rejected for now — detection must be embedding-based (no hardcoded phrase lists), i.e. probabilistic: the wrong failure mode for a binding control |
| (f) Stop FollowThrough bypassing rate caps | **Chosen** — systemic amplifier fix for every tenant |

Note: the pre-existing `motor_enable_cloud_actions: false` agent permission is NOT
a substitute for (b) — it gates at tool dispatch, so FollowThrough jobs still
enqueue and burn planner calls, and open-loop still fires with steps failing at
dispatch (wasted latency, failed-job noise, spurious divergence resets).

**Decision (Russ, 2026-07-04): one shared gate, both setters, plus d1 + f.**
Everything is opt-in and *declared*, never inferred: unflagged turns, agents,
owner chat, DMN, and internal jobs are byte-for-byte unchanged. Autonomy stays
the brain's default posture; answer-only is a contract a caller states about a
specific interaction — and marking `the_visionary.trading_bull` answer-only says
nothing about `the_visionary` under any other mandate.

## What shipped

### The shared gate
- `turn_ctx.bind_turn(..., answer_only=...)` carries the declaration
  ([brain/turn_ctx.py](../brain/turn_ctx.py)).
- `session_turn._effective_answer_only(features)` resolves the effective flag at
  turn start: turn-context declaration OR the agent's `answer_only` permission
  (fails open to False). When true, the turn is stamped
  (`features["answer_only"]`) and **`requires_action` is neutralized** before the
  frontal task subsystem and the motor branch read it — so there is no goal
  deposit, no "[task_queued]" acknowledgment the brain never honors, no motor
  planning, and no muscle-memory open-loop (its matching only runs inside motor
  execution). FollowThrough is gated separately at its enqueue site because it
  fires on every turn, action or not.

### Setters
- **API, session-sticky:** `POST /v1/sessions {"answer_only": true}` — stored on
  the session (Supabase `api_sessions.answer_only`, migration 022), echoed in the
  response, applied to every turn (POST /turns, /turns/stream, WS).
- **API, per-turn:** `{"answer_only": true|false}` in the turn body overrides the
  session default for that turn only.
- **Agent permission:** `answer_only: true` in `agents.permissions` (new
  restriction key group in [brain/agents.py](../brain/agents.py) — OR-toward-
  restriction: an agent can only turn it ON). Read on the turn hot path through a
  60 s TTL cache (`agents.answer_only()`), invalidated on `set_permissions`.

### Hygiene
- **(f)** FollowThrough's *reactive* commitment extractions now enqueue as
  `source="commitment"` instead of `source="user"`: subject to the autonomy
  rolling-window/session rate caps, the SpendRiskGate, and self-style recency
  dedup. The task-mode path (user explicitly asked, is awaiting) keeps
  `source="user"` and its bypasses. `motor_cortex.execute_internal_job` now sets
  `_current_source` for the job's duration so job records stamp the real
  initiator (previously the stamping sites read an attribute nothing ever set —
  every job record said "user").
- **(d1)** [scripts/invalidate_procedures.py](../scripts/invalidate_procedures.py):
  list muscle-memory procedures whose goal matches a regex, then `--reset` their
  use_count to 0 (below the open-loop threshold) or `--delete` them. Run as a
  Railway one-off against the trading org's persona volumes for the stale debate
  plans (pattern like `"audit their claims|Round \d|AAPL"`).

## Deploy order

1. Apply migration `022_api_sessions_answer_only.sql` FIRST — the session
   registry upserts the full row, and an unknown column makes the (best-effort)
   persist skip silently, so sessions would stop surviving restarts.
2. Deploy the brain (Railway auto-deploys from main).
3. Run `scripts/invalidate_procedures.py` (dry-run, then `--reset`) on the
   trading org's personas.
4. Scheduler App follow-up (separate repo): pass `"answer_only": true` at session
   create for debate seats; set the agent permission on the seat agents
   (`PUT /v1/agents/{agent_id} {"permissions": {"answer_only": true}}`).

## Verification

- `tests/test_answer_only.py` (16 tests): context propagation, both setters,
  per-turn override, boolean validation, fail-open, permission-key survival,
  TTL cache, commitment dedup + cap predicate, session persistence round-trip.
- Prod: rerun a debate; Railway logs must show zero
  `[MotorCortex] Running open-loop` and zero `[FollowThrough] Task enqueued` from
  debate sessions, with per-turn latency near single-draft time. The
  `[AnswerOnly] requires_action suppressed` info line confirms the gate is firing.

## Deferred / watch

- (e) in-message instruction honoring and (d2) matching retuning remain open —
  revisit if a caller that can't set flags hits the same pattern.
- Muscle-memory quality: semantic matching at sim≥0.90 over-generalizes across
  prompt templates ("Round 1 analyze" ≈ "Round 2 audit"); no decay, binary
  divergence reset. Its own project.
