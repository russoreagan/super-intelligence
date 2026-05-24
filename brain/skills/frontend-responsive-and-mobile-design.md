---
name: responsive-and-mobile-design
description: Use when designing responsive UI or mobile-friendly UX, including layouts that adapt across screen sizes, touch interactions, and native mobile patterns for Android (Material Design 3/Compose) and iOS (HIG/SwiftUI).
summary: Responsive layouts, mobile-first CSS, touch interactions, adaptive navigation, Android Material Design 3, and iOS Human Interface Guidelines.
triggers: [responsive, mobile, breakpoint, touch, screen size, adaptive, viewport, Material Design, SwiftUI, Compose]
disable-model-invocation: true

---
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

**Column and Row:**
```kotlin
Column(
    modifier = Modifier.padding(16.dp),
    verticalArrangement = Arrangement.spacedBy(12.dp),
    horizontalAlignment = Alignment.Start
) {
    Text(
        text = "Title",
        style = MaterialTheme.typography.headlineSmall
    )
    Text(
        text = "Subtitle",
        style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant
    )
}
```

**Lazy Lists with Adaptive Grids:**
```kotlin
LazyVerticalGrid(
    columns = GridCells.Adaptive(minSize = 150.dp),
    contentPadding = PaddingValues(16.dp),
    horizontalArrangement = Arrangement.spacedBy(12.dp),
    verticalArrangement = Arrangement.spacedBy(12.dp)
) {
    items(items) { item ->
        ItemCard(item = item)
    }
}
```

### Material 3 Navigation

**Bottom Navigation:**
```kotlin
NavigationBar {
    NavigationDestination.entries.forEach { destination ->
        NavigationBarItem(
            icon = { Icon(destination.icon, contentDescription = null) },
            label = { Text(destination.label) },
            selected = currentDestination == destination,
            onClick = { navController.navigate(destination.route) }
        )
    }
}
```

**Navigation Drawer:**
```kotlin
ModalNavigationDrawer(
    drawerState = drawerState,
    drawerContent = {
        ModalDrawerSheet {
            NavigationDrawerItem(
                icon = { Icon(Icons.Default.Home, null) },
                label = { Text("Home") },
                selected = true,
                onClick = { scope.launch { drawerState.close() } }
            )
        }
    }
) {
    // Main content
}
```

## iOS: Human Interface Guidelines

### HIG Principles
- **Clarity**: Content is legible, icons are precise
- **Deference**: UI helps users without competing
- **Depth**: Visual layers convey hierarchy

### SwiftUI Layouts

**Stack-Based:**
```swift
VStack(alignment: .leading, spacing: 12) {
    Text("Title")
        .font(.headline)
    Text("Subtitle")
        .font(.subheadline)
        .foregroundStyle(.secondary)
}
```

**Adaptive Grids:**
```swift
LazyVGrid(columns: [
    GridItem(.adaptive(minimum: 150, maximum: 200))
], spacing: 16) {
    ForEach(items) { item in
        ItemCard(item: item)
    }
}
```

### iOS Navigation

**NavigationStack (iOS 16+):**
```swift
NavigationStack(path: $path) {
    List(items) { item in
        NavigationLink(value: item) {
            ItemRow(item: item)
        }
    }
    .navigationTitle("Items")
    .navigationDestination(for: Item.self) { item in
        ItemDetailView(item: item)
    }
}
```

**TabView:**
```swift
TabView(selection: $selectedTab) {
    HomeView()
        .tabItem {
            Label("Home", systemImage: "house")
        }
        .tag(0)
    
    SearchView()
        .tabItem {
            Label("Search", systemImage: "magnifyingglass")
        }
        .tag(1)
}
```

### SF Symbols and Dynamic Type
```swift
// SF Symbols with multicolor
Image(systemName: "cloud.sun.fill")
    .symbolRenderingMode(.multicolor)

// Semantic fonts that scale with Dynamic Type
Text("Body text")
    .font(.body)
```

### iOS Materials
```swift
// Blur materials for overlays
Rectangle()
    .fill(.ultraThinMaterial)

// Vibrant backgrounds
Text("Overlay")
    .padding()
    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
```

## Accessibility Across Platforms

### Universal Requirements
- Minimum contrast ratio: 4.5:1 for text, 3:1 for UI
- Support system font scaling (Dynamic Type on iOS)
- Don't rely on color alone for meaning
- Ensure all interactive elements are reachable via keyboard/screen reader

### Platform-Specific
- **Android**: Follow Material accessibility guidelines, support TalkBack
- **iOS**: Support VoiceOver, ensure proper accessibility labels
- **Web**: Use semantic HTML, ARIA labels where needed

## Implementation Checklist
- [ ] Mobile-first CSS with progressive enhancement
- [ ] Touch targets meet minimum size (44px/48dp)
- [ ] Navigation adapts to screen size
- [ ] Typography scales with clamp() or platform settings
- [ ] Loading states for slow network conditions
- [ ] Keyboard doesn't obscure inputs
- [ ] Contrast meets accessibility requirements
- [ ] Tested on multiple screen sizes and devices
