---
name: error-handling
description: Use when designing APIs or implementing resilient behavior: defining error categories, propagating errors safely, adding retries/backoff, and producing actionable logs/messages.
summary: Error classification, custom hierarchies, retries with backoff, graceful degradation, and logging.
triggers: [exception, error handling, retry, fallback, graceful degradation, logging, resilience]
disable-model-invocation: true

---
# Error Handling (Definitive)

## Goal
Handle failures predictably: give users safe messages, give developers actionable diagnostics, and keep systems resilient under partial failure.

## Classify errors (first step)
- **Expected / recoverable**: validation, not-found, timeouts, rate limits.
- **Unexpected / bugs**: invariants violated, null pointers, impossible states.
- **Unrecoverable**: out-of-memory, corruption, critical misconfig.
## Choose an error style
- **Exceptions**: best for unexpected failures and centralized handling.
- **Result types**: best for expected failures you want callers to handle explicitly.
- **Error codes**: best for cross-service contracts (stable, documented).

## Patterns to apply
### 1) Custom error hierarchy
- Create a base error with: code, status (if HTTP), details, and (optional) cause. Derive `Validation`, `NotFound`, `ExternalService`, etc.
### 2) Boundary handling (don’t leak internals)
- Convert internal exceptions to stable API errors at boundaries (HTTP handlers, job runners). Avoid sensitive data in error messages.
### 3) Retries with backoff
- Retry only idempotent operations. Use exponential backoff + jitter. Stop retrying on non-transient errors.
### 4) Graceful degradation
- Prefer partial results over total failure when safe. Return empty defaults only when semantically correct (and document it).
### 5) Logging for debugging
- Structured logs; include correlation IDs. Log unexpected errors at higher severity; expected errors at lower. Capture root cause + context fields.
## Checklist
- [ ] Errors are categorized and consistent
- [ ] API returns stable codes/messages
- [ ] No secrets/PII in logs or error payloads
- [ ] Retries are bounded and safe
- [ ] Monitoring/alerts exist for critical failures
