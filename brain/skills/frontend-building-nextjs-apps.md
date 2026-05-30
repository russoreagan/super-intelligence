# Next.js 16 Applications

Build Next.js 16 applications correctly with distinctive design.

## Critical Breaking Changes (Next.js 16)

### 1. params and searchParams are Now Promises

**THIS IS THE MOST COMMON MISTAKE.**

```typescript
// WRONG - Next.js 15 pattern (WILL FAIL)
export default function Page({ params }: { params: { id: string } }) {
  return <div>ID: {params.id}</div>
}

// CORRECT - Next.js 16 pattern
export default async function Page({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  return <div>ID: {id}</div>
}
```

### 2. Client Components Need use() Hook

```typescript
"use client"
import { use } from "react"

export default function ClientPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = use(params)
  return <div>ID: {id}</div>
}
```

### 3. searchParams Also Async

```typescript
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ page?: string }>
}) {
  const { page } = await searchParams
  return <div>Page: {page ?? "1"}</div>
}
```

---

## Core Patterns

### Project Setup

```bash
npx create-next-app@latest my-app --typescript --tailwind --eslint
cd my-app

# Add shadcn/ui
npx shadcn@latest init
npx shadcn@latest add button form dialog table sidebar
```

### App Router Layout

```typescript
// app/layout.tsx
export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        {children}
      </body>
    </html>
  )
}
```

### Dynamic Routes

```typescript
// app/tasks/[id]/page.tsx
export default async function TaskPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  const task = await getTask(id)

  return (
    <main>
      <h1>{task.title}</h1>
    </main>
  )
}
```

### Server Actions

```typescript
// app/actions.ts
"use server"

import { revalidatePath } from "next/cache"

export async function createTask(formData: FormData) {
  const title = formData.get("title") as string

  await db.insert(tasks).values({ title })

  revalidatePath("/tasks")
}

// Usage in component
<form action={createTask}>
  <input name="title" />
  <button type="submit">Create</button>
</form>
```

### API Routes

```typescript
// app/api/tasks/route.ts
import { NextResponse } from "next/server"

export async function GET() {
  const tasks = await db.select().from(tasksTable)
  return NextResponse.json(tasks)
}

export async function POST(request: Request) {
  const body = await request.json()
  const task = await db.insert(tasksTable).values(body).returning()
  return NextResponse.json(task, { status: 201 })
}
```

### Middleware → proxy.ts

```typescript
// proxy.ts (replaces middleware.ts in Next.js 16)
import { NextRequest } from "next/server"

export function proxy(request: NextRequest) {
  // Authentication check
  const token = request.cookies.get("token")
  if (!token && request.nextUrl.pathname.startsWith("/dashboard")) {
    return Response.redirect(new URL("/login", request.url))
  }
}

export const config = {
  matcher: ["/dashboard/:path*"],
}
```

---

## Data Fetching
