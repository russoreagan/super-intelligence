-- 021_agent_jobs.sql
-- Durable, queryable record of autonomous "motor cortex" JOB OUTCOMES.
--
-- Until now job results lived only in second_brain/jobs/{id}.json on the tenant
-- volume (JobStore) — there was no DB table, so results were not pollable, survived
-- no volume loss, and could not power a dashboard. (An orphaned tasks.job_data column
-- from the 001/007 schema was never populated.) This table is the durable source of
-- truth for what an autonomous job produced.
--
-- Written best-effort by brain/agent_jobs_store.py, mirroring agent_usage (016) /
-- agent_turns (015): the brain (service role) upserts by job_id, RLS scopes reads to
-- the owning org, and companion/local mode (no Supabase) is a silent no-op with the
-- JSON JobStore as the fallback. The row is upserted INCREMENTALLY — once per
-- completed chunk/story with state='running' and results-so-far — so partial results
-- are durable even if the whole job later fails/defers/is killed; the terminal write
-- flips the same row to its final state.

create table if not exists agent_jobs (
  job_id text primary key,
  org_id uuid references organizations(id) on delete cascade not null,
  agent_id text not null default '',
  source text not null default 'self',       -- user | self | recovery
  goal text not null default '',
  -- brain.autonomy JobState: running | completed | deferred | failed |
  -- stopped_budget | awaiting_approval
  state text not null default 'running',
  reason_code text not null default '',
  reason_human text not null default '',
  summary text not null default '',
  productive_steps int not null default 0,
  stories_completed int not null default 0,
  stories_total int not null default 0,
  steps_json jsonb not null default '[]'::jsonb,
  results_json jsonb not null default '[]'::jsonb,
  source_links jsonb not null default '[]'::jsonb,
  written_files jsonb not null default '[]'::jsonb,
  cloud_usd numeric not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);
alter table agent_jobs enable row level security;
create policy "org can manage own agent_jobs" on agent_jobs for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
-- Newest-first listing per org, and per-agent filtering for the dashboard.
create index if not exists agent_jobs_org_updated_idx on agent_jobs(org_id, updated_at desc);
create index if not exists agent_jobs_org_state_idx on agent_jobs(org_id, state);
create index if not exists agent_jobs_org_agent_idx on agent_jobs(org_id, agent_id, updated_at desc);
grant select, insert, update, delete on agent_jobs to authenticated;
