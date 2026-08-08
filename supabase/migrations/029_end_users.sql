-- 029_end_users.sql
-- Which partner owns which end user.
--
-- An end_user_id is partner-chosen free text: "your customer". Sessions record the
-- partner_id that opened them, but NOTHING recorded who owns the customer, so every
-- other per-end-user surface was scoped to the org and nothing finer. In an org with
-- two partners that meant partner A could, for any end_user_id it could guess or had
-- seen:
--
--   • read which third-party services partner B's customer had connected;
--   • UPSERT a connector token onto that customer, pointing server_url at an
--     attacker-controlled MCP endpoint with an attacker-supplied token — B's agent
--     then builds a vault against it on its next refresh. A full connector hijack;
--   • delete the connection and silently break B's integration.
--
-- This table is the one place that answers "whose customer is this", so the API can
-- scope MCP tokens, and so a partner can finally erase its own customers (it is the
-- data controller and had no erasure route at all — the only one was owner-gated).
--
-- FIRST WRITER WINS. Claiming is `insert ... on conflict do nothing` followed by a
-- read-back, never an upsert: an upsert would let partner B overwrite partner A's
-- ownership row and thereby steal the customer, which is the exact bug being closed.
--
-- partner_id is NULLABLE and null means owner-owned. Rows backfilled from
-- api_sessions carry whatever opened them; everything else lands null and is
-- reachable only by an owner key — fail-closed, and no partner inherits a customer
-- it cannot prove it created.
--
-- Deliberately NOT denormalised onto episodes/tasks/dmn_state/speaker_profiles/
-- brain_schemas/api_sessions. Ownership on six tables is six places for the truth to
-- drift and six backfills; every one of them is reached through a session or a purge,
-- both of which already hold the end_user_id and can join here once at the edge.

create table if not exists end_users (
  org_id uuid references organizations(id) on delete cascade not null,
  end_user_id text not null,
  partner_id text,                  -- null = owner-owned
  created_ts timestamptz not null default now(),
  last_seen_ts timestamptz not null default now(),
  primary key (org_id, end_user_id)
);
alter table end_users enable row level security;
create policy "org can manage own end_users" on end_users for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
-- Listing / erasing everything belonging to one partner.
create index if not exists end_users_org_partner_idx on end_users(org_id, partner_id);
grant select, insert, update, delete on end_users to authenticated;

-- Backfill from the only table that carries BOTH columns. Anything not covered stays
-- unowned (owner-only), which is the safe direction: attributing a customer to the
-- wrong partner would hand over exactly the data this table exists to protect.
insert into end_users (org_id, end_user_id, partner_id)
select distinct org_id, end_user_id, partner_id
  from api_sessions
 where coalesce(end_user_id, '') <> ''
on conflict (org_id, end_user_id) do nothing;
