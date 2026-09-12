-- 035_end_users_erased_at.sql
--
-- Tombstone for right-to-erasure. DELETE /v1/end_users/{id} used to drop the
-- ownership row LAST, so a second erase of the same id (or any later read of it)
-- was indistinguishable from a foreign partner's id: both 404. A partner
-- discharging an erasure obligation could not tell "already erased" from
-- "never yours" — the plan's B10.
--
-- The row is now never deleted. Erasure stamps `erased_at`; the customer's data
-- rows are gone but ownership is remembered, so:
--   * the owning partner's second DELETE returns 410 Gone with `erased_at`;
--   * a foreign partner still gets 404 (the row's owner is still checked first);
--   * the owning partner reopening a session for the same id clears the stamp
--     (brain/api/end_users.py::revive) — a fresh start on the same handle.
--
-- Pre-migration safety: brain/api/end_users.py reads with select("*") and treats a
-- missing column as "live", and forget() falls back to the old row delete when
-- the update is refused, so the code deploys before this file is applied.
--
-- Numbering: 033 is intentionally absent from the repo (see 034's header).
-- Apply with `supabase db push` (numbered files) — never via the MCP apply_migration.

alter table end_users add column if not exists erased_at timestamptz;

-- Partner-scoped listing of LIVE customers (list_for_partner filters on it).
create index if not exists end_users_org_partner_live_idx
  on end_users(org_id, partner_id) where erased_at is null;
