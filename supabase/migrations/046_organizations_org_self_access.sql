-- 046_organizations_org_self_access.sql
-- The companion to 027, for the same class of bug and from the same cause.
--
-- The tenant brain connects on a gateway-minted org JWT whose `sub` IS the org id
-- (brain/gateway/org_token.py), so inside a tenant process auth.uid() evaluates to
-- the ORG id, not a user id. `organizations` has exactly one policy (006:34-41):
--
--   create policy "members can read their orgs" on organizations
--     for select using (exists (select 1 from memberships m
--                               where m.org_id = organizations.id and m.user_id = auth.uid()));
--
-- For a PERSONAL org that exists-check passes by accident: a membership row with
-- user_id = org_id exists because 006:54-56 seeded user_id and org_id to the same
-- uuid. For an org whose id is a fresh uuid there is no such row, so the tenant
-- reading its own governance row (brain/org_settings.py:92,
-- `.table("organizations").select("*").eq("id", org)`) gets [] and silently falls
-- back to defaults for learning_mode, instance_seed, max_dedicated_instances and
-- gpu_daily_usd_budget.
--
-- Silent is the problem. is_isolated() fails CLOSED on an unknown mode, so an
-- unreadable organizations row makes an org quietly WITHHOLD shared learning
-- rather than raise — a behaviour change with no error anywhere to trace it to.
--
-- Separately, there is no update grant on the table at all, so org_settings.py's
-- three write paths (the learning-mode switch, instance_seed, gpu_daily_usd_budget)
-- fail under an org JWT for EVERY org, personal or not. They appear to work today
-- only because the project signs JWTs asymmetrically, mint_org_token() returns ""
-- and tenants run on the service-role key, which bypasses RLS entirely
-- (see tests/security/test_org_scoping.py and brain/provisioner.py).
--
-- So this migration is INERT in production as it stands today. It is shipped
-- because it is the layer that has to be correct the moment HS256 minting works
-- again, and because 027 set exactly this precedent rather than leaving the hole.
--
-- It grants nobody anything across orgs: both policies are `id = auth.uid()`, and
-- only an org's own token can satisfy that.
--
-- Apply with `supabase db push` (numbered files) — never via the MCP apply_migration.

-- Note the column is `id`, not `org_id`: organizations IS the org, so its primary
-- key is the value every other table carries as org_id.
--
-- Wrapped in existence guards because Postgres has no `create policy if not
-- exists`, and a half-applied migration should be re-runnable (the reasoning
-- migration 028 used for its constraint).
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'organizations'
      and policyname = 'org can read its own row'
  ) then
    create policy "org can read its own row"
      on public.organizations
      for select
      using (id = auth.uid());
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'organizations'
      and policyname = 'org can update its own settings'
  ) then
    create policy "org can update its own settings"
      on public.organizations
      for update
      using (id = auth.uid())
      with check (id = auth.uid());
  end if;
end $$;

-- COLUMN-SCOPED on purpose. A blanket `grant update on organizations` would let
-- any tenant rewrite its own `plan` ('free' -> 'platform') or its `id`. Only the
-- four governance columns 037 and 038 introduced are writable by the org itself;
-- everything else on the row stays a control-plane decision.
grant update (learning_mode, instance_seed, max_dedicated_instances, gpu_daily_usd_budget)
  on public.organizations to authenticated;

-- No insert policy: org creation stays service-role (the provisioning path), as
-- 006's header states. The existing `grant select` from 006:43 is required and
-- deliberately left in place — org_settings.py's `update ... returning` needs both
-- the update and the select privilege to read its own result back.
