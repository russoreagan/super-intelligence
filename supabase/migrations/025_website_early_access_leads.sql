-- 025_website_early_access_leads.sql
-- Captures a table that reached prod out-of-band: it was applied on 2026-07-02 via
-- an ad-hoc Supabase MCP apply_migration (recorded only as the timestamp version
-- 20260702040552, name "create_website_early_access_leads") and never committed as
-- a numbered migration. Reproduced here verbatim from the live schema so the repo
-- is the single source of truth again.
--
-- Idempotent: the table already exists in prod, so `supabase db push` applying this
-- is a no-op there. It only materializes the table on fresh/branch databases.
--
-- Purpose: early-access email capture for the marketing site. RLS is enabled with
-- NO policies (deny-all) — rows are written by the service role / an edge function,
-- never by anon or authenticated end-users.

create table if not exists public.website_early_access_leads (
  id         uuid        not null default gen_random_uuid(),
  email      text        not null,
  source     text,
  user_agent text,
  created_at timestamptz not null default now(),
  primary key (id)
);

create unique index if not exists website_early_access_leads_email_idx
  on public.website_early_access_leads (lower(email));

alter table public.website_early_access_leads enable row level security;
