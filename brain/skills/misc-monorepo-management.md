---
name: monorepo-management
description: Use when setting up monorepos with Turborepo, Nx, or pnpm workspaces. Covers build optimization, caching, dependency management, CI/CD for monorepos, and package publishing.
summary: Monorepo management with Turborepo, Nx, pnpm workspaces, optimized builds, caching strategies, and CI/CD configuration.
triggers: [monorepo, turborepo, nx, pnpm workspaces, npm workspaces, lerna, build cache, workspace]
disable-model-invocation: true

---
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

### pnpm-workspace.yaml
```yaml
packages:
  - 'apps/*'
  - 'packages/*'
```

## Nx Setup

### nx.json
```json
{
  "$schema": "./node_modules/nx/schemas/nx-schema.json",
  "targetDefaults": {
    "build": {
      "dependsOn": ["^build"],
      "inputs": ["production", "^production"],
      "cache": true
    },
    "test": {
      "inputs": ["default", "^default"],
      "cache": true
    },
    "lint": {
      "inputs": ["default", "{workspaceRoot}/.eslintrc.json"],
      "cache": true
    }
  },
  "namedInputs": {
    "default": ["{projectRoot}/**/*", "sharedGlobals"],
    "production": [
      "default",
      "!{projectRoot}/**/*.spec.ts",
      "!{projectRoot}/tsconfig.spec.json"
    ],
    "sharedGlobals": ["{workspaceRoot}/tsconfig.base.json"]
  },
  "affected": {
    "defaultBase": "main"
  }
}
```

### Nx Commands
```bash
# Generate library
nx generate @nx/react:library ui --directory=packages/ui

# Run affected tests only
nx affected:test --base=main

# Dependency graph
nx graph

# Run task for specific project
nx build my-app

# Run task with parallelism
nx run-many --target=build --parallel=5
```

## Dependency Management

### Internal Dependencies
```json
// apps/web/package.json
{
  "dependencies": {
    "@repo/ui": "workspace:*",
    "@repo/config": "workspace:*"
  }
}
```

### Hoisting Control
```yaml
# .npmrc (pnpm)
public-hoist-pattern[]=*eslint*
public-hoist-pattern[]=*prettier*
shamefully-hoist=false
```

### Dependency Resolution
```json
// package.json - force consistent versions
{
  "pnpm": {
    "overrides": {
      "react": "^18.2.0",
      "typescript": "^5.0.0"
    }
  }
}
```

## CI/CD Optimization

### GitHub Actions with Turborepo
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2  # For change detection

      - uses: pnpm/action-setup@v2
        with:
          version: 9

      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: 'pnpm'

      - name: Install dependencies
        run: pnpm install --frozen-lockfile

      - name: Turborepo Cache
        uses: actions/cache@v4
        with:
          path: .turbo
          key: ${{ runner.os }}-turbo-${{ github.sha }}
          restore-keys: |
            ${{ runner.os }}-turbo-

      - name: Build
        run: pnpm build

      - name: Test
        run: pnpm test

      - name: Lint
        run: pnpm lint
```

### Remote Caching (Turborepo)
```bash
# Enable remote caching
npx turbo link

# Or self-hosted
turbo run build --api="https://cache.example.com" --token="xxx"
```

### Affected-Only CI
```yaml
# Run only affected targets
- name: Test Affected
  run: pnpm turbo run test --filter=...[origin/main]

# Or with Nx
- name: Test Affected
  run: npx nx affected:test --base=origin/main
```

## Build Caching

### Cache Inputs
```json
// turbo.json - control cache inputs
{
  "pipeline": {
    "build": {
      "inputs": [
        "src/**",
        "package.json",
        "tsconfig.json",
        "!**/*.test.ts"
      ]
    }
  }
}
```

### Environment Variables
```json
// turbo.json - include env vars in cache key
{
  "globalEnv": ["CI", "NODE_ENV"],
  "pipeline": {
    "build": {
      "env": ["DATABASE_URL", "API_KEY"]
    }
  }
}
```

## Shared Configurations

### ESLint Config Package
```javascript
// packages/eslint-config/index.js
module.exports = {
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'prettier',
  ],
  parser: '@typescript-eslint/parser',
  plugins: ['@typescript-eslint'],
  rules: {
    '@typescript-eslint/no-unused-vars': 'error',
    '@typescript-eslint/explicit-function-return-type': 'warn',
  },
};

// apps/web/.eslintrc.js
module.exports = {
  root: true,
  extends: ['@repo/eslint-config'],
};
```

### TypeScript Config
```json
// packages/tsconfig/base.json
{
  "$schema": "https://json.schemastore.org/tsconfig",
  "compilerOptions": {
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "declaration": true,
    "declarationMap": true
  }
}

// apps/web/tsconfig.json
{
  "extends": "@repo/tsconfig/nextjs.json",
  "include": ["src/**/*"],
  "exclude": ["node_modules"]
}
```

## Package Publishing

### Changesets Setup
```bash
pnpm add -D @changesets/cli
pnpm changeset init
```

```json
// .changeset/config.json
{
  "$schema": "https://unpkg.com/@changesets/config/schema.json",
  "changelog": "@changesets/cli/changelog",
  "commit": false,
  "fixed": [],
  "linked": [],
  "access": "restricted",
  "baseBranch": "main",
  "updateInternalDependencies": "patch"
}
```

```bash
# Create changeset
pnpm changeset

# Version packages
pnpm changeset version

# Publish
pnpm changeset publish
```

## Implementation Checklist
- [ ] Project structure follows apps/packages convention
- [ ] Turborepo or Nx configured with proper pipeline
- [ ] pnpm workspaces enabled
- [ ] Internal packages use workspace:* protocol
- [ ] Build caching configured and working
- [ ] Shared ESLint and TypeScript configs
- [ ] CI runs affected targets only
- [ ] Remote caching enabled for team
- [ ] Changesets for versioning (if publishing)
- [ ] Clean scripts remove all build artifacts
