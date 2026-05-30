# React Observability

## Problem Statement

Silent failures are debugging nightmares. Code that returns early without logging, error messages that lack context, and missing observability make production issues impossible to diagnose. Write code as if you'll debug it at 3am with only logs.

---

## Pattern: No Silent Early Returns

**Problem:** Early returns without logging create invisible failure paths.

```typescript
// WRONG - silent death
const saveData = (id: string, value: number) => {
  if (!validIds.has(id)) {
    return;  // ❌ Why did we return? No one knows.
  }
  // ... save logic
};

// CORRECT - observable
const saveData = (id: string, value: number) => {
  if (!validIds.has(id)) {
    logger.warn('[saveData] Dropping save - invalid ID', {
      id,
      value,
      validIds: Array.from(validIds),
    });
    return;
  }
  // ... save logic
};
```

**Rule:** Every early return should log why it's returning, with enough context to diagnose.

---

## Pattern: Error Message Design

**Problem:** Error messages that don't help diagnose the issue.

```typescript
// BAD - no context
throw new Error('Data not found');

// BAD - slightly better but still useless at 3am
throw new Error('Data not found. Please try again.');

// GOOD - diagnostic context included
throw new Error(
  `Data not found. ID: ${id}, ` +
  `Available: ${Object.keys(data).length} items, ` +
  `Last fetch: ${lastFetchTime}. This may indicate a caching issue.`
);
```

**Error message template:**

```typescript
throw new Error(
  `[${functionName}] ${whatFailed}. ` +
  `Context: ${relevantState}. ` +
  `Possible cause: ${hypothesis}.`
);
```

**What to include:**

| Element | Why |
|---------|-----|
| Function/location | Where the error occurred |
| What failed | The specific condition that wasn't met |
| Relevant state | Values that help diagnose |
| Possible cause | Your best guess for the fix |

---

## Pattern: Structured Logging

**Problem:** Console.log statements that are hard to parse and search.

```typescript
// BAD - unstructured
console.log('saving data', id, value);
console.log('current state', data);

// GOOD - structured with context object
logger.info('[saveData] Saving data', {
  id,
  value,
  existingCount: Object.keys(data).length,
});
```

**Logging levels:**

| Level | Use for |
|-------|---------|
| `error` | Exceptions, failures that need immediate attention |
| `warn` | Unexpected conditions that didn't fail but might indicate problems |
| `info` | Important business events (user actions, flow milestones) |
| `debug` | Detailed diagnostic info (state dumps, timing) |

**Wrapper for consistent logging:**

```typescript
// utils/logger.ts
const LOG_LEVELS = ['debug', 'info', 'warn', 'error'] as const;
type LogLevel = typeof LOG_LEVELS[number];

const currentLevel: LogLevel = process.env.NODE_ENV === 'development' ? 'debug' : 'warn';

function shouldLog(level: LogLevel): boolean {
  return LOG_LEVELS.indexOf(level) >= LOG_LEVELS.indexOf(currentLevel);
}
