---
name: refactoring
description: Use when reducing complexity, splitting components/services, extracting hooks/modules, or improving maintainability without changing behavior.
summary: Behavior-preserving improvements: extract hooks, split components, simplify conditionals, separate concerns.
triggers: [refactor, clean up, simplify, extract, split, maintainability, technical debt]
disable-model-invocation: true

---
# Refactoring (Definitive)

## Goal
Make behavior-preserving improvements that reduce complexity and improve readability, testability, and long-term maintainability.

## When to refactor
- Component/service is hard to reason about (many concerns mixed).\n+- High cyclomatic complexity or deeply nested conditionals.\n+- Excessive file size / repeated patterns.\n+- UI and business logic are tangled.\n+- Data fetching and transformation are embedded in view logic.\n+
## Refactoring workflow
1. **Define the invariant**: what must not change (inputs/outputs, UX, API contract).\n+2. **Choose seams**: boundaries where extraction is safe (hooks, modules, subcomponents).\n+3. **Refactor in small steps** with checkpoints.\n+4. **Add/upgrade tests** after structure is stable.\n+5. **Re-run checks** and verify the invariant.\n+
## Core patterns (best-of)
### 1) Extract custom hooks (state + side effects)
Use when state management or effect logic dominates a UI component.\n+- Move related `useState/useEffect` + derived state into a hook.\n+- Return a minimal API.\n+
### 2) Extract subcomponents (UI sections)
Use when a component contains multiple screens/sections/modals.\n+- Split into focused components.\n+- Keep orchestration in the parent.\n+
### 3) Simplify conditional logic
Use when you see deep nesting or long `if/else` chains.\n+- Prefer early returns.\n+- Replace branching with lookup tables.\n+- Convert “mode × locale × state” into maps.\n+
### 4) Extract data/API logic out of views
Use when a component handles fetching, caching, and transformation.\n+- Create a data hook or service layer function.\n+- Keep the component mostly presentational.\n+
### 5) Extract modal/dialog management
Use when many modal open/close states exist.\n+- Consolidate into a single reducer/state machine.\n+- Extract modal components and a dedicated hook.\n+
## Guardrails
- Don’t refactor and change behavior at the same time unless explicitly asked.\n+- Avoid “big bang” rewrites; prefer incremental extraction.\n+- Keep names precise and consistent; document new boundaries.\n+
