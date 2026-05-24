---
name: testing-and-tdd
description: Unified testing skill covering test strategy, TDD cycle, debugging failing/flaky tests, and requirement-driven test case generation.
summary: Test strategy, TDD red-green-refactor cycle, debugging flaky tests, and QA test case generation.
triggers: [test, TDD, unit test, integration test, coverage, flaky, QA, acceptance test]
disable-model-invocation: true

---
# Testing & TDD (Unified)

## Intent
Use this skill when writing tests, improving coverage, debugging failing tests, or producing QA test cases from requirements/PRDs.

## Modes
1. **Developer tests** (unit/integration/E2E): write or improve automated tests.\n+2. **TDD workflow**: implement features/bugfixes via red-green-refactor.\n+3. **Debugging tests**: systematic root-cause investigation for failures/flakes.\n+4. **QA test cases**: generate requirement-driven test cases (manual/automated planning).\n+
## Core principles
- **Behavior over implementation** (black-box testing).\n+- **Determinism**: avoid time-based flakiness; prefer condition-based waiting.\n+- **Coverage with intent**: cover happy path + edge cases + error cases.\n+- **Evidence before claims**: run the tests you reference.\n+
## TDD (red-green-refactor)
**Rule**: no production code without a failing test first.\n+\n+Cycle:\n+- **RED**: write a minimal failing test.\n+- **Verify RED**: watch it fail for the expected reason.\n+- **GREEN**: write the smallest code to pass.\n+- **Verify GREEN**: watch it pass (and ensure the suite stays green).\n+- **REFACTOR**: clean up while keeping tests green.\n+
## Debugging failing tests (systematic)
When a test fails or is flaky:\n+1. **Reproduce consistently**.\n+2. **Read the error carefully** (stack trace, line numbers).\n+3. **Check recent changes**.\n+4. **Trace data flow across layers** (instrument boundaries).\n+5. **Form one hypothesis** and test it with one minimal change.\n+6. **Add a regression test** for the bug/failure mode.\n+
## Developer testing guidance (lightweight)
- **Frontend**: Vitest + React Testing Library, prefer role/label queries.\n+- **Backend**: pytest (+ async support), mock external IO, validate error cases.\n+- **E2E** (optional): cover only critical happy paths and key errors.\n+
## QA test cases from requirements
When the user has a PRD or requirements and wants QA planning:\n+1. Extract requirements and acceptance criteria.\n+2. Generate scenarios:\n+   - Functional (happy path)\n+   - Edge cases (boundaries, empty states)\n+   - Error handling (invalid inputs, failure modes)\n+   - State transitions (if stateful)\n+3. Produce a **coverage matrix**: requirement → test cases.\n+
## Output formats
### Developer test work
Provide:\n+- what you tested\n+- what you added/changed\n+- gaps/risks\n+- how to run\n+\n+### QA test cases
Generate a markdown document:\n+- test categories\n+- unique IDs\n+- preconditions/steps/expected results\n+- coverage matrix\n+
