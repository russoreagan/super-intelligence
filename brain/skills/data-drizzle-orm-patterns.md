# Drizzle ORM Patterns - Complete PostgreSQL Reference

**Use when:** Working with database operations, schema design, migrations, or queries in Quetrex.

## Overview

This skill provides comprehensive Drizzle ORM patterns for PostgreSQL with Vercel Edge Runtime support. Drizzle is Quetrex's chosen ORM because it's edge-first, type-safe, and supports all deployment targets.

## Why Drizzle?

- **Edge Runtime Compatible**: Works with Vercel Edge Functions, Cloudflare Workers
- **Type-Safe**: Full TypeScript inference without code generation
- **Zero Dependencies**: No heavy Node.js runtime requirements
- **SQL-Like API**: Familiar to developers who know SQL
- **Lightweight**: ~7.4kb minified (vs Prisma's ~300kb)

## Skill Structure

This skill is organized into focused modules:

### 1. [queries-complete.md](./queries-complete.md)
Complete query patterns: select, insert, update, delete, joins, pagination, filtering, aggregations, subqueries, CTEs.

**When to use:**
- Building any database query
- Fetching data with filters
- Inserting/updating/deleting records
- Pagination or sorting
- Aggregating data (count, sum, avg)
- Complex joins or subqueries

### 2. [transactions.md](./transactions.md)
Transaction patterns: isolation levels, rollback, nested transactions, error handling, deadlock prevention.

**When to use:**
- Multiple operations that must succeed together
- Financial operations (payments, transfers)
- Data consistency requirements
- Race condition prevention
- Complex multi-step workflows

### 3. [relations.md](./relations.md)
Relationship patterns: one-to-one, one-to-many, many-to-many, self-referencing, cascading deletes, nested queries.

**When to use:**
- Defining schema relationships
- Querying related data
- Setting up cascading operations
- Working with hierarchical data
- Optimizing related data fetching

### 4. [migrations.md](./migrations.md)
Migration patterns: schema evolution, data migrations, zero-downtime deployments, rollback strategies.

**When to use:**
- Adding/modifying database schema
- Migrating data between schemas
- Deploying schema changes
- Rolling back problematic migrations
- Renaming tables/columns safely

### 5. [edge-runtime.md](./edge-runtime.md)
Edge deployment patterns: Vercel Edge Functions, Neon serverless, connection pooling, HTTP-based connections.

**When to use:**
- Deploying to Vercel Edge Runtime
- Using Neon serverless PostgreSQL
- Optimizing edge function performance
- Configuring connection pooling
- Understanding edge limitations

### 6. [performance.md](./performance.md)
Performance patterns: indexing, query optimization, N+1 prevention, batch operations, caching.

**When to use:**
- Slow queries
- High database load
- N+1 query problems
- Large data sets
- Performance optimization needed

### 7. [type-inference.md](./type-inference.md)
TypeScript inference patterns: InferModel, InferSelect, InferInsert, schema types, custom types.

**When to use:**
- Defining TypeScript types from schema
- Creating API types
- Type-safe query builders
- Custom type mappers
- Ensuring type safety
