-- 033_agent_folders.sql — user-curated organisation for the Agents workspace.
--
-- The roster already groups itself by persona and by role for free (agent_id =
-- "<persona>.<mandate_id>"), so those axes can never go stale. What it cannot
-- derive is how the OPERATOR thinks about their fleet — "Trading desk", "Ops".
-- That is the only new persisted state: a flat folder string and a pin flag.
--
-- `folder` is deliberately NOT a foreign key and NOT a tree. One level is enough
-- at this scale and it makes drag-to-file a single UPDATE. Unfiled = NULL. The
-- folder LIST is derived (select distinct folder), so there is no second table to
-- keep in sync and a folder simply disappears when its last agent leaves it.
--
-- Existing RLS on public.agents covers both columns (org-scoped row policies);
-- no policy changes are required.

alter table public.agents
  add column if not exists folder  text,
  add column if not exists pinned  boolean not null default false;

create index if not exists agents_org_folder_idx on public.agents (org_id, folder);
