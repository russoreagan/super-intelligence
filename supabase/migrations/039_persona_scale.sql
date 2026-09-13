-- 039_persona_scale.sql
--
-- Persona identity gets a table, and metering gets a daily rollup — the two
-- pieces of schema that let an org with thousands of purchase personas list,
-- search and cost them in O(page) instead of scanning every persona.json on the
-- network volume per request.
--
-- personas: the INDEX of the org's catalogue — one row per persona (built-ins and
--   customs alike), keyed (org_id, persona) so the purge sweep
--   (session_turn._PERSONA_PURGE_TABLES) and the audit conventions apply
--   unchanged. persona.json stays the source of truth for the SPEC (it is read one
--   at a time); this table is the source of truth for listing, search, activity
--   and the fleet rollups. Columns:
--     display_name, builtin, builtin_override, template, seed, tag, note, version,
--     spec_updated            — mirrored from the spec at every upsert/clone/delete
--     learned_state(_at)      — set once a sleep pass or a `current`-seed clone
--                               leaves learned state behind (brain/persona_index.py)
--     last_human_turn_ts      — the per-persona stamp (human_activity.stamp_persona)
--                               pushed in batches through persona_touch_batch; the
--                               isolated-org idle roster reads it (Phase D)
--     deleted_at              — soft delete on DELETE /v1/personas/{p}; the hard purge
--                               removes the row
--     cost_today_usd, cost_7d_usd, turns_7d, owner_bound, owner_ref, owner_count,
--     partner_id, fingerprint, fingerprint_at, tier, answer_only, enabled_agents
--                             — the fleet-console rollup columns (plan Part 2);
--                               owner_ref is an HMAC of the buyer id, never the id
--   RLS/grants mirror persona_owners (037). persona_touch_batch uses greatest() so
--   several processes (the shared instance, dedicated instances) can push the same
--   persona's stamp without regressing it.
--
-- agent_usage_daily: the rollup of the additive agent_usage ledger (016), keyed
--   (org_id, usage_date, agent_id, end_user_id). The router flushes the same delta
--   rows here through bump_agent_usage_daily (on conflict add — the 031 shape), so
--   a date-range read is one row per (agent, day) rather than one per flush. Adds
--   end_user_id to metering (per-customer cost; content-free) — the raw table gains
--   the column too so raw and daily agree. Readers mirror 016/017:
--   agent_usage_totals_daily (by agent), persona_usage_totals (by persona, for the
--   personas on one page), end_user_usage_totals (top-N customers) and the
--   service-role-only agent_usage_totals_all_daily.
--
-- persona_fingerprints: the audit fingerprint history (Phase F), so a drawer can
--   show when a persona's learned state last changed without recomputing it.
--
-- Indexes: agent_turns / tasks / api_sessions gain the (org, persona) shapes the
-- fleet routes read by. The episodes ivfflat is deliberately untouched (plan 3.5).
--
-- Pre-migration safety: brain/persona_index.py probes for the table and reads as
-- "index off" (60 s cache) when it is absent; agent_usage_store.bump_daily treats a
-- missing RPC as "not written" and the router keeps writing raw rows. Nothing
-- reads from these tables until Phase D flips persona_index_read /
-- agent_usage_read_daily.
--
-- Apply with `supabase db push` (numbered files), after 038 — never via the MCP
-- apply_migration.

create extension if not exists pg_trgm;

-- ── personas ──────────────────────────────────────────────────────────────────

create table if not exists personas (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  display_name text not null default '',
  builtin boolean not null default false,
  builtin_override boolean not null default false,
  template text not null default '',
  seed text not null default '',
  tag text not null default '',
  note text not null default '',
  version int not null default 0,
  spec_updated timestamptz,
  learned_state boolean not null default false,
  learned_state_at timestamptz,
  last_human_turn_ts timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz,
  cost_today_usd numeric not null default 0,
  cost_7d_usd numeric not null default 0,
  turns_7d int not null default 0,
  owner_bound boolean not null default false,
  owner_ref text not null default '',
  owner_count int not null default 0,
  partner_id text not null default '',
  fingerprint text not null default '',
  fingerprint_at timestamptz,
  tier text not null default 'full',
  answer_only boolean not null default false,
  enabled_agents int not null default 0,
  primary key (org_id, persona)
);
alter table personas
  drop constraint if exists personas_persona_slug_check;
alter table personas
  add constraint personas_persona_slug_check
  check (persona ~ '^[a-z0-9][a-z0-9_]{0,63}$');

create index if not exists personas_org_template_persona_idx
  on personas(org_id, template, persona) where deleted_at is null;
create index if not exists personas_org_last_turn_idx
  on personas(org_id, last_human_turn_ts desc);
create index if not exists personas_org_persona_pattern_idx
  on personas(org_id, persona text_pattern_ops);
create index if not exists personas_display_name_trgm_idx
  on personas using gin (display_name gin_trgm_ops);

alter table personas enable row level security;
drop policy if exists "org can manage own personas" on personas;
create policy "org can manage own personas" on personas for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on personas to authenticated;

-- Batched activity stamp: p_touches is {"<persona>": "<iso timestamptz>", ...}.
-- greatest() so a stale flush from one process never regresses a newer stamp
-- from another. Returns the number of rows advanced.
create or replace function public.persona_touch_batch(
  p_org_id  uuid,
  p_touches jsonb
)
returns int
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_n   int;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  update public.personas p
     set last_human_turn_ts = greatest(
           coalesce(p.last_human_turn_ts, '-infinity'::timestamptz),
           t.ts
         ),
         updated_at = now()
    from (
      select key as persona, value::timestamptz as ts
      from jsonb_each_text(coalesce(p_touches, '{}'::jsonb))
    ) t
   where p.org_id = v_org
     and p.persona = t.persona
     and (p.last_human_turn_ts is null or p.last_human_turn_ts < t.ts);
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;
revoke all on function public.persona_touch_batch(uuid, jsonb) from public;
revoke execute on function public.persona_touch_batch(uuid, jsonb) from anon, public;
grant execute on function public.persona_touch_batch(uuid, jsonb) to authenticated, service_role;

-- ── agent_usage_daily ─────────────────────────────────────────────────────────

alter table agent_usage
  add column if not exists end_user_id text not null default '';

create table if not exists agent_usage_daily (
  org_id uuid references organizations(id) on delete cascade not null,
  usage_date date not null,
  agent_id text not null default '',
  end_user_id text not null default '',
  persona text not null default '',
  calls bigint not null default 0,
  cloud_calls bigint not null default 0,
  in_tok bigint not null default 0,
  out_tok bigint not null default 0,
  cloud_usd numeric not null default 0,
  pod_s numeric not null default 0,
  updated_ts timestamptz not null default now(),
  primary key (org_id, usage_date, agent_id, end_user_id)
);
create index if not exists agent_usage_daily_org_persona_date_idx
  on agent_usage_daily(org_id, persona, usage_date desc);
create index if not exists agent_usage_daily_org_end_user_date_idx
  on agent_usage_daily(org_id, end_user_id, usage_date desc) where end_user_id <> '';

alter table agent_usage_daily enable row level security;
drop policy if exists "org can manage own agent_usage_daily" on agent_usage_daily;
create policy "org can manage own agent_usage_daily" on agent_usage_daily for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on agent_usage_daily to authenticated;

-- Atomic additive upsert of one flush's delta rows. p_rows is a JSON array of
-- {agent_id, end_user_id, persona, calls, cloud_calls, in_tok, out_tok, cloud_usd,
-- pod_s}. Returns the number of rows touched.
create or replace function public.bump_agent_usage_daily(
  p_org_id uuid,
  p_rows   jsonb,
  p_date   date default null
)
returns int
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := coalesce(auth.uid(), p_org_id);
  v_date date := coalesce(p_date, (now() at time zone 'utc')::date);
  v_n    int;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  insert into public.agent_usage_daily
    (org_id, usage_date, agent_id, end_user_id, persona,
     calls, cloud_calls, in_tok, out_tok, cloud_usd, pod_s)
  select v_org, v_date,
         coalesce(r.agent_id, ''), coalesce(r.end_user_id, ''), coalesce(r.persona, ''),
         coalesce(r.calls, 0), coalesce(r.cloud_calls, 0),
         coalesce(r.in_tok, 0), coalesce(r.out_tok, 0),
         coalesce(r.cloud_usd, 0), coalesce(r.pod_s, 0)
  from jsonb_to_recordset(coalesce(p_rows, '[]'::jsonb)) as r(
         agent_id text, end_user_id text, persona text,
         calls bigint, cloud_calls bigint, in_tok bigint, out_tok bigint,
         cloud_usd numeric, pod_s numeric)
  on conflict (org_id, usage_date, agent_id, end_user_id) do update
    set calls       = public.agent_usage_daily.calls + excluded.calls,
        cloud_calls = public.agent_usage_daily.cloud_calls + excluded.cloud_calls,
        in_tok      = public.agent_usage_daily.in_tok + excluded.in_tok,
        out_tok     = public.agent_usage_daily.out_tok + excluded.out_tok,
        cloud_usd   = public.agent_usage_daily.cloud_usd + excluded.cloud_usd,
        pod_s       = public.agent_usage_daily.pod_s + excluded.pod_s,
        persona     = case when excluded.persona <> '' then excluded.persona
                           else public.agent_usage_daily.persona end,
        updated_ts  = now();
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;
revoke all on function public.bump_agent_usage_daily(uuid, jsonb, date) from public;
revoke execute on function public.bump_agent_usage_daily(uuid, jsonb, date) from anon, public;
grant execute on function public.bump_agent_usage_daily(uuid, jsonb, date) to authenticated, service_role;

-- By agent over [since, until] (dates, inclusive) — the same columns
-- agent_usage_totals (016) returns, so the reader can switch behind a flag.
create or replace function public.agent_usage_totals_daily(
  p_org_id uuid,
  p_since  date default null,
  p_until  date default null
) returns table (
  agent_id text, calls bigint, cloud_calls bigint,
  in_tok bigint, out_tok bigint, cloud_usd numeric, pod_s numeric, last_ts timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
  select d.agent_id,
         sum(d.calls)::bigint, sum(d.cloud_calls)::bigint,
         sum(d.in_tok)::bigint, sum(d.out_tok)::bigint,
         sum(d.cloud_usd)::numeric, sum(d.pod_s)::numeric, max(d.updated_ts)
  from public.agent_usage_daily d
  where d.org_id = coalesce(auth.uid(), p_org_id)
    and (p_since is null or d.usage_date >= p_since)
    and (p_until is null or d.usage_date <= p_until)
  group by d.agent_id;
$$;
revoke all on function public.agent_usage_totals_daily(uuid, date, date) from public;
revoke execute on function public.agent_usage_totals_daily(uuid, date, date) from anon, public;
grant execute on function public.agent_usage_totals_daily(uuid, date, date) to authenticated, service_role;

-- By persona, for the personas on one page (O(page): the caller passes the slugs
-- it is rendering; null/empty = every persona).
create or replace function public.persona_usage_totals(
  p_org_id   uuid,
  p_personas text[] default null,
  p_since    date default null,
  p_until    date default null
) returns table (
  persona text, calls bigint, cloud_calls bigint,
  in_tok bigint, out_tok bigint, cloud_usd numeric, pod_s numeric, last_ts timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
  select d.persona,
         sum(d.calls)::bigint, sum(d.cloud_calls)::bigint,
         sum(d.in_tok)::bigint, sum(d.out_tok)::bigint,
         sum(d.cloud_usd)::numeric, sum(d.pod_s)::numeric, max(d.updated_ts)
  from public.agent_usage_daily d
  where d.org_id = coalesce(auth.uid(), p_org_id)
    and (p_personas is null or cardinality(p_personas) = 0 or d.persona = any(p_personas))
    and (p_since is null or d.usage_date >= p_since)
    and (p_until is null or d.usage_date <= p_until)
  group by d.persona;
$$;
revoke all on function public.persona_usage_totals(uuid, text[], date, date) from public;
revoke execute on function public.persona_usage_totals(uuid, text[], date, date) from anon, public;
grant execute on function public.persona_usage_totals(uuid, text[], date, date) to authenticated, service_role;

-- Top-N customers by cloud spend. end_user_id is the partner's opaque id — the
-- rollup carries no content.
create or replace function public.end_user_usage_totals(
  p_org_id uuid,
  p_since  date default null,
  p_until  date default null,
  p_limit  int default 50
) returns table (
  end_user_id text, calls bigint, cloud_calls bigint,
  in_tok bigint, out_tok bigint, cloud_usd numeric, pod_s numeric, last_ts timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
  select d.end_user_id,
         sum(d.calls)::bigint, sum(d.cloud_calls)::bigint,
         sum(d.in_tok)::bigint, sum(d.out_tok)::bigint,
         sum(d.cloud_usd)::numeric, sum(d.pod_s)::numeric, max(d.updated_ts)
  from public.agent_usage_daily d
  where d.org_id = coalesce(auth.uid(), p_org_id)
    and d.end_user_id <> ''
    and (p_since is null or d.usage_date >= p_since)
    and (p_until is null or d.usage_date <= p_until)
  group by d.end_user_id
  order by sum(d.cloud_usd) desc, d.end_user_id
  limit greatest(1, least(coalesce(p_limit, 50), 1000));
$$;
revoke all on function public.end_user_usage_totals(uuid, date, date, int) from public;
revoke execute on function public.end_user_usage_totals(uuid, date, date, int) from anon, public;
grant execute on function public.end_user_usage_totals(uuid, date, date, int) to authenticated, service_role;

-- Cross-org rollup for the platform superadmin (017 pattern): NOT org-filtered,
-- so it is granted ONLY to service_role.
create or replace function public.agent_usage_totals_all_daily(
  p_since date default null,
  p_until date default null
) returns table (
  org_id uuid, org_name text, agent_id text, calls bigint, cloud_calls bigint,
  in_tok bigint, out_tok bigint, cloud_usd numeric, pod_s numeric, last_ts timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
  select d.org_id, coalesce(o.name, '') as org_name, d.agent_id,
         sum(d.calls)::bigint, sum(d.cloud_calls)::bigint,
         sum(d.in_tok)::bigint, sum(d.out_tok)::bigint,
         sum(d.cloud_usd)::numeric, sum(d.pod_s)::numeric, max(d.updated_ts)
  from public.agent_usage_daily d
  left join public.organizations o on o.id = d.org_id
  where (p_since is null or d.usage_date >= p_since)
    and (p_until is null or d.usage_date <= p_until)
  group by d.org_id, o.name, d.agent_id;
$$;
revoke all on function public.agent_usage_totals_all_daily(date, date) from public, anon, authenticated;
grant execute on function public.agent_usage_totals_all_daily(date, date) to service_role;

-- ── persona_fingerprints ──────────────────────────────────────────────────────

create table if not exists persona_fingerprints (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  ts timestamptz not null default now(),
  fingerprint text not null default '',
  trigger text not null default '',
  primary key (org_id, persona, ts)
);
create index if not exists persona_fingerprints_org_persona_ts_idx
  on persona_fingerprints(org_id, persona, ts desc);
alter table persona_fingerprints enable row level security;
drop policy if exists "org can manage own persona_fingerprints" on persona_fingerprints;
create policy "org can manage own persona_fingerprints" on persona_fingerprints for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on persona_fingerprints to authenticated;

-- ── indexes on existing persona-keyed stores (plan 3.5) ───────────────────────

create index if not exists agent_turns_org_persona_ts_idx on agent_turns(org_id, persona, ts desc);
create index if not exists tasks_org_persona_idx on tasks(org_id, persona);
create index if not exists api_sessions_org_agent_idx on api_sessions(org_id, agent_id);
