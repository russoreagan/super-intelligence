# Testing Patterns Skill

## Purpose

Document testing strategies, patterns, and best practices for the Skills Agent Dashboard. This skill helps developers write effective tests for frontend, backend, and integration scenarios.

## When to Use

Use this skill when:
- Writing new tests
- Debugging failing tests
- Understanding test coverage
- Setting up test environments
- Implementing CI/CD pipelines
- Refactoring code with test safety

## Testing Stack

### Frontend Testing
- **Vitest** - Fast unit test runner
- **React Testing Library** - Component testing
- **MSW (Mock Service Worker)** - API mocking
- **Playwright** (optional) - E2E testing

### Backend Testing
- **pytest** - Python test framework
- **pytest-asyncio** - Async test support
- **pytest-mock** - Mocking utilities
- **httpx** - Async HTTP client for testing

## Frontend Testing Patterns

### Component Testing

```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { ChartWidget } from './ChartWidget';

describe('ChartWidget', () => {
  const mockChartSpec = {
    type: 'line' as const,
    datasets: [
      { name: '2025-01-01', value: 1000 },
      { name: '2025-01-08', value: 1200 },
    ],
    encodings: { x: 'name', y: 'value' },
    meta: {
      title: 'Test Chart',
      x_dimension: 'week',
      metric: 'spend',
      metric_label: 'Spend ($)',
    },
  };

  it('renders chart with title', () => {
    render(<ChartWidget spec={mockChartSpec} />);
    
    expect(screen.getByText('Test Chart')).toBeInTheDocument();
  });

  it('displays chart data', () => {
    render(<ChartWidget spec={mockChartSpec} />);
    
    // Recharts renders data in specific elements
    expect(screen.getByTestId('line-chart')).toBeInTheDocument();
  });

  it('handles empty data gracefully', () => {
    const emptySpec = {
      ...mockChartSpec,
      datasets: [],
    };
    
    render(<ChartWidget spec={emptySpec} />);
    
    expect(screen.getByText('No data available')).toBeInTheDocument();
  });

  it('calls onInsightsClick when insights button clicked', async () => {
    const onInsightsClick = vi.fn();
    
    render(
      <ChartWidget 
        spec={mockChartSpec} 
        onInsightsClick={onInsightsClick} 
      />
    );
    
    fireEvent.click(screen.getByLabelText('Get insights'));
    
    expect(onInsightsClick).toHaveBeenCalledTimes(1);
  });
});
```

### Hook Testing

```typescript
import { renderHook, act, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { usePerformanceData } from './usePerformanceData';

describe('usePerformanceData', () => {
  it('fetches data on mount', async () => {
    const { result } = renderHook(() => 
      usePerformanceData({ 
        companyId: 599, 
        dimensions: ['week'], 
        measures: ['spend'] 
      })
    );

    // Initially loading
    expect(result.current.isLoading).toBe(true);
    expect(result.current.data).toBeNull();

    // Wait for data
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
