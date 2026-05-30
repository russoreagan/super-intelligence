# Observability & Performance (Definitive)

## Goal
Find and prevent regressions by combining:\n+- the **three pillars** (metrics, traces, logs)\n+- **SLOs/error budgets** to define “good”\n+- **profiling** to remove bottlenecks\n+
## When to use
- Latency spikes, timeouts, throughput drops.\n+- Increased error rates or flaky behavior.\n+- You need to understand request flows across services.\n+- You’re building an evaluation/monitoring loop for AI/LLM systems.\n+
## The three pillars (practical)
### Metrics
- Golden signals: **latency, traffic, errors, saturation**.\n+- Use Prometheus/Grafana-style dashboards.\n+
### Traces
- Use distributed tracing (OpenTelemetry) to follow one request end-to-end.\n+- Look for the *slowest span* and dependency bottlenecks.\n+
### Logs
- Structured logs; correlate with trace IDs.\n+- Avoid sensitive data in logs.\n+
## SLOs and error budgets
### Define SLIs (what you measure)
- Availability (non-5xx / total)\n+- Latency (p95 below threshold)\n+- Durability (successful writes)\n+
### Set SLO targets (what you aim for)
- Pick targets based on user expectations and cost.\n+- Treat remaining error budget as a release-velocity control.\n+
### Alerting
- Prefer burn-rate alerts (fast + slow) over noisy per-incident alerts.\n+
## Tooling patterns
### Distributed tracing (Jaeger/Tempo)
- Instrument services with OpenTelemetry.\n+- Ensure context propagation.\n+- Use tags/attributes to filter traces.\n+
### Service mesh observability (Istio/Linkerd)
- Dashboards for golden signals.\n+- Service dependency maps/edges.\n+- Mesh-level latency and error queries.\n+
### LLM observability (LangSmith / Phoenix)
- Trace prompts/inputs/outputs, latency, and token/cost.\n+- Build datasets from production traces.\n+- Run evaluations (LLM-as-judge or custom evaluators).\n+- Compare prompts/models via experiments.\n+
## Performance optimization workflow
1. **Measure**: pick a baseline (latency p95, CPU, memory, cost).\n+2. **Profile**: CPU (cProfile), line profiler, memory profiler.\n+3. **Fix biggest bottleneck first** (algorithmic > micro-optimizations).\n+4. **Verify** with before/after numbers and regression tests.\n+
