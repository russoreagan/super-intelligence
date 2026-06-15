-- 009_agents.sql
-- Promote the persona↔role pairing to a first-class AGENT.
--
-- An agent = (persona, role/mandate). Elsewhere "agent" means one fused system
-- prompt; here a persona is a durable identity (chemistry/memory) and a role is a
-- swappable job, so their PAIRING is the agent. 008 already built this pairing as
-- `persona_mandates` with a reserved `overrides jsonb`; this migration renames it
-- to `agents` and gives it the two things a first-class object needs:
--
--   name        — optional display label (the agent_id itself stays DERIVED as
--                 `<persona>.<mandate_id>`; both halves are dot-free slugs, so the
--                 composite is unambiguous and nothing needs to be stored).
--   permissions — per-agent motor/operational RESTRICTIONS, applied as a narrowing
--                 WITHIN the org-level ceiling (settings.json). Empty {} = inherit.
--
-- Idempotent and order-independent vs 008: 008 may or may not be applied to a
-- given database (it was committed but not yet run on hosted). If `persona_mandates`
-- exists we rename it; otherwise we create `agents` fresh in 008's shape.

do $$
begin
  if exists (select 1 from information_schema.tables
             where table_schema = 'public' and table_name = 'persona_mandates')
     and not exists (select 1 from information_schema.tables
             where table_schema = 'public' and table_name = 'agents') then
    alter table persona_mandates rename to agents;
  end if;
end $$;

create table if not exists agents (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  mandate_id text not null,
  enabled boolean not null default true,
  overrides jsonb not null default '{}'::jsonb,
  sort_order int not null default 0,
  updated_at timestamptz not null default now(),
  primary key (org_id, persona, mandate_id),
  foreign key (org_id, mandate_id) references mandates(org_id, id) on delete cascade
);

alter table agents add column if not exists name text;
alter table agents add column if not exists permissions jsonb not null default '{}'::jsonb;

-- RLS: tenant-scoped, same as every other per-org table (008 created an
-- identically-scoped policy under the old name; recreate it idempotently here so
-- the rename path and the fresh-create path both end with the right policy).
alter table agents enable row level security;
drop policy if exists "org can manage own persona_mandates" on agents;
drop policy if exists "org can manage own agents" on agents;
create policy "org can manage own agents" on agents for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index if not exists agents_org_persona_idx on agents(org_id, persona);
grant select, insert, update, delete on agents to authenticated;
