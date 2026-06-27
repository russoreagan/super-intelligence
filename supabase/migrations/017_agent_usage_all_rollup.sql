-- 017_agent_usage_all_rollup.sql
-- Platform-superadmin cross-org rollup for the Agents dashboard "All orgs" view.
--
-- The org-scoped agent_usage_totals (016) returns one org's agents — that's what a
-- normal org sees (RLS + per-org brain process + p_org_id filter). A platform
-- super-admin additionally wants a fleet-wide view: every org's agents in one list.
--
-- SECURITY: this function is NOT org-filtered, so it is granted ONLY to
-- service_role — the platform credential the brain process holds. Tenant browsers
-- (anon) and scoped-org-JWT callers (authenticated) cannot invoke it at all, so the
-- DB itself denies any cross-org read to a tenant. The brain's /agents/usage
-- endpoint is the second gate: it only calls this when the caller is is_admin.

create or replace function agent_usage_totals_all(
  p_since timestamptz, p_until timestamptz
) returns table (
  org_id uuid, org_name text, agent_id text, calls bigint, cloud_calls bigint,
  in_tok bigint, out_tok bigint, cloud_usd numeric, pod_s numeric, last_ts timestamptz
) language sql stable security definer set search_path = public as $$
  select u.org_id, coalesce(o.name, '') as org_name, u.agent_id,
         sum(u.calls)::bigint, sum(u.cloud_calls)::bigint,
         sum(u.in_tok)::bigint, sum(u.out_tok)::bigint,
         sum(u.cloud_usd)::numeric, sum(u.pod_s)::numeric, max(u.ts)
  from agent_usage u
  left join organizations o on o.id = u.org_id
  where (p_since is null or u.ts >= p_since)
    and (p_until is null or u.ts <= p_until)
  group by u.org_id, o.name, u.agent_id;
$$;
revoke all on function agent_usage_totals_all(timestamptz, timestamptz) from public, anon, authenticated;
grant execute on function agent_usage_totals_all(timestamptz, timestamptz) to service_role;
