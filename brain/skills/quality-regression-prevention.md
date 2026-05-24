---
name: regression-prevention
description: Use when working on critical features that have broken before, or when implementing safeguards to prevent features from breaking in future updates. Provides multi-layered prevention strategy with automated tests, validation scripts, type-safe helpers, and process improvements.
summary: Multi-layered regression prevention: automated tests, data validation, type-safe helpers, CI/CD integration, and documentation-driven development to catch bugs before production.
triggers: [regression, prevent breaking, critical feature, data integrity, validation, test coverage, breaking change, revert, lost feature]
disable-model-invocation: true

---
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

```typescript
// tests/integration/critical-feature.test.ts
describe('Critical Feature - Required Fields', () => {
  test('CRITICAL: Field X must be populated', async () => {
    const record = await createRecord({ /* data */ });
    
    // CRITICAL ASSERTIONS
    expect(record.criticalField).toBeTruthy();
    expect(record.criticalField).not.toBeNull();
    expect(record.derivedField).toEqual(expectedValue);
  });
  
  test('CRITICAL: UI can display field X', async () => {
    // Simulate what the UI does
    const displayValue = record.criticalField || '';
    expect(displayValue).toBeTruthy();
  });
});
```

**What to test:**
- Critical fields are populated during creation
- Critical fields match related data (foreign keys)
- Derived fields are computed correctly
- UI components can access and display the data
- Multipart features work together (e.g., sum of parts equals total)

### Layer 3: Data Validation Scripts

**Problem:** Production data can drift from expected state.

**Solution:** Validation script that checks all records.

```typescript
// scripts/validate-data-integrity.ts
async function validateDataIntegrity() {
  const records = await prisma.model.findMany({ /* filters */ });
  const issues: Issue[] = [];
  
  for (const record of records) {
    // Check 1: Required field populated
    if (!record.criticalField) {
      issues.push({
        id: record.id,
        issue: 'Missing criticalField',
        severity: 'ERROR',
        fix: 'Set criticalField to X'
      });
    }
    
    // Check 2: Data consistency
    if (record.field1 !== record.relatedRef?.field1) {
      issues.push({
        id: record.id,
        issue: 'Field mismatch',
        severity: 'ERROR',
        fix: 'Update field1 to match relatedRef'
      });
    }
  }
  
  // Report and exit with error code if issues found
  if (issues.length > 0) {
    console.error(`Found ${issues.length} issues`);
    process.exit(1);
  }
  
  console.log('✅ All checks passed');
}
```

**Run before deployment:**
```json
{
  "scripts": {
    "validate:data": "tsx scripts/validate-data-integrity.ts",
    "predeploy": "npm run validate:data && npm run build"
  }
}
```

### Layer 4: Auto-Fix Scripts

**Problem:** When validation fails, manual fixes are error-prone.

**Solution:** Script that automatically fixes common issues.

```typescript
// scripts/fix-missing-fields.ts
async function fixMissingFields() {
  const broken = await prisma.model.findMany({
    where: { criticalField: null }
  });
  
  for (const record of broken) {
    // Fetch required data
    const required = await fetchRequiredData(record.foreignKeyId);
    
    // Update with correct values
    await prisma.model.update({
      where: { id: record.id },
      data: {
        criticalField: required.value,
        derivedField: computeDerivedValue(record, required)
      }
    });
    
    console.log(`✅ Fixed ${record.id}`);
  }
}
```

### Layer 5: CI/CD Integration

**Problem:** Tests don't run automatically on every change.

**Solution:** GitHub Actions workflow that blocks merge if tests fail.

```yaml
# .github/workflows/critical-feature-tests.yml
name: Critical Feature Tests

on:
  pull_request:
    paths:
      - 'app/api/critical/**'
      - 'lib/critical/**'
      - 'prisma/schema.prisma'

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
      - run: npm ci
      - run: npm run validate:data
      - run: npm run test:critical-feature
      
      - name: Report failure
        if: failure()
        run: |
          echo "❌ CRITICAL: Tests failed!"
          echo "This PR may break critical features."
          exit 1
```

### Layer 6: PR Template with Checklist

**Problem:** Reviewers don't know what to check.

**Solution:** PR template with critical feature checklist.

```markdown
## ⚠️ Critical Feature Changes

If this PR touches critical features:

### Required Checks
- [ ] Critical fields are populated
- [ ] Integration tests pass: `npm run test:critical-feature`
- [ ] Data validation passes: `npm run validate:data`
- [ ] Type-safe helpers used (not direct Prisma calls)

### Manual Testing
- [ ] Created new record via UI
- [ ] Verified display in UI
- [ ] Tested with existing records (no regressions)

**If any boxes checked, you MUST complete all checks above.**

See: `docs/REGRESSION_PREVENTION.md`
```

### Layer 7: Documentation

**Problem:** Future developers don't know about critical requirements.

**Solution:** Comprehensive documentation with examples.

**Required docs:**
1. **Prevention strategy** - Overall approach and workflow
2. **Quick reference** - Commands and common tasks
3. **Fix documentation** - What broke, why, and how it was fixed
4. **Code comments** - In critical sections

**Example code comment:**
```typescript
// CRITICAL: criticalField and derivedField MUST be populated
// These fields are required for:
// - UI display (ComponentName.tsx line 123)
// - Feature X (FeatureName.tsx line 456)
// See: docs/REGRESSION_PREVENTION.md
// Tests: tests/integration/critical-feature.test.ts
const data = {
  criticalField: required.value,  // ✅ Required
  derivedField: computed.value     // ✅ Required
};
```

---

## Workflow

### Before Making Changes

1. **Read prevention docs:**
   ```bash
   cat docs/REGRESSION_PREVENTION.md
   cat docs/CRITICAL_FEATURE_FIX.md
   ```

2. **Run validation:**
   ```bash
   npm run validate:data
   ```

3. **Check current tests:**
   ```bash
   npm run test:critical-feature
   ```

### While Coding

1. **Use type-safe helpers** instead of direct calls
2. **Add CRITICAL comments** explaining requirements
3. **Follow existing patterns** in the codebase
4. **Update tests** if adding new requirements

### After Making Changes

1. **Run tests:**
   ```bash
   npm run test:critical-feature
   npm run validate:data
   npm run build
   ```

2. **Manual testing:**
   - Create new record via UI
   - Verify display in relevant components
   - Test with existing records

3. **Update documentation** if patterns changed

### In PR

1. **Fill out checklist** in PR template
2. **Link to relevant docs** in description
3. **Wait for CI** to pass
4. **Get review** from someone familiar with the feature

---

## Common Patterns

### Pattern 1: Critical Fields in Database Models

**Scenario:** Model has optional fields that are actually required for certain game systems.

**Solution:**
```typescript
// 1. Type-safe helper
export async function createDnDCharacter(data: Input) {
  const dndClass = await prisma.dnDClass.findUnique({
    where: { id: data.dndClassId }
  });
  
  return await prisma.character.create({
    data: {
      ...data,
      dndClass: dndClass.name,              // ✅ Always set
      classLevels: { [dndClass.name.toLowerCase()]: 1 }  // ✅ Always set
    }
  });
}

// 2. Validation
export function validateDnDCharacter(char: any) {
  const errors: string[] = [];
  if (char.gameSystem === 'dnd5e') {
    if (!char.dndClass) errors.push('Missing dndClass');
    if (!char.classLevels) errors.push('Missing classLevels');
  }
  return { valid: errors.length === 0, errors };
}

// 3. Test
test('CRITICAL: dndClass must be populated', async () => {
  const char = await createDnDCharacter({ /* data */ });
  expect(char.dndClass).toBeTruthy();
  expect(char.classLevels).toBeTruthy();
});

// 4. Validation script
const dndChars = await prisma.character.findMany({
  where: { gameSystem: 'dnd5e' }
});
for (const char of dndChars) {
  const validation = validateDnDCharacter(char);
  if (!validation.valid) {
    issues.push({ id: char.id, errors: validation.errors });
  }
}
```

### Pattern 2: Derived Fields

**Scenario:** Field must be computed from other fields and kept in sync.

**Solution:**
```typescript
// 1. Computation function
export function computeDerivedField(data: Input): DerivedValue {
  // Deterministic computation
  return data.field1 + data.field2;
}

// 2. Type-safe helper
export async function createWithDerived(data: Input) {
  return await prisma.model.create({
    data: {
      ...data,
      derivedField: computeDerivedField(data)  // ✅ Always computed
    }
  });
}

// 3. Update helper
export async function updateWithDerived(id: string, updates: Partial<Input>) {
  const current = await prisma.model.findUnique({ where: { id } });
  const merged = { ...current, ...updates };
  
  return await prisma.model.update({
    where: { id },
    data: {
      ...updates,
      derivedField: computeDerivedField(merged)  // ✅ Recomputed
    }
  });
}

// 4. Validation
test('CRITICAL: derivedField matches computation', async () => {
  const record = await createWithDerived({ field1: 5, field2: 10 });
  expect(record.derivedField).toBe(15);
  
  const updated = await updateWithDerived(record.id, { field1: 20 });
  expect(updated.derivedField).toBe(30);
});
```

### Pattern 3: Foreign Key Consistency

**Scenario:** String field must match referenced model's field.

**Solution:**
```typescript
// 1. Type-safe helper
export async function createWithConsistency(data: Input) {
  const ref = await prisma.refModel.findUnique({
    where: { id: data.refId }
  });
  
  if (!ref) throw new Error('Invalid reference');
  
  return await prisma.model.create({
    data: {
      ...data,
      refId: ref.id,
      refName: ref.name  // ✅ Matches reference
    }
  });
}

// 2. Validation script
const records = await prisma.model.findMany({
  include: { ref: true }
});

for (const record of records) {
  if (record.refName !== record.ref?.name) {
    issues.push({
      id: record.id,
      issue: `refName mismatch: "${record.refName}" != "${record.ref?.name}"`,
      fix: `Update refName to "${record.ref?.name}"`
    });
  }
}
```

---

## Anti-Patterns

### ❌ DON'T: Rely on a single layer

```typescript
// ❌ Only tests, no validation script
// Problem: Tests pass but production data is broken
```

**DO:** Multiple layers of defense

### ❌ DON'T: Skip documentation

```typescript
// ❌ Fix the bug but don't document why
// Problem: Next developer breaks it again
```

**DO:** Document the why, not just the what

### ❌ DON'T: Make helpers optional

```typescript
// ❌ Provide helper but allow direct calls
// Problem: Developers take the easy path
```

**DO:** Enforce helper usage through code review

### ❌ DON'T: Write vague tests

```typescript
// ❌ test('it works', async () => { ... })
test('CRITICAL: dndClass must be populated', async () => { ... })
```

**DO:** Use "CRITICAL" prefix and specific descriptions

---

## Checklist

### When Implementing Prevention

- [ ] Identified critical fields that must always be populated
- [ ] Created type-safe helper function
- [ ] Added validation function
- [ ] Wrote integration tests with "CRITICAL" prefix
- [ ] Created data validation script
- [ ] Created auto-fix script
- [ ] Added CI/CD workflow
- [ ] Updated PR template with checklist
- [ ] Documented prevention strategy
- [ ] Added code comments in critical sections
- [ ] Tested all layers (helper, tests, validation, fix)

### When Working on Critical Features

- [ ] Read prevention docs before starting
- [ ] Used type-safe helpers (not direct calls)
- [ ] Added CRITICAL comments explaining requirements
- [ ] Ran integration tests before committing
- [ ] Ran validation script before committing
- [ ] Ran build to catch TypeScript errors
- [ ] Manual tested in UI
- [ ] Filled out PR checklist
- [ ] Linked to relevant docs in PR

---

## Success Metrics

Track these to measure effectiveness:

1. **Regression frequency** - Zero regressions in critical features for 3+ months
2. **Test pass rate** - 100% of PRs have passing tests
3. **Validation pass rate** - `npm run validate:data` runs clean on production
4. **Runtime errors** - Zero errors related to missing critical fields
5. **Adoption rate** - 100% of team members follow workflow

---

## References

### Internal Documentation
- `docs/REGRESSION_PREVENTION.md` - Complete strategy
- `docs/PREVENTING_REGRESSIONS_SUMMARY.md` - Quick reference
- `docs/CRITICAL_FEATURE_FIX.md` - Specific fix documentation

### Code Locations
- `lib/model/create-safely.ts` - Type-safe helpers
- `tests/integration/critical-feature.test.ts` - Integration tests
- `scripts/validate-data-integrity.ts` - Validation script
- `scripts/fix-missing-fields.ts` - Auto-fix script
- `.github/workflows/critical-feature-tests.yml` - CI/CD workflow
- `.github/PULL_REQUEST_TEMPLATE.md` - PR checklist

### NPM Scripts
```bash
npm run test:critical-feature    # Run integration tests
npm run validate:data            # Validate data integrity
npm run fix:data                 # Auto-fix common issues
npm run predeploy                # Run validation + build
```

---

## Related Skills

- **testing-and-tdd** - Test strategy and TDD workflow
- **database-patterns** - Database schema design and migrations
- **error-handling** - Fail-forward error design
- **code-review** - PR review checklist and standards
- **ci-cd-and-secrets** - CI/CD pipeline setup
