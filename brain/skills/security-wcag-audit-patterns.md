# WCAG Audit Patterns

Comprehensive guide to auditing web content against WCAG 2.2 guidelines with actionable remediation strategies.

## When to Use This Skill

- Conducting accessibility audits
- Fixing WCAG violations
- Implementing accessible components
- Preparing for accessibility lawsuits
- Meeting ADA/Section 508 requirements
- Achieving VPAT compliance

## Core Concepts

### 1. WCAG Conformance Levels

| Level | Description | Required For |
|-------|-------------|--------------|
| **A** | Minimum accessibility | Legal baseline |
| **AA** | Standard conformance | Most regulations |
| **AAA** | Enhanced accessibility | Specialized needs |

### 2. POUR Principles

```
Perceivable:  Can users perceive the content?
Operable:     Can users operate the interface?
Understandable: Can users understand the content?
Robust:       Does it work with assistive tech?
```

### 3. Common Violations by Impact

```
Critical (Blockers):
├── Missing alt text for functional images
├── No keyboard access to interactive elements
├── Missing form labels
└── Auto-playing media without controls

Serious:
├── Insufficient color contrast
├── Missing skip links
├── Inaccessible custom widgets
└── Missing page titles

Moderate:
├── Missing language attribute
├── Unclear link text
├── Missing landmarks
└── Improper heading hierarchy
```

## Audit Checklist

### Perceivable (Principle 1)

```markdown
## 1.1 Text Alternatives

### 1.1.1 Non-text Content (Level A)
- [ ] All images have alt text
- [ ] Decorative images have alt=""
- [ ] Complex images have long descriptions
- [ ] Icons with meaning have accessible names
- [ ] CAPTCHAs have alternatives

Check:
```html
<!-- Good -->
<img src="chart.png" alt="Sales increased 25% from Q1 to Q2">
<img src="decorative-line.png" alt="">

<!-- Bad -->
<img src="chart.png">
<img src="decorative-line.png" alt="decorative line">
```

## 1.2 Time-based Media

### 1.2.1 Audio-only and Video-only (Level A)
- [ ] Audio has text transcript
- [ ] Video has audio description or transcript

### 1.2.2 Captions (Level A)
- [ ] All video has synchronized captions
- [ ] Captions are accurate and complete
- [ ] Speaker identification included

### 1.2.3 Audio Description (Level A)
- [ ] Video has audio description for visual content

## 1.3 Adaptable

### 1.3.1 Info and Relationships (Level A)
- [ ] Headings use proper tags (h1-h6)
- [ ] Lists use ul/ol/dl
- [ ] Tables have headers
- [ ] Form inputs have labels
- [ ] ARIA landmarks present

Check:
```html
<!-- Heading hierarchy -->
<h1>Page Title</h1>
  <h2>Section</h2>
    <h3>Subsection</h3>
  <h2>Another Section</h2>

<!-- Table headers -->
<table>
  <thead>
    <tr><th scope="col">Name</th><th scope="col">Price</th></tr>
  </thead>
</table>
```

### 1.3.2 Meaningful Sequence (Level A)
- [ ] Reading order is logical
- [ ] CSS positioning doesn't break order
- [ ] Focus order matches visual order

### 1.3.3 Sensory Characteristics (Level A)
- [ ] Instructions don't rely on shape/color alone
- [ ] "Click the red button" → "Click Submit (red button)"

## 1.4 Distinguishable
