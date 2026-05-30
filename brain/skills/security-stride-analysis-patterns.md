# STRIDE Analysis Patterns

Systematic threat identification using the STRIDE methodology.

## When to Use This Skill

- Starting new threat modeling sessions
- Analyzing existing system architecture
- Reviewing security design decisions
- Creating threat documentation
- Training teams on threat identification
- Compliance and audit preparation

## Core Concepts

### 1. STRIDE Categories

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

### 2. Threat Analysis Matrix

| Category | Question | Control Family |
|----------|----------|----------------|
| **Spoofing** | Can attacker pretend to be someone else? | Authentication |
| **Tampering** | Can attacker modify data in transit/rest? | Integrity |
| **Repudiation** | Can attacker deny actions? | Logging/Audit |
| **Info Disclosure** | Can attacker access unauthorized data? | Encryption |
| **DoS** | Can attacker disrupt availability? | Rate limiting |
| **Elevation** | Can attacker gain higher privileges? | Authorization |

## Templates

### Template 1: STRIDE Threat Model Document

```markdown
# Threat Model: [System Name]

## 1. System Overview

### 1.1 Description
[Brief description of the system and its purpose]

### 1.2 Data Flow Diagram
```
[User] --> [Web App] --> [API Gateway] --> [Backend Services]
                              |
                              v
                        [Database]
```

### 1.3 Trust Boundaries
- **External Boundary**: Internet to DMZ
- **Internal Boundary**: DMZ to Internal Network
- **Data Boundary**: Application to Database

## 2. Assets

| Asset | Sensitivity | Description |
|-------|-------------|-------------|
| User Credentials | High | Authentication tokens, passwords |
| Personal Data | High | PII, financial information |
| Session Data | Medium | Active user sessions |
| Application Logs | Medium | System activity records |
| Configuration | High | System settings, secrets |

## 3. STRIDE Analysis

### 3.1 Spoofing Threats

| ID | Threat | Target | Impact | Likelihood |
|----|--------|--------|--------|------------|
| S1 | Session hijacking | User sessions | High | Medium |
| S2 | Token forgery | JWT tokens | High | Low |
| S3 | Credential stuffing | Login endpoint | High | High |

**Mitigations:**
- [ ] Implement MFA
- [ ] Use secure session management
- [ ] Implement account lockout policies

### 3.2 Tampering Threats

| ID | Threat | Target | Impact | Likelihood |
|----|--------|--------|--------|------------|
| T1 | SQL injection | Database queries | Critical | Medium |
| T2 | Parameter manipulation | API requests | High | High |
| T3 | File upload abuse | File storage | High | Medium |

**Mitigations:**
- [ ] Input validation on all endpoints
- [ ] Parameterized queries
- [ ] File type validation

### 3.3 Repudiation Threats
