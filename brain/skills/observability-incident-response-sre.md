# Incident Response & SRE (Unified)

## Goal
Build robust incident response capabilities with structured runbooks, blameless postmortems, effective on-call practices, and proactive reliability engineering.

## When to Use
- Creating incident response runbooks
- Writing postmortem documents
- Establishing on-call procedures
- Defining incident severity levels
- Implementing chaos engineering
- Building organizational learning from failures

## Incident Severity Levels

| Severity | Impact                     | Response Time     | Example                 |
| -------- | -------------------------- | ----------------- | ----------------------- |
| **SEV1** | Complete outage, data loss | 15 min            | Production down         |
| **SEV2** | Major degradation          | 30 min            | Critical feature broken |
| **SEV3** | Minor impact               | 2 hours           | Non-critical bug        |
| **SEV4** | Minimal impact             | Next business day | Cosmetic issue          |

## Incident Response Flow

```
Detection → Triage → Mitigation → Resolution → Postmortem
    ↓          ↓          ↓            ↓            ↓
  Alert    Classify    Fix now    Root cause   Learn & improve
```

## Runbook Structure

Every runbook should include:
1. Overview & Impact
2. Detection & Alerts
3. Initial Triage
4. Mitigation Steps
5. Root Cause Investigation
6. Resolution Procedures
7. Verification & Rollback
8. Communication Templates
9. Escalation Matrix

### Runbook Template

```markdown
# [Service Name] Outage Runbook

## Overview
**Service**: Payment Processing Service
**Owner**: Platform Team
**Slack**: #payments-incidents
**PagerDuty**: payments-oncall

## Impact Assessment
- [ ] Which customers are affected?
- [ ] What percentage of traffic is impacted?
- [ ] Are there financial implications?
- [ ] What's the blast radius?

## Detection
### Alerts
- `payment_error_rate > 5%` (PagerDuty)
- `payment_latency_p99 > 2s` (Slack)

### Dashboards
- [Service Dashboard](https://grafana/d/payments)
- [Error Tracking](https://sentry.io/payments)

## Initial Triage (First 5 Minutes)

### 1. Quick Health Checks
```bash
# Check service health
kubectl get pods -n payments -l app=payment-service

# Check recent deployments
kubectl rollout history deployment/payment-service -n payments

# Check error rates
curl "http://prometheus:9090/api/v1/query?query=rate(http_errors_total[5m])"
```

### 2. Classification Matrix
| Symptom              | Likely Cause        | Go To       |
| -------------------- | ------------------- | ----------- |
| All requests failing | Service down        | Section 4.1 |
| High latency         | Database/dependency | Section 4.2 |
| Partial failures     | Code bug            | Section 4.3 |
| Spike in errors      | Traffic surge       | Section 4.4 |

## Mitigation Procedures

### 4.1 Service Completely Down
```bash
# Check pod status
kubectl get pods -n payments

# Check logs
kubectl logs -n payments -l app=payment-service --tail=100

# ROLLBACK if recent deploy
kubectl rollout undo deployment/payment-service -n payments

# Scale up if needed
kubectl scale deployment/payment-service -n payments --replicas=10
```
