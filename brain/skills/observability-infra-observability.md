---
name: infra-observability
description: Use when instrumenting or operating platform observability (metrics/logs/traces, dashboards, alerting, and SLOs) for services and infrastructure, including service meshes and LLM systems.
summary: Three pillars observability, SLO/error-budget implementation, distributed tracing, service mesh monitoring, and LLM observability (LangSmith/Phoenix).
triggers: [observability, monitoring, metrics, traces, logs, SLO, SLI, error budget, Jaeger, Tempo, alerting]
disable-model-invocation: true

---
# Infra Observability (Definitive)

## Goal
Detect issues quickly and reduce MTTR using consistent telemetry, SLO-based alerting, and distributed tracing across services.

## When to Use
- Setting up observability for new services
- Debugging latency spikes, timeouts, or increased error rates
- Understanding request flows across microservices
- Defining and tracking SLOs/error budgets
- Building evaluation/monitoring loops for AI/LLM systems
- Implementing service mesh observability (Istio/Linkerd)

## Three Pillars of Observability

```
┌─────────────────────────────────────────────────────┐
│                  Observability                       │
├─────────────────┬─────────────────┬─────────────────┤
│     Metrics     │     Traces      │      Logs       │
│                 │                 │                 │
│ • Request rate  │ • Span context  │ • Access logs   │
│ • Error rate    │ • Latency       │ • Error details │
│ • Latency P50   │ • Dependencies  │ • Debug info    │
│ • Saturation    │ • Bottlenecks   │ • Audit trail   │
└─────────────────┴─────────────────┴─────────────────┘
```

### Golden Signals (mandatory for every service)

| Signal         | Description               | Alert Threshold   |
| -------------- | ------------------------- | ----------------- |
| **Latency**    | Request duration P50, P99 | P99 > 500ms       |
| **Traffic**    | Requests per second       | Anomaly detection |
| **Errors**     | 5xx error rate            | > 1%              |
| **Saturation** | Resource utilization      | > 80%             |

## Distributed Tracing (Jaeger/Tempo)

### Trace Structure
```
Trace (Request ID: abc123)
  ↓
Span (frontend) [100ms]
  ↓
Span (api-gateway) [80ms]
  ├→ Span (auth-service) [10ms]
  └→ Span (user-service) [60ms]
      └→ Span (database) [40ms]
```

### OpenTelemetry Setup (Python)
```python
from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Initialize tracer
resource = Resource(attributes={SERVICE_NAME: "my-service"})
provider = TracerProvider(resource=resource)
processor = BatchSpanProcessor(JaegerExporter(
    agent_host_name="jaeger",
    agent_port=6831,
))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

# Create spans for operations
tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("get_users") as span:
    span.set_attribute("user.count", 100)
    users = fetch_users_from_db()
```

### Key Practices
- Ensure context propagation across service boundaries
- Use tags/attributes for filtering (service, operation, status)
- Look for the *slowest span* to identify bottlenecks

## SLO Implementation

### SLI/SLO/SLA Hierarchy
```
SLA (Service Level Agreement)
  ↓ Contract with customers
SLO (Service Level Objective)
  ↓ Internal reliability target
SLI (Service Level Indicator)
  ↓ Actual measurement
```

### Common SLIs

**Availability SLI:**
```promql
sum(rate(http_requests_total{status!~"5.."}[28d]))
/
sum(rate(http_requests_total[28d]))
```

**Latency SLI (requests < 500ms):**
```promql
sum(rate(http_request_duration_seconds_bucket{le="0.5"}[28d]))
/
sum(rate(http_request_duration_seconds_count[28d]))
```

### Availability vs Downtime

| SLO %  | Downtime/Month | Downtime/Year |
| ------ | -------------- | ------------- |
| 99%    | 7.2 hours      | 3.65 days     |
| 99.9%  | 43.2 minutes   | 8.76 hours    |
| 99.95% | 21.6 minutes   | 4.38 hours    |
| 99.99% | 4.32 minutes   | 52.56 minutes |

### Error Budget Policy
```yaml
error_budget_policy:
  - remaining_budget: 100%
    action: Normal development velocity
  - remaining_budget: 50%
    action: Consider postponing risky changes
  - remaining_budget: 10%
    action: Freeze non-critical changes
  - remaining_budget: 0%
    action: Feature freeze, focus on reliability
```

### SLO-Based Alerting (burn-rate)
Prefer burn-rate alerts over noisy per-incident alerts:
- **Fast burn**: 14.4x rate, 1-hour window → consumes 2% budget in 1 hour
- **Slow burn**: 6x rate, 6-hour window → consumes 5% budget in 6 hours

## Service Mesh Observability (Istio/Linkerd)

### Key Istio Metrics Queries
```promql
# Request rate by service
sum(rate(istio_requests_total{reporter="destination"}[5m])) 
  by (destination_service_name)

# Error rate (5xx)
sum(rate(istio_requests_total{reporter="destination", response_code=~"5.."}[5m]))
  / sum(rate(istio_requests_total{reporter="destination"}[5m])) * 100

# P99 latency
histogram_quantile(0.99,
  sum(rate(istio_request_duration_milliseconds_bucket{reporter="destination"}[5m]))
  by (le, destination_service_name))
```

### Linkerd CLI Observability
```bash
# Top requests
linkerd viz top deploy/my-app

# Per-route metrics
linkerd viz routes deploy/my-app --to deploy/backend

# Service edges (dependencies)
linkerd viz edges deployment -n my-namespace
```

## LLM/AI System Observability

### LangSmith (managed platform)
```python
from langsmith import traceable
from openai import OpenAI

client = OpenAI()

@traceable
def generate_response(prompt: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content
```

### Phoenix (open-source, self-hosted)
```python
from phoenix.otel import register
from openinference.instrumentation.openai import OpenAIInstrumentor

# Configure OpenTelemetry with Phoenix
tracer_provider = register(
    project_name="my-llm-app",
    endpoint="http://localhost:6006/v1/traces"
)

# Instrument OpenAI SDK
OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
```

### LLM Observability Checklist
- [ ] Trace prompts/inputs/outputs
- [ ] Track latency per model/provider
- [ ] Monitor token usage and cost
- [ ] Build evaluation datasets from production traces
- [ ] Run regression tests with LLM-as-judge evaluators

## Implementation Checklist
- [ ] Golden signals dashboard for every service
- [ ] Distributed tracing instrumented with correlation IDs
- [ ] SLIs defined and SLO targets set
- [ ] Burn-rate alerting configured
- [ ] Runbooks linked to top alerts
- [ ] LLM traces captured (if applicable)
