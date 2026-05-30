# Regression Prevention (Unified)

## Intent

Use when:
- Working on features that have broken in previous commits
- Implementing safeguards for critical data fields
- Setting up validation to catch regressions before deployment
- Creating type-safe helpers to prevent common mistakes
- Establishing CI/CD checks for critical paths
- Documenting critical fields and their requirements

**Key insight:** The real solution isn't just fixing the bug—it's preventing the entire class of bugs through systematic safeguards.

---

## Core Principles

### 1. Multiple Layers of Defense

No single layer is perfect. Build redundancy:

1. **Type System** - Prevent mistakes at code level
2. **Unit Tests** - Catch bugs during development
3. **Integration Tests** - Catch bugs before commit
4. **Data Validation** - Catch data issues before deployment
5. **CI/CD** - Catch bugs before merge
6. **Code Review** - Human verification with checklists
7. **Monitoring** - Runtime validation in production

### 2. Fail Fast, Fail Loud

Errors should be:
- **Immediate** - Caught as early as possible in the pipeline
- **Specific** - Clear about what's wrong and how to fix it
- **Blocking** - Prevent bad code from reaching production
- **Documented** - Link to prevention docs and fix scripts

### 3. Make the Right Thing Easy

- Type-safe helpers that guarantee correctness
- Scripts that auto-fix common issues
- Clear documentation with examples
- NPM scripts for common workflows

---

## Implementation Strategy

### Layer 1: Type-Safe Helpers

**Problem:** Easy to forget to populate optional fields.

**Solution:** Create wrapper functions that guarantee correctness.

```typescript
// ❌ DON'T: Direct calls (easy to forget fields)
const record = await prisma.model.create({ data: rawData });

// ✅ DO: Type-safe helper (guarantees fields)
import { createModelSafely } from '@/lib/model/create-safely';
const record = await createModelSafely(rawData);
```

**Implementation:**
1. Identify critical fields that must always be populated
2. Create a wrapper function that fetches/computes required data
3. Return fully-populated object with guarantees
4. Export validation function for runtime checks

**Example structure:**
```typescript
// lib/model/create-safely.ts
export async function createModelSafely(data: Input) {
  // Fetch required data
  const required = await fetchRequiredData(data.foreignKeyId);
  
  if (!required) {
    throw new Error('Invalid foreign key');
  }
  
  // CRITICAL: Always populate these fields
  return await prisma.model.create({
    data: {
      ...data,
      criticalField: required.value,
      derivedField: computeDerivedValue(data, required)
    }
  });
}

export function validateModel(model: any) {
  const errors: string[] = [];
  if (!model.criticalField) errors.push('Missing criticalField');
  if (!model.derivedField) errors.push('Missing derivedField');
  return { valid: errors.length === 0, errors };
}
```

### Layer 2: Integration Tests

**Problem:** Changes break existing functionality without detection.

**Solution:** Test critical paths end-to-end.
