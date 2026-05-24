---
name: debugging
description: Use when debugging bugs, test failures, performance regressions, or unexpected behavior and you need a systematic root-cause approach with evidence and minimal, validated fixes.
summary: Systematic root-cause debugging with hypothesis testing, evidence-first investigation, and minimal fixes.
triggers: [bug, error, crash, failing, broken, investigate, debug, fix, not working]
disable-model-invocation: true

---
# Debugging (Definitive)

## Core rule
**No fixes without root-cause investigation first.** Avoid guess-and-check patches.

## Workflow (scientific method)
### 1) Reproduce
- Get exact steps and environment details.
- Determine if it’s consistent vs flaky.
- Reduce to a minimal reproduction when possible.

### 2) Read the evidence
- Read the full error message and stack trace.
- Note file paths, line numbers, and failure signatures.
- Check logs + network + recent changes.

### 3) Trace the data flow (multi-layer systems)
For boundaries (UI → API → service → DB, CI → build → deploy):
- log what enters/exits each layer
- confirm config/env propagation
- identify which layer first diverges

### 4) Form a single hypothesis
State: “I think X is the root cause because Y”.

### 5) Test minimally
Change one thing to validate the hypothesis (or add instrumentation).

### 6) Fix the root cause + add a regression test
- Add a failing test if feasible.
- Fix at the source, not where it explodes.
- Re-run verification commands and report results.

## Common tools (quick reference)
- **JS/TS**: `debugger`, DevTools breakpoints, network panel, performance profiler.
- **React**: React DevTools Profiler, render tracing, memoization checks.
- **Python**: `pdb`, logging, minimal scripts to isolate behavior.

## Strategies (systematic debugging)
- **Rubber duck**: Explain the problem and code aloud; often reveals the issue.
- **Profiling**: Use language/runtime profilers for performance bugs (hotspots, memory).
- **Minimal repro**: Strip to the smallest case that still fails.

## ESM/CommonJS Gotchas (Next.js, Node production)

- **CommonJS globals don't exist in ESM**: `module`, `require`, `__dirname`, `__filename` throw `ReferenceError` in ESM builds. Next.js production uses ESM.
- **Init errors cascade**: If startup code (e.g. Socket.IO init) throws, dependent features fail everywhere—e.g. all `emitToSession` calls return false. Trace the init path; the bug is often there, not at the call site.
- **Safe patterns**: Use `import.meta.url` instead of `__filename`; avoid `module.id` in logging; use `import()` instead of `require()`.

## Anti-patterns (stop signs)
- “Quick fix for now, investigate later”
- Multiple fixes in one attempt (can’t tell what worked)
- “It should work” without running verification

## Output format (when reporting)
```
## Repro
<steps + environment>

## Evidence
<error/logs + where it fails>

## Hypothesis
<single hypothesis>

## Test
<what changed to test it>

## Fix
<root-cause fix>

## Verification
<command + output summary>
```

