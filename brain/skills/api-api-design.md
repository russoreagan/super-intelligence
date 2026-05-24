---
name: api-design
description: Unified API design skill combining REST/GraphQL best practices, consistent error/pagination/versioning, and contract-first typed API patterns.
summary: REST/GraphQL API design with consistent contracts, error handling, pagination, and typed clients.
triggers: [API, endpoint, REST, GraphQL, contract, request, response, route]
disable-model-invocation: true

---
# API Design (Unified)

## Intent
Use when designing or reviewing APIs (REST/GraphQL), defining contracts, or integrating frontend↔backend with typed clients.

## Canonical principles
- **Resource-oriented REST**: nouns in URLs, HTTP methods for actions.
- **Consistent contracts**: stable request/response shapes, typed inputs/outputs.
- **Predictable errors**: one error schema across endpoints.
- **Pagination + filtering**: standard patterns for collections.
- **Versioning & change management**: plan breaking changes.
- **Security by default**: authn/authz, validation, rate limits.

## REST (baseline)
- Collections: `GET /resources`, `POST /resources`
- Items: `GET/PUT/PATCH/DELETE /resources/{id}`
- Prefer shallow nesting.

### Resource naming
- Plural nouns: `GET /users`, not `/user`.
- Hierarchical: `GET /users/{id}/orders`; avoid flat query params for relationships.
- Kebab-case for multi-word: `/shopping-carts`, `/order-items`.

### HTTP semantics
- **GET**: retrieve (idempotent, safe). **POST**: create. **PUT**: replace whole resource. **PATCH**: partial update. **DELETE**: remove.
- Success: 200 OK, 201 Created (+ `Location`), 204 No Content. Client errors: 400, 401, 403, 404, 409, 422.

### Standard collection parameters
- `page`, `page_size` (or cursor-based), `sort`, `search`, filters.

### Error contract
Return structured errors: error code/type, human message, optional details (field-level), request path/timestamp.

## GraphQL (when appropriate)
- Schema-first types and inputs.
- Cursor pagination (Relay style) for large lists.
- Use DataLoader patterns to avoid N+1.

## Contract-first typed APIs (frontend integration)
1. Define a contract per domain: path, method, input, output.
2. Register contracts in a router/module tree (no barrel imports).
3. Generate/use typed client + typed query keys.
4. Build hooks/services on top of those typed contracts.

## Review checklist (API changes)
- Correct method semantics (GET safe/idempotent, etc.)
- Naming consistency across endpoints
- Authz checks and validation
- Pagination for collections
- Backward compatibility / versioning plan
- Docs/examples updated
