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
