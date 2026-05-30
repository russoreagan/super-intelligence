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
