-- 027_memberships_org_read.sql
-- Companion to restoring RLS (tenants booting on an org JWT, sub = org_id).
--
-- Under the org JWT the tenant reads the DB as auth.uid() = org_id. Most tables
-- are already scoped that way (007 onward: `auth.uid() = org_id`), so they work
-- as-is. `memberships` is the exception: its only policy is user-oriented
-- ("users can read their memberships", auth.uid() = user_id), because the app UI
-- reads it under a USER JWT. But brain/org.py's is_member() / membership_role()
-- (used by brain/ui/auth.py to gate NON-owner access to a tenant) read
-- `memberships` filtered by user_id — historically under the service role. Under
-- the org JWT those reads see nothing (a tenant's auth.uid() is its org_id, not a
-- member's user_id), so a B2B multi-member org would deny its own members.
-- (Personal orgs are unaffected: brain/ui/auth.py short-circuits when sub == org_id
-- with no DB hit.)
--
-- Fix: let a tenant read its OWN org's memberships. Safe — it only exposes rows
-- already owned by that org, and it coexists (permissive OR) with the existing
-- user-scoped policy, so app-UI reads under a user JWT are unchanged.

create policy "org can read its own memberships"
  on public.memberships
  for select
  using (org_id = auth.uid());
