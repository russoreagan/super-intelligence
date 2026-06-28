-- 020_agent_skills.sql
-- Map app-provided skills to agents.
--
-- A skill applies to an agent's turns either because it is GLOBAL (all_agents) or
-- because it is explicitly MAPPED to that agent. This makes the per-turn skill set
-- agent-aware, mirroring the per-agent connector narrowing already on agents.permissions.
--
--   skills.all_agents  — true (default): the skill is available to every agent, exactly
--                        as before this migration (so existing skills are unchanged).
--                        false: available ONLY to the agents listed in agent_skills.
--   agent_skills       — the (agent → skill) mapping, used when all_agents = false.
--
-- An agent is the (persona, mandate_id) pairing (009_agents.sql), so the mapping is
-- keyed by both halves plus the skill id. Cascades clean it up when either the agent
-- or the skill row is removed.

alter table public.skills
  add column if not exists all_agents boolean not null default true;

create table if not exists public.agent_skills (
  org_id      uuid not null references organizations(id) on delete cascade,
  persona     text not null,
  mandate_id  text not null,
  skill_id    text not null,
  created_at  timestamptz not null default now(),
  primary key (org_id, persona, mandate_id, skill_id),
  foreign key (org_id, persona, mandate_id)
    references public.agents(org_id, persona, mandate_id) on delete cascade,
  foreign key (org_id, skill_id)
    references public.skills(org_id, id) on delete cascade
);

alter table public.agent_skills enable row level security;
create policy "org can manage own agent_skills"
  on public.agent_skills for all
  using (auth.uid() = org_id)
  with check (auth.uid() = org_id);

create index if not exists agent_skills_skill_idx on public.agent_skills (org_id, skill_id);
create index if not exists agent_skills_agent_idx on public.agent_skills (org_id, persona, mandate_id);

grant select, insert, update, delete on public.agent_skills to authenticated;
