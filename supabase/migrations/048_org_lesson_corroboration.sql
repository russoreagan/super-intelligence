-- 048_org_lesson_corroboration.sql
-- How a cross-learning lesson earns "established" (and with it, a place in every
-- turn of the persona that learned it). brain/hypothesis_store.py.
--
--   lesson_corroboration  'people' (default) — a lesson promotes only when 3
--                         DISTINCT sources (end users, or the owner lane) derive it.
--                         Right for an org serving many customers: one chatty
--                         customer can never self-promote a lesson that is then
--                         shown to everyone else.
--                         'occasions' — a lesson ALSO promotes once it has been
--                         re-derived on cross_learning_promote_days (3) distinct UTC
--                         days, whoever it came from. For an org whose traffic is its
--                         own (one person, often arriving as a single end user through
--                         their own app), where distinct-people corroboration can
--                         never fire and every lesson stays provisional forever.
--
-- An org-row value for the same reason as learning_mode (037): every process that
-- serves the org must read the same answer. Default 'people' keeps every existing
-- org exactly as it is; an org opts in with PUT /v1/org/permissions
-- {"lesson_corroboration": "occasions"} or the console's Learning mode block.
-- Isolated orgs are unaffected either way: injection is off there.
--
-- Idempotent. Apply with `supabase db push` (numbered files) — never via the MCP
-- apply_migration.

alter table public.organizations
  add column if not exists lesson_corroboration text not null default 'people';

alter table public.organizations
  drop constraint if exists organizations_lesson_corroboration_check;
alter table public.organizations
  add constraint organizations_lesson_corroboration_check
  check (lesson_corroboration in ('people', 'occasions'));

-- The same self-service column grant 047 narrowed to the governance columns.
grant update (lesson_corroboration) on public.organizations to authenticated;
