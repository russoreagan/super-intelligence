# PostgreSQL Migrations Skill

## Common PostgreSQL Migration Errors and Solutions

### 1. "Subquery uses ungrouped column from outer query"

**Cause**: Subquery in SELECT/CASE references columns from outer query that aren't in GROUP BY.

**Solution**: Use CTE (Common Table Expression) to separate aggregation from subqueries:

```sql
-- ❌ Bad - subquery references ungrouped p.id
SELECT
  SPLIT_PART(p.id, '/', 1) as author,
  COUNT(*) as count,
  CASE WHEN EXISTS (
    SELECT 1 FROM users WHERE username = SPLIT_PART(p.id, '/', 1)
  ) THEN TRUE ELSE FALSE END as claimed
FROM packages p
GROUP BY SPLIT_PART(p.id, '/', 1);

-- ✅ Good - use CTE to compute aggregates first
WITH author_stats AS (
  SELECT
    SPLIT_PART(p.id, '/', 1) as author,
    COUNT(*) as count
  FROM packages p
  GROUP BY SPLIT_PART(p.id, '/', 1)
)
SELECT
  author,
  count,
  EXISTS (SELECT 1 FROM users WHERE username = author_stats.author) as claimed
FROM author_stats;
```

### 2. "Functions in index expression must be marked IMMUTABLE"

**Cause**: PostgreSQL requires functions in indexes/generated columns to be IMMUTABLE.

**Problem Functions**:
- `array_to_string()` - marked STABLE, not IMMUTABLE
- `to_char()` - depends on timezone/locale settings
- `now()` - changes over time

**Solution**: Create IMMUTABLE wrapper functions:

```sql
-- Create IMMUTABLE wrapper for array_to_string
CREATE OR REPLACE FUNCTION immutable_array_to_string(text[], text)
RETURNS text AS $$
  SELECT array_to_string($1, $2)
$$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE;

-- Use in generated column
ALTER TABLE packages
ADD COLUMN search_vector tsvector
GENERATED ALWAYS AS (
  setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
  setweight(to_tsvector('english', immutable_array_to_string(tags, ' ')), 'B')
) STORED;

-- Now you can index it
CREATE INDEX idx_search ON packages USING gin(search_vector);
```

### 3. "Relation does not exist" (Extensions)

**Cause**: Extension not installed (e.g., `pg_stat_statements`, `pg_trgm`, `uuid-ossp`).

**Solution**: Make extension usage optional with error handling:

```sql
-- Try to create extension, ignore if unavailable
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    BEGIN
      CREATE EXTENSION pg_trgm;
    EXCEPTION
      WHEN insufficient_privilege OR feature_not_supported THEN
        RAISE NOTICE 'pg_trgm extension not available - skipping trigram indexes';
    END;
  END IF;
END $$;

-- Only create trigram indexes if extension exists
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    CREATE INDEX idx_name_trgm ON packages USING gin(name gin_trgm_ops);
  END IF;
END $$;
```

### 4. Idempotent Migrations

**Always use IF (NOT) EXISTS** to make migrations re-runnable:

```sql
-- Tables
CREATE TABLE IF NOT EXISTS users (...);

-- Columns
ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Drop operations
DROP TABLE IF EXISTS old_table CASCADE;
DROP INDEX IF EXISTS old_index;
DROP VIEW IF EXISTS old_view CASCADE;
DROP FUNCTION IF EXISTS old_function(args);
