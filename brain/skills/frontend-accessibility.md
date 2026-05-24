---
name: accessibility
description: Use when designing, auditing, or implementing UI to meet WCAG (AA baseline) including keyboard support, ARIA patterns, focus management, contrast, reduced motion, and screen reader compatibility.
summary: WCAG AA compliance: keyboard navigation, screen reader, contrast, focus states, and ARIA patterns.
triggers: [accessibility, a11y, WCAG, screen reader, keyboard, ARIA, focus, contrast]
disable-model-invocation: true

---
# Accessibility (Definitive)

## Goal
Ship interfaces that are:
- **keyboard operable**
- **screen reader compatible**
- **visually perceivable** (contrast, focus, touch targets)
- **robust** across browsers and assistive tech

## Default target
- Aim for **WCAG 2.2 AA** unless explicitly scoped otherwise.

## Workflow
### 1) Audit quickly (find blockers first)
Blockers typically include:
- missing labels / accessible names
- non-keyboard-operable custom controls
- missing focus states / focus traps
- broken heading hierarchy / landmarks
- dynamic content not announced

### 2) Fix systematically (POUR)
- **Perceivable**: text alternatives, contrast, reflow.
- **Operable**: keyboard access, no traps, focus visible.
- **Understandable**: clear labels, consistent behavior, error messaging.
- **Robust**: semantic HTML first, ARIA only when needed.

### 3) Verify with assistive tech
Minimum coverage:
- **NVDA + Firefox (Windows)**
- **VoiceOver + Safari (macOS)**
- **VoiceOver + Safari (iOS)**

## Implementation rules (high-signal)
### Prefer semantic HTML
Use:
- `<button>` for actions
- `<a href>` for navigation
- `<label for>` for inputs
- `<nav> <main> <header> <footer>` for landmarks

### Accessible names
Every interactive element must have an accessible name:
- visible text, or
- `aria-label`, or
- `aria-labelledby`

### Keyboard and focus
- All actions reachable via Tab.
- Focus order matches reading order.
- Always include a visible `:focus-visible` style.
- Modals: trap focus, close on Escape, return focus to opener.

### Dynamic content announcements
Use `role="status"` / `aria-live` for changes users need to know.

### Reduced motion
Respect `prefers-reduced-motion`.

### Touch targets
Aim for 44x44px for primary controls; never below 24x24px.

## Checklist (ship gate)
- [ ] Tab through the page: no traps, no dead ends
- [ ] Visible focus on all interactive elements
- [ ] Forms: labels, errors announced, `aria-invalid` used when applicable
- [ ] Contrast meets AA (text 4.5:1; large text 3:1; UI components 3:1)
- [ ] Headings are hierarchical; landmarks present
- [ ] Screen reader pass on the key flows
