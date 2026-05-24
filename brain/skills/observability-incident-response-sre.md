---
name: incident-response-sre
description: Use when building incident response procedures, writing blameless postmortems, creating runbooks, managing on-call rotations, or implementing SRE practices including chaos engineering.
summary: Incident response with runbook templates, blameless postmortems, on-call patterns, severity levels, and chaos engineering practices.
triggers: [incident, runbook, postmortem, on-call, SRE, outage, severity, chaos engineering, blameless]
disable-model-invocation: true

---
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

### 4.2 High Latency (Database)
```bash
# Check connection pool
kubectl exec -n payments deploy/payment-service -- curl localhost:8080/metrics | grep db_pool

# Find slow queries
psql -c "SELECT pid, now() - query_start AS duration, query 
         FROM pg_stat_activity 
         WHERE state = 'active' AND duration > interval '5 seconds';"

# Kill long-running queries
psql -c "SELECT pg_terminate_backend(pid);"
```

## Escalation Matrix
| Level | Contact          | When to Escalate           |
| ----- | ---------------- | -------------------------- |
| L1    | On-call engineer | Auto via PagerDuty         |
| L2    | Team lead        | > 15 min unresolved        |
| L3    | VP Engineering   | SEV1 > 30 min, data loss   |
| Exec  | CTO              | Customer impact > 1 hour   |

## Communication Templates
**Status Page Update:**
> We are investigating elevated error rates in our payment system. 
> Some customers may experience issues completing transactions.
> We will provide an update within 30 minutes.
```

## Blameless Postmortem

### Core Principles

| Blame-Focused            | Blameless                         |
| ------------------------ | --------------------------------- |
| "Who caused this?"       | "What conditions allowed this?"   |
| "Someone made a mistake" | "The system allowed this mistake" |
| Punish individuals       | Improve systems                   |
| Fear of speaking up      | Psychological safety              |

### Postmortem Triggers
- SEV1 or SEV2 incidents
- Customer-facing outages > 15 minutes
- Data loss or security incidents
- Near-misses that could have been severe
- Novel failure modes

### Postmortem Timeline
```
Day 0:     Incident occurs
Day 1-2:   Draft postmortem document
Day 3-5:   Postmortem meeting
Day 5-7:   Finalize document, create tickets
Week 2+:   Action item completion
Quarterly: Review patterns
```

### Postmortem Template

```markdown
# Postmortem: [Incident Title]

**Date**: 2024-01-15
**Authors**: @alice, @bob
**Status**: Draft | In Review | Final
**Severity**: SEV2
**Duration**: 47 minutes

## Executive Summary
On January 15, 2024, the payment service experienced a 47-minute outage 
affecting 12,000 customers. Root cause was database connection pool 
exhaustion triggered by deployment v2.3.4. Resolved by rolling back.

**Impact**:
- 12,000 customers unable to complete purchases
- Estimated revenue loss: $45,000
- 847 support tickets created
- No data loss

## Timeline (UTC)
| Time  | Event                                      |
| ----- | ------------------------------------------ |
| 14:23 | Deployment v2.3.4 completed                |
| 14:31 | First alert: payment_error_rate > 5%       |
| 14:33 | On-call acknowledges alert                 |
| 14:45 | DB connection exhaustion identified        |
| 14:52 | Decision to rollback                       |
| 15:10 | Rollback complete, error rate dropping     |
| 15:18 | Service fully recovered                    |

## Root Cause Analysis

### What Happened
Deployment v2.3.4 changed database query pattern that removed connection 
pooling. Each request opened new connection instead of reusing pool.

### 5 Whys
1. Why did service fail? → Database connections exhausted
2. Why were connections exhausted? → Each request opened new connection
3. Why did each request open new? → Code bypassed connection pool
4. Why did code bypass pool? → Developer unfamiliar with codebase
5. Why unfamiliar? → No documentation on connection patterns

### Contributing Factors
- Code review missed connection handling change
- No integration tests for connection pool behavior
- Staging environment has lower traffic (masked issue)
- DB connection alert threshold too high (90%)

## What Worked
- Error rate alert fired within 8 minutes
- On-call response was swift (2 min acknowledgment)
- Rollback decision was decisive

## What Didn't Work
- No deployment-correlated alerting
- Took 10 minutes to correlate with recent deploy
- Canary deployment would have caught this

## Action Items
| Priority | Action                              | Owner  | Due    |
| -------- | ----------------------------------- | ------ | ------ |
| P0       | Add connection pool integration test| @alice | Jan 20 |
| P1       | Lower DB connection alert threshold | @bob   | Jan 22 |
| P1       | Document connection management      | @alice | Jan 25 |
| P2       | Implement canary deployments        | @infra | Feb 15 |
```

## On-Call Practices

### Shift Handoff Checklist
```markdown
## Handoff: [Date] [Outgoing] → [Incoming]

### Active Issues
- [ ] Issue 1: Status, next steps
- [ ] Issue 2: Status, next steps

### Recent Incidents
- [Link] SEV2 - Payment outage (resolved)

### Upcoming Changes
- Deploy v2.3.5 scheduled for tomorrow 10 AM

### Watch Items
- DB CPU elevated but stable
- New feature flag enabled for 10% users
```

### On-Call Health
- **Pager load**: Track alerts per shift
- **MTTR**: Time to resolve incidents
- **Escalation rate**: How often L2+ needed
- **Sleep interruptions**: Night pages per week

## Chaos Engineering

### Principles
1. Start with hypothesis about steady state
2. Vary real-world events (failure injection)
3. Run experiments in production (safely)
4. Automate experiments to run continuously
5. Minimize blast radius

### Chaos Experiments
```yaml
# Chaos Monkey - Random instance termination
experiment:
  name: "EC2 Instance Failure"
  hypothesis: "System tolerates single instance failure"
  action:
    type: "terminate_instance"
    target: "random"
    asg: "payment-service"
  steady_state:
    - error_rate < 1%
    - latency_p99 < 500ms
  rollback:
    - scale_up: 1
```

### Experiment Types
| Type              | What It Tests           | Example                     |
| ----------------- | ----------------------- | --------------------------- |
| Instance failure  | Auto-scaling, redundancy| Kill random pod             |
| Network partition | Timeout handling        | Block traffic between zones |
| Latency injection | Timeout configuration   | Add 5s delay to DB calls    |
| Resource exhaust  | Memory/CPU limits       | Fill disk, exhaust memory   |
| Dependency failure| Graceful degradation    | Block external API calls    |

## Implementation Checklist
- [ ] Severity levels defined and communicated
- [ ] Runbooks created for critical services
- [ ] Postmortem process documented
- [ ] Postmortem template available
- [ ] On-call rotation established
- [ ] Handoff procedure documented
- [ ] Escalation paths clear
- [ ] Communication templates ready
- [ ] Chaos experiments designed
- [ ] Action item tracking in place
