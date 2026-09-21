-- 047_organizations_update_grant_fix.sql
-- 046's column-scoped UPDATE grant was a no-op, and that turned its new UPDATE
-- policy into a privilege escalation. This closes it.
--
-- WHAT 046 ASSUMED. It wrote
--   grant update (learning_mode, instance_seed, max_dedicated_instances,
--                 gpu_daily_usd_budget) on public.organizations to authenticated;
-- believing that naming columns would LIMIT what an org may write to its own row.
--
-- WHAT IS ACTUALLY TRUE. Supabase default-grants ALL table privileges on every
-- table in `public` to `anon` and `authenticated` — RLS is the intended gate, not
-- the grants (the same default that migration 026 had to undo for functions).
-- `organizations` therefore already carried a table-level UPDATE for both roles,
-- and a column grant on top of a table grant widens nothing and narrows nothing.
-- Verified against the live database: information_schema.column_privileges listed
-- UPDATE for `authenticated` on EVERY column, including `plan` and `id`.
--
-- WHY THAT MATTERS NOW. Before 046 there was no UPDATE policy on organizations at
-- all, so RLS denied every update no matter how generous the grant was. 046 added
--   create policy "org can update its own settings" ... using (id = auth.uid())
-- which is correct for the four governance columns it meant to allow — but with a
-- blanket column grant it permits ANY column. And `id = auth.uid()` is satisfied
-- by more than a tenant process: every org in production is a personal org whose
-- id equals its owner's user id, so an ordinary user's own browser JWT matches it.
-- A user could rewrite their own organizations row — `plan` ('free' -> 'platform'),
-- `name`, even `id` — by calling PostgREST directly with the anon key and their
-- access token. That is a control-plane write, and 046 opened it.
--
-- THE FIX. Revoke the blanket UPDATE, then re-grant exactly the four governance
-- columns, which is what 046 intended. `anon` loses UPDATE outright: an anonymous
-- caller has a null auth.uid() so no policy would admit it anyway, but leaving a
-- write privilege lying next to a policy is how the next mistake happens.
--
-- The policies from 046 are deliberately left in place — they are correct. It was
-- only ever the grant that was wrong.
--
-- Idempotent and safe to re-run. Nothing legitimate loses access: org_settings.py
-- writes only these four columns, and a tenant on the service-role key bypasses
-- grants entirely (which is the posture in production today — see
-- tests/security/test_org_scoping.py).
--
-- Apply with `supabase db push` (numbered files) — never via the MCP apply_migration.

revoke update on public.organizations from authenticated;
revoke update on public.organizations from anon;

-- Re-grant only what an org may legitimately change about itself. Everything else
-- on the row (plan, id, name, created_at) stays a control-plane decision made with
-- the service role.
grant update (learning_mode, instance_seed, max_dedicated_instances, gpu_daily_usd_budget)
  on public.organizations to authenticated;

-- `anon` has no business writing this table at all; the other default-granted
-- write verbs are revoked for the same reason the UPDATE was.
revoke insert, delete, truncate on public.organizations from anon;
