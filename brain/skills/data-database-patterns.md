# Database Patterns (Unified)

## Goal
Design efficient database schemas and optimize SQL queries for performance, scalability, and maintainability.

## When to Use
- Optimizing slow SQL queries
- Designing database schemas
- Creating effective indexes
- Analyzing query execution plans
- Implementing connection pooling
- Managing database migrations

## Query Optimization

### EXPLAIN ANALYZE
```sql
-- Always use EXPLAIN ANALYZE for actual execution stats
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT u.name, COUNT(o.id) as order_count
FROM users u
LEFT JOIN orders o ON o.user_id = u.id
WHERE u.created_at > '2024-01-01'
GROUP BY u.id;
```

### Reading Execution Plans
| Node Type       | Watch For                           |
| --------------- | ----------------------------------- |
| Seq Scan        | Missing index, small table OK       |
| Index Scan      | Good - using index                  |
| Index Only Scan | Best - all data from index          |
| Bitmap Scan     | Multiple index conditions           |
| Nested Loop     | OK for small outer, bad at scale    |
| Hash Join       | Good for large tables               |
| Sort            | Check if index can avoid sort       |

### Common Optimization Patterns

**Avoid SELECT * **
```sql
-- Bad: Fetches all columns
SELECT * FROM orders WHERE user_id = 123;

-- Good: Only needed columns
SELECT id, status, total FROM orders WHERE user_id = 123;
```

**Use EXISTS instead of COUNT**
```sql
-- Slow: Counts everything
SELECT * FROM users u 
WHERE (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) > 0;

-- Fast: Stops at first match
SELECT * FROM users u 
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id);
```

**Batch operations**
```sql
-- Slow: Many round trips
INSERT INTO logs (msg) VALUES ('msg1');
INSERT INTO logs (msg) VALUES ('msg2');
INSERT INTO logs (msg) VALUES ('msg3');

-- Fast: Single statement
INSERT INTO logs (msg) VALUES ('msg1'), ('msg2'), ('msg3');
```

## Index Strategies

### B-Tree Index (Default)
```sql
-- Standard index for equality and range queries
CREATE INDEX idx_orders_user_id ON orders(user_id);
CREATE INDEX idx_orders_created ON orders(created_at);

-- Composite index - column order matters
-- Supports: (user_id), (user_id, status), (user_id, status, created_at)
CREATE INDEX idx_orders_user_status ON orders(user_id, status);
```

### Partial Index
```sql
-- Index only active records (smaller, faster)
CREATE INDEX idx_orders_active ON orders(user_id) 
WHERE status = 'active';

-- Index only recent data
CREATE INDEX idx_orders_recent ON orders(created_at) 
WHERE created_at > '2024-01-01';
```

### Covering Index (Index-Only Scan)
```sql
-- Include all columns needed by query
CREATE INDEX idx_orders_cover ON orders(user_id) 
INCLUDE (status, total);

-- Query can be satisfied entirely from index
SELECT status, total FROM orders WHERE user_id = 123;
```

### GIN Index (Full-Text & JSON)
```sql
-- Full-text search
CREATE INDEX idx_products_search ON products 
USING GIN(to_tsvector('english', name || ' ' || description));

SELECT * FROM products 
WHERE to_tsvector('english', name || ' ' || description) @@ to_tsquery('laptop & gaming');
