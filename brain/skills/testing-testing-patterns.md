---
name: testing-patterns
description: Document testing strategies, patterns, and best practices. Use when writing new tests, debugging failing tests, understanding test coverage, setting up test environments, implementing CI/CD pipelines, or refactoring code with test safety.
disable-model-invocation: true

---
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

    expect(result.current.data).toBeDefined();
    expect(result.current.error).toBeNull();
  });

  it('handles errors', async () => {
    // Mock API to return error
    vi.mocked(fetchPerformanceData).mockRejectedValue(
      new Error('API Error')
    );

    const { result } = renderHook(() =>
      usePerformanceData({ 
        companyId: 599, 
        dimensions: ['week'], 
        measures: ['spend'] 
      })
    );

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.error).toBeDefined();
    expect(result.current.data).toBeNull();
  });

  it('refetches when params change', async () => {
    const { result, rerender } = renderHook(
      ({ companyId }) => usePerformanceData({ 
        companyId, 
        dimensions: ['week'], 
        measures: ['spend'] 
      }),
      { initialProps: { companyId: 599 } }
    );

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    const firstData = result.current.data;

    // Change company ID
    rerender({ companyId: 600 });

    await waitFor(() => {
      expect(result.current.data).not.toBe(firstData);
    });
  });
});
```

### API Mocking with MSW

```typescript
// src/mocks/handlers.ts
import { http, HttpResponse } from 'msw';

export const handlers = [
  http.get('/api/campaigns', ({ request }) => {
    const url = new URL(request.url);
    const companyId = url.searchParams.get('company_id');

    return HttpResponse.json([
      { id: 1, name: 'Campaign 1', status: 'active' },
      { id: 2, name: 'Campaign 2', status: 'paused' },
    ]);
  }),

  http.post('/api/charts/generate', async ({ request }) => {
    const body = await request.json();

    return HttpResponse.json({
      type: 'line',
      datasets: [
        { name: '2025-01-01', value: 1000 },
      ],
      encodings: { x: 'name', y: 'value' },
      meta: {
        title: 'Generated Chart',
        x_dimension: 'week',
        metric: 'spend',
        metric_label: 'Spend ($)',
      },
    });
  }),

  http.post('/api/chat/stream', () => {
    // Return a stream
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode('data: {"content":"Hello","done":false}\n\n')
        );
        controller.enqueue(
          encoder.encode('data: {"done":true}\n\n')
        );
        controller.close();
      },
    });

    return new HttpResponse(stream, {
      headers: {
        'Content-Type': 'text/event-stream',
      },
    });
  }),
];

// src/mocks/server.ts
import { setupServer } from 'msw/node';
import { handlers } from './handlers';

export const server = setupServer(...handlers);

// src/test/setup.ts
import { beforeAll, afterEach, afterAll } from 'vitest';
import { server } from '../mocks/server';

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
```

### Context Provider Testing

```typescript
import { render, screen } from '@testing-library/react';
import { DashboardProvider, useDashboard } from './DashboardContext';

const TestComponent = () => {
  const { dashboard, updateWidget } = useDashboard();
  
  return (
    <div>
      <div data-testid="dashboard-name">{dashboard?.name}</div>
      <button onClick={() => updateWidget('widget-1', { title: 'Updated' })}>
        Update Widget
      </button>
    </div>
  );
};

describe('DashboardContext', () => {
  it('provides dashboard state', () => {
    const mockDashboard = {
      id: 1,
      name: 'Test Dashboard',
      layout: { widgets: [] },
    };

    render(
      <DashboardProvider initialDashboard={mockDashboard}>
        <TestComponent />
      </DashboardProvider>
    );

    expect(screen.getByTestId('dashboard-name')).toHaveTextContent('Test Dashboard');
  });

  it('updates widget', async () => {
    render(
      <DashboardProvider>
        <TestComponent />
      </DashboardProvider>
    );

    fireEvent.click(screen.getByText('Update Widget'));

    // Verify update occurred
    await waitFor(() => {
      // Assert on the updated state
    });
  });
});
```

## Backend Testing Patterns

### Unit Testing Services

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from generative_dashboards.services.campaign_service import CampaignService

@pytest.fixture
def mock_mcp_client():
    """Mock MCP client."""
    client = AsyncMock()
    client.get_campaigns.return_value = [
        {"id": 1, "name": "Campaign 1", "status": "active"},
        {"id": 2, "name": "Campaign 2", "status": "paused"},
    ]
    return client

@pytest.fixture
def mock_cache():
    """Mock cache."""
    cache = AsyncMock()
    cache.get.return_value = None
    return cache

@pytest.fixture
def campaign_service(mock_mcp_client, mock_cache):
    """Create campaign service with mocks."""
    return CampaignService(mock_mcp_client, mock_cache)

@pytest.mark.asyncio
async def test_get_campaigns(campaign_service, mock_mcp_client, mock_cache):
    """Test getting campaigns."""
    campaigns = await campaign_service.get_campaigns(company_id=599)
    
    assert len(campaigns) == 2
    assert campaigns[0]["name"] == "Campaign 1"
    
    # Verify MCP client was called
    mock_mcp_client.get_campaigns.assert_called_once_with(
        company_id=599,
        filters=None
    )
    
    # Verify result was cached
    mock_cache.set.assert_called_once()

@pytest.mark.asyncio
async def test_get_campaigns_uses_cache(campaign_service, mock_mcp_client, mock_cache):
    """Test that cached data is returned."""
    cached_data = [{"id": 1, "name": "Cached Campaign"}]
    mock_cache.get.return_value = cached_data
    
    campaigns = await campaign_service.get_campaigns(company_id=599)
    
    assert campaigns == cached_data
    
    # MCP should not be called when cached
    mock_mcp_client.get_campaigns.assert_not_called()

@pytest.mark.asyncio
async def test_calculate_campaign_metrics(campaign_service):
    """Test metrics calculation."""
    # Mock get_campaign_data to return test data
    campaign_service.get_campaign_data = AsyncMock(return_value={
        "spend": 10000,
        "impressions": 100000,
        "visits": 1000,
        "conversions": 50,
        "revenue": 15000
    })
    
    metrics = await campaign_service.calculate_campaign_metrics(campaign_id=1)
    
    assert metrics.cpm == 100.0  # (10000 / 100000) * 1000
    assert metrics.roas == 1.5   # 15000 / 10000
    assert metrics.conversion_rate == 5.0  # (50 / 1000) * 100
```

### Testing API Endpoints

```python
import pytest
from httpx import AsyncClient
from generative_dashboards.main import app

@pytest.fixture
async def client():
    """Create test client."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

@pytest.mark.asyncio
async def test_get_campaigns_endpoint(client):
    """Test campaigns endpoint."""
    response = await client.get("/api/campaigns?company_id=599")
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0

@pytest.mark.asyncio
async def test_create_campaign_endpoint(client):
    """Test creating a campaign."""
    payload = {
        "name": "Test Campaign",
        "company_id": 599,
        "status": "active"
    }
    
    response = await client.post("/api/campaigns", json=payload)
    
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Campaign"
    assert "id" in data

@pytest.mark.asyncio
async def test_endpoint_validation(client):
    """Test endpoint validates input."""
    invalid_payload = {
        "name": "",  # Empty name
        "company_id": -1,  # Invalid ID
    }
    
    response = await client.post("/api/campaigns", json=invalid_payload)
    
    assert response.status_code == 422  # Validation error
    data = response.json()
    assert "detail" in data

@pytest.mark.asyncio
async def test_endpoint_authentication(client):
    """Test endpoint requires authentication."""
    response = await client.get("/api/campaigns?company_id=599")
    
    # Without auth token
    if response.status_code == 401:
        assert True
    else:
        # With auth token
        assert response.status_code == 200
```

### Testing Skills

```python
import pytest
from unittest.mock import AsyncMock, patch
from generative_dashboards.skills.data_analysis.skill import DataAnalysisSkill

@pytest.fixture
def skill():
    """Create data analysis skill."""
    skill_loader = AsyncMock()
    return DataAnalysisSkill(skill_loader)

@pytest.mark.asyncio
async def test_skill_execution(skill):
    """Test skill executes successfully."""
    message = "What's my best performing campaign?"
    context = {"company_id": 599}
    
    result = await skill.execute(message, context)
    
    assert result.success is True
    assert result.content is not None
    assert "metadata" in result.__dict__

@pytest.mark.asyncio
async def test_skill_handles_errors(skill):
    """Test skill handles errors gracefully."""
    message = "Invalid query"
    context = {}
    
    with patch.object(skill, '_fetch_data', side_effect=Exception("API Error")):
        result = await skill.execute(message, context)
        
        assert result.success is False
        assert result.error is not None

@pytest.mark.asyncio
async def test_skill_composition(skill):
    """Test skill calls other skills."""
    skill_loader = AsyncMock()
    data_querying_skill = AsyncMock()
    data_querying_skill.execute.return_value = {
        "data": [{"campaign": "Campaign 1", "spend": 10000}]
    }
    skill_loader.get_skill.return_value = data_querying_skill
    
    skill.skill_loader = skill_loader
    
    message = "Show me campaign spend"
    context = {"company_id": 599}
    
    result = await skill.execute(message, context)
    
    # Verify data_querying was called
    skill_loader.get_skill.assert_called_with("data_querying")
    data_querying_skill.execute.assert_called_once()
```

## Integration Testing Patterns

### Database Integration Tests

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from generative_dashboards.models import Base, Dashboard

@pytest.fixture(scope="function")
def db_session():
    """Create test database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    yield session
    
    session.close()
    Base.metadata.drop_all(engine)

def test_create_dashboard(db_session):
    """Test creating a dashboard."""
    dashboard = Dashboard(
        user_id=1,
        company_id=599,
        name="Test Dashboard",
        layout={"widgets": []}
    )
    
    db_session.add(dashboard)
    db_session.commit()
    
    # Verify dashboard was created
    assert dashboard.id is not None
    
    # Query dashboard
    retrieved = db_session.query(Dashboard).filter_by(id=dashboard.id).first()
    assert retrieved is not None
    assert retrieved.name == "Test Dashboard"

def test_update_dashboard(db_session):
    """Test updating a dashboard."""
    dashboard = Dashboard(
        user_id=1,
        company_id=599,
        name="Test Dashboard",
        layout={"widgets": []}
    )
    db_session.add(dashboard)
    db_session.commit()
    
    # Update dashboard
    dashboard.name = "Updated Dashboard"
    db_session.commit()
    
    # Verify update
    retrieved = db_session.query(Dashboard).filter_by(id=dashboard.id).first()
    assert retrieved.name == "Updated Dashboard"
```

### End-to-End Testing (Playwright - Optional)

```typescript
import { test, expect } from '@playwright/test';

test.describe('Dashboard Page', () => {
  test('creates new dashboard', async ({ page }) => {
    await page.goto('http://localhost:5173/dashboards');
    
    // Click create button
    await page.click('button:has-text("Create Dashboard")');
    
    // Fill in form
    await page.fill('input[name="name"]', 'E2E Test Dashboard');
    await page.fill('textarea[name="description"]', 'Created by E2E test');
    
    // Submit form
    await page.click('button[type="submit"]');
    
    // Verify redirect to new dashboard
    await expect(page).toHaveURL(/\/dashboards\/\d+/);
    
    // Verify dashboard name is displayed
    await expect(page.locator('h1')).toHaveText('E2E Test Dashboard');
  });

  test('adds widget to dashboard', async ({ page }) => {
    await page.goto('http://localhost:5173/dashboards/1');
    
    // Click add widget button
    await page.click('button:has-text("Add Widget")');
    
    // Select chart type
    await page.click('text=Line Chart');
    
    // Configure chart
    await page.fill('input[name="title"]', 'Test Chart');
    await page.selectOption('select[name="metric"]', 'spend');
    
    // Add widget
    await page.click('button:has-text("Add to Dashboard")');
    
    // Verify widget appears
    await expect(page.locator('[data-testid="widget"]')).toContainText('Test Chart');
  });
});
```

## Test Coverage

### Measuring Coverage

```bash
# Frontend
npm run test:coverage

# Backend
pytest --cov=generative_dashboards --cov-report=html
```

### Coverage Targets

- **Unit Tests**: 80%+ coverage
- **Integration Tests**: Critical paths covered
- **E2E Tests**: Happy paths + critical error cases

## Testing Best Practices

### ✅ Do

- Write tests before or alongside code (TDD/BDD)
- Test behavior, not implementation
- Use descriptive test names
- Arrange-Act-Assert pattern
- Mock external dependencies
- Test error cases
- Keep tests fast and isolated
- Use fixtures for setup
- Clean up after tests

### ❌ Don't

- Test implementation details
- Write flaky tests
- Use real API calls in unit tests
- Skip cleanup
- Share state between tests
- Ignore failing tests
- Test private methods directly
- Have tests depend on each other

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
        with:
          node-version: '18'
      - run: npm ci
      - run: npm run test:coverage
      - uses: codecov/codecov-action@v3
        with:
          files: ./coverage/coverage-final.json

  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: pytest --cov --cov-report=xml
      - uses: codecov/codecov-action@v3
        with:
          files: ./coverage.xml
```

## Related Skills

- **frontend-patterns/** - Component patterns
- **backend-patterns/** - Service patterns
- **debugging-patterns/** - Debugging strategies

---

## Project-specific testing patterns

### Testing Skills

```python
import pytest
from unittest.mock import AsyncMock, patch
from generative_dashboards.services.skill_loader import SkillLoader
from generative_dashboards.skills.chart_generation.skill import ChartGenerationSkill

@pytest.fixture
def skill_loader():
    """Create skill loader with test skills."""
    loader = SkillLoader()
    loader.load_skills()
    return loader

@pytest.fixture
def chart_skill(skill_loader):
    """Get chart generation skill."""
    return skill_loader.get_skill("chart_generation")

@pytest.mark.asyncio
async def test_chart_generation_intent_extraction(chart_skill):
    """Test chart skill extracts intent correctly."""
    message = "Show me spend over time by creative"
    context = {"company_id": 599}
    
    intent = await chart_skill.extract_intent(message, context)
    
    assert intent["x_dimension"] == "week"
    assert intent["breakdown_dimension"] == "creative"
    assert intent["primary_metric"] == "spend"
    assert intent["chart_type"] == "line"

@pytest.mark.asyncio
async def test_chart_generation_with_mock_data(chart_skill):
    """Test chart generation with mocked MCP data."""
    mock_data = {
        "data": [
            {"week": "2025-01-01", "creative": "Creative A", "spend": 1000},
            {"week": "2025-01-01", "creative": "Creative B", "spend": 800},
            {"week": "2025-01-08", "creative": "Creative A", "spend": 1200},
            {"week": "2025-01-08", "creative": "Creative B", "spend": 900},
        ]
    }
    
    with patch.object(chart_skill, '_fetch_data', return_value=mock_data):
        result = await chart_skill.execute(
            "Show me spend over time by creative",
            {"company_id": 599}
        )
    
    assert result.success is True
    assert result.chart_spec["type"] == "line"
    assert len(result.chart_spec["meta"]["series_keys"]) == 2
```

### Testing MCP Queries

```python
import pytest
from unittest.mock import AsyncMock, patch
from generative_dashboards.services.analytics_query_service import query_mcp

@pytest.fixture
def mock_mcp_response():
    """Standard MCP response fixture."""
    return {
        "data": [
            {"week": "2025-01-01", "campaign": "Campaign A", "spend": 10000},
            {"week": "2025-01-08", "campaign": "Campaign A", "spend": 12000},
        ],
        "metadata": {
            "dimensions": ["week", "campaign"],
            "measures": ["spend"],
        }
    }

@pytest.mark.asyncio
async def test_query_mcp_success(mock_mcp_response):
    """Test successful MCP query."""
    with patch('generative_dashboards.services.analytics_query_service._call_mcp', 
               return_value=mock_mcp_response):
        result = await query_mcp(
            company_id=599,
            dimensions=["week", "campaign"],
            non_lifts=[{"measure": "spend"}],
            start_date="2025-01-01",
            end_date="2025-01-31"
        )
    
    assert "data" in result
    assert len(result["data"]) == 2

@pytest.mark.asyncio
async def test_query_mcp_uses_cache():
    """Test MCP query uses cache when available."""
    cached_data = {"data": [{"week": "2025-01-01", "spend": 1000}]}
    
    with patch('generative_dashboards.services.analytics_query_service._get_from_cache',
               return_value=cached_data):
        result = await query_mcp(
            company_id=599,
            dimensions=["week"],
            non_lifts=[{"measure": "spend"}],
            start_date="2025-01-01",
            end_date="2025-01-31"
        )
    
    assert result == cached_data

@pytest.mark.asyncio
async def test_query_mcp_fallback_to_cache_on_error():
    """Test MCP falls back to cache on connection error."""
    cached_data = {"data": [{"week": "2025-01-01", "spend": 1000}]}
    
    with patch('generative_dashboards.services.analytics_query_service._call_mcp',
               side_effect=ConnectionError("MCP unavailable")):
        with patch('generative_dashboards.services.analytics_query_service._get_from_cache',
                   return_value=cached_data):
            result = await query_mcp(
                company_id=599,
                dimensions=["week"],
                non_lifts=[{"measure": "spend"}],
                start_date="2025-01-01",
                end_date="2025-01-31"
            )
    
    assert result == cached_data
```

### Testing Calculator Tool

```python
import pytest
from generative_dashboards.services.calculator_tool import CalculatorTool

@pytest.fixture
def calculator():
    return CalculatorTool()

def test_calculate_cpm(calculator):
    """Test CPM calculation."""
    result = calculator.calculate_cpm(spend=10000, impressions=1000000)
    
    assert result["cpm"] == 10.0
    assert "formatted" in result

def test_calculate_cpm_zero_impressions(calculator):
    """Test CPM with zero impressions doesn't crash."""
    result = calculator.calculate_cpm(spend=10000, impressions=0)
    
    assert result["cpm"] == 0
    assert result.get("warning") is not None

def test_calculate_roas(calculator):
    """Test ROAS calculation."""
    result = calculator.calculate_roas(spend=10000, revenue=50000)
    
    assert result["roas"] == 5.0

def test_efficiency_ranking(calculator):
    """Test efficiency ranking."""
    data = [
        {"network": "ESPN", "spend": 10000, "impressions": 500000},
        {"network": "CNN", "spend": 8000, "impressions": 800000},
        {"network": "FOX", "spend": 12000, "impressions": 400000},
    ]
    
    rankings = calculator.efficiency_ranking(
        data=data,
        group_field="network",
        metric_type="cpm",
        n=10
    )
    
    # CNN should be first (lowest CPM)
    assert rankings[0]["network"] == "CNN"
    assert rankings[0]["cpm"] == 10.0  # 8000/800000*1000
```

### Testing Analytics Catalog

```python
import pytest
from generative_dashboards.services.analytics_catalog import (
    resolve_dimension_id,
    resolve_metric_id,
    dimension_to_mcp_field,
    expand_required_non_lifts,
    list_dimension_ids,
)

def test_resolve_dimension_exact():
    """Test exact dimension resolution."""
    assert resolve_dimension_id("campaign") == "campaign"
    assert resolve_dimension_id("network") == "network"

def test_resolve_dimension_plural():
    """Test plural dimension resolution."""
    assert resolve_dimension_id("campaigns") == "campaign"
    assert resolve_dimension_id("networks") == "network"

def test_resolve_dimension_mcp_suffix():
    """Test dimension with MCP suffix."""
    assert resolve_dimension_id("campaign.name") == "campaign"
    assert resolve_dimension_id("network.name") == "network"

def test_resolve_dimension_invalid():
    """Test invalid dimension returns None."""
    assert resolve_dimension_id("invalid_dimension") is None

def test_dimension_to_mcp_field():
    """Test dimension to MCP field mapping."""
    assert dimension_to_mcp_field("campaign") == "campaign.name"
    assert dimension_to_mcp_field("week") == "week"
    assert dimension_to_mcp_field("network") == "network.name"

def test_expand_required_non_lifts():
    """Test derived metric expansion."""
    # CPM requires spend and impressions
    required = expand_required_non_lifts(["cpm"])
    assert "spend" in required
    assert "impressions" in required
    
    # Conversion rate requires conversions and visits
    required = expand_required_non_lifts(["conversion_rate"])
    assert "conversions" in required
    assert "visits" in required
```

### Testing Chart Components

```typescript
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { ChartWidget } from './ChartWidget';

describe('ChartWidget', () => {
  const mockChartSpec = {
    type: 'line' as const,
    datasets: [
      { name: '2025-01-01', 'Creative A': 1000, 'Creative B': 800 },
      { name: '2025-01-08', 'Creative A': 1200, 'Creative B': 900 },
    ],
    encodings: { x: 'name', y: 'value' },
    meta: {
      title: 'Spend by Creative',
      x_dimension: 'week',
      breakdown_dimension: 'creative',
      metric: 'spend',
      metric_label: 'Spend ($)',
      series_keys: ['Creative A', 'Creative B'],
    },
  };

  it('renders chart title', () => {
    render(<ChartWidget spec={mockChartSpec} />);
    expect(screen.getByText('Spend by Creative')).toBeInTheDocument();
  });

  it('renders legend with series keys', () => {
    render(<ChartWidget spec={mockChartSpec} />);
    expect(screen.getByText('Creative A')).toBeInTheDocument();
    expect(screen.getByText('Creative B')).toBeInTheDocument();
  });

  it('handles empty datasets', () => {
    const emptySpec = { ...mockChartSpec, datasets: [] };
    render(<ChartWidget spec={emptySpec} />);
    expect(screen.getByText('No data available')).toBeInTheDocument();
  });

  it('applies theme colors', () => {
    render(<ChartWidget spec={mockChartSpec} />);
    const chart = screen.getByTestId('line-chart');
    // Verify chart uses theme color variables
    expect(chart).toHaveStyle({
      '--series-0-color': 'var(--chart-color-1)',
    });
  });
});
```

### Test fixtures for domain data

```python
# tests/fixtures/test_data.py

import pytest
from datetime import datetime, timedelta

@pytest.fixture
def weekly_spend_data():
    """Weekly spend data fixture."""
    return {
        "data": [
            {"week": "2025-01-06", "spend": 10000, "impressions": 1000000},
            {"week": "2025-01-13", "spend": 12000, "impressions": 1100000},
            {"week": "2025-01-20", "spend": 11000, "impressions": 1050000},
        ]
    }

@pytest.fixture
def campaign_performance_data():
    """Campaign performance fixture."""
    return {
        "data": [
            {
                "campaign": "Campaign A",
                "spend": 50000,
                "impressions": 5000000,
                "visits": 10000,
                "conversions": 500,
                "revenue": 75000,
            },
            {
                "campaign": "Campaign B",
                "spend": 30000,
                "impressions": 4000000,
                "visits": 8000,
                "conversions": 400,
                "revenue": 60000,
            },
        ]
    }

@pytest.fixture
def delivery_split_data():
    """Linear vs Streaming split fixture."""
    return {
        "data": [
            {"delivery": "Linear", "spend": 50000, "impressions": 5000000},
            {"delivery": "Streaming", "spend": 30000, "impressions": 8000000},
        ]
    }

@pytest.fixture
def network_performance_data():
    """Network performance fixture."""
    return {
        "data": [
            {"network": "ESPN", "spend": 15000, "impressions": 1500000, "cpm": 10.0},
            {"network": "CNN", "spend": 12000, "impressions": 1800000, "cpm": 6.67},
            {"network": "HGTV", "spend": 8000, "impressions": 1000000, "cpm": 8.0},
        ]
    }
```

### MSW Handlers for Company APIs

```typescript
// src/mocks/handlers.ts
import { http, HttpResponse } from 'msw';

export const apiHandlers = [
  // Charts endpoint
  http.post('/api/charts/generate', async ({ request }) => {
    const body = await request.json();
    
    return HttpResponse.json({
      type: 'line',
      datasets: [
        { name: '2025-01-01', value: 1000 },
        { name: '2025-01-08', value: 1200 },
      ],
      encodings: { x: 'name', y: 'value' },
      meta: {
        title: 'Generated Chart',
        x_dimension: 'week',
        metric: 'spend',
        metric_label: 'Spend ($)',
      },
    });
  }),

  // Performance data endpoint
  http.get('/api/performance/:companyId', ({ params }) => {
    return HttpResponse.json({
      data: [
        { week: '2025-01-01', spend: 10000, impressions: 1000000 },
        { week: '2025-01-08', spend: 12000, impressions: 1100000 },
      ],
    });
  }),

  // Skill execution endpoint
  http.post('/api/skills/execute', async ({ request }) => {
    const body = await request.json();
    
    return HttpResponse.json({
      success: true,
      skill: body.skill,
      result: {
        content: 'Skill execution result',
        metadata: { skill: body.skill },
      },
    });
  }),
];
```

### Key test files

| File | Purpose |
|------|---------|
| `tests/` | Backend test directory |
| `frontend/src/test/setup.ts` | Frontend test setup |
| `frontend/src/mocks/` | MSW mock handlers |
| `tests/fixtures/` | Shared test fixtures |

---

Last Updated: 2026-01-20
