-- 015_agent_turns.sql
-- Durable activity log for engine-API "agent" turns (the partner-/agent-driven
-- path, e.g. the trading app), so the owner has a separate, auditable view of
-- what every agent is being asked — kept entirely out of the main chat feed.
--
-- The interactive UI keeps its recent turns in process memory (UiServer._chat_history)
-- and replays them on reconnect. Agent turns are now treated the same way, but the
-- recent buffer is backed by this table so the Agents view survives a restart (and,
-- when an org runs a dedicated agent-worker process, aggregates across processes).
--
-- One process serves one org; RLS scopes every row to auth.uid() = org_id, same as
-- the rest of the per-tenant schema. Companion/local mode (no Supabase) keeps only
-- the in-memory buffer and never touches this table.

create table if not exists agent_turns (
  org_id uuid references organizations(id) on delete cascade not null,
  id bigint generated always as identity,
  persona text not null default '',
  agent_id text not null default '',
  end_user_id text not null default '',
  session_id text not null default '',
  turn_id text not null default '',
  prompt text not null default '',
  response text not null default '',
  ts timestamptz not null default now(),
  primary key (org_id, id)
);
alter table agent_turns enable row level security;
create policy "org can manage own agent_turns" on agent_turns for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
-- Recent-first reads for the Agents view, overall and per agent.
create index if not exists agent_turns_org_ts_idx on agent_turns(org_id, ts desc);
create index if not exists agent_turns_org_agent_ts_idx on agent_turns(org_id, agent_id, ts desc);
grant select, insert, update, delete on agent_turns to authenticated;
