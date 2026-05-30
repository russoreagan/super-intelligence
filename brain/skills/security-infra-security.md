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
