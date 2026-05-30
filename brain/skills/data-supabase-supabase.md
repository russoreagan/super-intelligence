# Supabase Database Skill

Navigate and query the Empathy Ledger Supabase database with confidence.

## Database Relationship Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TENANTS (top-level)                            │
│                                    │                                        │
│    ┌───────────────────────────────┼───────────────────────────────┐        │
│    │                               │                               │        │
│    ▼                               ▼                               ▼        │
│ ┌──────────────┐           ┌──────────────┐           ┌──────────────────┐  │
│ │ organisations │◄──────────│   profiles   │──────────►│  tenant_members  │  │
│ └──────────────┘           └──────────────┘           └──────────────────┘  │
│        │                          │                                         │
│        │                          │ is_storyteller                          │
│        ▼                          ▼                                         │
│ ┌──────────────┐           ┌──────────────┐                                 │
│ │   projects   │◄──────────│    stories   │                                 │
│ └──────────────┘           └──────────────┘                                 │
│        │                          │                                         │
│        │                          ├────────────────────┐                    │
│        ▼                          ▼                    ▼                    │
│ ┌──────────────┐           ┌──────────────┐    ┌──────────────┐             │
│ │ transcripts  │           │media_assets  │    │story_distribs│             │
│ └──────────────┘           └──────────────┘    └──────────────┘             │
│        │                          │                    │                    │
│        │                          │                    ▼                    │
│        ▼                          ▼             ┌──────────────┐            │
│ ┌──────────────┐           ┌──────────────┐    │ embed_tokens │            │
│ │ key_quotes[] │           │media_usage   │    └──────────────┘            │
│ │ themes[]     │           │_tracking     │                                 │
│ │ ai_summary   │           └──────────────┘                                 │
│ └──────────────┘                                                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Complete Table Inventory

**Live Supabase:** 165 objects (153 tables, 7 views, 3 partitions, 2 system)
**Migration-defined:** 71 tables
**With TypeScript Types:** 35 tables

**See also:** [DATABASE_ALIGNMENT_AUDIT.md](../../../docs/DATABASE_ALIGNMENT_AUDIT.md)

> ⚠️ **Schema Drift Alert**: ~80 tables exist in Supabase but have no migration files.
> Use `npx supabase gen types typescript --local` to generate accurate types.

### 1. Identity & Access (12 tables)
