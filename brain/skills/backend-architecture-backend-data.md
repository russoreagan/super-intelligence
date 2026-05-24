---
name: architecture-backend-data
description: Use when making architectural/backend/data changes in this codebase (new endpoints/services/skills/migrations) and you need the correct files, patterns, and integration steps.
summary: Codebase orientation: architecture patterns, backend services, data models, and integration checklists.
triggers: [architecture, backend, data model, how does this work, where is, codebase, service, endpoint]
disable-model-invocation: true

---
# Architecture + Backend + Data Patterns (Definitive)

## Goal
Make correct, integrated changes across architecture, backend services, and data models without breaking the agent workflow.

## Start here (orientation)
### Key docs
- `docs/architecture/UNIFIED_AGENT_FOUNDING_PRINCIPLES.md`
- `docs/architecture/SKILLS_VS_TOOLS_FRAMEWORK.md`
- `docs/architecture/CHARTS_AND_DASHBOARD_EDITING_PLATFORM.md`
- `docs/prd/ai-foundational-architecture-prd.md`

### Key directories
- Backend: `generative_dashboards/` (routers, services, skills)\n+- Frontend: `frontend/`\n+- DB schema/migrations: `schema/`\n+
## Core architecture patterns
### Skills-based agent architecture
- Skills are self-contained and invoked by routing.\n+- Prefer composition (skill calls another) over duplication.\n+- Keep skill “when to use / when not to use” crisp.\n+
### MCP integration for data access
- Treat MCP as the primary data interface.\n+- Add caching where latency/cost matters.\n+
### Frontend ↔ backend contract
- API endpoints in `generative_dashboards/routers/`\n+- Frontend API calls in `frontend/src/api/`\n+- Keep shared types consistent (frontend TS interfaces; backend Pydantic models).\n+
## Backend patterns (FastAPI)
### Endpoint basics
- Use async handlers.\n+- Return typed responses.\n+- Convert internal exceptions into stable HTTP errors.\n+
### Streaming (SSE)
- Stream incremental chunks.\n+- Send an explicit completion signal.\n+- Emit errors as structured payloads.\n+
### Service layer
- Put business logic in `generative_dashboards/services/`.\n+- Keep routers thin.\n+- Use caching at service boundaries (stable cache keys, TTL).\n+
## Data patterns
### Schema and migrations
- DB schema evolves via migrations in `schema/`.\n+- Add indexes for hot filters/sorts.\n+- Store flexible structures as JSONB when appropriate (e.g., layout/messages).\n+
### Models
- Frontend: TS interfaces for Dashboard/Widget/ChartSpec.\n+- Backend: Pydantic models for request/response and validation.\n+
## Integration checklists
### Add a new skill
- Create `generative_dashboards/skills/<skill>/SKILL.md`\n+- Register in skill loader\n+- Add routing rules\n+- Verify the API path executes end-to-end\n+
### Add a new API endpoint
- Add router handler\n+- Add service function\n+- Add frontend client\n+- Verify integration end-to-end\n+
### Add a new table/field
- Migration + indexes\n+- Update backend models\n+- Update frontend types\n+- Backfill/migrate existing data if needed\n+
