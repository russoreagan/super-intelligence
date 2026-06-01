-- Multi-tenant brain schema
-- Run this in Supabase SQL editor or via supabase CLI: supabase db push

-- Enable required extensions
create extension if not exists "uuid-ossp";
create extension if not exists vector;

-- ── User profiles ─────────────────────────────────────────────────────────────
-- Extends Supabase's built-in auth.users
create table if not exists user_profiles (
  id uuid references auth.users primary key,
  active_persona text not null default 'the_visionary',
  settings jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table user_profiles enable row level security;
create policy "users can manage own profile"
  on user_profiles for all
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- ── Schema files (replaces second_brain/schema/*.md) ─────────────────────────
create table if not exists brain_schemas (
  id bigserial primary key,
  user_id uuid references auth.users not null,
  persona text not null,
  filename text not null,
  content text not null default '',
  updated_at timestamptz not null default now(),
  unique(user_id, persona, filename)
);

alter table brain_schemas enable row level security;
create policy "users can manage own schemas"
  on brain_schemas for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index on brain_schemas(user_id, persona);

-- ── Episodic memory (replaces LanceDB episodes/) ─────────────────────────────
-- vector(768): nomic-embed-text and gemini-embedding-001 both produce 768-dim
create table if not exists episodes (
  id bigserial primary key,
  user_id uuid references auth.users not null,
  persona text not null,
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
  vector vector(768)
);

alter table episodes enable row level security;
create policy "users can manage own episodes"
  on episodes for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index on episodes(user_id, persona, ts desc);
-- ivfflat index for approximate nearest-neighbour search
-- lists=100 works well up to ~1M vectors; bump to 200 at 10M+
create index on episodes using ivfflat (vector vector_cosine_ops) with (lists = 100);

-- ── Wiring edges (replaces wiring.json) ──────────────────────────────────────
create table if not exists wiring_edges (
  id bigserial primary key,
  user_id uuid references auth.users not null,
  persona text not null,
  source text not null,
  target text not null,
  weight float not null default 1.0,
  polarity text not null default 'excitatory',
  updated_at timestamptz not null default now(),
  unique(user_id, persona, source, target)
);

alter table wiring_edges enable row level security;
create policy "users can manage own wiring"
  on wiring_edges for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index on wiring_edges(user_id, persona);

-- ── Wiring snapshots (replaces wiring_history/*.json) ────────────────────────
create table if not exists wiring_snapshots (
  id bigserial primary key,
  user_id uuid references auth.users not null,
  persona text not null,
  session_id text,
  ts float,
  edges jsonb not null default '[]'::jsonb
);

alter table wiring_snapshots enable row level security;
create policy "users can manage own snapshots"
  on wiring_snapshots for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index on wiring_snapshots(user_id, persona, ts desc);

-- ── Tasks (replaces task_queue.json + jobs/*.json) ───────────────────────────
create table if not exists tasks (
  id text not null,
  user_id uuid references auth.users not null,
  persona text not null,
  goal text,
  status text default 'open',
  source text default 'user',
  priority int default 1,
  created_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  success bool,
  job_data jsonb default '{}'::jsonb,
  primary key(id, user_id)
);

alter table tasks enable row level security;
create policy "users can manage own tasks"
  on tasks for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ── DMN state (replaces dmn_novelty.json + dmn_routing_weights.json) ─────────
create table if not exists dmn_state (
  user_id uuid references auth.users not null,
  persona text not null,
  routing_weights jsonb not null default '{}'::jsonb,
  novelty_cache jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  primary key(user_id, persona)
);

alter table dmn_state enable row level security;
create policy "users can manage own dmn state"
  on dmn_state for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ── Speaker profiles (replaces speaker_profiles/*.json) ──────────────────────
-- vector(192): ECAPA-TDNN embeddings
create table if not exists speaker_profiles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users not null,
  name text,
  embedding vector(192),
  prosody_baseline jsonb default '{}'::jsonb,
  sample_count int not null default 0,
  enrolled_ts float,
  updated_ts float
);

alter table speaker_profiles enable row level security;
create policy "users can manage own speakers"
  on speaker_profiles for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index on speaker_profiles(user_id);
