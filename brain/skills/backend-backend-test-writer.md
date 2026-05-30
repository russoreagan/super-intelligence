# Backend Test Writer

Generate comprehensive backend tests for MERN stack code. Analyzes file type, detects project conventions, produces tests that actually run.

**Philosophy:** Smart defaults, zero config. Detect everything from project.

<workflow>

## Workflow

Copy and track progress:

- [ ] Phase 0: Infrastructure check
- [ ] Phase 1: Analyze target files
- [ ] Phase 2: Generate tests
- [ ] Phase 3: Report summary

### Phase 0: Infrastructure Check

Before generating tests, verify setup:

1. Check `package.json` for test framework (Jest/Vitest/Mocha)
2. If none → prompt: "Set up Jest + Supertest?"
3. Check for `mongodb-memory-server` (needed for integration tests)
4. Detect test file convention (colocated vs `__tests__/` vs `tests/`)

**Stop if:** No test framework and user declines setup.

### Phase 1: Analyze Target Files

| Pattern | Type | Test Approach |
|---------|------|---------------|
| `routes/`, `*.routes.js` | Route | Integration (Supertest + real DB) |
| `controllers/` | Controller | Integration |
| `services/` | Service | Unit (mocked deps) |
| `models/`, `*.model.js` | Model | Unit (validation tests) |
| `middleware/` | Middleware | Unit (mock req/res/next) |
| `utils/`, `helpers/` | Utility | Unit (pure functions) |

**Override:** User can specify `--unit` or `--integration`.

### Phase 2: Generate Tests

Process files sequentially with progress. User can stop anytime.

**Each test includes:**
- Proper imports for detected framework
- Setup/teardown (DB connection, cleanup)
- Comprehensive coverage:
  - Success cases (happy path)
  - Validation errors (400)
  - Not found (404)
  - Auth failures (401/403) if protected
  - Edge cases (duplicates, empty, null)

**Reference:** See [test-patterns.md](reference/test-patterns.md) for complete code examples.

### Phase 3: Report

```
Generated: X test files
Coverage: Y test cases total
Next: Run `npm test` to verify
```

</workflow>

<quick-reference>

## Quick Reference

| File Type | Imports | DB Setup |
|-----------|---------|----------|
| Route | `supertest`, `mongodb-memory-server` | Real (in-memory) |
| Service | `jest` | Mocked |
| Model | `mongoose` | Mocked |
| Middleware | `jest` | None |

## Test Structure Pattern

```javascript
describe('[Resource] [Method]', () => {
  describe('success cases', () => {
    it('should [expected behavior]', async () => {});
  });
  describe('validation errors', () => {
    it('should return 400 for [invalid case]', async () => {});
  });
  describe('edge cases', () => {
    it('should handle [edge case]', async () => {});
  });
});
```

</quick-reference>

<checklists>

## Checklists

### Infrastructure (Check First)
- [ ] Test framework in `package.json`
- [ ] Test script defined (`"test": "jest"`)
- [ ] Supertest installed (integration tests)
- [ ] mongodb-memory-server installed (DB tests)
