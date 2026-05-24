---
name: integration-patterns
description: Document MCP integration patterns and service layer architecture. Use when integrating with MCP performance servers, building services that query analytics data, connecting frontend to MCP-backed APIs, understanding data access patterns, or implementing error handling for data queries.
disable-model-invocation: true

---
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

@router.post("/query")
async def query_analytics(request: AnalyticsQueryRequest):
    """Endpoint for analytics queries."""
    try:
        # Service handles MCP communication
        data = await query_mcp(
            company_id=request.company_id,
            dimensions=request.dimensions,
            non_lifts=request.non_lifts,
            lifts=request.lifts,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        
        # Calculator tools handle derived metrics
        if request.include_derived:
            data = calculate_derived_metrics(data, request.derived_metrics)
        
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"MCP query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

### Error Handling (Fail-Forward Pattern)

```python
# ✅ CORRECT: Graceful degradation
async def get_performance_summary(company_id: int):
    """Fail-forward: try detailed, fallback to summary."""
    try:
        return await query_mcp(
            company_id=company_id,
            dimensions=["day", "campaign", "creative"],
            non_lifts=[{"measure": "spend"}, {"measure": "conversions"}],
        )
    except Exception as e:
        logger.warning(f"Detailed query failed: {e}, falling back to summary")
        return await query_mcp(
            company_id=company_id,
            dimensions=["day"],  # Simpler query
            non_lifts=[{"measure": "spend"}],
        )
```

## Integration Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (React)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  Components  │  │   Contexts   │  │  API Client  │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└────────────────────────────┬────────────────────────────┘
                             │ HTTP/WebSocket
                             │
┌────────────────────────────┴────────────────────────────┐
│                   Backend (FastAPI)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   Routers    │  │   Services   │  │    Skills    │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└────────────────────────────┬────────────────────────────┘
                             │ MCP Protocol
                             │
┌────────────────────────────┴────────────────────────────┐
│                   MCP Performance Server                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  Data Query  │  │    Cache     │  │   Database   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

## Frontend-Backend Integration

### API Client Pattern

**Frontend: `/frontend/src/api/client.ts`**

```typescript
import axios, { AxiosInstance, AxiosRequestConfig } from 'axios';

class APIClient {
  private client: AxiosInstance;

  constructor(baseURL: string) {
    this.client = axios.create({
      baseURL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor for auth
    this.client.interceptors.request.use((config) => {
      const token = localStorage.getItem('auth_token');
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
    });

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401) {
          // Handle unauthorized - redirect to login
          window.location.href = '/login';
        }
        return Promise.reject(error);
      }
    );
  }

  async get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.get<T>(url, config);
    return response.data;
  }

  async post<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.post<T>(url, data, config);
    return response.data;
  }

  async patch<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.patch<T>(url, data, config);
    return response.data;
  }

  async delete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.delete<T>(url, config);
    return response.data;
  }
}

export const apiClient = new APIClient(
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
);
```

### Streaming Integration Pattern

**Frontend: Consuming SSE Stream**

```typescript
async function* streamChatResponse(message: string, context?: any) {
  const response = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ message, context }),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const reader = response.body?.getReader();
  const decoder = new TextDecoder();

  if (!reader) {
    throw new Error('No response body');
  }

  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();

    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = JSON.parse(line.slice(6));
        
        if (data.error) {
          throw new Error(data.error);
        }
        
        if (data.done) {
          return;
        }
        
        yield data.content;
      }
    }
  }
}

// Usage in component
const [response, setResponse] = useState('');

async function handleSubmit() {
  setResponse('');
  
  for await (const chunk of streamChatResponse(message, context)) {
    setResponse(prev => prev + chunk);
  }
}
```

**Backend: SSE Streaming**

```python
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import json
import asyncio

router = APIRouter()

@router.post("/chat/stream")
async def stream_chat(request: ChatRequest):
    """Stream chat responses."""
    
    async def event_generator():
        try:
            async for chunk in agent.stream_response(
                request.message,
                request.context
            ):
                data = json.dumps({
                    "content": chunk,
                    "done": False
                })
                yield f"data: {data}\n\n"
                await asyncio.sleep(0)  # Allow other tasks to run
            
            # Send completion
            yield f"data: {json.dumps({'done': True})}\n\n"
            
        except Exception as e:
            error_data = json.dumps({"error": str(e)})
            yield f"data: {error_data}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
```

## MCP Integration Patterns

### MCP Server Configuration

**File: `mcp-configs/your-server.json`**

```json
{
  "mcpServers": {
    "performance-report": {
      "command": "uvx",
      "args": ["--from", "mcp-server-performance-report", "mcp-server-performance-report"],
      "env": {
        "API_URL": "https://your-api.example.com",
        "API_KEY": "${API_KEY}"
      }
    }
  }
}
```

### MCP Client Integration

**Backend: Using MCP Client**

```python
from pathlib import Path
import json
import subprocess
from typing import List, Dict, Any

class MCPPerformanceClient:
    """Client for MCP Performance Report Server."""
    
    def __init__(self, company_id: int):
        self.company_id = company_id
        self.config_file = Path(f"mcp-configs/company-{company_id}.json")
        self.cache_dir = Path("mcp-data-cache")
        self.cache_dir.mkdir(exist_ok=True)
    
    async def get_performance_report(
        self,
        dimensions: List[str],
        measures: List[str],
        start_date: str,
        end_date: str,
        filters: List[Dict] = None
    ) -> Dict[str, Any]:
        """
        Get performance report from MCP server.
        
        Args:
            dimensions: List of dimensions (e.g., ["week", "campaign"])
            measures: List of measures (e.g., ["spend", "impressions"])
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            filters: Optional list of filters
        
        Returns:
            Performance report data
        """
        # Check cache first
        cache_key = self._generate_cache_key(
            dimensions, measures, start_date, end_date, filters
        )
        cached = self._get_from_cache(cache_key)
        if cached:
            return cached
        
        # Call MCP server
        params = {
            "company_id": self.company_id,
            "dimensions": dimensions,
            "measures": measures,
            "start_date": start_date,
            "end_date": end_date,
            "filters": filters or []
        }
        
        result = await self._call_mcp_tool(
            "get_performance_report",
            params
        )
        
        # Cache result
        self._save_to_cache(cache_key, result)
        
        return result
    
    async def _call_mcp_tool(
        self,
        tool_name: str,
        params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call MCP server tool."""
        # This is a simplified example
        # Actual implementation depends on MCP protocol
        
        # For now, using subprocess to call MCP server
        process = await asyncio.create_subprocess_exec(
            "mcp-client",
            "--config", str(self.config_file),
            "--server", "performance-report",
            "--tool", tool_name,
            "--params", json.dumps(params),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise Exception(f"MCP call failed: {stderr.decode()}")
        
        return json.loads(stdout.decode())
    
    def _generate_cache_key(self, *args) -> str:
        """Generate cache key from parameters."""
        import hashlib
        key_string = json.dumps(args, sort_keys=True)
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def _get_from_cache(self, key: str) -> Optional[dict]:
        """Get data from cache."""
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                return json.load(f)
        return None
    
    def _save_to_cache(self, key: str, data: dict):
        """Save data to cache."""
        cache_file = self.cache_dir / f"{key}.json"
        with open(cache_file, "w") as f:
            json.dump(data, f)
```

### MCP Cache Management

**Script: `update_mcp_cache.py`**

```python
#!/usr/bin/env python3
"""
Update MCP cache with fresh data.
Run this periodically to keep cache warm.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta

async def update_cache_for_company(company_id: int):
    """Update cache for a specific company."""
    client = MCPPerformanceClient(company_id)
    
    # Common queries to pre-cache
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    
    queries = [
        # Weekly trends
        {
            "dimensions": ["week"],
            "measures": ["spend", "impressions", "visits", "conversions"],
        },
        # Campaign performance
        {
            "dimensions": ["campaign"],
            "measures": ["spend", "conversions", "revenue"],
        },
        # Creative performance
        {
            "dimensions": ["creative"],
            "measures": ["spend", "impressions", "cpv", "cpx"],
        },
        # Network breakdown
        {
            "dimensions": ["network"],
            "measures": ["spend", "cpm"],
        },
    ]
    
    for query in queries:
        try:
            print(f"Caching: {query['dimensions']}")
            await client.get_performance_report(
                dimensions=query["dimensions"],
                measures=query["measures"],
                start_date=start_date,
                end_date=end_date
            )
            print(f"  ✓ Cached")
        except Exception as e:
            print(f"  ✗ Error: {e}")

async def main():
    """Update cache for all companies."""
    company_ids = [599]  # Add more company IDs as needed
    
    for company_id in company_ids:
        print(f"\nUpdating cache for company {company_id}")
        await update_cache_for_company(company_id)

if __name__ == "__main__":
    asyncio.run(main())
```

## Skills Integration Pattern

### Skill Composition

Skills can call other skills to compose functionality:

```python
class DataAnalysisSkill(BaseSkill):
    """Skill for data analysis."""
    
    def __init__(self, skill_loader):
        self.skill_loader = skill_loader
    
    async def execute(self, message: str, context: dict) -> SkillResult:
        # 1. Use data_querying skill to fetch data
        data_querying = self.skill_loader.get_skill("data_querying")
        data = await data_querying.execute(
            self._build_query(message),
            context
        )
        
        # 2. Use calculations skill for derived metrics
        if self._needs_calculations(message):
            calculations = self.skill_loader.get_skill("calculations")
            metrics = await calculations.execute(
                self._build_calculation_request(data),
                context
            )
        
        # 3. Analyze data
        insights = self._analyze(data, metrics)
        
        # 4. Format response
        return SkillResult(
            content=self._format_insights(insights),
            metadata={"skill_chain": ["data_querying", "calculations"]}
        )
```

### Skill Communication Protocol

```python
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class SkillRequest:
    """Request to execute a skill."""
    message: str
    context: Optional[dict] = None
    metadata: Optional[dict] = None

@dataclass
class SkillResult:
    """Result from skill execution."""
    content: Any
    metadata: dict
    success: bool = True
    error: Optional[str] = None

class SkillRouter:
    """Routes requests to appropriate skills."""
    
    def __init__(self, skill_loader: SkillLoader):
        self.skill_loader = skill_loader
        self.routing_rules = self._load_routing_rules()
    
    async def route(self, request: SkillRequest) -> SkillResult:
        """Route request to appropriate skill."""
        
        # Determine which skill to use
        skill_name = self._select_skill(request.message, request.context)
        
        # Load and execute skill
        skill = self.skill_loader.get_skill(skill_name)
        if not skill:
            return SkillResult(
                content=None,
                metadata={},
                success=False,
                error=f"Skill not found: {skill_name}"
            )
        
        try:
            result = await skill.execute(
                request.message,
                request.context
            )
            return result
        except Exception as e:
            return SkillResult(
                content=None,
                metadata={"skill": skill_name},
                success=False,
                error=str(e)
            )
```

## Database Integration Pattern

### Database Models

```python
from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()

class Dashboard(Base):
    """Dashboard model."""
    __tablename__ = "dashboards"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    layout = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    widgets = relationship("Widget", back_populates="dashboard")

class Widget(Base):
    """Widget model."""
    __tablename__ = "widgets"
    
    id = Column(Integer, primary_key=True)
    dashboard_id = Column(Integer, ForeignKey("dashboards.id"))
    type = Column(String, nullable=False)  # "chart", "kpi", "table"
    config = Column(JSON, nullable=False)
    position = Column(JSON, nullable=False)
    
    # Relationships
    dashboard = relationship("Dashboard", back_populates="widgets")
```

### Repository Pattern

```python
from sqlalchemy.orm import Session
from typing import List, Optional

class DashboardRepository:
    """Repository for dashboard operations."""
    
    def __init__(self, session: Session):
        self.session = session
    
    def get_by_id(self, dashboard_id: int) -> Optional[Dashboard]:
        """Get dashboard by ID."""
        return self.session.query(Dashboard).filter(
            Dashboard.id == dashboard_id
        ).first()
    
    def get_by_user(self, user_id: int) -> List[Dashboard]:
        """Get all dashboards for a user."""
        return self.session.query(Dashboard).filter(
            Dashboard.user_id == user_id
        ).all()
    
    def create(self, dashboard: Dashboard) -> Dashboard:
        """Create new dashboard."""
        self.session.add(dashboard)
        self.session.commit()
        self.session.refresh(dashboard)
        return dashboard
    
    def update(self, dashboard: Dashboard) -> Dashboard:
        """Update existing dashboard."""
        self.session.commit()
        self.session.refresh(dashboard)
        return dashboard
    
    def delete(self, dashboard_id: int) -> bool:
        """Delete dashboard."""
        dashboard = self.get_by_id(dashboard_id)
        if dashboard:
            self.session.delete(dashboard)
            self.session.commit()
            return True
        return False
```

## Chart Pipeline Integration

### Complete Chart Generation Flow

```
User Request: "Show me spend by creative"
    ↓
Frontend (Chart Component)
    ↓
POST /api/charts/generate
    {
      "message": "Show me spend by creative",
      "context": { "company_id": 599, "date_range": "last_30_days" }
    }
    ↓
Backend Router
    ↓
Skills-Based Agent
    ↓
chart_generation Skill
    - Parse intent
    - Extract: x_dimension="creative", metric="spend"
    - Determine chart type="bar"
    ↓
data_querying Skill
    - Call MCP: get_performance_report(dimensions=["creative"], measures=["spend"])
    - Return data
    ↓
Chart Spec JSON
    {
      "type": "bar",
      "datasets": [
        { "name": "Creative A", "value": 10000 },
        { "name": "Creative B", "value": 8000 }
      ],
      "encodings": { "x": "name", "y": "value" },
      "meta": {
        "title": "Spend by Creative",
        "x_dimension": "creative",
        "metric": "spend"
      }
    }
    ↓
Frontend Receives Spec
    ↓
reportChartBuilder.ts
    - Transform data for Recharts
    - Apply colors
    - Configure axes
    ↓
Recharts Component
    - Render chart
```

## Best Practices

### ✅ Do

- Use typed interfaces for all integrations
- Implement retry logic for external calls
- Cache frequently accessed data
- Validate data at integration boundaries
- Log integration points for debugging
- Use async/await for I/O operations
- Handle errors gracefully
- Document integration contracts

### ❌ Don't

- Trust external data without validation
- Skip error handling
- Make synchronous external calls
- Hardcode API endpoints
- Expose internal errors to clients
- Skip authentication/authorization
- Forget to clean up resources
- Ignore timeouts

## Troubleshooting Integration Issues

### Common Issues

**1. CORS Errors**
```typescript
// Frontend: Axios config
axios.create({
  baseURL: API_URL,
  withCredentials: true,  // Include cookies
});

// Backend: FastAPI CORS
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**2. MCP Connection Failures**
```python
# Add retry logic
import tenacity

@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=10)
)
async def call_mcp_with_retry(params):
    return await mcp_client.get_performance_report(**params)
```

**3. Streaming Connection Drops**
```typescript
// Frontend: Reconnect on error
async function streamWithReconnect(message: string, maxRetries = 3) {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      for await (const chunk of streamChatResponse(message)) {
        yield chunk;
      }
      return; // Success
    } catch (error) {
      if (attempt === maxRetries - 1) throw error;
      await sleep(1000 * (attempt + 1)); // Exponential backoff
    }
  }
}
```

## Related Skills

- **architecture-context/** - System architecture
- **frontend-patterns/** - Frontend implementation
- **backend-patterns/** - Backend implementation

---

## MCP Integration Patterns

### MCP Server Architecture

Use Model Context Protocol (MCP) for structured data access:

```
Frontend (React)
    ↓ HTTP/SSE
Backend (FastAPI)
    ↓ MCP Protocol
MCP Server
    ↓
Data source / platform
```

**MCP Server Configuration:**
```json
// mcp-configs/your-server.json
{
  "mcpServers": {
    "performance-report": {
      "command": "uvx",
      "args": ["--from", "mcp-server-performance-report", "mcp-server-performance-report"],
      "env": {
        "API_URL": "https://your-api.example.com",
        "API_KEY": "${API_KEY}"
      }
    }
  }
}
```

### Skill Composition Pattern

Skills can invoke other skills through the skill loader:

```python
class DataAnalysisSkill(BaseSkill):
    """Data analysis composes multiple skills."""
    
    async def execute(self, message: str, context: dict) -> SkillResult:
        # 1. Use data_querying skill to fetch data
        data_skill = self.skill_loader.get_skill("data_querying")
        data = await data_skill.execute(
            self._build_query(message),
            context
        )
        
        # 2. Use calculations skill for derived metrics
        calc_skill = self.skill_loader.get_skill("calculations")
        metrics = await calc_skill.execute(
            {"operation": "compute_summary", "data": data},
            context
        )
        
        # 3. Analyze and generate insights
        insights = self._analyze(data, metrics)
        
        return SkillResult(
            content=self._format_insights(insights),
            metadata={"skills_used": ["data_querying", "calculations"]}
        )
```

**Common Skill Compositions:**

| Primary Skill | Supporting Skills | Purpose |
|---------------|-------------------|---------|
| `chart_generation` | `data_querying`, `calculations` | Create visualizations |
| `data_analysis` | `data_querying`, `calculations` | Analyze patterns |
| `widget_insights` | `data_querying`, `calculations` | Generate insights |
| `presentation-decks` | `chart_generation`, `data_querying` | PowerPoint generation |

### Skills-Based Agent Integration

**Skill Selection Flow:**
```python
async def process_request(message: str, context: dict):
    # Phase 1: Select skill (fast path → cache → LLM)
    if fast_path_match(message):
        skill_name = get_fast_path_skill(message)
    elif cache_hit(message, context):
        skill_name = get_cached_skill(message, context)
    else:
        skill_name = await llm_select_skill(message, context)
    
    # Phase 2: Load skill instructions
    skill = skill_loader.get_skill(skill_name)
    skill_context = skill.get_instructions()
    
    # Phase 3: Execute with tools
    result = await skill.execute(message, context)
    
    return result
```

**Fast-Path Routing (`skill_routing.yaml`):**
```yaml
fast_paths:
  # Visualization
  - patterns: ["show me", "display", "visualize", "chart", "graph"]
    skill: chart_generation
  
  # Analysis
  - patterns: ["why", "explain", "analyze", "what happened"]
    skill: data_analysis
  
  # Definitions
  - patterns: ["what is", "define", "how do"]
    skill: knowledge_base
  
  # Insights
  - patterns: ["insight", "recommendation"]
    skill: widget_insights

context_routes:
  - context_key: widget_insights_panel
    skill: widget_insights
  - context_key: dashboard_editing
    skill: dashboard_editing
```

### Calculator Tool Integration

```python
from generative_dashboards.services.calculator_tool import CalculatorTool

calculator = CalculatorTool()

# Standard metrics
cpm = calculator.calculate_cpm(spend=10000, impressions=1000000)
cpv = calculator.calculate_cpv(spend=10000, visits=5000)
roas = calculator.calculate_roas(spend=10000, revenue=50000)

# Rankings
top_networks = calculator.efficiency_ranking(
    data=network_data,
    group_field="network",
    metric_type="cpm",
    n=10
)

# Period comparison
delta = calculator.calculate_period_delta(
    current_period=current_data,
    previous_period=previous_data,
    metrics=["spend", "conversions"]
)
```

### Frontend-Backend Chart Flow

```
User: "Show me spend over time by creative"
    ↓
Frontend: POST /api/charts/generate
    {
      "message": "Show me spend over time by creative",
      "context": { "company_id": 599 }
    }
    ↓
Backend: SkillsBasedAgent
    ↓
    └→ chart_generation skill selected
       └→ data_querying skill invoked
          └→ query_mcp(dimensions=["week", "creative"], non_lifts=[{"measure": "spend"}])
       └→ calculations skill invoked (if needed)
       └→ Generate ChartSpec
    ↓
Response: ChartSpec JSON
    {
      "type": "line",
      "datasets": [...],
      "meta": {
        "x_dimension": "week",
        "breakdown_dimension": "creative",
        "series_keys": ["Creative A", "Creative B"]
      }
    }
    ↓
Frontend: reportChartBuilder.ts
    └→ Transform data for Recharts
    └→ Render chart
```


### Service Layer Pattern

All tool integrations go through services:

```python
# Tool → Service → External System

# Tool (thin wrapper)
async def execute_tool(tool_id: str, params: dict):
    if tool_id == "query_performance":
        return await performance_service.query(**params)
    elif tool_id == "calculate_metrics":
        return calculator_service.calculate(**params)

# Service (business logic)
class PerformanceService:
    async def query(self, company_id: int, dimensions: list, **kwargs):
        # Check cache first
        cached = await self.cache.get(company_id, dimensions)
        if cached:
            return cached
        
        # Query MCP
        result = await self.mcp_client.get_performance_report(
            company_id=company_id,
            dimensions=dimensions,
            **kwargs
        )
        
        # Cache result
        await self.cache.set(company_id, dimensions, result)
        
        return result
```

### Error Handling Integration

```python
# Fail-forward pattern from tools
class ToolResponse(BaseModel):
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    hint: Optional[str] = None
    options: Optional[List[str]] = None
    example: Optional[dict] = None

# Tool returns actionable error
if not valid_dimension:
    return ToolResponse(
        success=False,
        error=f"Invalid dimension: {dim}",
        hint="Use a valid dimension from the catalog",
        options=["week", "campaign", "creative", "network"],
        example={"dimensions": ["week", "campaign"]}
    )

# Agent uses error info to self-correct or explain to user
```

### Key Integration Concepts

| Concept | Purpose |
|---------|---------|
| **MCP Protocol** | Standard for structured data access |
| **Service Layer** | Business logic and MCP communication |
| **Calculator Tools** | Deterministic metric calculations |
| **Fail-Forward Pattern** | Graceful degradation on errors |

## Related Skills

- **backend-patterns/** - Python service implementation
- **data-patterns/** - Analytics catalog and MCP query structure
- **architecture-context/** - AI architecture principles

---

Last Updated: 2026-01-21
