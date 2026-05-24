---
name: design-system-patterns
description: Use when creating or updating design tokens, theming, or component styling conventions, especially when ensuring theme compliance and consistent token fallback behavior across the app.
summary: Design tokens, theming, component styling conventions, and theme compliance rules.
triggers: [design system, token, theme, styling, CSS variable, color palette]
disable-model-invocation: true

---
# Design System Patterns (Definitive)

## Goal
Create a consistent, scalable design system with:\n+- clear token hierarchy (theme → design tokens → last-resort values)\n+- predictable component styling conventions\n+- safe patterns for theming + variants\n+- guardrails that prevent “random CSS” and theme breakage\n+\n+## Token precedence (MANDATORY)
Always follow this fallback chain:
1. **Theme variables** (user-customizable CSS vars)
2. **Design tokens** (semantic colors/spacing) as fallback
3. **Hardcoded** only when no token exists

### Required pattern
```ts
// Theme var with fallback
color: 'var(--text-primary, hsl(0, 0%, 0%))'

// Full chain for complex values
background: 'var(--bg-card, var(--color-surface, hsl(210, 7%, 95%)))'
```

## Theme compliance (frontend work)
When implementing new UI:\n+- consult the Theme Manager AI **before** writing styling\n+- use only recommended theme variables\n+- have Theme Manager AI review before marking work complete\n+\n+## Component styling rules (app guardrails)
### Glass effects
- Glass is for **cards/overlays**, not buttons.\n+
### Borders
- Buttons always use **solid borders** (do not respect `--widget-borders`).\n+- Cards/containers can respect the border toggle.\n+
## Token hierarchy (recommended)
1. **Primitive tokens**: raw colors, spacing, radii.\n+2. **Semantic tokens**: intent-based (`text-primary`, `bg-surface`, `border-primary`).\n+3. **Component tokens**: `button-bg`, `card-border`, etc.\n+\n+## Practical workflow
1. Define/confirm semantic tokens first.\n+2. Implement components using semantic tokens.\n+3. Only introduce component tokens when needed.\n+4. Enforce fallbacks in every CSS var usage.\n+5. Add variants via a consistent system (e.g. `variant`/`size` matrix).\n+\n+## Checklist (review)
- No hardcoded colors unless proven necessary.\n+- Every `var(--x)` has a fallback.\n+- Buttons: solid borders; no glass.\n+- Cards: respect border toggle.\n+- Theme switching doesn’t break contrast.\n+
