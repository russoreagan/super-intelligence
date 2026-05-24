---
name: backend-patterns
description: Provide comprehensive understanding of Python, FastAPI, and service patterns. Use when creating new API endpoints, building backend services, working with database models, implementing business logic, integrating with external APIs, or debugging backend issues.
disable-model-invocation: true

---
# Backend Patterns Skill

## Purpose

Provide comprehensive understanding of Python, FastAPI, and service patterns. This skill helps developers build scalable, maintainable backend services following AI-first principles.

## When to Use

Use this skill when:
- Creating new API endpoints
- Building backend services
- Working with database models
- Implementing business logic
- Integrating with external APIs and data services
- Debugging backend issues

## AI Agent Principles (CRITICAL)

**Skills vs Tools Framework:**
- **Skills** = Instructions (what to do, how to think)
- **Tools** = Deterministic execution (external actions)
- **Models** = Narration & orchestration (natural language)

**Core Rule: Compute Outside the Model**
```python
# ❌ WRONG: Asking LLM to calculate
prompt = "Calculate the CPA: spend=$1000, conversions=50"
response = llm(prompt)  # LLM does math (unreliable)

# ✅ CORRECT: Calculator tool pattern
def calculate_cpa(spend: float, conversions: int) -> float:
    """Deterministic calculation outside LLM."""
    return spend / conversions if conversions > 0 else 0.0

cpa = calculate_cpa(1000, 50)  # Tool does math (reliable)
prompt = f"Analyze this CPA: ${cpa:.2f}"  # LLM interprets result
```

**Fail-Forward Error Pattern:**
```python
# ✅ CORRECT: Graceful degradation
try:
    detailed_data = await fetch_detailed_metrics(company_id)
except Exception as e:
    logger.warning(f"Detailed fetch failed: {e}")
    detailed_data = await fetch_summary_metrics(company_id)  # Fallback
    
# Always return something useful, never crash the agent
```

## Technology Stack

### Core Technologies
- **Python 3.11+** - Programming language
- **FastAPI** - Web framework
- **Pydantic** - Data validation
- **SQLAlchemy** - ORM (if applicable)
- **asyncio** - Async/await patterns

### Data & Caching
- **MCP (Model Context Protocol)** - Structured data access layer
- **JSON caching** - Performance optimization
- **PostgreSQL/SQLite** - Database

### Tools
- **pytest** - Testing framework
- **uvicorn** - ASGI server
- **python-dotenv** - Environment configuration

## Project Structure

```
backend/
├── routers/              # API endpoints
│   ├── agent.py         # Main agent endpoint
│   ├── resources.py     # Resource CRUD
│   ├── chat.py          # Chat sessions
│   └── analytics.py     # Analytics endpoints
├── services/            # Business logic layer
│   ├── data_catalog.py  # Canonical dimensions & metrics
│   ├── chart_converter.py         # Chart conversion
│   └── data_service.py            # Data access
├── skills/              # Agent skills
├── models/              # Database models (if applicable)
└── main.py              # Application entry point
```

## Analytics Catalog Pattern

**A data catalog defines the canonical source of truth for all dimensions and metrics.**

Example catalog structure:

```python
# Key dimensions (subset)
DIMENSIONS = {
    # Time
    "day": {"label": "Day", "mcp_field": "day", "kind": "time"},
    "week": {"label": "Week", "mcp_field": "week", "kind": "time"},
    "month": {"label": "Month", "mcp_field": "month", "kind": "time"},
    
    # Core Entities
    "campaign": {"label": "Campaign", "mcp_field": "campaign.name"},
    "creative": {"label": "Creative", "mcp_field": "creative.name"},
    "network": {"label": "Network", "mcp_field": "network.name"},
    
    # Demographics
    "age": {"label": "Age", "mcp_field": "age.name"},
    "gender": {"label": "Gender", "mcp_field": "gender.name"},
    "income": {"label": "Household Income", "mcp_field": "income.name"},
}

# Key metrics (subset)
METRICS = {
    # Non-lift (directly queryable from MCP)
    "impressions": {"label": "Impressions", "kind": "non_lift"},
    "spend": {"label": "Spend", "kind": "non_lift"},
    "conversions": {"label": "Conversions", "kind": "non_lift"},
    
    # Lift (calculated after query)
    "cpa": {
        "label": "CPA", 
        "kind": "lift",
        "formula": "spend / conversions",
        "numerator": "spend",
        "denominator": "conversions"
    },
    "cpm": {
        "label": "CPM",
        "kind": "lift", 
        "formula": "(spend / impressions) * 1000",
        "numerator": "spend",
        "denominator": "impressions",
        "multiplier": 1000
    },
}
```

**Critical Rule: Calculator Tool Pattern**

```python
# ❌ WRONG: Never ask LLM to calculate derived metrics
def get_cpa_from_llm(spend: float, conversions: int):
    return llm(f"Calculate CPA from ${spend} and {conversions} conversions")

# ✅ CORRECT: Always use calculator tools
def calculate_lift_metrics(data: pd.DataFrame, metric_config: dict) -> pd.DataFrame:
    """Deterministic calculation of derived metrics outside LLM."""
    if metric_config["kind"] == "lift":
        numerator = data[metric_config["numerator"]]
        denominator = data[metric_config["denominator"]]
        multiplier = metric_config.get("multiplier", 1)
        
        # Safe division with zero handling
        result = np.where(
            denominator > 0,
            (numerator / denominator) * multiplier,
            0
        )
        return result
    return data
```

**Why This Matters:**
- LLMs are unreliable at math (hallucinate wrong numbers)
- Derived metrics (CPA, CPM, ROAS) must be calculated deterministically
- The catalog defines ALL valid dimensions/metrics for your data domain

## API Endpoint Patterns

### Basic Endpoint

```python
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

class Campaign(BaseModel):
    id: int
    name: str
    status: str

@router.get("/", response_model=list[Campaign])
async def get_campaigns(company_id: int) -> list[Campaign]:
    """Get all campaigns for a company."""
    try:
        campaigns = await fetch_campaigns(company_id)
        return campaigns
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{campaign_id}", response_model=Campaign)
async def get_campaign(campaign_id: int) -> Campaign:
    """Get a specific campaign."""
    campaign = await fetch_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign

@router.post("/", response_model=Campaign, status_code=201)
async def create_campaign(campaign: Campaign) -> Campaign:
    """Create a new campaign."""
    created = await save_campaign(campaign)
    return created
```

### Streaming Response Pattern

```python
from fastapi.responses import StreamingResponse
import json

@router.post("/chat/stream")
async def stream_chat(request: ChatRequest):
    """Stream chat responses using Server-Sent Events."""
    
    async def event_generator():
        try:
            async for chunk in agent.stream_response(request.message):
                # Format as SSE
                data = json.dumps({"content": chunk, "done": False})
                yield f"data: {data}\n\n"
            
            # Send completion signal
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            error_data = json.dumps({"error": str(e)})
            yield f"data: {error_data}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )
```

### Dependency Injection Pattern

```python
from fastapi import Depends
from typing import Annotated

async def get_current_user(token: str = Header(...)) -> User:
    """Dependency to get current authenticated user."""
    user = await verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user

async def get_company_access(
    company_id: int,
    user: Annotated[User, Depends(get_current_user)]
) -> Company:
    """Verify user has access to company."""
    if not await user_has_access(user.id, company_id):
        raise HTTPException(status_code=403, detail="Access denied")
    return await get_company(company_id)

@router.get("/companies/{company_id}/data")
async def get_company_data(
    company: Annotated[Company, Depends(get_company_access)]
):
    """Get company data with automatic auth and access control."""
    return await fetch_data(company.id)
```

## Service Layer Patterns

### Service Class Pattern

```python
from typing import Optional, List
from dataclasses import dataclass

@dataclass
class CampaignService:
    """Service for campaign operations."""
    
    def __init__(self, mcp_client, cache):
        self.mcp_client = mcp_client
        self.cache = cache
    
    async def get_campaigns(
        self, 
        company_id: int,
        status: Optional[str] = None
    ) -> List[Campaign]:
        """Get campaigns with optional filtering."""
        cache_key = f"campaigns:{company_id}:{status}"
        
        # Check cache
        cached = await self.cache.get(cache_key)
        if cached:
            return cached
        
        # Fetch from MCP
        campaigns = await self.mcp_client.get_campaigns(
            company_id=company_id,
            filters={"status": status} if status else None
        )
        
        # Cache result
        await self.cache.set(cache_key, campaigns, ttl=300)
        
        return campaigns
    
    async def calculate_campaign_metrics(
        self, 
        campaign_id: int
    ) -> CampaignMetrics:
        """Calculate derived metrics for a campaign."""
        data = await self.get_campaign_data(campaign_id)
        
        return CampaignMetrics(
            cpm=self._calculate_cpm(data),
            roas=self._calculate_roas(data),
            conversion_rate=self._calculate_conversion_rate(data)
        )
    
    def _calculate_cpm(self, data: dict) -> float:
        """Calculate cost per thousand impressions."""
        if data["impressions"] == 0:
            return 0.0
        return (data["spend"] / data["impressions"]) * 1000
    
    def _calculate_roas(self, data: dict) -> float:
        """Calculate return on ad spend."""
        if data["spend"] == 0:
            return 0.0
        return data["revenue"] / data["spend"]
    
    def _calculate_conversion_rate(self, data: dict) -> float:
        """Calculate conversion rate."""
        if data["visits"] == 0:
            return 0.0
        return (data["conversions"] / data["visits"]) * 100
```

### Singleton Pattern for Services

```python
from functools import lru_cache

class SkillLoader:
    """Singleton service for loading skills."""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.skills = {}
        self._load_skills()
        self._initialized = True
    
    def _load_skills(self):
        """Load all skills from skills directory."""
        # Implementation...
        pass

@lru_cache()
def get_skill_loader() -> SkillLoader:
    """Get singleton skill loader instance."""
    return SkillLoader()
```

## Agent & Skills Patterns

### Skills-Based Agent Pattern

See `generative_dashboards/services/skills_based_agent.py`:

```python
class SkillsBasedAgent:
    """Main agent that orchestrates skill execution."""
    
    def __init__(self, skill_loader: SkillLoader):
        self.skill_loader = skill_loader
        self.routing_config = SkillRoutingConfig()
    
    async def process_request(
        self, 
        message: str,
        context: Optional[dict] = None
    ) -> AgentResponse:
        """Process user request and route to appropriate skill."""
        
        # 1. Determine intent and select skill
        skill_name = await self.routing_config.select_skill(message, context)
        
        # 2. Load skill
        skill = self.skill_loader.get_skill(skill_name)
        if not skill:
            raise ValueError(f"Skill not found: {skill_name}")
        
        # 3. Execute skill
        result = await skill.execute(message, context)
        
        return AgentResponse(
            skill=skill_name,
            content=result.content,
            metadata=result.metadata
        )
    
    async def stream_response(
        self,
        message: str,
        context: Optional[dict] = None
    ):
        """Stream response chunks as they're generated."""
        skill_name = await self.routing_config.select_skill(message, context)
        skill = self.skill_loader.get_skill(skill_name)
        
        async for chunk in skill.stream_execute(message, context):
            yield chunk
```

### Skill Interface Pattern

```python
from abc import ABC, abstractmethod

class BaseSkill(ABC):
    """Base class for all skills."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Skill name."""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Skill description."""
        pass
    
    @abstractmethod
    async def execute(
        self, 
        message: str, 
        context: Optional[dict] = None
    ) -> SkillResult:
        """Execute the skill and return result."""
        pass
    
    async def stream_execute(
        self,
        message: str,
        context: Optional[dict] = None
    ):
        """Stream execution results."""
        result = await self.execute(message, context)
        yield result.content

class DataAnalysisSkill(BaseSkill):
    """Skill for data analysis."""
    
    @property
    def name(self) -> str:
        return "data_analysis"
    
    @property
    def description(self) -> str:
        return "Analyze TV advertising data and provide insights"
    
    async def execute(
        self,
        message: str,
        context: Optional[dict] = None
    ) -> SkillResult:
        # 1. Parse user intent
        intent = self._parse_intent(message)
        
        # 2. Fetch required data
        data = await self._fetch_data(intent, context)
        
        # 3. Perform analysis
        insights = self._analyze(data, intent)
        
        # 4. Format response
        return SkillResult(
            content=self._format_response(insights),
            metadata={"intent": intent, "data_points": len(data)}
        )
```

## MCP Integration Patterns

### MCP Client Pattern

```python
import json
from pathlib import Path

class MCPPerformanceClient:
    """Client for MCP performance report server."""
    
    def __init__(self, config_path: Path):
        self.config = self._load_config(config_path)
        self.cache_dir = Path("mcp-data-cache")
    
    def _load_config(self, config_path: Path) -> dict:
        """Load MCP configuration."""
        with open(config_path) as f:
            return json.load(f)
    
    async def get_performance_report(
        self,
        company_id: int,
        dimensions: list[str],
        measures: list[str],
        start_date: str,
        end_date: str,
        filters: Optional[list[dict]] = None
    ) -> dict:
        """Get performance report from MCP server."""
        
        # Check cache first
        cache_key = self._generate_cache_key(
            company_id, dimensions, measures, start_date, end_date, filters
        )
        cached = await self._get_from_cache(cache_key)
        if cached:
            return cached
        
        # Call MCP server
        result = await self._call_mcp_server(
            "get_performance_report",
            {
                "company_id": company_id,
                "dimensions": dimensions,
                "measures": measures,
                "start_date": start_date,
                "end_date": end_date,
                "filters": filters or []
            }
        )
        
        # Cache result
        await self._save_to_cache(cache_key, result)
        
        return result
    
    async def _call_mcp_server(self, method: str, params: dict) -> dict:
        """Call MCP server method."""
        # Implementation depends on MCP protocol
        pass
```

### Caching Pattern

```python
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

class DataCache:
    """File-based cache for data."""
    
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(exist_ok=True)
    
    def _generate_key(self, **kwargs) -> str:
        """Generate cache key from parameters."""
        key_string = json.dumps(kwargs, sort_keys=True)
        return hashlib.md5(key_string.encode()).hexdigest()
    
    async def get(self, key: str) -> Optional[dict]:
        """Get cached data if not expired."""
        cache_file = self.cache_dir / f"{key}.json"
        
        if not cache_file.exists():
            return None
        
        with open(cache_file) as f:
            cached = json.load(f)
        
        # Check expiration
        cached_time = datetime.fromisoformat(cached["cached_at"])
        if datetime.now() - cached_time > timedelta(hours=1):
            return None
        
        return cached["data"]
    
    async def set(self, key: str, data: dict):
        """Save data to cache."""
        cache_file = self.cache_dir / f"{key}.json"
        
        with open(cache_file, "w") as f:
            json.dump({
                "data": data,
                "cached_at": datetime.now().isoformat()
            }, f)
```

## Error Handling Patterns

### Custom Exceptions

```python
class DashboardException(Exception):
    """Base exception for dashboard application."""
    pass

class SkillNotFoundError(DashboardException):
    """Skill not found."""
    pass

class DataFetchError(DashboardException):
    """Error fetching data."""
    pass

class ValidationError(DashboardException):
    """Data validation error."""
    pass
```

### Exception Handler

```python
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(DashboardException)
async def dashboard_exception_handler(
    request: Request, 
    exc: DashboardException
):
    """Handle application exceptions."""
    return JSONResponse(
        status_code=400,
        content={"error": str(exc), "type": type(exc).__name__}
    )

@app.exception_handler(Exception)
async def general_exception_handler(
    request: Request,
    exc: Exception
):
    """Handle unexpected exceptions."""
    # Log error
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )
```

## Validation Patterns

### Pydantic Models

```python
from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import date

class PerformanceReportRequest(BaseModel):
    """Request model for performance reports."""
    
    company_id: int = Field(..., gt=0, description="Company ID")
    dimensions: List[str] = Field(..., min_items=1, max_items=5)
    measures: List[str] = Field(..., min_items=1)
    start_date: date
    end_date: date
    filters: Optional[List[dict]] = None
    
    @validator("end_date")
    def end_after_start(cls, v, values):
        """Validate end_date is after start_date."""
        if "start_date" in values and v < values["start_date"]:
            raise ValueError("end_date must be after start_date")
        return v
    
    @validator("dimensions")
    def valid_dimensions(cls, v):
        """Validate dimensions are allowed."""
        allowed = ["day", "week", "month", "campaign", "creative", "network"]
        invalid = [d for d in v if d not in allowed]
        if invalid:
            raise ValueError(f"Invalid dimensions: {invalid}")
        return v
    
    class Config:
        json_schema_extra = {
            "example": {
                "company_id": 599,
                "dimensions": ["week", "campaign"],
                "measures": ["spend", "impressions", "conversions"],
                "start_date": "2025-01-01",
                "end_date": "2025-01-31"
            }
        }
```

## Testing Patterns

### Unit Tests

```python
import pytest
from unittest.mock import AsyncMock, Mock

@pytest.mark.asyncio
async def test_campaign_service_get_campaigns():
    """Test getting campaigns."""
    # Arrange
    mcp_client = AsyncMock()
    mcp_client.get_campaigns.return_value = [
        {"id": 1, "name": "Campaign 1"},
        {"id": 2, "name": "Campaign 2"}
    ]
    cache = AsyncMock()
    cache.get.return_value = None
    
    service = CampaignService(mcp_client, cache)
    
    # Act
    campaigns = await service.get_campaigns(company_id=599)
    
    # Assert
    assert len(campaigns) == 2
    assert campaigns[0]["name"] == "Campaign 1"
    mcp_client.get_campaigns.assert_called_once()
    cache.set.assert_called_once()

@pytest.mark.asyncio
async def test_campaign_service_uses_cache():
    """Test that service uses cached data."""
    # Arrange
    cached_data = [{"id": 1, "name": "Cached Campaign"}]
    mcp_client = AsyncMock()
    cache = AsyncMock()
    cache.get.return_value = cached_data
    
    service = CampaignService(mcp_client, cache)
    
    # Act
    campaigns = await service.get_campaigns(company_id=599)
    
    # Assert
    assert campaigns == cached_data
    mcp_client.get_campaigns.assert_not_called()
```

### Integration Tests

```python
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_get_campaigns_endpoint():
    """Test campaigns endpoint."""
    response = client.get("/api/campaigns?company_id=599")
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0

def test_create_campaign_endpoint():
    """Test creating a campaign."""
    payload = {
        "name": "Test Campaign",
        "status": "active"
    }
    
    response = client.post("/api/campaigns", json=payload)
    
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Campaign"
    assert "id" in data
```

## Best Practices

### ✅ Do

- Use async/await for I/O operations
- Validate all inputs with Pydantic
- Implement proper error handling
- Use dependency injection for services
- Cache expensive operations
- Write tests for business logic
- Use type hints everywhere
- Log important operations
- Follow RESTful conventions

### ❌ Don't

- Use synchronous I/O in async functions
- Trust user input without validation
- Expose internal errors to clients
- Put business logic in routers
- Hardcode configuration
- Skip error handling
- Use `Any` type without reason
- Return sensitive data in errors

## Related Skills

- **architecture-context/** - AI architecture principles
- **integration-patterns/** - API integration patterns
- **data-patterns/** - Data catalog and data access
- **frontend-patterns/** - Frontend API integration

---

Last Updated: 2026-01-21
