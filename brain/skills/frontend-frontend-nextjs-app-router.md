# Next.js App Router Expert

## Overview

Expert guidance for Next.js App Router development including page creation, dynamic routing, nested layouts, Server/Client component optimization, and ERP role-based dashboards.

## Core Capabilities

### 1. Page Creation (`/app/.../page.tsx`)

**Rules:**
- Use **Server Components** by default (no 'use client' directive)
- Async data fetching with `fetch()` or ORM queries directly
- No `useEffect` for initial data load
- Use `Suspense` boundaries for loading states
- SEO metadata via `generateMetadata()`

**Template:**
```typescript
// app/dashboard/page.tsx
import { Suspense } from 'react';
import { TaskList } from '@/components/TaskList';
import { TaskListSkeleton } from '@/components/TaskListSkeleton';

export const metadata = {
  title: 'Dashboard',
  description: 'Your task management dashboard',
};

export default async function DashboardPage() {
  const tasks = await fetchTasks();

  return (
    <main className="p-4">
      <h1>Dashboard</h1>
      <Suspense fallback={<TaskListSkeleton />}>
        <TaskList initialTasks={tasks} />
      </Suspense>
    </main>
  );
}
```

### 2. Dynamic Routes (`[slug]/page.tsx`)

**When to use:**
- Student profiles: `app/students/[studentId]/page.tsx`
- Task details: `app/tasks/[taskId]/page.tsx`
- Course pages: `app/courses/[courseId]/page.tsx`

**Rules:**
- Extract params from `params` prop (read-only)
- Use `generateMetadata()` for SEO
- Handle 404 with `not-found.tsx`

**Template:**
```typescript
// app/students/[studentId]/page.tsx
import { notFound } from 'next/navigation';

type Params = Promise<{ studentId: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { studentId } = await params;
  const student = await getStudent(studentId);

  if (!student) return { title: 'Student Not Found' };

  return {
    title: `${student.name} - Student Profile`,
  };
}

export default async function StudentProfile({ params }: { params: Params }) {
  const { studentId } = await params;
  const student = await getStudent(studentId);

  if (!student) notFound();

  return <StudentProfileView student={student} />;
}
```

### 3. Parallel Routes (`@folder`)

**When to use:**
- Dashboard variants: `app/dashboard@(admin|teacher)/page.tsx`
- Split layouts: `app/settings@(user|organization)/layout.tsx`

**Template:**
```typescript
// app/dashboard/@admin/page.tsx - Admin dashboard
export default function AdminDashboard() {
  return <AdminPanel />;
}

// app/dashboard/@teacher/page.tsx - Teacher dashboard
export default function TeacherDashboard() {
  return <TeacherPanel />;
}
```

### 4. Layouts & Templates

**Root Layout (`app/layout.tsx`):**
```typescript
import { Providers } from '@/components/Providers';
import './globals.css';

export const metadata = {
  title: 'Todo Evolution',
  description: 'Task management for education',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
```
