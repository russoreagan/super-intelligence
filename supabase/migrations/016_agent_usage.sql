-- 016_agent_usage.sql
-- Durable per-agent model-usage ledger, so the Agents dashboard can show
-- CUMULATIVE token + cost totals over a date/time range — summed across every
-- boot/shutdown cycle, not just the current in-memory session.
--
-- The live ModelRouter meters each agent's model calls in memory (which agent
-- drove the model, how many tokens, how much pod compute-time + cloud $). That
-- meter resets when the brain process restarts, so it can't answer "what did this
-- agent cost over the last 7 days across all the times it ran." The router flushes
-- its accumulated DELTA since the last flush into this table on a timer (and at
-- sleep/consolidate); summing rows in a [since, until] window gives the cumulative
-- total. Each row is an additive slice — never a running total — so summation is
-- correct regardless of restarts.
--
-- pod_s = seconds of local GPU-pod compute (sum of local-call latency) attributed
-- to the agent; the dashboard multiplies it by the pod's $/hr to estimate the
-- agent's share of the shared-pod cost. cloud_usd is the agent's real metered
-- cloud spend. One process serves one org; RLS scopes every row to the org, same
-- as agent_turns (015). Companion/local mode (no Supabase) keeps only the
-- in-memory meter and never touches this table.

create table if not exists agent_usage (
  org_id uuid references organizations(id) on delete cascade not null,
  id bigint generated always as identity,
  persona text not null default '',
  agent_id text not null default '',
  calls int not null default 0,
  cloud_calls int not null default 0,
  in_tok bigint not null default 0,
  out_tok bigint not null default 0,
  cloud_usd numeric not null default 0,
  pod_s numeric not null default 0,
  ts timestamptz not null default now(),
  primary key (org_id, id)
);
alter table agent_usage enable row level security;
create policy "org can manage own agent_usage" on agent_usage for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
-- Range sums for the dashboard, overall and per agent.
create index if not exists agent_usage_org_ts_idx on agent_usage(org_id, ts desc);
create index if not exists agent_usage_org_agent_ts_idx on agent_usage(org_id, agent_id, ts desc);
grant select, insert, update, delete on agent_usage to authenticated;

-- Server-side rollup so a date-range query returns one summed row per agent
-- instead of thousands of delta rows. org_id is passed explicitly (the brain uses
-- the service role, so auth.uid() is null); callers must scope to their own org.
create or replace function agent_usage_totals(
  p_org_id uuid, p_since timestamptz, p_until timestamptz
) returns table (
  agent_id text, calls bigint, cloud_calls bigint,
  in_tok bigint, out_tok bigint, cloud_usd numeric, pod_s numeric, last_ts timestamptz
) language sql stable security definer set search_path = public as $$
  select agent_id,
         sum(calls)::bigint, sum(cloud_calls)::bigint,
         sum(in_tok)::bigint, sum(out_tok)::bigint,
         sum(cloud_usd)::numeric, sum(pod_s)::numeric, max(ts)
  from agent_usage
  where org_id = p_org_id
    and (p_since is null or ts >= p_since)
    and (p_until is null or ts <= p_until)
  group by agent_id;
$$;
grant execute on function agent_usage_totals(uuid, timestamptz, timestamptz) to authenticated, service_role;
