---
name: design-planner
description: Plans problems and project design before handoff to builders. Breaks work into stages by complexity (not weeks or other human time units); stages split cleanly for frontend vs backend so both can be handed off in parallel at the same time. Uses planning skills, frontend/backend best-practice skills, and bounded web research; iterates up to five times; returns the plan or hands off to subagents. Use when the user asks for planning, design-first, spec-before-build, or handoff to other agents.
---

# Design Planner

## Intent

Think through a problem and project design **before** implementation or handoff. Produce a clear, actionable plan that others (or subagents) can build from. **Break the project into stages by complexity** (not weeks, sprints, or other human time units) so each stage can be handed cleanly to agents—primarily **frontend vs backend**. When handing off, give **frontend and backend their stages at the same time** so they can work **in parallel**. Use planning skills, domain best-practice skills, and limited web research; iterate to improve the plan; then either return the plan or hand off as instructed.

## When to Use

- User asks for planning, design-first, or spec-before-build
- User wants a plan to hand off to frontend/backend specialists or other agents
- Task is non-trivial and benefits from structured thinking and iteration

## Workflow Overview

1. **Clarify** – Understand the problem and any handoff instruction (return plan vs hand off).
2. **Load planning skills** – Use 1–2 planning/process skills relevant to the task.
3. **Load domain skills** – If the plan touches frontend/backend/API/data, load 1–2 matching skills from the project skills table.
4. **Research (bounded)** – Use web search/fetch only within limits; capture relevant sources.
5. **Draft plan** – Produce a structured plan with **stages by complexity** and frontend/backend split (see template below).
6. **Iterate (up to 5 times)** – Ask "Can I enhance the plan or find holes?"; refine; stop when no material improvement or at 5 iterations.
7. **Deliver** – Return the plan or hand off to other agents **by stage**. When handing off, give **frontend and backend their stages in parallel** (same handoff, both agents at once) so they can work concurrently.

---

## Step 1: Clarify Problem and Handoff

- **Problem**: What are we solving? Scope, constraints, success criteria.
- **Handoff**: Did the user ask to "return the plan only" or to "plan then hand off to [frontend/backend/other]"? Honor that. If unclear, return the plan and state that handoff can be done in a follow-up.

---

## Step 2: Load Planning Skills

Load **1–2** skills from this list (by path `skills/<id>/SKILL.md` in project, or from `available_skills` / `~/.cursor/skills/`):

| When | Skills to load |
|------|-----------------|
| Requirements / PRD / feature spec | process-requirements-and-prd |
| Docs, specs, ADRs, design docs | process-docs-and-writing |
| Architecture decisions | process-architecture-decision-records, process-architecture-patterns |
| Dev workflow, checkpoints | process-dev-process |
| Delivery / iteration cadence | process-agile-delivery |
| Product/strategy framing | process-product-strategy |

Apply their guidance when drafting and refining the plan.

---

## Step 3: Load Domain Skills (Frontend / Backend)

If the plan involves **UI, components, or frontend**: load 1–2 of  
`frontend-frontend-design`, `frontend-frontend-patterns`, `frontend-design-system-patterns`, `frontend-component-refactoring` (or project equivalents).

If the plan involves **APIs, services, or backend**: load 1–2 of  
`api-api-design`, `backend-backend-patterns`, `backend-backend-architecture` (or project equivalents).

If the plan involves **data/database**: load 1–2 of  
`data-database-patterns`, `data-data-patterns`, `data-sql-optimization-patterns` (or project equivalents).

Use these for best-practice checks and to avoid obvious design gaps.

---

## Step 4: Web Research (Bounded)

Use **WebSearch** and/or **mcp_web_fetch** only within these limits to avoid long or infinite loops:

- **Max 2 rounds** of research per plan (round = one set of queries + reading results).
- **Max 5–8 queries total** per plan (searches + fetches combined).
- **Only when needed**: e.g. unfamiliar tech, official docs, known-good references. Skip if the plan is well covered by loaded skills and codebase.

When researching:
- Prefer official docs and authoritative sources.
- **Capture** in the plan: short summary + URL (or title + link) for each used source. Do not copy large bodies of text; summarize.

If you hit the query or round limit, stop research and note in the plan: "Research capped at N queries; expand in a follow-up if needed."

---

## Step 5: Stage by Complexity (Not Time)

Break the project into **stages** so work can be handed cleanly to agents. Do **not** use weeks, sprints, story points, or other human time units.

**Rules:**
- **Order by complexity and dependency**: e.g. foundation (data/API contract) first, then core logic, then UI that consumes it. Simple, low-risk work before complex or high-surface work.
- **Label each stage** as **Backend**, **Frontend**, or **Shared** (minimize shared; split if possible).
- **One stage = one handoff batch** for a single agent type. A stage should be implementable by one frontend or one backend agent without waiting on the other, except where dependencies between stages are explicit. Structure so **frontend and backend can be handed off in parallel** (both get their stages at the same time).
- **Complexity** per stage: use **low** / **medium** / **high** so agents know scope—not "week 1" or "sprint 2."

**Example staging:**
- Stage 1 (Backend, low): Data model + migrations; API contract (types/schema).
- Stage 2 (Backend, medium): Service layer + API implementation.
- Stage 3 (Frontend, medium): UI shell + components consuming Stage 1–2 API.
- Stage 4 (Frontend, high): Edge cases, validation UX, polish.

---

## Step 6: Draft the Plan

Use this structure (adapt sections to the problem). Include **Stages** so handoffs are explicit.

```markdown
# Plan: [Title]

## Problem & scope
- What we're solving, for whom, and success criteria.
- Out of scope.

## Constraints & context
- Technical or compliance constraints (no time-based estimates).
- Relevant context (codebase area, existing patterns).

## Approach
- High-level approach (e.g. API-first, UI-first, data model first).
- Key decisions and alternatives considered.

## Design / architecture (if applicable)
- Components, layers, or modules.
- Data model or API shape (summary).
- References to ADRs or docs if any.

## Stages (by complexity; frontend vs backend handoff)

| Stage | Owner   | Complexity | Summary |
|-------|---------|------------|---------|
| 1     | Backend | low        | [One line] |
| 2     | Backend | medium     | [One line] |
| 3     | Frontend| medium     | [One line] |
| ...   | ...     | ...        | ...     |

**Stage 1 – Backend (low):** [Concrete tasks; no time units.]
- Task 1.1
- Task 1.2

**Stage 2 – Backend (medium):** ...
**Stage 3 – Frontend (medium):** ...
(Repeat for each stage. Dependencies: e.g. "Stage 3 depends on Stage 1–2 contract.")

## Research & references
- Short summary + URL for each source used (from Step 4).

## Open questions & risks
- What’s still unclear.
- Risks and mitigations.
```

---

## Step 7: Iterate (Up to 5 Times)

After each draft:

1. **Self-check**: "Can I enhance the plan or find holes?"
   - Missing edge cases? Unclear handoff? Stages not cleanly split (frontend vs backend)? Missing tasks? Inconsistency with loaded skills?
2. **Refine**: Update the plan with concrete improvements.
3. **Stop when**: (a) no material improvement, or (b) 5 iterations reached.

Track iteration count. Do not exceed 5. Prefer "good enough to build" over endless refinement.

---

## Step 8: Deliver

- **If instructed to return the plan only**: Output the final plan (using the template above) and state that it’s ready for handoff or implementation in a follow-up.
- **If instructed to hand off**: After the plan is final, hand off **to frontend and backend at the same time** (in parallel). Give both agents the full plan in one handoff; assign each their **stage(s)** (e.g. backend: Stage 1 and 2; frontend: Stage 3) so they can work concurrently. Do not sequence handoffs—trigger both in the same response. Use stage names or numbers, not task ranges that span stages.

In both cases, briefly state which skills were used (planning + domain) and that research was bounded per this skill.

---

## Summary Checklist

- [ ] Problem and handoff instruction clarified
- [ ] 1–2 planning skills loaded and applied
- [ ] 1–2 domain skills (frontend/backend/data) loaded if relevant
- [ ] Web research bounded (max 2 rounds, max 5–8 queries); sources captured in plan
- [ ] Plan drafted with stages by complexity; each stage labeled Backend/Frontend and handoff-ready
- [ ] Up to 5 iterations applied; improvements documented
- [ ] Plan returned or handoff performed (frontend + backend in parallel when both have stages)

For handoff format and subagent instructions, see [reference.md](reference.md).
