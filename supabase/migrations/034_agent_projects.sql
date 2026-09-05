-- 034_agent_projects.sql
--
-- The AGENT-scoped standing work queue: one row per PROJECT, not per run.
--
-- Until now the DMN's projects lived as markdown under `## Projects assigned by
-- Russ` inside open_questions__<mandate>.md in brain_schemas, parsed by a regex
-- (brain/dmn.py::_parse_projects). Three problems followed from that substrate:
--   (a) load_core_context() folded the whole file into every turn's prompt;
--   (b) a status write resolved its target filename at COMPLETION time, so it could
--       land in a different mandate's ledger than it was read from;
--   (c) nothing carried the owning agent forward to the job, so every background
--       job ran at the ORG permission ceiling (session_turn._run_task never called
--       bind_agent) — exactly the work that runs unsupervised ignored per-agent
--       narrowing.
--
-- This table is the single writer for scheduling and status. The markdown section
-- survives only as a human INPUT surface, imported one way (brain/dmn.py::
-- set_projects_context and scripts/migrate_projects_to_table.py).
--
-- Scoping: (persona, mandate_id). agent_id = "<persona>.<mandate_id>" is derived,
-- never stored, matching brain/agents.py. Persona-level LEARNING is untouched and
-- stays keyed (org_id, persona) with no mandate dimension — episodes, wiring_edges,
-- dmn_state, self.md, user.md. One persona wearing two mandates keeps one memory and
-- one self-model; only its authorization list splits.
--
-- Deliberately NO end_user_id column: a project belongs to an agent, never to one of
-- that agent's end users, so this table is correctly ABSENT from
-- session_turn._PURGE_TABLES. Do not "fix" that omission.
--
-- Per-agent facts are DERIVED, not stored here: last-served = max(last_started_at)
-- over the agent's rows; spend today = agent_usage_totals (016). No second table.
--
-- Apply with `supabase db push` (numbered files) — never via the Supabase MCP
-- apply_migration (timestamp versions → the 2026-07-17 split-brain). Note the repo
-- is missing 033_agent_folders.sql (applied remotely via MCP); this is 034 because
-- prod already records 033.

create table if not exists agent_projects (
  id            text not null,                     -- p-<sha1(persona|mandate|title)[:12]>, deterministic
  org_id        uuid references organizations(id) on delete cascade not null,
  persona       text not null,
  mandate_id    text not null default '',          -- '' = unscoped (local dev / no mandate)
  title         text not null default '',
  task          text not null default '',
  -- ranking inputs (brain/project_scheduler.py)
  priority      smallint not null default 2,       -- 0 critical · 1 primary · 2 normal · 3 background
  user_waiting  boolean  not null default false,   -- the user asked / is waiting on the follow-through
  unblocks      text[]   not null default '{}',    -- ids of projects this one unblocks
  deadline_at   timestamptz,
  urgency_score real,                              -- LLM-appraised at intake (follow-up); null until then
  est_cost_usd  real,
  bears_on      text[]   not null default '{}',
  appraised_at  timestamptz,
  -- lifecycle: ready | running | pending | blocked | done | failed | cancelled
  -- Only READY is selectable (PENDING counts once deferred_until has passed).
  state         text not null default 'ready',
  status_note   text not null default '',          -- display only; NEVER parsed for eligibility
  max_runs      smallint not null default 1,       -- 0 = unlimited (recurring) — set deliberately
  ready_at      timestamptz not null default now(),-- the aging clock; advances only while ready
  deferred_until timestamptz,
  blocked_reason text not null default '',
  runs          int not null default 0,
  consecutive_failures int not null default 0,
  in_flight_task_id text not null default '',      -- task_queue Task.id while a step runs
  last_started_at  timestamptz,
  last_finished_at timestamptz,
  last_job_id   text not null default '',          -- joins agent_jobs.job_id → realized cloud_usd
  source        text not null default 'manual',    -- manual | seed | markdown_import | dmn
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (id, org_id)
);

alter table agent_projects enable row level security;
create policy "org can manage own agent_projects" on agent_projects for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);

-- Selection: every project for one persona across ALL its mandates (the idle lane
-- arbitrates across agents, so it reads them together).
create index if not exists agent_projects_org_agent_state_idx
  on agent_projects(org_id, persona, mandate_id, state);
-- The aging sweep and "what is ready" reads.
create index if not exists agent_projects_org_state_ready_idx
  on agent_projects(org_id, state, ready_at);

grant select, insert, update, delete on agent_projects to authenticated;
