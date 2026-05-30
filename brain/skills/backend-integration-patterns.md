# Integration Patterns Skill

## Purpose

Document **MCP integration patterns and service layer architecture**. Use when integrating with MCP servers, building services that query data, or connecting frontend to MCP-backed APIs.

## When to Use

Use this skill when:
- Integrating with MCP performance servers
- Building services that query analytics/data
- Connecting frontend to MCP-backed APIs
- Understanding data access patterns
- Implementing error handling for data queries

## MCP Architecture

**MCP (Model Context Protocol) is a standard for structured data access and tooling.**

### System Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (React/Vite)                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  Components  │  │   Contexts   │  │  API Client  │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└────────────────────────────┬────────────────────────────┘
                             │ HTTP/REST
                             │
┌────────────────────────────┴────────────────────────────┐
│              Backend (FastAPI/Python)                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   Routers    │  │   Services   │  │  Calculator  │ │
│  │  (FastAPI)   │  │  (Business)  │  │    Tools     │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└────────────────────────────┬────────────────────────────┘
                             │ MCP Protocol
                             │
┌────────────────────────────┴────────────────────────────┐
│                MCP Performance Server                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  Query API   │  │    Cache     │  │   Database   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### MCP Query Pattern (Standard)

**Service Layer:**
```python
# generative_dashboards/services/analytics_query_service.py

async def query_mcp(
    company_id: int,
    dimensions: list[str],
    non_lifts: list[dict],
    lifts: list[dict] = None,
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Query MCP for performance/analytics data.
    
    Args:
        company_id: Tenant/organization identifier
        dimensions: List of dimension keys from analytics catalog
        non_lifts: List of metric dicts {"measure": "spend"}
        lifts: Optional lift metrics with attribution
        start_date: ISO format date
        end_date: ISO format date
        
    Returns:
        dict with "results" (list of row dicts) and "total_rows"
    """
    # Build MCP request
    request = {
        "company_id": company_id,
        "dimensions": dimensions,
        "non_lifts": non_lifts,
        "lifts": lifts or [],
        "start_date": start_date,
        "end_date": end_date,
    }
    
    # Call MCP server
    response = await mcp_client.get_performance_report(**request)
    return response
```

**Router Layer:**
```python
# generative_dashboards/routers/analytics.py
