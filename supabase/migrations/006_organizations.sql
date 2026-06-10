-- 006_organizations.sql
-- Org-based tenancy: an organization is the tenant unit (it owns a brain process
-- + all per-tenant data). Users are MEMBERS of an org with a role. The platform
-- super-user stays the existing app_metadata.is_admin (no new table needed).
--
-- Per-tenant data tables (brain_schemas, episodes, wiring_*, …) are accessed by
-- the brain via the SERVICE ROLE (bypasses RLS) scoped by the tenant key, so they
-- need NO change here: the brain simply scopes by org_id instead of user_id, and
-- for the single existing dev tenant org_id == user_id (see seed below), so its
-- rows match unchanged. The column stays named user_id for now (it holds the
-- tenant/org id); a clean rename can come later.

create table if not exists organizations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  plan text not null default 'free',
  created_at timestamptz not null default now()
);

create table if not exists memberships (
  user_id uuid references auth.users not null,
  org_id uuid references organizations on delete cascade not null,
  role text not null default 'member' check (role in ('admin', 'member')),
  created_at timestamptz not null default now(),
  primary key (user_id, org_id)
);
create index if not exists memberships_org_idx on memberships(org_id);

alter table organizations enable row level security;
alter table memberships enable row level security;

-- A user may read orgs they belong to, and their own memberships. All writes go
-- through the service role (provisioning / admin endpoints); no user-side writes.
create policy "members can read their orgs" on organizations
  for select using (
    exists (select 1 from memberships m where m.org_id = organizations.id and m.user_id = auth.uid())
  );
create policy "users can read their memberships" on memberships
  for select using (auth.uid() = user_id);

grant select on organizations, memberships to authenticated;

-- ── Seed ──────────────────────────────────────────────────────────────────────
-- Give every EXISTING auth user a personal org whose id equals their user id, and
-- an admin membership. With one dev user this is exactly the "keep my memory,
-- zero migration" case: their data (keyed by user_id) is now keyed by an org_id of
-- the same value. New B2B orgs get fresh UUIDs and multiple members later. Safe to
-- re-run (idempotent); does nothing for users already seeded.
insert into organizations (id, name, plan)
  select u.id, coalesce(u.email, 'owner') || ' (personal)', 'platform'
  from auth.users u
  on conflict (id) do nothing;

insert into memberships (user_id, org_id, role)
  select u.id, u.id, 'admin'
  from auth.users u
  on conflict (user_id, org_id) do nothing;
