---
name: database-patterns
description: Use when optimizing SQL queries, designing PostgreSQL schemas, implementing indexes, connection pooling, and query performance tuning. Covers query analysis, EXPLAIN plans, and database design patterns.
summary: Database design and SQL optimization with PostgreSQL indexes, query tuning, EXPLAIN analysis, connection pooling, and schema design patterns.
triggers: [PostgreSQL, SQL optimization, database, query tuning, index, EXPLAIN, connection pool, schema design]
disable-model-invocation: true

---
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

-- JSONB containment
CREATE INDEX idx_metadata ON products USING GIN(metadata);
SELECT * FROM products WHERE metadata @> '{"color": "red"}';
```

### Expression Index
```sql
-- Index on computed value
CREATE INDEX idx_users_lower_email ON users(LOWER(email));

-- Query must match expression exactly
SELECT * FROM users WHERE LOWER(email) = 'user@example.com';
```

## Schema Design Patterns

### Normalization vs Denormalization
```sql
-- Normalized (3NF): No data duplication
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    total DECIMAL(10,2)
);

-- Denormalized: Duplicate data for read performance
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    user_id INT,
    user_name VARCHAR(100),  -- Duplicated
    user_email VARCHAR(255), -- Duplicated
    total DECIMAL(10,2)
);
```

### Soft Deletes
```sql
CREATE TABLE posts (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255),
    deleted_at TIMESTAMP NULL
);

-- Partial index for active records only
CREATE INDEX idx_posts_active ON posts(id) WHERE deleted_at IS NULL;

-- All queries filter by deleted_at
SELECT * FROM posts WHERE deleted_at IS NULL;
```

### Audit Trail
```sql
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(50),
    record_id INT,
    action VARCHAR(10), -- INSERT, UPDATE, DELETE
    old_data JSONB,
    new_data JSONB,
    changed_by INT,
    changed_at TIMESTAMP DEFAULT NOW()
);

-- Trigger for automatic auditing
CREATE OR REPLACE FUNCTION audit_trigger() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO audit_log(table_name, record_id, action, old_data, new_data, changed_by)
    VALUES (
        TG_TABLE_NAME,
        COALESCE(NEW.id, OLD.id),
        TG_OP,
        row_to_json(OLD),
        row_to_json(NEW),
        current_setting('app.user_id', true)::int
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

### Temporal Tables (SCD Type 2)
```sql
CREATE TABLE products_history (
    id INT,
    name VARCHAR(255),
    price DECIMAL(10,2),
    valid_from TIMESTAMP NOT NULL,
    valid_to TIMESTAMP,
    CONSTRAINT pk_products_history PRIMARY KEY (id, valid_from)
);

-- Query as of specific time
SELECT * FROM products_history
WHERE id = 123 
  AND valid_from <= '2024-06-01' 
  AND (valid_to IS NULL OR valid_to > '2024-06-01');
```

## Connection Pooling

### PgBouncer Configuration
```ini
; pgbouncer.ini
[databases]
mydb = host=localhost port=5432 dbname=mydb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt

; Pool settings
pool_mode = transaction  ; transaction, session, statement
default_pool_size = 20
min_pool_size = 5
max_client_conn = 1000
max_db_connections = 100

; Timeouts
server_idle_timeout = 600
server_connect_timeout = 15
query_timeout = 300
```

### Application Connection Pool
```python
# SQLAlchemy with connection pool
from sqlalchemy import create_engine

engine = create_engine(
    "postgresql://user:pass@localhost/mydb",
    pool_size=20,           # Connections to keep open
    max_overflow=10,        # Extra connections allowed
    pool_timeout=30,        # Wait time for connection
    pool_recycle=1800,      # Recycle connections after 30 min
    pool_pre_ping=True      # Verify connection health
)
```

## Query Performance Patterns

### Pagination
```sql
-- Offset-based (slow for large offsets)
SELECT * FROM orders ORDER BY id LIMIT 20 OFFSET 1000;

-- Keyset/cursor pagination (fast at any offset)
SELECT * FROM orders 
WHERE id > 1000  -- last_seen_id from previous page
ORDER BY id LIMIT 20;
```

### Batch Processing
```sql
-- Process in batches to avoid locks
DO $$
DECLARE
    batch_size INT := 1000;
    processed INT := 0;
BEGIN
    LOOP
        UPDATE orders
        SET status = 'archived'
        WHERE id IN (
            SELECT id FROM orders 
            WHERE status = 'completed' 
              AND created_at < NOW() - INTERVAL '1 year'
            LIMIT batch_size
            FOR UPDATE SKIP LOCKED
        );
        
        GET DIAGNOSTICS processed = ROW_COUNT;
        EXIT WHEN processed = 0;
        
        COMMIT;
        PERFORM pg_sleep(0.1);  -- Reduce lock contention
    END LOOP;
END $$;
```

### Materialized Views
```sql
-- Pre-compute expensive aggregations
CREATE MATERIALIZED VIEW daily_sales AS
SELECT 
    DATE(created_at) as sale_date,
    COUNT(*) as order_count,
    SUM(total) as revenue
FROM orders
WHERE status = 'completed'
GROUP BY DATE(created_at);

-- Create index on materialized view
CREATE UNIQUE INDEX idx_daily_sales ON daily_sales(sale_date);

-- Refresh (can be scheduled)
REFRESH MATERIALIZED VIEW CONCURRENTLY daily_sales;
```

## Monitoring & Diagnostics

### Slow Query Log
```sql
-- Enable in postgresql.conf
-- log_min_duration_statement = 1000  -- Log queries > 1s

-- Find slow queries from pg_stat_statements
SELECT 
    substring(query, 1, 100) as query,
    calls,
    round(total_exec_time::numeric, 2) as total_ms,
    round(mean_exec_time::numeric, 2) as avg_ms,
    rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

### Table Statistics
```sql
-- Update statistics for optimizer
ANALYZE orders;

-- Check table stats
SELECT 
    relname,
    n_live_tup,
    n_dead_tup,
    last_vacuum,
    last_autovacuum,
    last_analyze
FROM pg_stat_user_tables
WHERE relname = 'orders';
```

### Index Usage
```sql
-- Find unused indexes
SELECT 
    schemaname,
    relname,
    indexrelname,
    idx_scan,
    pg_size_pretty(pg_relation_size(indexrelid)) as size
FROM pg_stat_user_indexes
WHERE idx_scan = 0
ORDER BY pg_relation_size(indexrelid) DESC;
```

## Implementation Checklist
- [ ] All queries use EXPLAIN ANALYZE in development
- [ ] Indexes support common query patterns
- [ ] Composite index column order matches query filters
- [ ] Partial indexes for filtered queries
- [ ] Connection pooling configured appropriately
- [ ] Slow query logging enabled
- [ ] Regular VACUUM and ANALYZE scheduled
- [ ] Unused indexes identified and removed
- [ ] Proper pagination strategy (keyset for large tables)
- [ ] Materialized views for expensive aggregations
