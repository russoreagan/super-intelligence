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
