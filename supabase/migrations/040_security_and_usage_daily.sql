-- 040_security_and_usage_daily.sql
-- Audit 2026-09-13. Three independent, idempotent pieces (safe to re-run):
--
--   A. Revoke `anon` EXECUTE on six SECURITY DEFINER functions the Supabase
--      security advisor flags as anon-callable. Supabase's default privileges
--      grant EXECUTE to anon on every new public function, and `revoke ... from
--      public` does NOT remove that direct grant (see 004 and 026 for the same
--      lesson). `authenticated` is KEPT where the app calls the function as a
--      user: the console/brain call get_mcp_connectors / delete_mcp_connector
--      (brain/clusters/cma_executor.py) and register_mcp_connector under the org
--      JWT, and agent_usage_totals backs the agents dashboard. The two trigger
--      functions (rls_auto_enable, drop_personal_org_on_user_delete) are never
--      called by a client — Postgres checks EXECUTE on a trigger function only
--      when the trigger is CREATED, not when it fires — so they also lose
--      `authenticated`.
--
--      rls_auto_enable() does not exist in this repo's migrations (it reached prod
--      out-of-band), so every statement is guarded with to_regprocedure(): a
--      missing function is skipped rather than aborting the migration, which
--      also keeps the file valid on a fresh/branch database.
--
--   B. website_early_access_leads (025): RLS is enabled with NO policy, which the
--      advisor reports as "RLS enabled, no policies". That is the design — rows
--      are written by the service role only (marketing-site edge function), never
--      by anon or authenticated — so no policy is added. The table privileges
--      are revoked from anon/authenticated as belt-and-braces: with deny-all RLS
--      they were already unusable, now they are visibly absent.
--
--   C. GET /v1/usage advertises a 92-day window, but agent_usage_by_day (038)
--      reads the RAW agent_usage ledger, which brain/agent_usage_store.prune_raw
--      trims to agent_usage_raw_retention_days (7). Anything older than a week
--      silently vanished from the bill. agent_usage_by_day_daily reads the
--      per-day rollup (agent_usage_daily, 039) with the SAME column shape, and
--      the store prefers it, falling back to the raw RPC where this migration
--      has not been applied. Same grants as 038: authenticated + service_role,
--      anon/public revoked. `set search_path = public`, org-scoped through
--      coalesce(auth.uid(), p_org_id) exactly like 038 (the org JWT wins; a
--      service-role caller passes its own org id).

-- ── A. anon (and, for the trigger functions, authenticated) revokes ──────────
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.agent_usage_totals(uuid, timestamptz, timestamptz)',
    'public.delete_mcp_connector(text)',
    'public.get_mcp_connectors()',
    'public.register_mcp_connector(text, text, text, text)'
  ] loop
    if to_regprocedure(fn) is not null then
      execute format('revoke execute on function %s from anon, public', fn);
    else
      raise notice '040: % not present — skipped', fn;
    end if;
  end loop;

  foreach fn in array array[
    'public.drop_personal_org_on_user_delete()',
    'public.rls_auto_enable()'
  ] loop
    if to_regprocedure(fn) is not null then
      execute format('revoke execute on function %s from anon, authenticated, public', fn);
    else
      raise notice '040: % not present — skipped', fn;
    end if;
  end loop;
end $$;

-- ── B. early-access leads: deny-all by design, make it explicit ─────────────
do $$
begin
  if to_regclass('public.website_early_access_leads') is not null then
    execute 'revoke all on table public.website_early_access_leads from anon, authenticated';
    execute 'comment on table public.website_early_access_leads is '
      || quote_literal(
           'Marketing-site early-access capture. RLS enabled with NO policy on purpose: '
           'service role writes only (025). Do not add anon/authenticated policies.'
         );
  end if;
end $$;

-- ── C. per-day usage over the daily rollup (same shape as agent_usage_by_day) ─
create or replace function public.agent_usage_by_day_daily(
  p_org_id uuid,
  p_since  date default null,
  p_until  date default null
) returns table (
  day date, persona text, agent_id text,
  calls bigint, cloud_calls bigint, in_tok bigint, out_tok bigint,
  cloud_usd numeric, pod_s numeric
)
language sql
stable
security definer
set search_path = public
as $$
  select d.usage_date as day,
         d.persona, d.agent_id,
         sum(d.calls)::bigint, sum(d.cloud_calls)::bigint,
         sum(d.in_tok)::bigint, sum(d.out_tok)::bigint,
         sum(d.cloud_usd)::numeric, sum(d.pod_s)::numeric
  from public.agent_usage_daily d
  where d.org_id = coalesce(auth.uid(), p_org_id)
    and (p_since is null or d.usage_date >= p_since)
    and (p_until is null or d.usage_date < p_until)
  group by 1, 2, 3;
$$;
revoke all on function public.agent_usage_by_day_daily(uuid, date, date) from public;
revoke execute on function public.agent_usage_by_day_daily(uuid, date, date) from anon, public;
grant execute on function public.agent_usage_by_day_daily(uuid, date, date) to authenticated, service_role;
