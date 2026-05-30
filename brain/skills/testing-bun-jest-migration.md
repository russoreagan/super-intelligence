# Bun Jest Migration

Bun's test runner is Jest-compatible. Most Jest tests run without changes.

## Quick Migration

```bash
# 1. Remove Jest dependencies
bun remove jest ts-jest @types/jest babel-jest

# 2. Update test script
# package.json: "test": "bun test"

# 3. Run tests
bun test
```

## Import Changes

```typescript
// Before (Jest)
import { describe, it, expect, jest } from '@jest/globals';

// After (Bun) - No import needed, or explicit:
import { describe, test, expect, mock, spyOn } from "bun:test";
```

## API Compatibility

### Fully Compatible

| Jest | Bun | Notes |
|------|-----|-------|
| `describe()` | `describe()` | Identical |
| `it()` / `test()` | `test()` | Use `test()` |
| `expect()` | `expect()` | Same matchers |
| `beforeAll/Each` | `beforeAll/Each` | Identical |
| `afterAll/Each` | `afterAll/Each` | Identical |
| `jest.fn()` | `mock()` | Use `mock()` |
| `jest.spyOn()` | `spyOn()` | Identical |

### Requires Changes

| Jest | Bun Equivalent |
|------|----------------|
| `jest.mock('module')` | `mock.module('module', () => {...})` |
| `jest.useFakeTimers()` | `import { setSystemTime } from "bun:test"` |
| `jest.setTimeout()` | Third argument to `test()` |
| `jest.clearAllMocks()` | Call `.mockClear()` on each mock |

## Mock Migration

### Mock Functions

```typescript
// Jest
const fn = jest.fn().mockReturnValue('value');

// Bun
const fn = mock(() => 'value');
// Or for compatibility:
import { jest } from "bun:test";
const fn = jest.fn(() => 'value');
```

### Module Mocking

```typescript
// Jest (top-level hoisting)
jest.mock('./utils', () => ({
  helper: jest.fn(() => 'mocked')
}));

// Bun (inline, no hoisting)
import { mock } from "bun:test";
mock.module('./utils', () => ({
  helper: mock(() => 'mocked')
}));
```

### Spy Migration

```typescript
// Jest
jest.spyOn(console, 'log').mockImplementation(() => {});

// Bun (identical)
spyOn(console, 'log').mockImplementation(() => {});
```

## Timer Migration

```typescript
// Jest
jest.useFakeTimers();
jest.setSystemTime(new Date('2024-01-01'));
jest.advanceTimersByTime(1000);

// Bun - supports Jest-compatible timer APIs
import { setSystemTime } from "bun:test";
import { jest } from "bun:test";

jest.useFakeTimers();
jest.setSystemTime(new Date('2024-01-01'));
jest.advanceTimersByTime(1000);  // Now supported
```

## Snapshot Testing

```typescript
// Jest
expect(component).toMatchSnapshot();
expect(data).toMatchInlineSnapshot(`"expected"`);

// Bun (identical)
expect(component).toMatchSnapshot();
expect(data).toMatchInlineSnapshot(`"expected"`);
```

Update snapshots:
```bash
bun test --update-snapshots
```

## Configuration Migration

### jest.config.js → bunfig.toml

```javascript
// jest.config.js (before)
module.exports = {
  testMatch: ['**/*.test.ts'],
  testTimeout: 10000,
  setupFilesAfterEnv: ['./jest.setup.ts'],
  collectCoverage: true,
  coverageThreshold: { global: { lines: 80 } }
};
```

```toml
# bunfig.toml (after)
[test]
root = "./"
preload = ["./jest.setup.ts"]
timeout = 10000
coverage = true
coverageThreshold = 0.8
```

## Common Migration Issues

### Issue: `jest.mock` Not Working
