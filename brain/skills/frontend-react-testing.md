# React Testing (Web)

## Problem Statement

React testing requires understanding component rendering, user interactions, and async state management. This skill covers Jest with React Testing Library patterns for web applications.

---

## Pattern: Zustand Store Testing

**Problem:** Store state persists between tests, causing flaky tests.

```typescript
import { useAppStore } from '@/stores/appStore';

const initialState = {
  items: [],
  loading: false,
  error: null,
};

describe('App Store', () => {
  // Reset store before each test
  beforeEach(() => {
    useAppStore.setState(initialState, true); // true = replace entire state
  });

  it('adds item to store', async () => {
    const store = useAppStore.getState();

    await store.addItem({ id: '1', name: 'Test' });

    expect(useAppStore.getState().items).toHaveLength(1);
  });

  it('handles loading state', async () => {
    const store = useAppStore.getState();

    const loadPromise = store.fetchItems();
    expect(useAppStore.getState().loading).toBe(true);

    await loadPromise;
    expect(useAppStore.getState().loading).toBe(false);
  });
});
```

**Key points:**
- Use `setState(initialState, true)` to replace (not merge) state
- Get fresh state with `getState()` after async operations
- Don't rely on component re-renders in store tests

---

## Pattern: Async Store Operations

**Problem:** Testing async Zustand actions with proper waiting.

```typescript
import { act, waitFor } from '@testing-library/react';

it('loads data correctly', async () => {
  const store = useAppStore.getState();

  // Wrap async store operations in act
  await act(async () => {
    await store.loadData('123');
  });

  // Verify state after async completes
  await waitFor(() => {
    const state = useAppStore.getState();
    expect(Object.keys(state.data).length).toBeGreaterThan(0);
  });
});

// For complex flows, verify each step
it('completes multi-step flow', async () => {
  const store = useAppStore.getState();

  // Step 1
  await act(async () => {
    await store.loadItems();
  });
  expect(useAppStore.getState().items).toBeDefined();

  // Step 2
  await act(async () => {
    await store.processItems();
  });
  expect(useAppStore.getState().processed).toBe(true);
});
```

---

## Pattern: Component Testing

```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

describe('ItemCard', () => {
  const mockItem = {
    id: '1',
    title: 'Test Item',
    price: 99.99,
  };

  it('displays item data', () => {
    render(<ItemCard item={mockItem} />);

    expect(screen.getByText('Test Item')).toBeInTheDocument();
    expect(screen.getByText('$99.99')).toBeInTheDocument();
  });

  it('calls onClick when clicked', async () => {
    const user = userEvent.setup();
    const onClick = jest.fn();

    render(<ItemCard item={mockItem} onClick={onClick} />);

    await user.click(screen.getByRole('button'));

    expect(onClick).toHaveBeenCalledWith(mockItem.id);
  });

  it('shows loading state', () => {
    render(<ItemCard item={mockItem} loading />);
