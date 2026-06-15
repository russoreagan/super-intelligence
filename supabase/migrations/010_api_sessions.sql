-- 010_api_sessions.sql
-- Durable engine-API sessions.
--
-- The engine API binds a partner-facing session id to one end_user (and an agent /
-- mandate). v1 kept these in process memory (brain/api/sessions.py), so every
-- Railway redeploy dropped every open session — a partner mid-conversation lost
-- the binding. Persist them so they survive restarts. The `pending` column also
-- gives the cloud-write confirmation flow a per-session home (the executor's
-- in-process pending slot is process-global and unsafe across concurrent
-- sessions). `partner_id` is reserved for per-partner key scoping.
--
-- One process serves one org; RLS scopes every row to auth.uid() = org_id, same
-- as the rest of the per-tenant schema. Companion/local mode (no Supabase) keeps
-- the in-memory registry and never touches this table.

create table if not exists api_sessions (
  org_id uuid references organizations(id) on delete cascade not null,
  session_id text not null,
  end_user_id text not null,
  agent_id text,
  mandate_id text,
  partner_id text,
  pending jsonb,
  created_ts timestamptz not null default now(),
  updated_ts timestamptz not null default now(),
  primary key (org_id, session_id)
);
alter table api_sessions enable row level security;
create policy "org can manage own api_sessions" on api_sessions for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index if not exists api_sessions_org_end_user_idx on api_sessions(org_id, end_user_id);
grant select, insert, update, delete on api_sessions to authenticated;
