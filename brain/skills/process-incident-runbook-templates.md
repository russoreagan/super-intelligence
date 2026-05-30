# Incident Runbook Templates

Production-ready templates for incident response runbooks covering detection, triage, mitigation, resolution, and communication.

## When to Use This Skill

- Creating incident response procedures
- Building service-specific runbooks
- Establishing escalation paths
- Documenting recovery procedures
- Responding to active incidents
- Onboarding on-call engineers

## Core Concepts

### 1. Incident Severity Levels

| Severity | Impact | Response Time | Example |
|----------|--------|---------------|---------|
| **SEV1** | Complete outage, data loss | 15 min | Production down |
| **SEV2** | Major degradation | 30 min | Critical feature broken |
| **SEV3** | Minor impact | 2 hours | Non-critical bug |
| **SEV4** | Minimal impact | Next business day | Cosmetic issue |

### 2. Runbook Structure

```
1. Overview & Impact
2. Detection & Alerts
3. Initial Triage
4. Mitigation Steps
5. Root Cause Investigation
6. Resolution Procedures
7. Verification & Rollback
8. Communication Templates
9. Escalation Matrix
```

## Runbook Templates

### Template 1: Service Outage Runbook

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
- `payment_success_rate < 95%` (PagerDuty)

### Dashboards
- [Payment Service Dashboard](https://grafana/d/payments)
- [Error Tracking](https://sentry.io/payments)
- [Dependency Status](https://status.stripe.com)

## Initial Triage (First 5 Minutes)

### 1. Assess Scope
```bash
# Check service health
kubectl get pods -n payments -l app=payment-service

# Check recent deployments
kubectl rollout history deployment/payment-service -n payments

# Check error rates
curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(http_requests_total{status=~'5..'}[5m]))"
```

### 2. Quick Health Checks
- [ ] Can you reach the service? `curl -I https://api.company.com/payments/health`
- [ ] Database connectivity? Check connection pool metrics
- [ ] External dependencies? Check Stripe, bank API status
- [ ] Recent changes? Check deploy history

### 3. Initial Classification
| Symptom | Likely Cause | Go To Section |
|---------|--------------|---------------|
| All requests failing | Service down | Section 4.1 |
| High latency | Database/dependency | Section 4.2 |
| Partial failures | Code bug | Section 4.3 |
| Spike in errors | Traffic surge | Section 4.4 |

## Mitigation Procedures

### 4.1 Service Completely Down
```bash
# Step 1: Check pod status
kubectl get pods -n payments

# Step 2: If pods are crash-looping, check logs
kubectl logs -n payments -l app=payment-service --tail=100

# Step 3: Check recent deployments
kubectl rollout history deployment/payment-service -n payments

# Step 4: ROLLBACK if recent deploy is suspect
kubectl rollout undo deployment/payment-service -n payments
