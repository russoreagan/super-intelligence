---
name: code-review
description: Unified code review skill combining review technique, severity triage, language-specific checks, and review workflow (requesting/receiving). Use for PR reviews, diff reviews, or focused file reviews.
summary: PR/diff review with severity triage (critical/important/minor), checklists, and actionable feedback.
triggers: [review, PR, diff, feedback, approve, merge, code quality]
disable-model-invocation: true

---
# Code Review (Unified)

## Intent
Use this skill when the user asks to review code (PR/diff/files) or when you want to proactively validate changes before merge.

This skill merges:
- **How to review** (mindset, process, severity)
- **What to review** (correctness, security, performance, tests, maintainability)
- **How to use reviews in a workflow** (requesting/receiving review)
- **Format options** (general vs. strict templates used by some upstream skills)

## Review mode detection
Pick one:

1. **Diff/PR review**: review a commit range, PR, or staged changes.\n+2. **File-targeted review**: review one or more named files.\n+3. **Docs review**: review markdown/docs for clarity and style.\n+
## Core review process (all modes)
1. **Context**: what’s the goal + what changed?\n+2. **Risk scan** (fast): correctness, security, performance, data integrity.\n+3. **Deep pass**: line-by-line in changed areas.\n+4. **Tests**: are they present and meaningful? any gaps?\n+5. **Summarize + triage**: blocking vs. important vs. minor.\n+6. **Actionability**: each issue should have “what/why/how to fix”.\n+
## Severity labels
Use consistent severity:\n+- **Critical / Blocking**: bugs, security issues, correctness/data loss risks, broken UX.\n+- **Important**: likely issues, maintainability problems, missing tests for risky changes.\n+- **Minor**: readability, small refactors, nits.\n+
## What to look for (checklist)
- **Correctness**: edge cases, null/empty, concurrency, ordering, idempotency.\n+- **Security**: input validation, authz/authn, secrets, injection, data exposure.\n+- **Performance**: N+1, unnecessary loops, large payloads, hot-path allocations.\n+- **Tests**: behavior > implementation, determinism, coverage for risky logic.\n+- **Maintainability**: naming, single responsibility, duplication, clarity.\n+- **Docs/UX** (if applicable): user-facing clarity, constraints, error messaging.\n+
## Language-specific emphasis (lightweight)
- **TypeScript/JS**: type safety (avoid `any`), error handling for async, React patterns, stable dependencies.\n+- **Python**: exception specificity, mutable defaults, type hints (when expected), IO blocking in hot paths.\n+- **Docs**: conversational clarity, non-condescending tone, descriptive links, consistent structure.\n+
## Output formats
Default output (recommended unless the user asks otherwise):\n+\n+```\n+## Summary\n+<1–3 bullets>\n+\n+## Critical\n+1. <issue> (file:line)\n+   - Why it matters: ...\n+   - Suggested fix: ...\n+\n+## Important\n+...\n+\n+## Minor\n+...\n+\n+## Test plan\n+- <what to run / what to verify>\n+```\n+\n+If the user explicitly wants one of these strict formats:\n+- **Dify Frontend Review template**: “urgent issues + suggestions” with the exact spacing.\n+- **Metabase Docs review**: numbered “Issue N” format.\n+\n+## Requesting code review (workflow)
When you finish a meaningful chunk of work:\n+1. Identify the **base** and **head** revision.\n+2. Provide a short “what changed / requirements / risk areas” summary.\n+3. Ask a reviewer (human or subagent) to review the diff.\n+4. Treat feedback as input; fix blocking issues first.\n+
## Receiving code review (workflow)
When you receive review feedback:\n+1. Read all items first.\n+2. Clarify any unclear items **before** implementing.\n+3. Verify the suggestion fits this codebase; push back with technical reasoning when needed.\n+4. Implement one item at a time; test after each meaningful change.\n+
