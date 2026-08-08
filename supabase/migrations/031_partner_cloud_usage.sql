-- 031_partner_cloud_usage.sql
-- Per-partner daily cloud spend, so one partner cannot exhaust the org's budget for
-- its siblings.
--
-- Until now the daily USD ceiling was a single per-ORG counter kept in a JSON file on
-- the tenant volume (brain/model_router.py). Two problems:
--
--   1. Isolation. Every partner in an org drew on one budget, so one partner's spend
--      tripped the ceiling for everyone. And over-budget behaviour was invisible on a
--      full-tier brain: the router silently rerouted to local models rather than
--      erroring, so a partner paying for cloud-tier answers just got worse ones.
--
--   2. Lost updates. The file is a full-dict overwrite (no atomic increment, no lock),
--      keyed to an org-canonical path with no persona component. Under multi-persona
--      every persona process clobbers the others' totals wholesale.
--
-- This table is the authoritative per-partner counter. The bump RPC does the increment
-- in the database with `usd = usd + excluded.usd`, so concurrent processes (dedicated
-- persona instances, a redeploy mid-day) accumulate correctly instead of racing. The
-- brain reads the RETURNING value back as its in-memory total for enforcement.
--
-- SECURITY: the RPC is security-definer with the `p_org_id` service-key fallback (the
-- brain signs asymmetrically, so auth.uid() is null there), and it carries the
-- `revoke ... from anon, public` that migration 026 established every such function
-- must — Supabase default-grants EXECUTE to anon on create.
--
-- NOTE on scope: the budget meters per partner_id, and a partner may hold several keys
-- (011), so a per-KEY override would be ambiguous (which key's override governs the
-- partner's aggregate?). The per-partner ceiling is therefore a single org setting
-- (`partner_cloud_daily_usd_budget`), not a column here. A genuine per-partner override,
-- if ever needed, belongs in its own (org_id, partner_id) table, not on api_keys.

create table if not exists partner_cloud_usage (
  org_id uuid references organizations(id) on delete cascade not null,
  partner_id text not null,
  usage_date date not null,
  usd numeric not null default 0,
  usd_autonomous numeric not null default 0,
  updated_ts timestamptz not null default now(),
  primary key (org_id, partner_id, usage_date)
);
alter table partner_cloud_usage enable row level security;
create policy "org can manage own partner_cloud_usage" on partner_cloud_usage for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on partner_cloud_usage to authenticated;

-- Atomic increment. Returns the partner's new running total for the day so the caller
-- can use it as the authoritative in-memory figure without a second read.
create or replace function public.bump_partner_cloud_usd(
  p_partner_id  text,
  p_usd         numeric,
  p_autonomous  numeric default 0,
  p_org_id      uuid default null,
  p_date        date default null
)
returns numeric
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := coalesce(auth.uid(), p_org_id);
  v_date date := coalesce(p_date, (now() at time zone 'utc')::date);
  v_total numeric;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  insert into public.partner_cloud_usage (org_id, partner_id, usage_date, usd, usd_autonomous)
    values (v_org, p_partner_id, v_date, p_usd, p_autonomous)
  on conflict (org_id, partner_id, usage_date) do update
    set usd = public.partner_cloud_usage.usd + excluded.usd,
        usd_autonomous = public.partner_cloud_usage.usd_autonomous + excluded.usd_autonomous,
        updated_ts = now()
  returning usd into v_total;
  return v_total;
end;
$$;

-- Read a partner's current daily total (read-through on a cold process). 0 if none.
create or replace function public.get_partner_cloud_usd(
  p_partner_id text,
  p_org_id     uuid default null,
  p_date       date default null
)
returns numeric
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := coalesce(auth.uid(), p_org_id);
  v_date date := coalesce(p_date, (now() at time zone 'utc')::date);
  v_total numeric;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  select usd into v_total
    from public.partner_cloud_usage
    where org_id = v_org and partner_id = p_partner_id and usage_date = v_date;
  return coalesce(v_total, 0);
end;
$$;

revoke all on function public.bump_partner_cloud_usd(text, numeric, numeric, uuid, date) from public;
revoke all on function public.get_partner_cloud_usd(text, uuid, date) from public;
revoke execute on function public.bump_partner_cloud_usd(text, numeric, numeric, uuid, date) from anon, public;
revoke execute on function public.get_partner_cloud_usd(text, uuid, date) from anon, public;
grant execute on function public.bump_partner_cloud_usd(text, numeric, numeric, uuid, date) to authenticated, service_role;
grant execute on function public.get_partner_cloud_usd(text, uuid, date) to authenticated, service_role;
