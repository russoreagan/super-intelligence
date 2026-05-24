---
name: frontend-patterns
description: Provide comprehensive understanding of React, TypeScript, and UI patterns. Use when creating new React components, working with TypeScript types, implementing UI features, styling components with design systems, managing component state, or integrating with backend APIs.
disable-model-invocation: true

---
# Frontend Patterns Skill

## Purpose

Provide comprehensive understanding of React, TypeScript, and UI patterns. Use when building consistent, maintainable components with design tokens and modern tooling.

## When to Use

Use this skill when:
- Creating new React components
- Working with TypeScript types
- Implementing UI features
- Styling components with design tokens or a design system
- Managing component state
- Integrating with backend APIs

## Frontend stack

**Build tools:** Vite, npm/pnpm/yarn, Node 18+

**Core:** React 18+, TypeScript 5+, ESLint (flat config)

**UI & styling:** Design tokens or CSS variables for theming; Radix UI or similar for accessible primitives.

**State:** React Context, React Query (or TanStack Query), useState/useReducer.

## Styling: use design tokens, not hardcoded values

Prefer theme variables and design tokens over raw colors:

```typescript
// ✅ Prefer theme/token variables
const styles = {
  color: 'var(--color-text-primary, hsl(0, 0%, 10%))',
  background: 'var(--color-bg-surface, hsl(0, 0%, 98%))',
  border: '1px solid var(--color-border, hsl(0, 0%, 90%))',
};
```

```typescript
// ❌ Avoid raw hex/hsl in component code
const styles = { color: '#000000', background: '#f5f5f5' };
```

## Component Patterns

### Vite project setup

```bash
npm install   # or pnpm / yarn
npm run dev   # Vite dev server
npm run build # Production build
```

**package.json (typical):**
```json
{
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "lint": "eslint .",
    "type-check": "tsc --noEmit"
  }
}
```

### ESLint configuration

Use ESLint flat config (eslint.config.mjs):

```javascript
// eslint.config.mjs
import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import typescript from '@typescript-eslint/eslint-plugin';
import tsParser from '@typescript-eslint/parser';

export default [
  js.configs.recommended,
  {
    files: ['**/*.{ts,tsx,js,jsx}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      react,
      'react-hooks': reactHooks,
      '@typescript-eslint': typescript,
    },
    rules: {
      'react/react-in-jsx-scope': 'off',  // Not needed in React 18
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-unused-vars': ['warn', { 
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_' 
      }],
    },
    settings: {
      react: { version: 'detect' },
    },
  },
];
```

### Vite configuration

```typescript
// vite.config.ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tsconfigPaths from 'vite-tsconfig-paths';

export default defineConfig({
  plugins: [
    react(),
    tsconfigPaths(),  // Support tsconfig path aliases
  ],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'esnext',
    sourcemap: true,
  },
});
```

### Environment Variables (Vite)

```typescript
// Access environment variables (VITE_ prefix)
const apiUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const isDev = import.meta.env.DEV;
const isProd = import.meta.env.PROD;

// Type definitions (vite-env.d.ts)
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_FEATURE_FLAG: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
```

### Dynamic Imports (Vite)

```typescript
// Vite pattern (replaces require.context)
const modules = import.meta.glob<{ default: ComponentType }>(
  './components/**/*.tsx',
  { eager: true }
);

// Process glob imports
for (const path of Object.keys(modules)) {
  const component = modules[path].default;
  // Use component
}
```

### Functional Components with TypeScript

```typescript
import React from 'react';

interface MyComponentProps {
  title: string;
  count: number;
  onAction?: () => void;
}

export const MyComponent: React.FC<MyComponentProps> = ({ 
  title, 
  count, 
  onAction 
}) => {
  return (
    <div>
      <h2>{title}</h2>
      <p>Count: {count}</p>
      {onAction && <button onClick={onAction}>Action</button>}
    </div>
  );
};
```

### Hooks Pattern

```typescript
import { useState, useEffect, useCallback, useMemo } from 'react';

export const useMyFeature = (initialValue: string) => {
  const [value, setValue] = useState(initialValue);
  const [loading, setLoading] = useState(false);

  // Memoized computed value
  const processedValue = useMemo(() => {
    return value.toUpperCase();
  }, [value]);

  // Memoized callback
  const handleUpdate = useCallback((newValue: string) => {
    setValue(newValue);
  }, []);

  // Effect with cleanup
  useEffect(() => {
    const subscription = subscribe(value);
    return () => subscription.unsubscribe();
  }, [value]);

  return { value, processedValue, handleUpdate, loading };
};
```

### Context Pattern

```typescript
import { createContext, useContext, useState, ReactNode } from 'react';

interface MyContextValue {
  data: string[];
  addItem: (item: string) => void;
}

const MyContext = createContext<MyContextValue | undefined>(undefined);

export const MyProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [data, setData] = useState<string[]>([]);

  const addItem = (item: string) => {
    setData(prev => [...prev, item]);
  };

  return (
    <MyContext.Provider value={{ data, addItem }}>
      {children}
    </MyContext.Provider>
  );
};

export const useMyContext = () => {
  const context = useContext(MyContext);
  if (!context) {
    throw new Error('useMyContext must be used within MyProvider');
  }
  return context;
};
```

## State Management Patterns

### Local State (Component-Level)

```typescript
// Simple state
const [count, setCount] = useState(0);

// Complex state with reducer
type State = { count: number; error: string | null };
type Action = 
  | { type: 'increment' }
  | { type: 'decrement' }
  | { type: 'error'; error: string };

const reducer = (state: State, action: Action): State => {
  switch (action.type) {
    case 'increment':
      return { ...state, count: state.count + 1 };
    case 'decrement':
      return { ...state, count: state.count - 1 };
    case 'error':
      return { ...state, error: action.error };
    default:
      return state;
  }
};

const [state, dispatch] = useReducer(reducer, { count: 0, error: null });
```

### Global State (Context)

Located in `frontend/src/contexts/`:
- **DashboardContext.tsx** - Dashboard state
- **ThemeContext.tsx** - Theme configuration
- **AuthContext.tsx** - User authentication

### Server State (React Query)

```typescript
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';

// Fetch data
const { data, isLoading, error } = useQuery({
  queryKey: ['campaigns', companyId],
  queryFn: () => fetchCampaigns(companyId),
  staleTime: 5 * 60 * 1000, // 5 minutes
});

// Mutate data
const queryClient = useQueryClient();
const mutation = useMutation({
  mutationFn: createCampaign,
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ['campaigns'] });
  },
});
```

## API Integration Patterns

### Service Layer Pattern

Create services in `frontend/src/api/`:

```typescript
// api/campaigns.ts
import { apiClient } from './client';

export interface Campaign {
  id: string;
  name: string;
  status: 'active' | 'paused';
}

export const campaignService = {
  async getAll(companyId: number): Promise<Campaign[]> {
    const response = await apiClient.get(`/companies/${companyId}/campaigns`);
    return response.data;
  },

  async getById(id: string): Promise<Campaign> {
    const response = await apiClient.get(`/campaigns/${id}`);
    return response.data;
  },

  async create(data: Omit<Campaign, 'id'>): Promise<Campaign> {
    const response = await apiClient.post('/campaigns', data);
    return response.data;
  },

  async update(id: string, data: Partial<Campaign>): Promise<Campaign> {
    const response = await apiClient.patch(`/campaigns/${id}`, data);
    return response.data;
  },

  async delete(id: string): Promise<void> {
    await apiClient.delete(`/campaigns/${id}`);
  },
};
```

### Error Handling Pattern

```typescript
import { AxiosError } from 'axios';

try {
  const data = await campaignService.getAll(companyId);
  setData(data);
} catch (error) {
  if (error instanceof AxiosError) {
    if (error.response?.status === 404) {
      setError('Campaigns not found');
    } else if (error.response?.status === 401) {
      setError('Unauthorized');
      // Redirect to login
    } else {
      setError('Failed to load campaigns');
    }
  } else {
    setError('Unexpected error occurred');
  }
}
```

## TypeScript Patterns

### Type Definitions

```typescript
// Explicit types for props
interface ButtonProps {
  variant: 'primary' | 'secondary' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  onClick?: () => void;
  children: React.ReactNode;
}

// Union types for state
type LoadingState = 
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: string[] }
  | { status: 'error'; error: string };

// Generic types
interface ApiResponse<T> {
  data: T;
  meta: {
    page: number;
    total: number;
  };
}

// Utility types
type PartialCampaign = Partial<Campaign>;
type RequiredCampaign = Required<Campaign>;
type CampaignKeys = keyof Campaign;
```

### Type Guards

```typescript
function isApiError(error: unknown): error is AxiosError {
  return error instanceof AxiosError;
}

function hasData<T>(response: ApiResponse<T> | null): response is ApiResponse<T> {
  return response !== null && 'data' in response;
}
```

## Styling Patterns

### Tailwind + Theme Variables

```typescript
<div
  className="rounded-lg p-4 shadow-md"
  style={{
    backgroundColor: 'var(--bg-card, var(--default-bg-card))',
    color: 'var(--text-primary, var(--default-text-primary))',
    borderColor: 'var(--border-primary, var(--default-border-primary))',
  }}
>
  Content
</div>
```

### Conditional Styling

```typescript
import clsx from 'clsx';

<button
  className={clsx(
    'px-4 py-2 rounded',
    isActive && 'ring-2 ring-blue-500',
    disabled && 'opacity-50 cursor-not-allowed'
  )}
  style={{
    backgroundColor: isActive 
      ? 'var(--bg-active, var(--default-bg-active))'
      : 'var(--bg-card, var(--default-bg-card))'
  }}
>
  Button
</button>
```

### Responsive Design

```typescript
<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
  {items.map(item => (
    <Card key={item.id} {...item} />
  ))}
</div>
```

## Performance Patterns

### Memoization

```typescript
import { memo, useMemo, useCallback } from 'react';

// Memoize expensive computations
const expensiveValue = useMemo(() => {
  return data.reduce((acc, item) => acc + item.value, 0);
}, [data]);

// Memoize callbacks to prevent re-renders
const handleClick = useCallback(() => {
  console.log('clicked');
}, []);

// Memoize components
export const ExpensiveComponent = memo<Props>(({ data }) => {
  return <div>{/* expensive rendering */}</div>;
});
```

### Code Splitting

```typescript
import { lazy, Suspense } from 'react';

const HeavyComponent = lazy(() => import('./HeavyComponent'));

function App() {
  return (
    <Suspense fallback={<LoadingSpinner />}>
      <HeavyComponent />
    </Suspense>
  );
}
```

### Virtual Scrolling

```typescript
import { useVirtualizer } from '@tanstack/react-virtual';

function LargeList({ items }: { items: string[] }) {
  const parentRef = useRef<HTMLDivElement>(null);

  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 50,
  });

  return (
    <div ref={parentRef} style={{ height: '400px', overflow: 'auto' }}>
      <div style={{ height: `${rowVirtualizer.getTotalSize()}px` }}>
        {rowVirtualizer.getVirtualItems().map(virtualRow => (
          <div key={virtualRow.index}>{items[virtualRow.index]}</div>
        ))}
      </div>
    </div>
  );
}
```

## Form Patterns

### Controlled Inputs

```typescript
const [formData, setFormData] = useState({
  name: '',
  email: '',
});

const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
  setFormData(prev => ({
    ...prev,
    [e.target.name]: e.target.value
  }));
};

<form onSubmit={handleSubmit}>
  <input
    name="name"
    value={formData.name}
    onChange={handleChange}
  />
  <input
    name="email"
    value={formData.email}
    onChange={handleChange}
  />
</form>
```

### Form Validation

```typescript
const [errors, setErrors] = useState<Record<string, string>>({});

const validate = (data: FormData): Record<string, string> => {
  const errors: Record<string, string> = {};
  
  if (!data.name) {
    errors.name = 'Name is required';
  }
  
  if (!data.email.includes('@')) {
    errors.email = 'Invalid email';
  }
  
  return errors;
};

const handleSubmit = (e: React.FormEvent) => {
  e.preventDefault();
  const validationErrors = validate(formData);
  
  if (Object.keys(validationErrors).length > 0) {
    setErrors(validationErrors);
    return;
  }
  
  // Submit form
};
```

## Testing Patterns

### Component Tests

```typescript
import { render, screen, fireEvent } from '@testing-library/react';
import { MyComponent } from './MyComponent';

describe('MyComponent', () => {
  it('renders with title', () => {
    render(<MyComponent title="Test" count={5} />);
    expect(screen.getByText('Test')).toBeInTheDocument();
    expect(screen.getByText('Count: 5')).toBeInTheDocument();
  });

  it('calls onAction when button clicked', () => {
    const onAction = jest.fn();
    render(<MyComponent title="Test" count={5} onAction={onAction} />);
    
    fireEvent.click(screen.getByText('Action'));
    expect(onAction).toHaveBeenCalledTimes(1);
  });
});
```

### Hook Tests

```typescript
import { renderHook, act } from '@testing-library/react';
import { useMyFeature } from './useMyFeature';

describe('useMyFeature', () => {
  it('updates value', () => {
    const { result } = renderHook(() => useMyFeature('initial'));
    
    expect(result.current.value).toBe('initial');
    
    act(() => {
      result.current.handleUpdate('updated');
    });
    
    expect(result.current.value).toBe('updated');
  });
});
```

## File Structure

```
frontend/src/
├── components/          # Reusable UI components
│   ├── common/         # Generic components (Button, Card, etc.)
│   ├── charts/         # Chart components
│   ├── dashboard/      # Dashboard-specific components
│   └── layout/         # Layout components (Header, Sidebar, etc.)
├── pages/              # Route components
├── contexts/           # React Context providers
├── hooks/              # Custom hooks
├── api/                # API service layer
├── utils/              # Utility functions
├── types/              # TypeScript type definitions
├── styles/             # Global styles
└── assets/             # Static assets (images, fonts, etc.)
```

## Best Practices

### ✅ Do

- Use TypeScript for all components and hooks
- Follow theme system with proper fallback chains
- Memoize expensive computations and callbacks
- Use React Query for server state
- Implement error boundaries
- Write tests for complex components
- Use semantic HTML
- Ensure accessibility (ARIA attributes, keyboard nav)

### ❌ Don't

- Hardcode colors without theme variables
- Use inline styles without fallbacks
- Put business logic in components
- Mutate state directly
- Use `any` type in TypeScript
- Skip error handling
- Create god components (>300 lines)
- Ignore accessibility

## Common Pitfalls

### Stale Closures

```typescript
// ❌ Wrong - stale closure
useEffect(() => {
  const interval = setInterval(() => {
    console.log(count); // Always logs initial count
  }, 1000);
  return () => clearInterval(interval);
}, []); // Missing count dependency

// ✅ Correct
useEffect(() => {
  const interval = setInterval(() => {
    console.log(count);
  }, 1000);
  return () => clearInterval(interval);
}, [count]); // Include dependency
```

### Unnecessary Re-renders

```typescript
// ❌ Wrong - creates new object on every render
<Component style={{ color: 'red' }} />

// ✅ Correct - memoize style object
const style = useMemo(() => ({ color: 'red' }), []);
<Component style={style} />
```

## Related Skills

- **architecture-context/** - System architecture  
- **visualization-patterns/** - Chart building
- **integration-patterns/** - API integration
- **backend-patterns/** - Backend services and APIs
- **design-system-patterns/** - Design tokens and theming

---

Last Updated: 2026-01-21

### ESLint (flat config)

```javascript
// eslint.config.mjs
import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import typescript from '@typescript-eslint/eslint-plugin';
import tsParser from '@typescript-eslint/parser';

export default [
  js.configs.recommended,
  {
    files: ['**/*.{ts,tsx,js,jsx}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      react,
      'react-hooks': reactHooks,
      '@typescript-eslint': typescript,
    },
    rules: {
      'react/react-in-jsx-scope': 'off',  // Not needed in React 18
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-unused-vars': ['warn', { 
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_' 
      }],
    },
    settings: {
      react: { version: 'detect' },
    },
  },
];
```

**VS Code Integration:**
```json
// .vscode/settings.json
{
  "eslint.enable": true,
  "eslint.experimental.useFlatConfig": true,
  "editor.codeActionsOnSave": {
    "source.fixAll.eslint": true
  }
}
```

### Vite Configuration

```typescript
// vite.config.ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tsconfigPaths from 'vite-tsconfig-paths';

export default defineConfig({
  plugins: [
    react(),
    tsconfigPaths(),  // Support tsconfig path aliases
  ],
  server: {
    port: 3000,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'esnext',
    sourcemap: true,
  },
});
```

### Environment Variables (Vite)

```typescript
// Access environment variables
const apiUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const isDev = import.meta.env.DEV;
const isProd = import.meta.env.PROD;

// Type definitions (vite-env.d.ts)
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_FEATURE_FLAG: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
```

### Dynamic Imports (Vite)

```typescript
// Replace require.context with import.meta.glob
// Webpack pattern (old)
const modules = require.context('./components', true, /\.tsx$/);

// Vite pattern (new)
const modules = import.meta.glob<{ default: ComponentType }>(
  './components/**/*.tsx',
  { eager: true }
);

// Process glob imports
for (const path of Object.keys(modules)) {
  const component = modules[path].default;
  // Use component
}
```

### Theme variable fallback

Use theme vars with fallbacks so styles still work when a variable is missing:

```typescript
// ✅ Theme var with fallback
const correctStyle = {
  color: 'var(--text-primary, hsl(0, 0%, 0%))',
  background: 'var(--bg-card, hsl(210, 7%, 95%))',
  border: '1px solid var(--border-primary, hsl(210, 7%, 82%))',
};

// ❌ No fallback — invisible if var missing
const wrongStyle = { color: 'var(--text-primary)' };
```

### Component patterns

**Chart Widget Pattern** (`frontend/src/components/charts/`):
```typescript
interface ChartWidgetProps {
  spec: ChartSpec;
  onInsightsClick?: () => void;
  widgetId?: string;
}

// ChartSpec structure (from skills_based_agent)
interface ChartSpec {
  type: 'line' | 'bar' | 'pie' | 'area' | 'composed';
  datasets: ChartDataset[];
  encodings: { x: string; y: string };
  meta: {
    title: string;
    x_dimension: string;
    breakdown_dimension?: string;
    metric: string;
    metric_label: string;
    series_keys?: string[];
    time_range_weeks?: number;
  };
}
```

**Dashboard Context Pattern** (`frontend/src/contexts/DashboardContext.tsx`):
```typescript
interface DashboardContextValue {
  dashboard: Dashboard | null;
  widgets: Widget[];
  updateWidget: (id: string, updates: Partial<Widget>) => void;
  addWidget: (widget: Widget) => void;
  removeWidget: (id: string) => void;
  saveDashboard: () => Promise<void>;
}
```

### Vite Environment Variables

Vite uses `VITE_`-prefixed environment variables:

```typescript
// Access environment variables
const apiUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const useMock = import.meta.env.VITE_USE_MOCK === 'true';
const isDev = import.meta.env.DEV;

// Environment variable types (frontend/src/vite-env.d.ts)
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_USE_MOCK: string;
  readonly VITE_IN_CHART_AI_ENABLED: string;
  readonly VITE_AGENT_ADMIN_ENABLED: string;
}
```

### Icon loading

Icons are loaded using Vite's `import.meta.glob` (not webpack's `require.context`):

```typescript
// frontend/src/components/Icon.tsx
const iconModules = import.meta.glob<{ default: string }>(
  '../../icons/*.{svg,png}',
  { eager: true }
);

const localIconUrlByName: Record<string, string> = {};
for (const path of Object.keys(iconModules)) {
  const baseName = path.split('/').pop()?.replace(/\.(svg|png)$/, '') || '';
  localIconUrlByName[baseName] = iconModules[path].default;
}
```

### Key frontend files

| File | Purpose |
|------|---------|
| `frontend/src/utils/reportChartBuilder.ts` | Core chart building logic (1900+ lines) |
| `frontend/src/contexts/DashboardContext.tsx` | Dashboard state management |
| `frontend/src/contexts/ThemeContext.tsx` | Theme configuration |
| `frontend/src/api/client.ts` | API client with auth interceptors |
| `frontend/src/components/charts/` | Recharts wrapper components |

### Module Federation

Use Module Federation for microfrontend architecture:

```typescript
// vite.config.ts (federation configuration)
import federation from '@originjs/vite-plugin-federation';

export default defineConfig({
  plugins: [
    react(),
    federation({
      name: 'my-app',
      filename: 'remoteEntry.js',
      exposes: {
        './MyComponent': './src/components/MyComponent',
      },
      remotes: {
        'remote-app': 'http://localhost:3001/assets/remoteEntry.js',
      },
      shared: {
        react: { singleton: true },
        'react-dom': { singleton: true },
      },
    }),
  ],
});
```

**Consuming remote components:**
```typescript
import { lazy } from 'react';

const RemoteComponent = lazy(() => import('remote-app/MyComponent'));

function App() {
  return (
    <Suspense fallback={<Loading />}>
      <RemoteComponent />
    </Suspense>
  );
}
```

### Key frontend stack

| Technology | Version | Purpose |
|------------|---------|---------|
| **Vite** | 5+ | Build tool and dev server |
| **Yarn** | v1.22.22 (classic) | Package manager |
| **Node** | v24+ | JavaScript runtime |
| **React** | 18+ | UI library |
| **TypeScript** | 5+ | Type safety |
| **ESLint** | 9+ (flat config) | Linting |
| Design tokens / CSS vars | — | Theming and styling |

---

Last Updated: 2026-01-21
