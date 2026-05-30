# Playwright Testing Expertise

You are an expert in Playwright testing framework with deep knowledge of browser automation, selectors, page objects, and best practices for end-to-end testing.

## Your Capabilities

1. **Playwright Configuration**: Projects, browsers, reporters, and fixtures
2. **Locators & Selectors**: Role-based, text, CSS, and chained locators
3. **Page Object Model**: Organizing tests with page objects
4. **Assertions**: Built-in assertions, custom matchers, auto-waiting
5. **Test Fixtures**: Built-in and custom fixtures, test isolation
6. **Debugging**: Traces, screenshots, videos, and Playwright Inspector
7. **API Testing**: Request fixtures and API testing capabilities

## When to Use This Skill

Claude should automatically invoke this skill when:
- The user mentions Playwright, playwright.config, or Playwright features
- Files matching `*.spec.ts` in e2e, tests, or playwright directories are encountered
- The user asks about locators, page objects, or browser automation
- E2E or integration testing is discussed
- Browser testing configuration is needed

## How to Use This Skill

### Accessing Resources

Use `{baseDir}` to reference files in this skill directory:
- Scripts: `{baseDir}/scripts/`
- Documentation: `{baseDir}/references/`
- Templates: `{baseDir}/assets/`

## Available Resources

This skill includes ready-to-use resources in `{baseDir}`:

- **references/playwright-cheatsheet.md** - Quick reference for locators, assertions, actions, and CLI commands
- **assets/page-object.template.ts** - Complete Page Object Model template with base class and examples
- **scripts/check-playwright-setup.sh** - Validates Playwright configuration and browser installation

## Playwright Best Practices

### Test Structure
```typescript
import { test, expect } from '@playwright/test';

test.describe('Contact Form', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/contact');
  });

  test('should show success message after form submission', async ({ page }) => {
    // Arrange
    await page.getByLabel('Name').fill('Test User');
    await page.getByLabel('Email').fill('test@example.com');
    await page.getByLabel('Message').fill('Hello, this is a test message.');

    // Act
    await page.getByRole('button', { name: 'Submit' }).click();

    // Assert
    await expect(page.getByText('Thank you for your message')).toBeVisible();
    await expect(page.getByLabel('Name')).toBeEmpty();
  });
});
```

### Locator Best Practices

#### Preferred Locators (Most Resilient)
```typescript
// Role-based (best)
page.getByRole('button', { name: 'Submit' });
page.getByRole('textbox', { name: 'Email' });
page.getByRole('heading', { level: 1 });

// Label-based
page.getByLabel('Email address');
page.getByPlaceholder('Enter your email');

// Text-based
page.getByText('Welcome');
page.getByTitle('Close');
```

#### Chaining Locators
```typescript
page.getByRole('listitem')
  .filter({ hasText: 'Product 1' })
  .getByRole('button', { name: 'Add' });
```

#### Test IDs (Last Resort)
```typescript
page.getByTestId('submit-button');
```
