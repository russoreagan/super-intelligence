---
name: data-patterns
description: Document data models, query patterns, and data integration. Use when querying data sources for analytics/performance data, working with dimensions and metrics from a data catalog, understanding data models and transformations, optimizing data access patterns, or building queries for charts and dashboards.
disable-model-invocation: true

---
# Data Patterns Skill

## Purpose

Document **data models, query patterns, and data integration**. This skill helps developers work with analytics data, database schemas, and performance data.

## When to Use

Use this skill when:
- Querying data sources for analytics/performance data
- Working with dimensions and metrics from a data catalog
- Understanding data models and transformations
- Optimizing data access patterns
- Building queries for charts and dashboards

## Analytics Catalog Pattern

**A data catalog serves as the canonical source of truth for all dimensions and metrics.**

### Dimension Categories (Example)

| Category | Dimensions | Description |
|----------|------------|-------------|
| **Time** | day, week, month | Time-based aggregation |
| **Core** | campaign, creative, channel | Primary business entities |
| **Delivery** | inventory_type, device, tier | Delivery method/platform |
| **Geo** | region, market | Geographic segmentation |
| **Demographics** | age, gender, income | User demographics |

### Metric Categories

| Category | Metrics | Description |
|----------|---------|-------------|
| **Raw** | spend, impressions, visits, conversions, revenue | Directly queryable from data source |
| **Derived** | conversion_rate, response_rate | Calculated from raw metrics using calculator tools |
| **Computed** | lift, incremental_value | Requires additional attribution logic |

### Data Query Structure (Standard Pattern)

```python
# Standard data query pattern
result = await data_service.query(
    entity_id=123,  # Primary entity identifier
    dimensions=["week", "campaign"],  # Dimension IDs from catalog
    metrics=[
        {"measure": "spend"},
        {"measure": "impressions"},
        {"measure": "conversions"},
    ],
    start_date="2025-01-01",
    end_date="2025-01-31",
)
```

### Derived Metrics (Calculator Pattern)

```python
# ❌ WRONG: Never ask LLM to calculate
prompt = f"Calculate CPA from spend={spend} and conversions={conversions}"

# ✅ CORRECT: Use calculator tools
def calculate_cpa(spend: float, conversions: int) -> float:
    """Deterministic CPA calculation."""
    return spend / conversions if conversions > 0 else 0.0

# Apply to DataFrame
data["cpa"] = calculate_cpa(data["spend"], data["conversions"])
```

### Data Query API Structure

**Standard dimensions/metrics structure:**

```python
{
    "dimensions": ["day", "campaign", "channel"],
    "metrics": [
        {"measure": "spend"},
        {"measure": "impressions"},
        {"measure": "conversions"}
    ],
    "filters": [
        {"field": "campaign.name", "op": "in", "value": ["Campaign A"]}
    ]
}
```

## Database Schema

### Core Tables

Located in `/schema/migrations/`:

#### Dashboards Table

```sql
CREATE TABLE dashboards (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    layout JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_dashboards_user_id ON dashboards(user_id);
CREATE INDEX idx_dashboards_company_id ON dashboards(company_id);
```

#### Chat Sessions Table

```sql
CREATE TABLE chat_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    title VARCHAR(255),
    messages JSONB NOT NULL DEFAULT '[]',
    context JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_chat_sessions_user_id ON chat_sessions(user_id);
CREATE INDEX idx_chat_sessions_updated_at ON chat_sessions(updated_at DESC);
```

#### Insights & Recommendations

```sql
CREATE TABLE ci_dashboard_insights (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    category VARCHAR(50) NOT NULL,  -- 'strategy', 'creative', 'spend', 'performance', 'opportunity'
    summary TEXT NOT NULL,
    full_text TEXT NOT NULL,
    advertisers JSONB NOT NULL DEFAULT '[]',
    metrics JSONB NOT NULL DEFAULT '{}',
    priority VARCHAR(20) DEFAULT 'medium',  -- 'high', 'medium', 'low'
    date_generated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_range_start DATE,
    date_range_end DATE
);

CREATE INDEX idx_ci_insights_company ON ci_dashboard_insights(company_id);
CREATE INDEX idx_ci_insights_category ON ci_dashboard_insights(category);
CREATE INDEX idx_ci_insights_priority ON ci_dashboard_insights(priority);
CREATE INDEX idx_ci_insights_date ON ci_dashboard_insights(date_generated DESC);
```

#### User Feedback

```sql
CREATE TABLE user_feedback (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    feedback_type VARCHAR(50) NOT NULL,  -- 'thumbs_up', 'thumbs_down', 'comment'
    content TEXT,
    context JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_session FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_user_feedback_user_id ON user_feedback(user_id);
CREATE INDEX idx_user_feedback_session_id ON user_feedback(session_id);
```

## Data Models

### TypeScript Types (Frontend)

```typescript
// Dashboard models
export interface Dashboard {
  id: number;
  user_id: number;
  company_id: number;
  name: string;
  description?: string;
  layout: DashboardLayout;
  created_at: string;
  updated_at: string;
}

export interface DashboardLayout {
  widgets: Widget[];
  grid_config?: GridConfig;
}

export interface Widget {
  id: string;
  type: 'chart' | 'kpi' | 'table' | 'text';
  config: WidgetConfig;
  position: WidgetPosition;
}

export interface WidgetConfig {
  title?: string;
  chart_spec?: ChartSpec;
  kpi_config?: KPIConfig;
  table_config?: TableConfig;
}

export interface WidgetPosition {
  x: number;
  y: number;
  w: number;  // width in grid units
  h: number;  // height in grid units
}

// Chart models
export interface ChartSpec {
  type: 'line' | 'bar' | 'pie' | 'area' | 'composed';
  datasets: ChartDataset[];
  encodings: ChartEncodings;
  meta: ChartMeta;
}

export interface ChartDataset {
  [key: string]: string | number;  // Dynamic keys for series
}

export interface ChartEncodings {
  x: string;
  y: string;
}

export interface ChartMeta {
  title: string;
  x_dimension: string;
  breakdown_dimension?: string;
  metric: string;
  metric_label: string;
  series_keys?: string[];
  time_range_weeks?: number;
}

// Chat models
export interface ChatSession {
  id: number;
  user_id: number;
  company_id: number;
  title?: string;
  messages: ChatMessage[];
  context: ChatContext;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  metadata?: {
    skill?: string;
    widget_id?: string;
    [key: string]: any;
  };
}

export interface ChatContext {
  company_id: number;
  dashboard_id?: number;
  widget_id?: string;
  date_range?: {
    start: string;
    end: string;
  };
  [key: string]: any;
}

// Performance data models
export interface PerformanceData {
  dimensions: string[];
  measures: string[];
  data: PerformanceRow[];
}

export interface PerformanceRow {
  [dimension: string]: string | number;
}

// Insight models
export interface Insight {
  id: number;
  company_id: number;
  category: 'strategy' | 'creative' | 'spend' | 'performance' | 'opportunity';
  summary: string;
  full_text: string;
  advertisers: string[];
  metrics: Record<string, any>;
  priority: 'high' | 'medium' | 'low';
  date_generated: string;
  date_range_start?: string;
  date_range_end?: string;
}
```

### Python Models (Backend)

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, date

# Dashboard models
class WidgetPosition(BaseModel):
    x: int
    y: int
    w: int  # width
    h: int  # height

class WidgetConfig(BaseModel):
    title: Optional[str] = None
    chart_spec: Optional[Dict[str, Any]] = None
    kpi_config: Optional[Dict[str, Any]] = None
    table_config: Optional[Dict[str, Any]] = None

class Widget(BaseModel):
    id: str
    type: Literal['chart', 'kpi', 'table', 'text']
    config: WidgetConfig
    position: WidgetPosition

class DashboardLayout(BaseModel):
    widgets: List[Widget]
    grid_config: Optional[Dict[str, Any]] = None

class Dashboard(BaseModel):
    id: Optional[int] = None
    user_id: int
    company_id: int
    name: str
    description: Optional[str] = None
    layout: DashboardLayout
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

# Chart models
class ChartEncodings(BaseModel):
    x: str
    y: str

class ChartMeta(BaseModel):
    title: str
    x_dimension: str
    breakdown_dimension: Optional[str] = None
    metric: str
    metric_label: str
    series_keys: Optional[List[str]] = None
    time_range_weeks: Optional[int] = None

class ChartSpec(BaseModel):
    type: Literal['line', 'bar', 'pie', 'area', 'composed']
    datasets: List[Dict[str, Any]]
    encodings: ChartEncodings
    meta: ChartMeta

# Performance data models
class PerformanceReportRequest(BaseModel):
    company_id: int = Field(..., gt=0)
    dimensions: List[str] = Field(..., min_items=1, max_items=5)
    measures: List[str] = Field(..., min_items=1)
    start_date: date
    end_date: date
    filters: Optional[List[Dict[str, Any]]] = None

class PerformanceData(BaseModel):
    dimensions: List[str]
    measures: List[str]
    data: List[Dict[str, Any]]

# Insight models
class Insight(BaseModel):
    id: Optional[int] = None
    company_id: int
    category: Literal['strategy', 'creative', 'spend', 'performance', 'opportunity']
    summary: str
    full_text: str
    advertisers: List[str]
    metrics: Dict[str, Any]
    priority: Literal['high', 'medium', 'low'] = 'medium'
    date_generated: Optional[datetime] = None
    date_range_start: Optional[date] = None
    date_range_end: Optional[date] = None
```

## Query Patterns

### MCP Performance Queries

#### Basic Query

```python
async def get_weekly_spend(company_id: int, weeks: int = 12):
    """Get weekly spend for last N weeks."""
    end_date = datetime.now().date()
    start_date = end_date - timedelta(weeks=weeks*7)
    
    result = await mcp_client.get_performance_report(
        company_id=company_id,
        dimensions=["week"],
        measures=["spend"],
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat()
    )
    
    return result
```

#### Multi-Dimensional Query

```python
async def get_campaign_performance_by_creative(
    company_id: int,
    campaign_id: int,
    start_date: str,
    end_date: str
):
    """Get campaign performance broken down by creative."""
    result = await mcp_client.get_performance_report(
        company_id=company_id,
        dimensions=["campaign", "creative"],
        measures=["spend", "impressions", "conversions"],
        start_date=start_date,
        end_date=end_date,
        filters=[{
            "dimension": "campaign.id",
            "operator": "equals",
            "value": campaign_id
        }]
    )
    
    return result
```

#### Derived Metrics Query

```python
async def get_channel_efficiency(
    company_id: int,
    start_date: str,
    end_date: str
):
    """Get efficiency metrics by channel."""
    # Get raw data
    result = await mcp_client.get_performance_report(
        company_id=company_id,
        dimensions=["delivery"],  # Linear vs Streaming
        measures=["spend", "impressions", "visits", "conversions"],
        start_date=start_date,
        end_date=end_date
    )
    
    # Calculate derived metrics
    for row in result["data"]:
        row["cpm"] = (row["spend"] / row["impressions"]) * 1000
        row["cpv"] = row["spend"] / row["visits"] if row["visits"] > 0 else 0
        row["cpx"] = row["spend"] / row["conversions"] if row["conversions"] > 0 else 0
        row["conversion_rate"] = (row["conversions"] / row["visits"]) * 100 if row["visits"] > 0 else 0
    
    return result
```

### Database Queries

#### Get User Dashboards

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

async def get_user_dashboards(
    session: Session,
    user_id: int,
    company_id: Optional[int] = None
) -> List[Dashboard]:
    """Get all dashboards for a user."""
    query = select(Dashboard).where(Dashboard.user_id == user_id)
    
    if company_id:
        query = query.where(Dashboard.company_id == company_id)
    
    query = query.order_by(Dashboard.updated_at.desc())
    
    result = await session.execute(query)
    return result.scalars().all()
```

#### Get Recent Insights

```python
async def get_recent_insights(
    session: Session,
    company_id: int,
    limit: int = 10,
    category: Optional[str] = None
) -> List[Insight]:
    """Get recent insights for a company."""
    query = select(Insight).where(
        Insight.company_id == company_id
    )
    
    if category:
        query = query.where(Insight.category == category)
    
    query = query.order_by(
        Insight.priority.desc(),
        Insight.date_generated.desc()
    ).limit(limit)
    
    result = await session.execute(query)
    return result.scalars().all()
```

## Data Transformation Patterns

### MCP Data to Chart Data

```python
def transform_to_chart_data(
    mcp_data: Dict[str, Any],
    x_dimension: str,
    breakdown_dimension: Optional[str],
    metric: str
) -> List[Dict[str, Any]]:
    """
    Transform MCP performance data to chart-ready format.
    
    Args:
        mcp_data: Raw data from MCP server
        x_dimension: Dimension for x-axis (e.g., "week")
        breakdown_dimension: Optional dimension for series (e.g., "creative")
        metric: Metric to display (e.g., "spend")
    
    Returns:
        List of chart datasets
    """
    if not breakdown_dimension:
        # Single series
        return [
            {
                "name": row[x_dimension],
                "value": row[metric]
            }
            for row in mcp_data["data"]
        ]
    
    # Multi-series: pivot by breakdown dimension
    from collections import defaultdict
    
    pivoted = defaultdict(dict)
    series_keys = set()
    
    for row in mcp_data["data"]:
        x_value = row[x_dimension]
        series_key = row[breakdown_dimension]
        metric_value = row[metric]
        
        pivoted[x_value][series_key] = metric_value
        series_keys.add(series_key)
    
    # Convert to list of dicts
    datasets = []
    for x_value, series_data in sorted(pivoted.items()):
        dataset = {"name": x_value}
        dataset.update(series_data)
        datasets.append(dataset)
    
    return datasets, list(series_keys)
```

### Database Model to API Response

```python
def dashboard_to_response(dashboard: Dashboard) -> Dict[str, Any]:
    """Convert database model to API response."""
    return {
        "id": dashboard.id,
        "user_id": dashboard.user_id,
        "company_id": dashboard.company_id,
        "name": dashboard.name,
        "description": dashboard.description,
        "layout": dashboard.layout,
        "created_at": dashboard.created_at.isoformat(),
        "updated_at": dashboard.updated_at.isoformat(),
        "widget_count": len(dashboard.layout.get("widgets", [])),
    }
```

### API Request to Database Model

```python
def request_to_dashboard(
    request: Dashboard,
    user_id: int
) -> Dashboard:
    """Convert API request to database model."""
    return Dashboard(
        user_id=user_id,
        company_id=request.company_id,
        name=request.name,
        description=request.description,
        layout=request.layout.dict() if isinstance(request.layout, BaseModel) else request.layout,
    )
```

## Caching Patterns

### File-Based Cache

```python
from pathlib import Path
import json
import hashlib
from datetime import datetime, timedelta

class FileCache:
    """File-based cache for performance data."""
    
    def __init__(self, cache_dir: Path, ttl_hours: int = 1):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)
    
    def _generate_key(self, **params) -> str:
        """Generate cache key from parameters."""
        key_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, **params) -> Optional[dict]:
        """Get cached data if not expired."""
        key = self._generate_key(**params)
        cache_file = self.cache_dir / f"{key}.json"
        
        if not cache_file.exists():
            return None
        
        with open(cache_file) as f:
            cached = json.load(f)
        
        # Check expiration
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if datetime.now() - cached_at > self.ttl:
            cache_file.unlink()  # Delete expired cache
            return None
        
        return cached["data"]
    
    def set(self, data: dict, **params):
        """Save data to cache."""
        key = self._generate_key(**params)
        cache_file = self.cache_dir / f"{key}.json"
        
        with open(cache_file, "w") as f:
            json.dump({
                "data": data,
                "cached_at": datetime.now().isoformat(),
                "params": params
            }, f)
    
    def invalidate(self, **params):
        """Invalidate cached data."""
        key = self._generate_key(**params)
        cache_file = self.cache_dir / f"{key}.json"
        
        if cache_file.exists():
            cache_file.unlink()
    
    def clear_all(self):
        """Clear entire cache."""
        for cache_file in self.cache_dir.glob("*.json"):
            cache_file.unlink()
```

### In-Memory Cache with TTL

```python
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

class MemoryCache:
    """In-memory cache with TTL."""
    
    def __init__(self, ttl_seconds: int = 300):
        self.cache: Dict[str, tuple[Any, datetime]] = {}
        self.ttl = timedelta(seconds=ttl_seconds)
    
    def get(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        if key not in self.cache:
            return None
        
        value, cached_at = self.cache[key]
        
        if datetime.now() - cached_at > self.ttl:
            del self.cache[key]
            return None
        
        return value
    
    def set(self, key: str, value: Any):
        """Set cached value."""
        self.cache[key] = (value, datetime.now())
    
    def invalidate(self, key: str):
        """Invalidate cached value."""
        if key in self.cache:
            del self.cache[key]
    
    def clear(self):
        """Clear entire cache."""
        self.cache.clear()
```

## Data Validation Patterns

### Input Validation

```python
from pydantic import validator, root_validator

class PerformanceReportRequest(BaseModel):
    company_id: int
    dimensions: List[str]
    measures: List[str]
    start_date: date
    end_date: date
    
    @validator("dimensions")
    def validate_dimensions(cls, v):
        """Validate dimensions are allowed."""
        allowed = [
            "day", "week", "month",
            "campaign", "creative", "network", "publisher",
            "dma", "device", "delivery"
        ]
        invalid = [d for d in v if d not in allowed]
        if invalid:
            raise ValueError(f"Invalid dimensions: {invalid}")
        return v
    
    @validator("measures")
    def validate_measures(cls, v):
        """Validate measures are allowed."""
        allowed = [
            "spend", "impressions", "visits", "conversions", "revenue",
            "cpm", "cpv", "cpx", "roas", "spot_count"
        ]
        invalid = [m for m in v if m not in allowed]
        if invalid:
            raise ValueError(f"Invalid measures: {invalid}")
        return v
    
    @root_validator
    def validate_date_range(cls, values):
        """Validate date range."""
        start = values.get("start_date")
        end = values.get("end_date")
        
        if start and end:
            if end < start:
                raise ValueError("end_date must be after start_date")
            
            if (end - start).days > 365:
                raise ValueError("Date range cannot exceed 365 days")
        
        return values
```

### Output Validation

```python
def validate_performance_data(data: Dict[str, Any]) -> bool:
    """Validate performance data structure."""
    required_keys = ["dimensions", "measures", "data"]
    
    if not all(key in data for key in required_keys):
        return False
    
    if not isinstance(data["data"], list):
        return False
    
    # Validate each row has required dimensions and measures
    for row in data["data"]:
        for dim in data["dimensions"]:
            if dim not in row:
                return False
        
        for measure in data["measures"]:
            if measure not in row:
                return False
    
    return True
```

## Best Practices

### ✅ Do

- Use Pydantic for data validation
- Define explicit TypeScript types
- Cache expensive queries
- Use database indexes for frequent queries
- Validate data at boundaries
- Use transactions for multi-step operations
- Handle missing/null values gracefully
- Document data transformations

### ❌ Don't

- Trust unvalidated data
- Store sensitive data unencrypted
- Skip data validation
- Use `Any` type without reason
- Ignore database constraints
- Mutate cached data
- Skip error handling
- Hardcode data values

## Related Skills

- **backend-patterns/** - Service and API patterns
- **integration-patterns/** - API integration
- **architecture-context/** - System architecture

---

Last Updated: 2026-01-21

