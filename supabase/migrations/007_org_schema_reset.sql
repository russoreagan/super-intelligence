-- 007_org_schema_reset.sql
-- Clean-slate reset of the per-tenant brain tables while there is no data worth
-- preserving. Three decisions baked in now because they are nearly free pre-users
-- and painful after:
--
--   1. The tenant key column is named org_id and references organizations(id) —
--      006 kept it named user_id ("holds the tenant/org id") for a zero-migration
--      cutover; that constraint no longer exists, so the name now tells the truth.
--   2. end_user_id (text, '' = companion mode) is a first-class dimension on every
--      per-relationship table — the engine-mode third key (partner → persona →
--      end user). Retrofitting it onto populated tables would mean backfills.
--   3. RLS is the PRIMARY enforcement, not a convention: tenant brain processes
--      authenticate with gateway-minted JWTs whose sub IS the org_id, so the
--      policy `auth.uid() = org_id` scopes every query in the database itself.
--      The service role (gateway/provisioner/admin scripts only) bypasses RLS.
--      The existing RPCs are SECURITY INVOKER, so they inherit this for free.
--
-- Also adds the mandates table: a partner's assignment catalog as DATA (id'd,
-- versioned, with conduct rules + reward weights) instead of prompt-layer config.
-- Episodes/tasks stamp the active mandate_id for auditability.
--
-- DESTRUCTIVE: drops the 001 brain tables. Vault tables (003-005) and
-- organizations/memberships (006) are untouched (their keys belong to users).

-- ── Drop the 001 generation ───────────────────────────────────────────────────
drop function if exists match_episodes(vector, uuid, text, int, text[]);
drop function if exists match_episodes_by_tag(vector, uuid, text, text, int);
drop table if exists brain_schemas;
drop table if exists episodes;
drop table if exists wiring_edges;
drop table if exists wiring_snapshots;
drop table if exists tasks;
drop table if exists dmn_state;
drop table if exists speaker_profiles;
-- user_profiles stays user-keyed (a user preference, not tenant data).

-- ── Mandates: the partner's assignment catalog, as data ──────────────────────
-- id is a partner-chosen slug ("billing_support", "companion") — the same value
-- the engine API's mandate_id selector names per session. Conduct rules and
-- reward weights live here so the same persona can be a different professional
-- under different mandates while keeping its temperament (resting chemistry).
create table if not exists mandates (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  id text not null,
  role_text text not null default '',
  conduct_rules jsonb not null default '{}'::jsonb,
  reward_weights jsonb not null default '{}'::jsonb,
  version int not null default 1,
  active boolean not null default true,
  updated_at timestamptz not null default now(),
  primary key (org_id, persona, id)
);
alter table mandates enable row level security;
create policy "org can manage own mandates" on mandates for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on mandates to authenticated;

-- ── Schema files ──────────────────────────────────────────────────────────────
create table if not exists brain_schemas (
  id bigserial primary key,
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  end_user_id text not null default '',
  filename text not null,
  content text not null default '',
  updated_at timestamptz not null default now(),
  unique(org_id, persona, end_user_id, filename)
);
alter table brain_schemas enable row level security;
create policy "org can manage own schemas" on brain_schemas for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index on brain_schemas(org_id, persona, end_user_id);
grant select, insert, update, delete on brain_schemas to authenticated;
grant usage on sequence brain_schemas_id_seq to authenticated;

-- ── Episodic memory ───────────────────────────────────────────────────────────
create table if not exists episodes (
  id bigserial primary key,
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  end_user_id text not null default '',
  mandate_id text,
  session_id text,
  turn_id text,
  ts float,
  user_input text,
  entity_response text,
  topic_tags text[] default '{}',
  emotion_state text,
  user_emotion text,
  entities text[] default '{}',
  neuromod_snapshot jsonb default '{}'::jsonb,
  surprise_score float default 0.0,
  cog_signature jsonb default '{}'::jsonb,
  vector vector(768)
);
alter table episodes enable row level security;
create policy "org can manage own episodes" on episodes for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index on episodes(org_id, persona, end_user_id, ts desc);
create index on episodes using ivfflat (vector vector_cosine_ops) with (lists = 100);
grant select, insert, update, delete on episodes to authenticated;
grant usage on sequence episodes_id_seq to authenticated;

-- ── Wiring edges ──────────────────────────────────────────────────────────────
create table if not exists wiring_edges (
  id bigserial primary key,
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  source text not null,
  target text not null,
  weight float not null default 1.0,
  polarity text not null default 'excitatory',
  updated_at timestamptz not null default now(),
  unique(org_id, persona, source, target)
);
alter table wiring_edges enable row level security;
create policy "org can manage own wiring" on wiring_edges for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index on wiring_edges(org_id, persona);
grant select, insert, update, delete on wiring_edges to authenticated;
grant usage on sequence wiring_edges_id_seq to authenticated;

-- ── Wiring snapshots ──────────────────────────────────────────────────────────
create table if not exists wiring_snapshots (
  id bigserial primary key,
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  session_id text,
  ts float,
  edges jsonb not null default '[]'::jsonb
);
alter table wiring_snapshots enable row level security;
create policy "org can manage own snapshots" on wiring_snapshots for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index on wiring_snapshots(org_id, persona, ts desc);
grant select, insert, update, delete on wiring_snapshots to authenticated;
grant usage on sequence wiring_snapshots_id_seq to authenticated;

-- ── Tasks ─────────────────────────────────────────────────────────────────────
create table if not exists tasks (
  id text not null,
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  end_user_id text not null default '',
  mandate_id text,
  goal text,
  status text default 'open',
  source text default 'user',
  priority int default 1,
  created_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  success bool,
  job_data jsonb default '{}'::jsonb,
  primary key(id, org_id)
);
alter table tasks enable row level security;
create policy "org can manage own tasks" on tasks for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on tasks to authenticated;

-- ── DMN state ─────────────────────────────────────────────────────────────────
create table if not exists dmn_state (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  end_user_id text not null default '',
  routing_weights jsonb not null default '{}'::jsonb,
  novelty_cache jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  primary key(org_id, persona, end_user_id)
);
alter table dmn_state enable row level security;
create policy "org can manage own dmn state" on dmn_state for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on dmn_state to authenticated;

-- ── Speaker profiles ──────────────────────────────────────────────────────────
create table if not exists speaker_profiles (
  id uuid primary key default gen_random_uuid(),
  org_id uuid references organizations(id) on delete cascade not null,
  end_user_id text not null default '',
  name text,
  embedding vector(192),
  prosody_baseline jsonb default '{}'::jsonb,
  sample_count int not null default 0,
  enrolled_ts float,
  updated_ts float
);
alter table speaker_profiles enable row level security;
create policy "org can manage own speakers" on speaker_profiles for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index on speaker_profiles(org_id);
grant select, insert, update, delete on speaker_profiles to authenticated;

-- ── Vector search functions ───────────────────────────────────────────────────
-- SECURITY INVOKER (the default): RLS on episodes is the enforcement. The org
-- param is a filter for the service-role path (gateway/scripts, RLS bypassed);
-- for a tenant JWT, RLS already pins rows to auth.uid() = org_id, so a wrong
-- org_id_param simply returns nothing.
create or replace function match_episodes(
  query_vector vector(768),
  org_id_param uuid,
  persona_param text,
  match_count int,
  exclude_tags text[] default null,
  end_user_param text default null
)
returns table (
  id bigint, session_id text, turn_id text, ts float,
  user_input text, entity_response text,
  topic_tags text[], emotion_state text, user_emotion text,
  entities text[], neuromod_snapshot jsonb, surprise_score float,
  cog_signature jsonb, mandate_id text, end_user_id text,
  similarity float
)
language sql stable
as $$
  select
    id, session_id, turn_id, ts,
    user_input, entity_response,
    topic_tags, emotion_state, user_emotion,
    entities, neuromod_snapshot, surprise_score,
    cog_signature, mandate_id, end_user_id,
    1 - (vector <=> query_vector) as similarity
  from episodes
  where
    org_id = org_id_param
    and persona = persona_param
    and (end_user_param is null or end_user_id = end_user_param)
    and (exclude_tags is null or not (topic_tags && exclude_tags))
  order by vector <=> query_vector
  limit match_count;
$$;

create or replace function match_episodes_by_tag(
  query_vector vector(768),
  org_id_param uuid,
  persona_param text,
  tag_param text,
  match_count int,
  end_user_param text default null
)
returns table (
  id bigint, session_id text, turn_id text, ts float,
  user_input text, entity_response text,
  topic_tags text[], emotion_state text, user_emotion text,
  entities text[], neuromod_snapshot jsonb, surprise_score float,
  cog_signature jsonb, mandate_id text, end_user_id text,
  similarity float
)
language sql stable
as $$
  select
    id, session_id, turn_id, ts,
    user_input, entity_response,
    topic_tags, emotion_state, user_emotion,
    entities, neuromod_snapshot, surprise_score,
    cog_signature, mandate_id, end_user_id,
    1 - (vector <=> query_vector) as similarity
  from episodes
  where
    org_id = org_id_param
    and persona = persona_param
    and (end_user_param is null or end_user_id = end_user_param)
    and tag_param = any(topic_tags)
  order by vector <=> query_vector
  limit match_count;
$$;

-- ── Re-seed personal orgs (idempotent; mirrors 006) ──────────────────────────
insert into organizations (id, name, plan)
  select u.id, coalesce(u.email, 'owner') || ' (personal)', 'platform'
  from auth.users u
  on conflict (id) do nothing;

insert into memberships (user_id, org_id, role)
  select u.id, u.id, 'admin'
  from auth.users u
  on conflict (user_id, org_id) do nothing;
