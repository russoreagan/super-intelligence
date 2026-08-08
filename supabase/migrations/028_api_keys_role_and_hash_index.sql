-- 028_api_keys_role_and_hash_index.sql
-- An owner-grade API key, and an index the gateway's lookup can actually use.
--
-- ROLE. Owner-gated routes (/v1/partner_keys, /v1/dmn, the skills admin lane, the
-- GDPR purge) are reachable only with an owner credential, and until now the only
-- owner credential was the BRAIN_API_KEYS env value. That value is per-tenant, so
-- the multi-tenant gateway — which maps a token to an org by looking it up ACROSS
-- all orgs in this table — cannot see it. The consequence was a dead end: on
-- api.elyceum.app no caller could reach an owner-gated route at all, including the
-- one route that mints keys. It also left the gateway unable to tell an owner from
-- a partner, which is why POST /v1/sleep accepted any partner key and let one
-- partner sweep every brain in the org and pause the shared GPU pod.
--
-- A table key with role='owner' fixes both: it lives here, so it resolves through
-- the gateway, and it carries the role the gateway needs to gate on. Minting one is
-- itself owner-gated at every call site. Default 'partner' means every existing row
-- keeps exactly the scope it has today — this migration grants nobody anything.
--
-- INDEX. The gateway filters on key_hash alone (it does not know the org yet — that
-- is what it is resolving). The only index was (org_id, key_hash), and key_hash is
-- not its leading column, so that query fell back to a sequential scan on every
-- request. Auth has no rate limiting, so an unauthenticated flood of invalid keys
-- was an unbounded sequential-scan generator against the database. The hash is
-- high-cardinality, so this index is small and exact.
--
-- Both changes are additive and idempotent; RLS and grants are inherited unchanged.

alter table api_keys
  add column if not exists role text not null default 'partner';

-- Added separately from the column so re-running against a table that already has
-- the column still installs the constraint.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'api_keys_role_check'
  ) then
    alter table api_keys
      add constraint api_keys_role_check check (role in ('owner', 'partner'));
  end if;
end $$;

create index if not exists api_keys_hash_idx on api_keys(key_hash);
