---
name: dev-process
description: Use when executing development work end-to-end (plan → implement → checkpoints → verify → PR/merge), especially when you need strict verification and consistent workflow hygiene.
summary: End-to-end dev workflow: plan → implement → verify → PR with strict verification gates.
triggers: [workflow, process, plan, verify, checkpoint, worktree, git hygiene]
disable-model-invocation: true

---
# Dev Process (Definitive)

## Goal
Provide one consistent workflow for delivering changes safely:
- plan-driven execution
- checkpoints and review loops
- verification before claiming completion
- clean git hygiene (branches/worktrees)

## Workflow
### 1) Start: choose workspace strategy
- If you need isolation or parallel work, create a **git worktree**.
- Ensure worktree directories are ignored.
- Verify a clean baseline (tests) before starting.

### 2) Execute via a plan (preferred)
- Read the plan and flag gaps.
- Execute in **small batches** (default: 3 tasks) with verification after each batch.
- Stop when blocked; don’t guess.

### 3) Review checkpoints
- Request review at meaningful boundaries (batch complete, risky changes, before merge).
- Apply feedback with technical rigor.

### 4) Verification gate (non-negotiable)
Before saying “done”, “fixed”, “passing”, or creating PR/commit:
1. Identify the command that proves the claim
2. Run it
3. Read output + exit code
4. Only then make the claim (with evidence)

### 5) Finish branch
When implementation is complete and verified:
- verify tests
- choose merge/PR/keep/discard
- cleanup worktree if applicable

## Defaults
- Batch size: 3 tasks
- Target: tests passing + no unverified claims
