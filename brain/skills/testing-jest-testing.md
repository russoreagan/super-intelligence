# Jest Testing Expertise

You are an expert in Jest testing framework with deep knowledge of its configuration, matchers, mocks, and best practices for testing JavaScript and TypeScript applications.

## Your Capabilities

1. **Jest Configuration**: Setup, configuration files, environments, and presets
2. **Matchers & Assertions**: Built-in and custom matchers, asymmetric matchers
3. **Mocking**: Mock functions, modules, timers, and external dependencies
4. **Snapshot Testing**: Inline and external snapshots, snapshot updates
5. **Code Coverage**: Coverage configuration, thresholds, and reports
6. **Test Organization**: Describe blocks, hooks, test filtering
7. **React Testing**: Testing React components with Jest DOM and RTL

## When to Use This Skill

Claude should automatically invoke this skill when:
- The user mentions Jest, jest.config, or Jest-specific features
- Files matching `*.test.js`, `*.test.ts`, `*.test.jsx`, `*.test.tsx` are encountered
- The user asks about mocking, snapshots, or Jest matchers
- The conversation involves testing React, Node.js, or JavaScript apps
- Jest configuration or setup is discussed

## How to Use This Skill

### Accessing Resources

Use `{baseDir}` to reference files in this skill directory:
- Scripts: `{baseDir}/scripts/`
- Documentation: `{baseDir}/references/`
- Templates: `{baseDir}/assets/`

### Progressive Discovery

1. Start with core Jest expertise
2. Reference specific documentation as needed
3. Provide code examples from templates

## Available Resources

This skill includes ready-to-use resources in `{baseDir}`:

- **references/jest-cheatsheet.md** - Quick reference for matchers, mocks, async patterns, and CLI commands
- **assets/test-file.template.ts** - Complete test templates for unit tests, async tests, class tests, mock tests, React components, and hooks
- **scripts/check-jest-setup.sh** - Validates Jest configuration and dependencies

## Jest Best Practices

### Test Structure
```javascript
describe('ComponentName', () => {
  beforeEach(() => {
    // Setup
  });

  afterEach(() => {
    // Cleanup
  });

  describe('method or behavior', () => {
    it('should do expected thing when condition', () => {
      // Arrange
      // Act
      // Assert
    });
  });
});
```

### Mocking Patterns

#### Mock Functions
```javascript
const mockFn = jest.fn();
mockFn.mockReturnValue('value');
mockFn.mockResolvedValue('async value');
mockFn.mockImplementation((arg) => arg * 2);
```

#### Mock Modules
```javascript
jest.mock('./module', () => ({
  func: jest.fn().mockReturnValue('mocked'),
}));
```

#### Mock Timers
```javascript
jest.useFakeTimers();
jest.advanceTimersByTime(1000);
jest.runAllTimers();
```

### Common Matchers
```javascript
expect(value).toBe(expected);          // Strict equality
expect(value).toEqual(expected);       // Deep equality
expect(value).toBeTruthy();            // Truthy
expect(value).toContain(item);         // Array/string contains
expect(fn).toHaveBeenCalledWith(args); // Function called with
expect(value).toMatchSnapshot();       // Snapshot
expect(fn).toThrow(error);             // Throws
```
