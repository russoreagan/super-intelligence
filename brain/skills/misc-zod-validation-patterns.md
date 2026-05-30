# Zod Validation Patterns Skill

**Use this skill when:** Working with user input validation, API request validation, form data validation, or data transformation in Quetrex.

## Purpose

This skill provides comprehensive patterns for using Zod validation library in TypeScript applications. It ensures input validation is done correctly, securely, and consistently across the codebase.

## What's Covered

1. **[Schema Patterns](./schema-patterns.md)** - Complete guide to all Zod schema types
   - Primitives (string, number, boolean, date)
   - Collections (array, object, map, set, record)
   - Advanced types (union, intersection, discriminated unions)
   - Optional/nullable patterns
   - Branded types and recursive schemas

2. **[Error Handling](./error-handling.md)** - Robust error management
   - Custom error messages
   - Internationalization (i18n)
   - Error formatting for UI display
   - Safe parsing patterns
   - Error recovery strategies

3. **[Refinements](./refinements.md)** - Custom validation logic
   - Basic and chained refinements
   - Cross-field validation
   - Conditional validation
   - Business logic validation
   - File upload validation

4. **[Transforms](./transforms.md)** - Data transformation and normalization
   - Type coercion
   - Data cleaning and normalization
   - Computed fields
   - Preprocessing patterns

5. **[Async Validation](./async-validation.md)** - Asynchronous validation patterns
   - Database uniqueness checks
   - API validations
   - Concurrent async validations
   - Error handling and timeouts

6. **[Type Inference](./type-inference.md)** - TypeScript type extraction
   - z.infer patterns
   - Input vs output types
   - Generic schema types
   - Discriminated union inference

7. **[API Integration](./api-integration.md)** - Next.js integration patterns
   - API routes validation
   - Server Actions validation
   - Form data and file uploads
   - Error response formatting

8. **[Common Schemas](./common-schemas.md)** - Reusable schema library
   - Email, password, phone validation
   - URL, UUID, date schemas
   - Address, credit card validation
   - Username, slug, color schemas

## Quick Start

### Basic Usage

```typescript
import { z } from 'zod'

// Define schema
const userSchema = z.object({
  email: z.string().email(),
  age: z.number().int().positive(),
  role: z.enum(['admin', 'user'])
})

// Parse data (throws on error)
const user = userSchema.parse(data)

// Safe parse (returns result object)
const result = userSchema.safeParse(data)
if (result.success) {
  console.log(result.data)
} else {
  console.error(result.error)
}
```

### Type Inference

```typescript
// Extract TypeScript type from schema
type User = z.infer<typeof userSchema>
// { email: string; age: number; role: 'admin' | 'user' }
```

### API Route Example

```typescript
// src/app/api/users/route.ts
import { NextRequest, NextResponse } from 'next/server'
import { z } from 'zod'

const createUserSchema = z.object({
  email: z.string().email(),
  password: z.string().min(8)
})

export async function POST(request: NextRequest) {
  const body = await request.json()
