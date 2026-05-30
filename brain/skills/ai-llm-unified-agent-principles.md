# Unified Agent Founding Principles

## Scope

This document captures a unified approach to building production AI agents, synthesized from building multiple agent systems across different product shapes (e.g., real-time/stateful experiences and request/response analytics workflows).

The goal is to provide **founding principles** you can use to explain and standardize how your company builds AI systems going forward.

---

## TL;DR

- **Prefer one capable agent with modular capabilities** over coordinated specialists unless domains are truly independent.
- **Treat skills as instruction** (how to decide + how to explain) and **tools as execution** (how to compute + how to enforce policy).
- **Use progressive disclosure** to keep context small and relevant; load detailed guidance only when needed.
- **Keep "truth" outside the model**: tools/services compute deterministically; the model narrates from validated summaries.
- **Partition tools by state** when your product has distinct modes (editing vs viewing, setup vs execution).
- **Use multi-agent only when it buys you something real** (distinct identities, parallelism, long-lived side conversations) and keep it bounded with strong contracts.
- **Make time/state explicit** (freshness, pacing, SLAs) and let tools compute it; the agent explains implications and next steps.
- **Treat long-running work as jobs** (job IDs, progress, retries, cancellation).
- **Design tools to fail forward**: errors should include hints, options, and examples so the agent self-corrects quickly.
- **Document for both humans and AI** (AGENTS.md / project rules / checklists). This is a scaling lever, not a nice-to-have.

---

## Key concepts

### Agent (the orchestrator)
An agent is the runtime that:
- understands user intent
- manages state (conversation + application state)
- selects skills (guidance) and calls tools (execution)
- synthesizes tool outputs into user-facing results

### Skill (instruction)
A skill is a reusable, on-demand bundle of **instructional** content:
- workflows, best practices, and constraints
- triggers ("when to use / when not to use")
- examples and common pitfalls

Skills are **not executable**. They teach the agent how to behave.

### Tool (execution)
A tool is a typed, executable interface:
- data access (DB/API/MCP/cache)
- deterministic computation (aggregates, rankings, validations)
- state changes (writes, exports, workflow transitions)

Tools are where you enforce **correctness, policy, and provenance**.

### Handler / service layer (glue)
The glue layer translates "what the skill wants" into "what the tools do":
- handlers orchestrate tool calls in a consistent order
- services contain business logic; tools stay thin wrappers

---

## How the pieces fit together

The shortest way to understand this system is: **the agent decides**, **skills guide**, **tools execute**, and **handlers/services keep execution correct and repeatable**.
