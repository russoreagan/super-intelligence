# React Hook Form + Zod Validation

**Status**: Production Ready ✅
**Last Updated**: 2025-11-21
**Dependencies**: None (standalone)
**Latest Versions**: react-hook-form@7.66.1, zod@4.1.12, @hookform/resolvers@5.2.2

---

## Quick Start (10 Minutes)

### 1. Install Packages

```bash
bun add react-hook-form@7.66.1 zod@4.1.12 @hookform/resolvers@5.2.2
```

**Why These Packages**:
- **react-hook-form**: Performant, flexible forms with minimal re-renders
- **zod**: TypeScript-first schema validation with type inference
- **@hookform/resolvers**: Adapter connecting Zod to React Hook Form

### 2. Create Your First Form

```typescript
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

// 1. Define validation schema
const loginSchema = z.object({
  email: z.string().email('Invalid email address'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
})

// 2. Infer TypeScript type from schema
type LoginFormData = z.infer<typeof loginSchema>

function LoginForm() {
  // 3. Initialize form with zodResolver
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      email: '',
      password: '',
    },
  })

  // 4. Handle form submission
  const onSubmit = async (data: LoginFormData) => {
    // Data is guaranteed to be valid here
    console.log('Valid data:', data)
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)}>
      <div>
        <label htmlFor="email">Email</label>
        <input id="email" type="email" {...register('email')} />
        {errors.email && (
          <span role="alert" className="error">
            {errors.email.message}
          </span>
        )}
      </div>

      <div>
        <label htmlFor="password">Password</label>
        <input id="password" type="password" {...register('password')} />
        {errors.password && (
          <span role="alert" className="error">
            {errors.password.message}
          </span>
        )}
      </div>

      <button type="submit" disabled={isSubmitting}>
        {isSubmitting ? 'Logging in...' : 'Login'}
      </button>
    </form>
  )
}
```

**CRITICAL**:
- Always set `defaultValues` to prevent "uncontrolled to controlled" warnings
- Use `zodResolver(schema)` to connect Zod validation
- Type form with `z.infer<typeof schema>` for full type safety
- Validate on both client AND server (never trust client validation alone)

**Template**: See `templates/basic-form.tsx` for complete working example

### 3. Add Server-Side Validation

```typescript
// server/api/login.ts
import { z } from 'zod'

// SAME schema on server
const loginSchema = z.object({
  email: z.string().email('Invalid email address'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
})
