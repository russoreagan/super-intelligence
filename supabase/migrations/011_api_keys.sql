-- 011_api_keys.sql
-- Per-partner engine-API keys.
--
-- v1 auth compared the bearer token against a single env/settings key per process
-- (the org owner's key) — fine for one integrator, but a partner embedding the
-- brain for many of THEIR customers had no per-partner identity, so sessions
-- couldn't be scoped or audited by partner. This table mints named keys, each
-- mapped to a partner_id. Only the SHA-256 hash is stored — the plaintext is shown
-- once at creation and never recoverable. The env/settings key still works and is
-- treated as the org owner (full access, partner_id null).
--
-- RLS scopes to auth.uid() = org_id like the rest of the per-tenant schema.

create table if not exists api_keys (
  org_id uuid references organizations(id) on delete cascade not null,
  id text not null,                 -- public key id (a handle; safe to surface)
  key_hash text not null,           -- sha256 hex of the bearer token
  partner_id text not null,
  label text,
  active boolean not null default true,
  created_ts timestamptz not null default now(),
  primary key (org_id, id),
  unique (org_id, key_hash)
);
alter table api_keys enable row level security;
create policy "org can manage own api_keys" on api_keys for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index if not exists api_keys_org_hash_idx on api_keys(org_id, key_hash);
grant select, insert, update, delete on api_keys to authenticated;
