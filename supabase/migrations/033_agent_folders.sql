-- 033_agent_folders.sql
--
-- Folder tree + pin for the Agents/Personas organisation redesign (2026-08-09).
--
-- HISTORY NOTE: this migration was applied to prod on 2026-08-09 through the
-- Supabase MCP `apply_migration` and recorded as version 033, but the repo never
-- carried the file — so `supabase db push` refused to run ("Remote migration
-- versions not found in local migrations directory") for every migration after
-- it. This file is the repo mirror of what prod recorded, reproduced verbatim
-- from supabase_migrations.schema_migrations.statements on 2026-09-12 (the same
-- reconciliation 025_website_early_access_leads.sql did). Idempotent, so it is a
-- no-op on prod (already applied) and creates the columns on a fresh database.
--
-- Go-forward rule (docs/SYSTEMS.md, memory finding_migration_history_divergence):
-- ONE mechanism — numbered files + `supabase db push`. Never the MCP apply_migration.

alter table public.agents
  add column if not exists folder text,
  add column if not exists pinned boolean not null default false;

create index if not exists agents_org_folder_idx on public.agents (org_id, folder);
