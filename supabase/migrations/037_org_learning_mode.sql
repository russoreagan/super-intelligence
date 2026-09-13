-- 037_org_learning_mode.sql
--
-- The org-level learning mode: is a persona ONE learning identity shared across
-- every customer it talks to (consolidated — today's design, the default), or is
-- each persona a separate individual whose learning never reaches another
-- (isolated — the marketplace shape, one persona per purchase)?
--
-- Lives on the organizations row and NOT in settings.json on purpose: each brain
-- instance's settings.json is seeded once from the bundled defaults and never
-- overwritten (brain/provisioner.py), so a dedicated persona instance and the
-- org's shared instance would hold different copies of an org-wide governance
-- value. A column here is read identically by every process (brain/org_settings.py,
-- 60 s TTL cache).
--
--   learning_mode   'consolidated' | 'isolated'
--   instance_seed   what a NEW persona clone starts with in an isolated org:
--                   'default' = spec only + fresh self.md + baseline wiring;
--                   'current' = the template's learned competence (wiring, chunks,
--                   stances, sequence weights, ignition tally, and its de-identified
--                   History summary / Stable preferences). Chosen at the switch to
--                   isolated; changeable later; a clone body may override per clone.
--
-- persona_owners: in an isolated org the FIRST end_user_id to open a session on a
-- persona owns it; any other end user gets 404 for that persona. First-writer-wins,
-- same shape as end_users (029): insert ... on conflict do nothing, then read back.
-- The home persona is exempt (it is the org's own agent), and so are owner keys.
--
-- Pre-migration safety: brain/org_settings.py reads with select("*") and treats a
-- missing column as the default; persona_owners lookups tolerate a missing table
-- (binding is then not enforced, and the switch response says so). The write
-- paths (PUT /v1/org/permissions learning_mode) fail loudly with "apply migration
-- 037" until this file is applied.
--
-- Apply with `supabase db push` (numbered files), after 035 and 036 — never via
-- the MCP apply_migration.

alter table organizations
  add column if not exists learning_mode text not null default 'consolidated';
alter table organizations
  drop constraint if exists organizations_learning_mode_check;
alter table organizations
  add constraint organizations_learning_mode_check
  check (learning_mode in ('consolidated', 'isolated'));

alter table organizations
  add column if not exists instance_seed text not null default 'default';
alter table organizations
  drop constraint if exists organizations_instance_seed_check;
alter table organizations
  add constraint organizations_instance_seed_check
  check (instance_seed in ('current', 'default'));

create table if not exists persona_owners (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  end_user_id text not null,
  created_at timestamptz not null default now(),
  primary key (org_id, persona)
);
alter table persona_owners enable row level security;
create policy "org can manage own persona_owners" on persona_owners for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
grant select, insert, update, delete on persona_owners to authenticated;
