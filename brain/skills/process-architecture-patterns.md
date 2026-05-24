---
name: architecture-patterns
description: Guiding principles for Clean Architecture, Hexagonal, and DDD. Use when planning how to structure or refactor backend code.
disable-model-invocation: true

---
# Architecture Patterns — Principles Reference

## Clean Architecture
- Dependencies point inward: Entities → Use Cases → Interface Adapters → Frameworks
- Inner layers know nothing about outer layers
- Business logic is independent of frameworks, UI, and databases
- Structure: `domain/` (entities, interfaces), `use_cases/`, `adapters/` (repos, controllers), `infrastructure/`

## Hexagonal (Ports & Adapters)
- Domain core contains business logic; ports are abstract interfaces; adapters implement them
- Swap implementations without touching core (e.g. mock for tests, real DB for prod)
- When adding a new integration: define a Port (ABC), write an Adapter, inject at startup

## Domain-Driven Design
- **Entities**: have identity, mutable state, own business rules as methods
- **Value Objects**: immutable, defined by attributes (`@dataclass(frozen=True)`)
- **Aggregates**: consistency boundary — only persist/load via aggregate root
- **Repositories**: abstract persistence behind an interface, reconstruct aggregates
- **Domain Events**: record what happened, publish after successful persistence

## Key Rules
1. Never put business logic in controllers or adapters — delegate to use cases/domain
2. Controllers should only translate HTTP ↔ domain types
3. Repositories return domain entities, not ORM rows
4. Keep bounded contexts explicit — don't let models from different domains bleed together
5. Prefer rich domain models (behavior + data) over anemic ones (data only)
6. Don't apply Clean Architecture to simple CRUD — it adds overhead without benefit
