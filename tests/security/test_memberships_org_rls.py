"""
RLS for `memberships` under a tenant's ORG JWT (migration 027), against real Postgres.

Restoring RLS boots each tenant on an org JWT (sub = org_id), so the tenant reads
the DB as auth.uid() = org_id. brain/org.py's is_member()/membership_role() read
`memberships` to gate non-owner access; the pre-027 policy is user-scoped
(auth.uid() = user_id), so a tenant (auth.uid() = org_id) would see none of its
members. 027 adds an org-scoped read policy. This test proves:

  • a tenant (org JWT) can read ALL of its own org's membership rows, and
  • a regular user (user JWT) still sees ONLY their own membership — no new
    cross-member visibility.

Skipped when `pgserver` isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

MIGRATION_027 = (
    Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "027_memberships_org_read.sql"
)

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_1 = "11111111-1111-1111-1111-111111111111"
USER_2 = "22222222-2222-2222-2222-222222222222"

# Minimal faithful stand-in: the memberships table, auth.uid(), the pre-027
# user-scoped policy, and the `authenticated` role RLS is evaluated against.
PRELUDE = f"""
do $$ begin
  if not exists (select from pg_roles where rolname='authenticated') then create role authenticated; end if;
end $$;
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $fn$
  select (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid $fn$;
-- Supabase-standard grants so the authenticated role can evaluate auth.uid() in policies.
grant usage on schema auth to authenticated;
grant execute on function auth.uid() to authenticated;

create table public.memberships (
  org_id  uuid not null,
  user_id uuid not null,
  role    text not null default 'member',
  primary key (org_id, user_id)
);
grant select on public.memberships to authenticated;
alter table public.memberships enable row level security;

-- the pre-existing user-scoped policy (as on prod: "users can read their memberships")
create policy "users can read their memberships"
  on public.memberships for select using (auth.uid() = user_id);

insert into public.memberships(org_id, user_id, role) values
  ('{ORG_A}','{USER_1}','admin'),
  ('{ORG_A}','{USER_2}','member');
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    pg = pgserver.get_server(tmp_path_factory.mktemp("pg_memberships_rls"))
    try:
        pg.psql(PRELUDE)
        pg.psql(MIGRATION_027.read_text())

        def count_as(sub: str) -> int:
            """Rows in memberships visible to a caller whose JWT sub = `sub`,
            evaluated as the (non-superuser) authenticated role so RLS applies."""
            # Set the claims GUC as the superuser FIRST, then drop to the limited
            # authenticated role (which can't set it) so RLS is actually enforced.
            body = (
                "\\pset tuples_only on\n\\pset format unaligned\n"
                "begin;\n"
                f'set local request.jwt.claims = \'{{"sub":"{sub}"}}\';\n'
                "set local role authenticated;\n"
                "select count(*) from public.memberships;\n"
                "commit;"
            )
            lines = [ln.strip() for ln in pg.psql(body).splitlines()]
            vals = [ln for ln in lines if ln.isdigit()]
            return int(vals[-1]) if vals else -1

        yield count_as
    finally:
        pg.cleanup()


def test_tenant_org_jwt_reads_all_its_members(db):
    # Org JWT: sub = org_id → sees BOTH membership rows of org A (the 027 fix).
    assert db(ORG_A) == 2


def test_regular_user_still_sees_only_their_own(db):
    # User JWT: sub = user_id → sees ONLY their own row; 027 grants no cross-member view.
    assert db(USER_1) == 1
    assert db(USER_2) == 1


def test_unrelated_identity_sees_nothing(db):
    assert db("99999999-9999-9999-9999-999999999999") == 0
