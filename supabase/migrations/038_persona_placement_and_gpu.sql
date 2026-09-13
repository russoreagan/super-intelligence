-- 038_persona_placement_and_gpu.sql
--
-- The premium placement tier (plan §10): a persona can be PLACED on a dedicated
-- brain instance, and that instance can be given its own GPU pod (or share one pod
-- per org) instead of a slice of the platform pool. The entitlement is a row here;
-- the gateway's desired-state loop (brain/gateway/placement_control.py) makes the
-- processes and pods match the rows.
--
-- Two org-level caps join learning_mode / instance_seed (037) on the organizations
-- row, for the same reason those live there: settings.json is per process and is
-- seeded once, while these must read identically from the shared instance, every
-- dedicated instance and the gateway.
--
--   max_dedicated_instances  per-org ceiling on dedicated persona instances.
--                            0 = fall back to the deployment's BRAIN_MAX_DEDICATED.
--   gpu_daily_usd_budget     per-org daily (UTC) ceiling on standalone / org pod
--                            spend. 0 = the org may not hold standalone pods at all
--                            (POST .../placement with pod standalone|org → 402).
--
-- persona_placement: one row per placed persona, PK (org_id, persona).
--   mode        shared | dedicated   — its own process, or per-turn binding on the
--                                      org's shared instance (the default when no
--                                      row exists).
--   pod         pool | standalone | org — which GPU the dedicated instance's local
--                                      calls go to: the platform pool, a pod of its
--                                      own, or one pod shared by this org's dedicated
--                                      instances.
--   gpu_type    RunPod gpu_type_id override for a standalone/org pod (null = the
--               pool's ranking; a set value lifts the pool's price ceiling — premium).
--   always_on   keep the instance up regardless of client traffic (the 24 h reaper
--               skips it). Dormancy still stops its DMN demand, so its pod sleeps.
--   paid_until  null = open-ended; past → the controller consolidates and stops the
--               instance and the placement reads as demoted.
--   RLS mirrors persona_owners (037): the org manages its own rows.
--
-- gpu_usage: wall-clock metering for standalone / org pods, one additive row per
-- controller tick per persona (pod_kind org splits the tick evenly across the org's
-- dedicated instances). The pool share stays in agent_usage.pod_s (016). Summing
-- rows over [since, until] is correct across restarts, same as agent_usage.
--
-- Two per-day rollups back GET /v1/usage. Both are SECURITY DEFINER with the 030
-- shape — coalesce(auth.uid(), p_org_id): under an org JWT auth.uid() wins and the
-- caller cannot name another org; under the service role (auth.uid() null) the brain
-- passes its own org id — and both revoke anon/public per 026, because a new
-- security-definer function inherits Supabase's default anon EXECUTE grant.
--
-- Pre-migration safety: brain/persona_placement.py reads with select("*") and
-- treats a missing table as "no placements" (one log line); brain/org_settings.py
-- treats missing columns as 0; the usage stores treat a missing RPC as no data.
-- The write paths fail loudly with "apply migration 038" until this is applied.
--
-- Apply with `supabase db push` (numbered files), after 037 — never via the MCP
-- apply_migration.

alter table organizations
  add column if not exists max_dedicated_instances int not null default 0;
alter table organizations
  add column if not exists gpu_daily_usd_budget numeric not null default 0;

create table if not exists persona_placement (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  mode text not null default 'dedicated',
  pod text not null default 'pool',
  gpu_type text,
  always_on boolean not null default true,
  paid_until timestamptz,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (org_id, persona)
);
alter table persona_placement
  drop constraint if exists persona_placement_mode_check;
alter table persona_placement
  add constraint persona_placement_mode_check
  check (mode in ('shared', 'dedicated'));
alter table persona_placement
  drop constraint if exists persona_placement_pod_check;
alter table persona_placement
  add constraint persona_placement_pod_check
  check (pod in ('pool', 'standalone', 'org'));
alter table persona_placement enable row level security;
create policy "org can manage own persona_placement" on persona_placement for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on persona_placement to authenticated;

create table if not exists gpu_usage (
  org_id uuid references organizations(id) on delete cascade not null,
  id bigint generated always as identity,
  persona text not null default '',
  pod_kind text not null default 'standalone',
  pod_id text not null default '',
  seconds numeric not null default 0,
  usd numeric not null default 0,
  ts timestamptz not null default now(),
  primary key (org_id, id)
);
alter table gpu_usage
  drop constraint if exists gpu_usage_pod_kind_check;
alter table gpu_usage
  add constraint gpu_usage_pod_kind_check
  check (pod_kind in ('standalone', 'org', 'pool'));
alter table gpu_usage enable row level security;
create policy "org can read own gpu_usage" on gpu_usage for select
  using (auth.uid() = org_id);
create index if not exists gpu_usage_org_ts_idx on gpu_usage(org_id, ts desc);
grant select on gpu_usage to authenticated;

-- Per-day, per-persona rollup of the model-usage ledger (016) for GET /v1/usage.
-- Days are UTC. p_until is EXCLUSIVE so [since, until) windows tile without
-- double-counting a boundary row.
create or replace function public.agent_usage_by_day(
  p_org_id uuid,
  p_since  timestamptz default null,
  p_until  timestamptz default null
) returns table (
  day date, persona text, agent_id text,
  calls bigint, cloud_calls bigint, in_tok bigint, out_tok bigint,
  cloud_usd numeric, pod_s numeric
)
language sql
stable
security definer
set search_path = ''
as $$
  select (u.ts at time zone 'UTC')::date as day,
         u.persona, u.agent_id,
         sum(u.calls)::bigint, sum(u.cloud_calls)::bigint,
         sum(u.in_tok)::bigint, sum(u.out_tok)::bigint,
         sum(u.cloud_usd)::numeric, sum(u.pod_s)::numeric
  from public.agent_usage u
  where u.org_id = coalesce(auth.uid(), p_org_id)
    and (p_since is null or u.ts >= p_since)
    and (p_until is null or u.ts < p_until)
  group by 1, 2, 3;
$$;
revoke all on function public.agent_usage_by_day(uuid, timestamptz, timestamptz) from public;
revoke execute on function public.agent_usage_by_day(uuid, timestamptz, timestamptz) from anon, public;
grant execute on function public.agent_usage_by_day(uuid, timestamptz, timestamptz) to authenticated, service_role;

-- Per-day, per-persona rollup of standalone / org pod wall-clock for GET /v1/usage.
create or replace function public.gpu_usage_by_day(
  p_org_id uuid,
  p_since  timestamptz default null,
  p_until  timestamptz default null
) returns table (
  day date, persona text, pod_kind text, seconds numeric, usd numeric
)
language sql
stable
security definer
set search_path = ''
as $$
  select (g.ts at time zone 'UTC')::date as day,
         g.persona, g.pod_kind,
         sum(g.seconds)::numeric, sum(g.usd)::numeric
  from public.gpu_usage g
  where g.org_id = coalesce(auth.uid(), p_org_id)
    and (p_since is null or g.ts >= p_since)
    and (p_until is null or g.ts < p_until)
  group by 1, 2, 3;
$$;
revoke all on function public.gpu_usage_by_day(uuid, timestamptz, timestamptz) from public;
revoke execute on function public.gpu_usage_by_day(uuid, timestamptz, timestamptz) from anon, public;
grant execute on function public.gpu_usage_by_day(uuid, timestamptz, timestamptz) to authenticated, service_role;
