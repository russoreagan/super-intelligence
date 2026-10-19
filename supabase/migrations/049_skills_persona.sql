-- 049_skills_persona.sql
-- A self-written skill can belong to ONE persona (isolated orgs).
--
-- In an isolated org every persona is a separate individual (guide §20): what one
-- persona learns may never reach another. Tier-2 self-authored skills used to be
-- switched off there, because a skill row is org-wide. With this column a persona's
-- own skill is stamped with its slug, and the brain selects, injects and lists it
-- only while that persona is bound (brain/skill_scope.py). NULL = org-level: every
-- partner-submitted skill, and every skill in a consolidated org, exactly as before.
--
-- Persona hard purge deletes a persona's rows by this column. The setting
-- `isolated_persona_learning: 0` restores the old behavior (no self-written skills
-- in an isolated org).
--
-- Idempotent. Apply with `supabase db push` (numbered files) — never via the MCP
-- apply_migration. The brain tolerates the column being absent (reads fall back to
-- org-level only, and persona-scoped authoring is skipped) until this is applied.

alter table public.skills
  add column if not exists persona text;

create index if not exists skills_org_persona_idx
  on public.skills (org_id, persona)
  where persona is not null;
