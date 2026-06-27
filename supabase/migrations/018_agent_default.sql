-- 018_agent_default.sql
-- Mark ONE agent per org as the default — the agent the org's brain process boots
-- as (its owner-lane persona). Until now the boot persona was a loose
-- settings.json `persona_name` with no link to the agents table; this is the
-- pointer the provisioner reads at spawn to make the process BE its default agent
-- (e.g. the built-in "The Admin"). Switching the default + restart changes which
-- agent the owner lane runs as; client-app agents keep running in their own lane.
--
-- Idempotent: safe to re-run.

alter table agents
  add column if not exists is_default boolean not null default false;

-- At most one default per org. Partial unique index so non-default rows (the
-- common case) are unconstrained.
create unique index if not exists agents_one_default_per_org
  on agents (org_id)
  where is_default;
