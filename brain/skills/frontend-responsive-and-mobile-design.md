# Responsive & Mobile Design (Definitive)

## Goal
Build interfaces that remain usable and polished across screen sizes, input modes, and device constraints—including native mobile apps for Android and iOS.

## When to Use
- Designing responsive web layouts
- Building mobile-first CSS/React applications
- Implementing adaptive navigation patterns
- Creating native Android apps with Material Design 3
- Creating native iOS apps following Human Interface Guidelines
- Ensuring touch-friendly interactions across devices

## Responsive Web Foundations

### Mobile-First Approach
Start with the smallest screen and progressively enhance:
```css
/* Base styles (mobile) */
.container {
  padding: 16px;
}

/* Tablet and up */
@media (min-width: 768px) {
  .container {
    padding: 24px;
    max-width: 720px;
  }
}

/* Desktop */
@media (min-width: 1024px) {
  .container {
    padding: 32px;
    max-width: 1200px;
  }
}
```

### Fluid Typography with clamp()
```css
/* Fluid font that scales between 16px and 24px */
h1 {
  font-size: clamp(1rem, 2vw + 1rem, 1.5rem);
}

/* Fluid spacing */
.section {
  padding: clamp(1rem, 5vw, 3rem);
}
```

### Container Queries (component-level responsiveness)
```css
.card-container {
  container-type: inline-size;
}

@container (min-width: 400px) {
  .card {
    display: grid;
    grid-template-columns: 1fr 2fr;
  }
}
```

## Layout Patterns

### CSS Grid for 2D Layouts
```css
/* Responsive grid with auto-fit */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 24px;
}
```

### Flexbox for 1D Layouts
```css
.nav {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

/* Stack on mobile, row on larger screens */
@media (max-width: 640px) {
  .nav {
    flex-direction: column;
  }
}
```

### Adaptive Navigation Patterns
| Screen Size | Pattern              |
| ----------- | -------------------- |
| Mobile      | Bottom tabs or hamburger drawer |
| Tablet      | Side navigation rail |
| Desktop     | Full sidebar or top navigation |

## Mobile UX Essentials

### Touch Targets
- **Minimum**: 44x44px (iOS) / 48x48dp (Android)
- **Recommended**: 48x48px or larger
- **Spacing**: At least 8px between targets

### Gesture Affordances
- Swipe actions should have visual hints
- Pull-to-refresh: show loading indicator
- Long-press: provide haptic feedback when available
- Edge swipes: reserve for system gestures

### Mobile Network Considerations
- Show loading states immediately
- Use skeleton screens instead of spinners
- Implement progressive image loading
- Optimize for offline-first when possible

### Keyboard-Safe Forms
- Avoid inputs being hidden behind keyboard
- Use appropriate input types (`type="email"`, `inputmode="numeric"`)
- Place submit buttons where they remain visible

## Android: Material Design 3

### Material Design 3 Principles
- **Personalization**: Dynamic color adapts UI to user's wallpaper
- **Accessibility**: Tonal palettes ensure sufficient contrast
- **Large Screens**: Responsive layouts for tablets and foldables

### Jetpack Compose Layouts
