# Monorepo Management (Unified)

## Goal
Build efficient, scalable monorepos with optimized builds, proper caching, shared dependencies, and streamlined CI/CD.

## When to Use
- Setting up new monorepo projects
- Migrating from multi-repo to monorepo
- Optimizing build and test performance
- Managing shared dependencies
- Implementing code sharing strategies
- Setting up CI/CD for monorepos

## Why Monorepos?

**Advantages:**
- Shared code and dependencies
- Atomic commits across projects
- Consistent tooling and standards
- Easier refactoring
- Better code visibility

**Challenges:**
- Build performance at scale
- CI/CD complexity
- Access control
- Large Git repository

## Tool Comparison

| Tool     | Best For                    | Key Feature            |
| -------- | --------------------------- | ---------------------- |
| Turborepo| Most JS/TS monorepos        | Zero-config caching    |
| Nx       | Complex enterprise projects | Computation caching    |
| pnpm     | Dependency management       | Efficient disk space   |
| Lerna    | Legacy projects             | Package publishing     |

## Turborepo Setup

### Project Structure
```
my-monorepo/
├── apps/
│   ├── web/           # Next.js app
│   └── docs/          # Documentation site
├── packages/
│   ├── ui/            # Shared UI components
│   ├── config/        # Shared configurations
│   └── tsconfig/      # Shared TypeScript configs
├── turbo.json         # Turborepo configuration
├── package.json       # Root package.json
└── pnpm-workspace.yaml
```

### turbo.json
```json
{
  "$schema": "https://turbo.build/schema.json",
  "globalDependencies": ["**/.env.*local"],
  "pipeline": {
    "build": {
      "dependsOn": ["^build"],
      "outputs": ["dist/**", ".next/**", "!.next/cache/**"]
    },
    "test": {
      "dependsOn": ["build"],
      "outputs": ["coverage/**"]
    },
    "lint": {
      "outputs": []
    },
    "dev": {
      "cache": false,
      "persistent": true
    },
    "type-check": {
      "dependsOn": ["^build"],
      "outputs": []
    }
  }
}
```

### Root package.json
```json
{
  "name": "my-monorepo",
  "private": true,
  "workspaces": ["apps/*", "packages/*"],
  "scripts": {
    "build": "turbo run build",
    "dev": "turbo run dev",
    "test": "turbo run test",
    "lint": "turbo run lint",
    "format": "prettier --write \"**/*.{ts,tsx,md}\"",
    "clean": "turbo run clean && rm -rf node_modules"
  },
  "devDependencies": {
    "turbo": "^2.0.0",
    "prettier": "^3.0.0",
    "typescript": "^5.0.0"
  },
  "packageManager": "pnpm@9.0.0"
}
```

### Package Configuration
```json
// packages/ui/package.json
{
  "name": "@repo/ui",
  "version": "0.0.0",
  "private": true,
  "main": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "exports": {
    ".": {
      "import": "./dist/index.js",
      "types": "./dist/index.d.ts"
    },
    "./button": {
      "import": "./dist/button.js",
      "types": "./dist/button.d.ts"
    }
  },
  "scripts": {
    "build": "tsup src/index.ts --format esm --dts",
    "dev": "tsup src/index.ts --format esm --dts --watch",
    "clean": "rm -rf dist"
  },
  "peerDependencies": {
    "react": "^18.0.0"
  }
}
```
