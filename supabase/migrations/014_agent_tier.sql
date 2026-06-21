-- Agent runtime tier: how the persona behind this agent is run.
--   'lite'  → cloud-tier / ephemeral: no persistent local pod, consolidates only
--             on demand (the orchestrator's end-of-debate commit). Cheap, burstable.
--   'full'  → a continuously-awake local-thinking brain (its own pod): DMN idle
--             rumination + ongoing consolidation between sessions. Richer, costlier.
-- A persona is one process, so its EFFECTIVE tier is the max over its enabled
-- agents ("full dominates"); see brain/agents.py::effective_tier. Defaults to 'lite'.
alter table agents add column if not exists tier text not null default 'lite'
  check (tier in ('lite', 'full'));
