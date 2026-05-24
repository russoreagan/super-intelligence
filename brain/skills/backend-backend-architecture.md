---
name: backend-architecture
description: Use when designing distributed systems with microservices, CQRS, event sourcing, sagas, and workflow orchestration patterns. Covers service decomposition, inter-service communication, and resilience.
summary: Backend architecture with microservices, CQRS, event sourcing, saga patterns, circuit breakers, and workflow orchestration.
triggers: [microservices, CQRS, event sourcing, saga, circuit breaker, distributed system, service mesh, event-driven]
disable-model-invocation: true

---
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

class CommandHandler(ABC, Generic[T]):
    @abstractmethod
    async def handle(self, command: T):
        pass

class CreateOrderHandler(CommandHandler[CreateOrder]):
    def __init__(self, repository, event_store):
        self.repository = repository
        self.event_store = event_store

    async def handle(self, command: CreateOrder) -> str:
        order = Order.create(
            customer_id=command.customer_id,
            items=command.items
        )
        await self.event_store.append(order.uncommitted_events)
        return order.id
```

### Query Implementation
```python
@dataclass
class GetOrderById:
    order_id: str

class OrderQueryHandler:
    def __init__(self, read_db):
        self.read_db = read_db

    async def handle(self, query: GetOrderById) -> OrderReadModel:
        return await self.read_db.find_one({"order_id": query.order_id})
```

## Event Sourcing

### Event Store
```python
@dataclass
class Event:
    event_id: str
    aggregate_id: str
    event_type: str
    data: dict
    timestamp: datetime
    version: int

class EventStore:
    async def append(self, stream_id: str, events: list[Event], expected_version: int):
        # Optimistic concurrency check
        current = await self.get_stream_version(stream_id)
        if current != expected_version:
            raise ConcurrencyError()
        
        await self.store.insert_many([
            {**e.__dict__, "stream_id": stream_id}
            for e in events
        ])
        
        # Publish to event bus
        for event in events:
            await self.event_bus.publish(event)

    async def load(self, stream_id: str) -> list[Event]:
        return await self.store.find({"stream_id": stream_id}).sort("version")
```

### Aggregate Reconstruction
```python
class Order:
    def __init__(self):
        self.id = None
        self.status = None
        self.items = []
        self._uncommitted = []

    @classmethod
    def from_events(cls, events: list[Event]) -> "Order":
        order = cls()
        for event in events:
            order._apply(event)
        return order

    def _apply(self, event: Event):
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event.data)

    def _on_order_created(self, data):
        self.id = data["order_id"]
        self.status = "pending"
        self.items = data["items"]

    def _on_order_shipped(self, data):
        self.status = "shipped"
```

## Saga Pattern

### Orchestration-Based Saga
```python
class OrderSaga:
    """Coordinates order creation across services."""
    
    steps = [
        ("reserve_inventory", "release_inventory"),
        ("process_payment", "refund_payment"),
        ("create_shipment", "cancel_shipment"),
    ]

    async def execute(self, order: Order):
        completed = []
        try:
            for step, compensation in self.steps:
                await getattr(self, step)(order)
                completed.append(compensation)
        except Exception as e:
            # Compensate in reverse order
            for compensation in reversed(completed):
                await getattr(self, compensation)(order)
            raise SagaFailed(e)

    async def reserve_inventory(self, order):
        await self.inventory_client.reserve(order.items)

    async def release_inventory(self, order):
        await self.inventory_client.release(order.items)

    async def process_payment(self, order):
        await self.payment_client.charge(order.customer_id, order.total)

    async def refund_payment(self, order):
        await self.payment_client.refund(order.payment_id)
```

### Choreography-Based Saga
```python
# Each service reacts to events

class PaymentService:
    async def on_order_created(self, event: OrderCreatedEvent):
        result = await self.process_payment(event.order_id, event.total)
        if result.success:
            await self.publish(PaymentCompletedEvent(order_id=event.order_id))
        else:
            await self.publish(PaymentFailedEvent(order_id=event.order_id))

class InventoryService:
    async def on_payment_completed(self, event: PaymentCompletedEvent):
        await self.reserve_items(event.order_id)
        await self.publish(InventoryReservedEvent(order_id=event.order_id))

    async def on_payment_failed(self, event: PaymentFailedEvent):
        await self.release_items(event.order_id)
```

## Resilience Patterns

### Circuit Breaker
```python
import asyncio
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = None

    async def call(self, func, *args, **kwargs):
        if self.state == CircuitState.OPEN:
            if self._should_try_reset():
                self.state = CircuitState.HALF_OPEN
            else:
                raise CircuitOpenError()

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
```

### Retry with Exponential Backoff
```python
import asyncio
from functools import wraps

def retry(max_attempts=3, base_delay=1.0, max_delay=60.0):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    await asyncio.sleep(delay)
        return wrapper
    return decorator

@retry(max_attempts=3, base_delay=1.0)
async def call_external_service():
    return await http_client.get("/api/data")
```

### Bulkhead Pattern
```python
import asyncio

class Bulkhead:
    def __init__(self, max_concurrent: int):
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def execute(self, func, *args, **kwargs):
        async with self.semaphore:
            return await func(*args, **kwargs)

# Isolate resources per service
payment_bulkhead = Bulkhead(max_concurrent=10)
inventory_bulkhead = Bulkhead(max_concurrent=20)

async def process_order(order):
    payment = await payment_bulkhead.execute(process_payment, order)
    inventory = await inventory_bulkhead.execute(reserve_inventory, order)
```

## Service Discovery

### Consul Registration
```python
import consul

class ServiceRegistry:
    def __init__(self):
        self.consul = consul.Consul()

    def register(self, service_name: str, port: int):
        self.consul.agent.service.register(
            service_name,
            service_id=f"{service_name}-{uuid.uuid4()}",
            port=port,
            check=consul.Check.http(f"http://localhost:{port}/health", interval="10s")
        )

    def discover(self, service_name: str) -> list[str]:
        _, services = self.consul.health.service(service_name, passing=True)
        return [f"{s['Service']['Address']}:{s['Service']['Port']}" for s in services]
```

## Implementation Checklist
- [ ] Services organized by business capability or subdomain
- [ ] Clear service boundaries with defined contracts
- [ ] Async communication for non-blocking operations
- [ ] CQRS implemented where read/write patterns differ
- [ ] Event sourcing for audit requirements
- [ ] Saga pattern for distributed transactions
- [ ] Circuit breakers on external calls
- [ ] Retry with exponential backoff
- [ ] Bulkheads to isolate failures
- [ ] Service discovery configured
- [ ] Health checks implemented
- [ ] Distributed tracing enabled
