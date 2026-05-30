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
