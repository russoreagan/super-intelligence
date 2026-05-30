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
