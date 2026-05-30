# Backend Architecture Patterns (Unified)

## Goal
Design scalable, resilient distributed systems using proven architectural patterns for service decomposition, data management, and inter-service communication.

## When to Use
- Decomposing monoliths into microservices
- Designing service boundaries and contracts
- Implementing inter-service communication
- Managing distributed data and transactions
- Building resilient distributed systems
- Implementing event-driven architectures

## Service Decomposition Strategies

### By Business Capability
Organize services around business functions. Each service owns its domain.

```
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│   Order     │ │   Payment   │ │  Inventory  │
│   Service   │ │   Service   │ │   Service   │
└──────┬──────┘ └──────┬──────┘ └──────┬──────┘
       │               │               │
       └───────────────┴───────────────┘
                Message Broker
```

### By Subdomain (DDD)
- **Core Domain**: What makes your business unique
- **Supporting Subdomain**: Necessary but not differentiating
- **Generic Subdomain**: Common problems with existing solutions

### Strangler Fig Pattern
Gradually extract from monolith:
1. New functionality as microservices
2. Proxy routes to old/new systems
3. Incrementally migrate features

## Communication Patterns

### Synchronous (Request/Response)

| Protocol | Best For               | Trade-offs                |
| -------- | ---------------------- | ------------------------- |
| REST     | Simple CRUD, public API| Latency, coupling         |
| gRPC     | Internal services      | Complexity, binary format |
| GraphQL  | Flexible client queries| Query complexity          |

### Asynchronous (Event-Driven)

| Pattern    | Use Case                  | Implementation        |
| ---------- | ------------------------- | --------------------- |
| Pub/Sub    | Fan-out notifications     | Kafka, SNS, EventBridge|
| Queue      | Work distribution         | SQS, RabbitMQ         |
| Streaming  | Real-time data processing | Kafka, Kinesis        |

## CQRS Pattern

### Architecture
```
              ┌─────────────┐
              │   Client    │
              └──────┬──────┘
                     │
        ┌────────────┴────────────┐
        │                         │
        ▼                         ▼
 ┌─────────────┐          ┌─────────────┐
 │  Commands   │          │   Queries   │
 │    API      │          │    API      │
 └──────┬──────┘          └──────┬──────┘
        │                         │
        ▼                         ▼
 ┌─────────────┐          ┌─────────────┐
 │   Write     │──Events─▶│    Read     │
 │   Model     │          │   Model     │
 └─────────────┘          └─────────────┘
```

### Command Implementation
```python
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

@dataclass
class Command:
    command_id: str = None

@dataclass
class CreateOrder(Command):
    customer_id: str
    items: list
    shipping_address: dict

@dataclass
class CancelOrder(Command):
    order_id: str
    reason: str

T = TypeVar('T', bound=Command)
