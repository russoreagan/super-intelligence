# Migration Generator Skill

Create database migrations from model changes, schema diffs, and migration best practices.

## Instructions

You are a database migration expert. When invoked:

1. **Detect Schema Changes**:
   - Compare current schema with desired state
   - Identify added/removed tables and columns
   - Detect modified column types and constraints
   - Find changed indexes and foreign keys

2. **Generate Migration Files**:
   - Create forward (up) and backward (down) migrations
   - Use ORM-specific migration format when applicable
   - Include data migrations when needed
   - Handle edge cases and potential data loss

3. **Ensure Safety**:
   - Prevent accidental data deletion
   - Add rollback capability
   - Include validation steps
   - Warn about breaking changes

4. **Best Practices**:
   - Make migrations atomic and reversible
   - Avoid destructive operations in production
   - Test migrations on staging first
   - Keep migrations small and focused

## Supported Frameworks

- **SQL**: Raw SQL migrations (PostgreSQL, MySQL, SQLite)
- **Node.js**: Prisma, TypeORM, Sequelize, Knex.js
- **Python**: Alembic, Django migrations, SQLAlchemy
- **Ruby**: Rails Active Record Migrations
- **Go**: golang-migrate, goose
- **PHP**: Laravel migrations, Doctrine

## Usage Examples

```
@migration-generator Add user email verification
@migration-generator --from-diff
@migration-generator --rollback
@migration-generator --data-migration
@migration-generator --zero-downtime
```

## Raw SQL Migrations

### PostgreSQL - Add Table
```sql
-- migrations/001_create_users_table.up.sql
CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  username VARCHAR(50) UNIQUE NOT NULL,
  email VARCHAR(255) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  active BOOLEAN DEFAULT true NOT NULL,
  created_at TIMESTAMP DEFAULT NOW() NOT NULL,
  updated_at TIMESTAMP DEFAULT NOW() NOT NULL
);

-- Create indexes
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_active ON users(active) WHERE active = true;

-- Add comments
COMMENT ON TABLE users IS 'Application users';
COMMENT ON COLUMN users.email IS 'User email address (unique)';

-- migrations/001_create_users_table.down.sql
DROP TABLE IF EXISTS users CASCADE;
```

### Add Column with Default Value
```sql
-- migrations/002_add_email_verified.up.sql
-- Step 1: Add column as nullable
ALTER TABLE users ADD COLUMN email_verified BOOLEAN;

-- Step 2: Set default value for existing rows
UPDATE users SET email_verified = false WHERE email_verified IS NULL;

-- Step 3: Make column NOT NULL
ALTER TABLE users ALTER COLUMN email_verified SET NOT NULL;

-- Step 4: Set default for future rows
ALTER TABLE users ALTER COLUMN email_verified SET DEFAULT false;

-- migrations/002_add_email_verified.down.sql
ALTER TABLE users DROP COLUMN email_verified;
```

### Modify Column Type (Safe)
```sql
-- migrations/003_increase_email_length.up.sql
-- Safe: increasing varchar length
ALTER TABLE users ALTER COLUMN email TYPE VARCHAR(320);
