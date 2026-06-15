-- 008_mandate_assignments.sql
-- Make mandates an ORG-LEVEL library and introduce the persona↔mandate pairing
-- as its own object.
--
-- 007 scoped a mandate to (org_id, persona, id): a role only existed in the
-- context of one persona, and the same role text had to be duplicated to reuse
-- it across personas. The product model is the inverse — a partner/org authors a
-- catalog of roles ONCE, then assigns any of them to any persona. So:
--
--   mandates          becomes keyed (org_id, id)            — the library
--   persona_mandates  new table keyed (org_id, persona, id) — the assignment
--
-- The assignment row is a first-class object (the "unique pairing") carrying an
-- `overrides` jsonb reserved for future per-pairing tweaks (reward deltas,
-- conduct nudges) so the pairing can grow without another migration.
--
-- Backfill-safe: existing (org_id, persona, id) rows collapse to one library row
-- per (org_id, id) and seed one assignment per original (org_id, persona, id).
-- In practice the table is empty (DB reset 2026-06-10), so this is a no-op then,
-- but correct if any rows exist.

-- ── Snapshot the persona-scoped rows before we drop the column ────────────────
create temporary table _old_mandates on commit drop as table mandates;

-- ── Collapse the library to one row per (org_id, id) ──────────────────────────
-- Keep the freshest definition: highest version, then latest updated_at.
delete from mandates m
using (
  select org_id, id,
    (array_agg(ctid order by version desc, updated_at desc))[1] as keep
  from mandates
  group by org_id, id
) k
where m.org_id = k.org_id and m.id = k.id and m.ctid <> k.keep;

-- ── Re-key mandates to the org-level library shape (org_id, id) ───────────────
alter table mandates drop constraint mandates_pkey;
alter table mandates drop column persona;
alter table mandates add primary key (org_id, id);

-- ── The pairing object: which library mandates a persona may be given ─────────
-- A turn names a mandate_id (engine API); it applies only if a matching, enabled
-- assignment exists for the active persona. overrides is reserved (unused today).
create table if not exists persona_mandates (
  org_id uuid references organizations(id) on delete cascade not null,
  persona text not null,
  mandate_id text not null,
  enabled boolean not null default true,
  overrides jsonb not null default '{}'::jsonb,
  sort_order int not null default 0,
  updated_at timestamptz not null default now(),
  primary key (org_id, persona, mandate_id),
  foreign key (org_id, mandate_id) references mandates(org_id, id) on delete cascade
);
alter table persona_mandates enable row level security;
create policy "org can manage own persona_mandates" on persona_mandates for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index on persona_mandates(org_id, persona);
grant select, insert, update, delete on persona_mandates to authenticated;

-- ── Backfill assignments from the original persona-scoped rows ────────────────
insert into persona_mandates (org_id, persona, mandate_id, enabled, sort_order)
  select distinct org_id, persona, id, active, 0
  from _old_mandates
  on conflict (org_id, persona, mandate_id) do nothing;
