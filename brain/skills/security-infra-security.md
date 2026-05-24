---
name: infra-security
description: Use when hardening infrastructure and delivery pipelines with STRIDE threat modeling, Kubernetes security policies, defense-in-depth controls, and DevSecOps scanning automation.
summary: Infrastructure hardening with STRIDE threat modeling, K8s security policies, NetworkPolicies, defense-in-depth controls, and DevSecOps scanning.
triggers: [infrastructure security, RBAC, NetworkPolicy, pod security, threat model, STRIDE, SAST, DevSecOps]
disable-model-invocation: true

---
# Infra Security (Definitive)

## Goal
Reduce security risk with defense-in-depth across infrastructure, deployment, and operations using systematic threat modeling and layered controls.

## When to Use
- Hardening Kubernetes clusters
- Conducting threat modeling for infrastructure
- Implementing security requirements from threats
- Setting up DevSecOps scanning pipelines
- Designing network segmentation and access controls

## STRIDE Threat Modeling

### STRIDE Categories
```
S - Spoofing       → Authentication threats
T - Tampering      → Integrity threats
R - Repudiation    → Non-repudiation threats
I - Information    → Confidentiality threats
    Disclosure
D - Denial of      → Availability threats
    Service
E - Elevation of   → Authorization threats
    Privilege
```

### Threat Analysis Matrix

| Category            | Question                                  | Control Family |
| ------------------- | ----------------------------------------- | -------------- |
| **Spoofing**        | Can attacker pretend to be someone else?  | Authentication |
| **Tampering**       | Can attacker modify data in transit/rest? | Integrity      |
| **Repudiation**     | Can attacker deny actions?                | Logging/Audit  |
| **Info Disclosure** | Can attacker access unauthorized data?    | Encryption     |
| **DoS**             | Can attacker disrupt availability?        | Rate limiting  |
| **Elevation**       | Can attacker gain higher privileges?      | Authorization  |

### Threat Model Document Template
```markdown
# Threat Model: [System Name]

## 1. System Overview
- Description: [Brief description]
- Data Flow Diagram: [DFD with trust boundaries]
- Trust Boundaries: External → DMZ → Internal → Database

## 2. Assets
| Asset | Sensitivity | Description |
|-------|-------------|-------------|
| User Credentials | High | Auth tokens, passwords |
| Personal Data | High | PII, financial info |
| Configuration | High | Secrets, system settings |

## 3. STRIDE Analysis
### 3.1 Spoofing Threats
| ID | Threat | Target | Impact | Likelihood |
|----|--------|--------|--------|------------|
| S1 | Session hijacking | User sessions | High | Medium |

**Mitigations:** MFA, secure session management, account lockout

## 4. Risk Matrix
              IMPACT
         Low  Med  High Crit
    Low   1    2    3    4
L   Med   2    4    6    8
I   High  3    6    9   12
K   Crit  4    8   12   16
```

## Defense in Depth

### Control Layers
```
                    ┌──────────────────────┐
                    │      Perimeter       │ ← Firewall, WAF
                    │   ┌──────────────┐   │
                    │   │   Network    │   │ ← Segmentation, IDS
                    │   │  ┌────────┐  │   │
                    │   │  │  Host  │  │   │ ← EDR, Hardening
                    │   │  │ ┌────┐ │  │   │
                    │   │  │ │App │ │  │   │ ← Auth, Validation
                    │   │  │ │Data│ │  │   │ ← Encryption
                    │   │  │ └────┘ │  │   │
                    │   │  └────────┘  │   │
                    │   └──────────────┘   │
                    └──────────────────────┘
```

### Control Types

| Type         | Purpose                   | Examples                    |
| ------------ | ------------------------- | --------------------------- |
| Preventive   | Stop attacks before occur | Firewall, input validation  |
| Detective    | Identify attacks in progress | IDS, log monitoring      |
| Corrective   | Respond and recover       | Incident response, backup   |

## Kubernetes Security

### Pod Security Standards
Apply appropriate standard per namespace:
- **Privileged**: Unrestricted (only for system workloads)
- **Baseline**: Minimally restrictive, prevents known privilege escalations
- **Restricted**: Heavily restricted, follows hardening best practices

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: production
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/audit: restricted
```

### Secure Pod Configuration
```yaml
apiVersion: v1
kind: Pod
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: app
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true
    resources:
      limits:
        memory: "512Mi"
        cpu: "500m"
      requests:
        memory: "256Mi"
        cpu: "250m"
```

### NetworkPolicy (default-deny + explicit allows)
```yaml
# Default deny all ingress/egress
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: production
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress

---
# Allow specific traffic
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-to-api
spec:
  podSelector:
    matchLabels:
      app: api
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: frontend
    ports:
    - protocol: TCP
      port: 8080
```

### RBAC Best Practices
- Use namespaced roles over cluster roles when possible
- Follow least-privilege principle
- Audit permissions regularly
- Use service accounts per workload

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: app-reader
  namespace: production
rules:
- apiGroups: [""]
  resources: ["configmaps", "secrets"]
  verbs: ["get", "list"]
  resourceNames: ["app-config", "app-secrets"]
```

## Security Requirements from Threats

### Requirement Types

| Type               | Focus                   | Example                               |
| ------------------ | ----------------------- | ------------------------------------- |
| **Functional**     | What system must do     | "System must authenticate users"      |
| **Non-functional** | How system must perform | "Authentication must complete in <2s" |
| **Constraint**     | Limitations imposed     | "Must use approved crypto libraries"  |

### Converting Threats to Requirements
```
Threat (STRIDE)  →  Security Requirement  →  Technical Control
     ↓                      ↓                        ↓
"SQL Injection"    "Validate all inputs"    "Parameterized queries +
                                             input validation library"
```

### Security User Story Template
```markdown
**SEC-001: Input Validation**

As a security-conscious system,
I need to validate and sanitize all user inputs,
So that SQL injection and XSS attacks are prevented.

**Acceptance Criteria:**
- [ ] All API inputs validated against schema
- [ ] Special characters escaped in database queries
- [ ] User-generated content sanitized before rendering

**Priority:** Critical
**Threat References:** T1-SQL-Injection, I2-XSS
```

## DevSecOps Scanning

### CI/CD Security Pipeline
```yaml
stages:
  - lint
  - sast
  - test
  - container-scan
  - deploy

sast:
  stage: sast
  script:
    - semgrep --config auto --json -o sast-results.json .
  allow_failure: false  # Block on critical findings

container-scan:
  stage: container-scan
  script:
    - trivy image --severity HIGH,CRITICAL --exit-code 1 $IMAGE
```

### SAST Tool Recommendations
- **Semgrep**: Fast, customizable rules, good for most languages
- **CodeQL**: Deep analysis, good for security research
- **SonarQube**: Comprehensive, includes code quality

### Scanning Strategy
1. Start in "report-only" mode
2. Triage findings and tune false positives
3. Gate critical findings (block merge)
4. Gradually increase severity thresholds

## Implementation Checklist
- [ ] Threat model documented with STRIDE analysis
- [ ] Security requirements derived from threats
- [ ] Kubernetes: Pod Security Standards enforced
- [ ] Kubernetes: NetworkPolicies with default-deny
- [ ] Kubernetes: RBAC with least privilege
- [ ] Secure pod configurations (non-root, read-only, dropped capabilities)
- [ ] SAST scanning in CI pipeline
- [ ] Container image scanning enabled
- [ ] Secrets stored in secret manager (not in Git)
- [ ] Audit logging enabled
