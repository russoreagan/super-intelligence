-- 019_skills.sql
-- App-provided skills: an ORG-LEVEL library of reusable capability docs a partner
-- app registers over the engine API. Mirrors the mandates library (007/008) — a
-- plain org-scoped table the pod reads/writes under its org JWT (auth.uid()=org_id),
-- no vault (a skill carries no secret). What a skill IS: name + description (the
-- text the SkillSelector embeds/matches on) + a markdown body (instructions injected
-- into the turn, fenced, when the skill is selected). conduct/scripts are NOT here —
-- v1 is instructions-only; executable capability stays on the MCP-connector boundary.
--
-- ADMISSION LIFECYCLE (the prompt-injection mitigation). A skill body is partner-
-- supplied untrusted content, so it is screened before it can ever be injected:
--
--   pending   just submitted/edited — being screened (transient)
--   enabled   cleared (LLM auto-approve or superadmin) — eligible for injection
--   flagged   the screener had questions — awaiting superadmin review
--   rejected  the submitted body was refused — never injected
--
-- Only `approved_body` is ever injected, and only while active. `body` holds the
-- latest (possibly still-being-screened) submission; `approved_body` holds the last
-- body a human/LLM cleared. On edit we keep the prior approved_body live until the
-- new one clears — this is the TOCTOU guard (an attacker can't get a benign body
-- approved then swap in a payload: the swap resets status to pending and re-screens,
-- and the old approved version keeps serving meanwhile).
--
-- The security BOUNDARY is not this table — it is the runtime (tool permissions,
-- approval gating, per-org isolation, fenced precedence injection). Screening is
-- defense-in-depth layered on top.

create table if not exists public.skills (
  org_id        uuid        not null references organizations(id) on delete cascade,
  id            text        not null,                        -- partner-chosen slug
  display_name  text,
  description   text        not null default '',             -- routing/embedding text
  body          text        not null default '',             -- latest submitted instructions
  approved_body text,                                         -- last-cleared body (what injects)
  keywords      text[]      not null default '{}',
  allowed_tools text[]      not null default '{}',            -- declared scope (enforced at runtime)
  tier          int         not null default 2,
  status        text        not null default 'pending'
                  check (status in ('pending', 'enabled', 'flagged', 'rejected')),
  screen_notes  jsonb       not null default '{}'::jsonb,     -- static findings + judge verdict
  submitted_by  text,                                         -- partner_id / admin that submitted
  reviewed_by   text,                                         -- superadmin that approved/rejected
  reviewed_at   timestamptz,
  version       int         not null default 1,
  active        boolean     not null default true,            -- soft-delete (delete = active=false)
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (org_id, id)
);

alter table public.skills enable row level security;

-- Pod reads/writes under its org JWT. The cross-org superadmin review queue is the
-- control plane's job (it iterates orgs with the per-org owner credential), so no
-- service-role escape hatch lives here — keeping a per-org pod unable to reach other
-- tenants' skills (the connector-isolation lesson).
create policy "org can manage own skills"
  on public.skills for all
  using (auth.uid() = org_id)
  with check (auth.uid() = org_id);

-- Hot paths: load the org's live skills at boot; list the flagged review queue.
create index if not exists skills_live_idx
  on public.skills (org_id) where active and approved_body is not null;
create index if not exists skills_status_idx
  on public.skills (org_id, status);

grant select, insert, update, delete on public.skills to authenticated;

-- Session-level pin: app-provided skill ids the partner forces into every turn of a
-- session (on top of relevance selection). Lives on the session so it survives a pod
-- restart with the rest of the session state.
alter table public.api_sessions
  add column if not exists pinned_skills text[] not null default '{}';
