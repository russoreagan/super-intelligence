---
name: frontend-design
description: Use when designing or building frontend UI (components, pages, flows) and you need a polished, production-grade result with strong visual hierarchy, responsive behavior, and thoughtful interaction states.
summary: Production-grade UI with hierarchy, responsiveness, interaction states, and performance.
triggers: [UI, frontend, component, page, layout, polish, design, React, CSS]
disable-model-invocation: true

---
# Frontend Design (Definitive)

## Goal
Produce **production-grade UI** that feels intentionally designed:\n+- clear hierarchy and layout\n+- strong typography + spacing\n+- responsive from mobile to desktop\n+- crisp interaction states and feedback\n+- performance-aware component architecture\n+\n+This skill intentionally **folds in the basics** of responsive + interaction design. Deep design-system/theming work lives in the dedicated design-system canonical skill.\n+
## When to use
Use when the user asks to:\n+- build a component/page/app UI\n+- “make it look better / more polished”\n+- apply a specific visual direction (minimal, editorial, playful, etc.)\n+- implement responsive behavior\n+- add microinteractions, transitions, loading states\n+\n+## Workflow (high signal, low fluff)
### 1) Set a clear design direction
Before coding, decide:\n+- **Audience + job**: who uses this, what outcome?\n+- **Tone**: pick a distinct direction (minimal, editorial, utilitarian, playful, luxury, etc.).\n+- **Differentiator**: one memorable signature (layout motif, type pairing, interaction moment).\n+- **Constraints**: a11y, performance, platform, dark mode.\n+\n+### 2) Build hierarchy first
- Identify 1–2 primary actions and one primary information block.\n+- Use spacing and type scale to make the hierarchy obvious.\n+- Prefer fewer, stronger decisions over “many okay” choices.\n+\n+### 3) Component structure (practical)
- Keep components small and composable.\n+- Prefer “layout + slots/children” patterns for reuse.\n+- Avoid styling that leaks outside component boundaries.\n+\n+### 4) Responsiveness (baseline)
- Default to **mobile-first**.\n+- Use **fluid** typography/spacing where appropriate (`clamp`).\n+- Use **container queries** for component-level responsiveness when available.\n+- Prefer intrinsic layouts: `minmax`, `auto-fit`, `aspect-ratio`.\n+\n+### 5) Interaction + feedback (baseline)
- Every interactive element needs: hover, active, focus-visible, disabled.\n+- Always include loading + empty + error states for async UI.\n+- Motion is for communication: feedback, orientation, focus, continuity.\n+- Respect `prefers-reduced-motion`.\n+\n+### 6) Performance sanity checks (UI-facing)
- Avoid unnecessary re-renders in large trees.\n+- Lazy-load heavy UI and non-critical modules.\n+- Avoid waterfall async where possible (parallelize).\n+\n+## Checklists
### Visual checklist
- Hierarchy reads in 3 seconds.\n+- Type scale is consistent; body text is readable.\n+- Spacing system feels intentional (use a scale).\n+- Color contrast is acceptable (AA baseline).\n+- Components align to a grid; nothing “almost lines up”.\n+\n+### Responsive checklist
- Layout works at ~360px, ~768px, ~1024px.\n+- Touch targets are large enough.\n+- No horizontal overflow.\n+- Images don’t stretch; aspect ratio holds.\n+\n+### Interaction checklist
- Focus-visible is obvious.\n+- Motion is subtle and purposeful.\n+- Loading states don’t cause layout jump.\n+- Errors are recoverable and clearly messaged.\n+\n+## References (use when needed)
- For deep component library + API patterns: see the canonical design-system skill.\n+- For heavy React/Next performance rules: prefer the Vercel ruleset sources (don’t copy repo-specific commands).\n+
