# Job stores: ownership contracts + unification plan

*2026-07-01, from the holistic review. Status: design — phase 1 is documentation
(this file), phases 2–3 are implementable increments.*

## The four stores and what each actually is

| Store | Medium | Owns | Lifecycle |
|---|---|---|---|
| `task_queue.json` (PersistentTaskQueue) | volume JSON | **Intent**: work waiting to run (pending/deferred/blocked), dedup, priority, boot recovery | pre-execution → deleted/terminal on completion |
| `second_brain/jobs/{id}.json` (JobStore) | volume JSON | **Full record + resumable checkpoint**: steps, complete tool outputs, written files, spoken summary; `done=False` marks resumability | written incrementally during execution; trimmed by count/size caps |
| `agent_jobs` table (migration 021) | Supabase | **Durable queryable outcome**: state machine (running/completed/failed/deferred/stopped/awaiting), reasons, summary, productive_steps; powers `/v1/jobs` | upserted per chunk + at terminal state; survives volume loss |
| open-threads ledger | volume (DMN state) | **Cognition**: what the brain is *thinking about* across idle ticks — not a job record | DMN-internal; conclusions get committed to memory |

Verdict from the review: these are four *concerns*, not four copies — full unification
into one store would conflate dispatch state with outcome history with cognition.
The real defects are (a) no enforced linkage, (b) outcome persistence duplicated
across four motor call sites, (c) summary/state fields drifting between JobStore
and agent_jobs projections.

## Contract (phase 1 — normative as of this doc)

- `agent_jobs` is the **source of truth for outcomes**. Anything user- or
  API-facing (recall_jobs, /v1/jobs, dashboards) reads it first; JobStore JSON is
  the local/companion fallback and the resume checkpoint — never the primary read
  path when Supabase is available. (Already true in code: `_recall_jobs`,
  `list_jobs`.)
- `task_queue.json` never stores outcomes beyond its own status enum; its
  `job_id` field is the foreign key into JobStore/agent_jobs, set when execution
  starts. A terminal task with no corresponding agent_jobs row is a bug (the boot
  `agent_jobs_store.reconcile()` repairs the JobStore→table half).
- open-threads stays out of the job system entirely; the only sanctioned link is
  `DMN.note_job_result()` seeding reflection from a finished job.

## Phase 2 — single outcome write path (next implementable step)

Today four motor_cortex sites each do their own `job_store.save(...)` +
`_mirror_job_to_table(...)` with hand-assembled kwargs: `_notify_job_complete`,
`_persist_gated_outcome`, `_deferred_outcome`, `_persist_progress`. Consolidate on
one `_persist_job_record(outcome_or_kwargs, *, resumable: bool)` helper so:
- the JobStore projection and the agent_jobs projection can't drift (one place
  builds both),
- every write logs the same way on failure (reconcile depends on this),
- new fields (e.g. cloud_usd attribution) get added once.

Risk: low — pure consolidation, byte-identical records; gate on the full suite
plus `tests/test_review_hardening.py` reconcile tests.

## Phase 3 — optional, only if pain recurs

- Emit task_queue terminal transitions into agent_jobs as state changes (one
  history for "what happened to the work I asked for", including quarantines —
  today a quarantined poison task is visible only in logs + queue JSON).
- Add `org_id`-scoped retention to agent_jobs (the JSON store trims by
  count/size; the table currently grows unbounded).

## Non-goals

- Moving the resume checkpoint to Supabase (volume JSON is the right latency/
  consistency point for mid-job writes).
- Merging open-threads into the job system (cognition ≠ work tracking).
