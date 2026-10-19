-- 050_lesson_corroboration_people_or_days.sql
-- Rename the second lesson_corroboration value (048) from 'occasions' to
-- 'people_or_days', which says what it does: a lesson becomes established once 3
-- different people taught it OR it came up on 3 different days — a superset of
-- 'people', not an alternative to it.
--
-- The check constraint keeps accepting 'occasions' so a process still running the
-- previous build can never fail a write during the deploy; the code reads
-- 'occasions' as 'people_or_days' and writes only the new name. Any row already on
-- the old name is moved to the new one.
--
-- Idempotent. Apply with `supabase db push` (numbered files) — never via the MCP
-- apply_migration.

alter table public.organizations
  drop constraint if exists organizations_lesson_corroboration_check;
alter table public.organizations
  add constraint organizations_lesson_corroboration_check
  check (lesson_corroboration in ('people', 'people_or_days', 'occasions'));

update public.organizations
  set lesson_corroboration = 'people_or_days'
  where lesson_corroboration = 'occasions';
