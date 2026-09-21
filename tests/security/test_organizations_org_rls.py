"""
RLS for `organizations` under a tenant's ORG JWT (migration 046), against real Postgres.

The companion to 027's test, for the companion bug. A tenant reads the database as
auth.uid() = org_id, but organizations' only policy (006) is a memberships
exists-check. For a PERSONAL org that passes by accident — a row with
user_id = org_id exists because 006 seeded both to the same uuid — while an org
with a fresh uuid matches nothing and reads its own governance row as absent. The
consequence is silent: learning_mode falls back to a default, and is_isolated()
fails closed, so the org quietly withholds shared learning with no error anywhere.

This proves:
  • a personal org could already read its row (the accident), and
  • a RANDOM-UUID org can now read its row too (the regression this fixes), and
  • a user JWT still sees only the orgs they are a member of, and
  • an org may update its governance columns but NOT `plan` (the column grant).

Skipped when `pgserver` isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

_MIG = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
MIGRATION_046 = _MIG / "046_organizations_org_self_access.sql"
MIGRATION_047 = _MIG / "047_organizations_update_grant_fix.sql"

# A personal org: its id IS its owner's user id (the 006 seed pattern).
PERSONAL_ORG = "11111111-1111-1111-1111-111111111111"
OWNER = PERSONAL_ORG
# A second environment for the same person: a fresh uuid, no membership row whose
# user_id equals it. This is the shape that was unreadable before 046.
STAGING_ORG = "5a5a5a5a-0000-4000-8000-000000000001"
STRANGER = "99999999-9999-9999-9999-999999999999"

PRELUDE = f"""
do $$ begin
  if not exists (select from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists (select from pg_roles where rolname='anon') then create role anon; end if;
end $$;
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $fn$
  select (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid $fn$;
grant usage on schema auth to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;

create table public.organizations (
  id   uuid primary key,
  name text not null,
  plan text not null default 'free',
  learning_mode text not null default 'consolidated',
  instance_seed text not null default 'default',
  max_dedicated_instances int not null default 0,
  gpu_daily_usd_budget numeric not null default 0
);
create table public.memberships (
  org_id  uuid not null,
  user_id uuid not null,
  role    text not null default 'member',
  primary key (org_id, user_id)
);
-- Supabase default-grants ALL privileges on every public table to anon and
-- authenticated; RLS is the intended gate, not the grants. Reproducing that here
-- is load-bearing: with only `grant select` this harness made 046's column-scoped
-- UPDATE grant look like it limited anything, and it does not — a table-level
-- grant already covers every column. Migration 047 is what actually narrows it.
grant all on public.organizations, public.memberships to anon, authenticated;
alter table public.organizations enable row level security;
alter table public.memberships enable row level security;

-- The 006 policies, verbatim in shape: members read their orgs; users read their
-- own memberships. No org-scoped policy on organizations — that is what 046 adds.
create policy "members can read their orgs" on public.organizations
  for select using (
    exists (select 1 from public.memberships m
            where m.org_id = organizations.id and m.user_id = auth.uid())
  );
create policy "users can read their memberships"
  on public.memberships for select using (auth.uid() = user_id);

insert into public.organizations(id, name) values
  ('{PERSONAL_ORG}', 'Acme'),
  ('{STAGING_ORG}',  'Acme (staging)');
insert into public.memberships(org_id, user_id, role) values
  ('{PERSONAL_ORG}', '{OWNER}', 'admin'),
  ('{STAGING_ORG}',  '{OWNER}', 'admin');
"""


def _as(pg, sub: str, sql: str) -> str:
    """Run `sql` as the authenticated role with a JWT whose sub is `sub`.

    The claims GUC is set as superuser first, then the role drops to
    `authenticated` (which cannot set it), so RLS is genuinely enforced."""
    body = (
        "\\pset tuples_only on\n\\pset format unaligned\n"
        "begin;\n"
        f'set local request.jwt.claims = \'{{"sub":"{sub}"}}\';\n'
        "set local role authenticated;\n"
        f"{sql}\n"
        "commit;"
    )
    return pg.psql(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg_orgs_rls"))
    try:
        server.psql(PRELUDE)
        server.psql(MIGRATION_046.read_text())
        server.psql(MIGRATION_047.read_text())
        yield server
    finally:
        server.cleanup()


def _count(out: str) -> int:
    """The numeric result out of psql's chatter (BEGIN/COMMIT land on stdout too).

    -1 when no number came back at all, so a query that silently returned nothing
    fails the assertion instead of quietly matching zero."""
    vals = [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]
    return int(vals[-1]) if vals else -1


def test_personal_org_reads_its_own_row(pg):
    """Already worked before 046, by accident — the memberships exists-check
    passes because a personal org's id equals its owner's user id."""
    out = _as(pg, PERSONAL_ORG, "select name from public.organizations where id = auth.uid();")
    assert "Acme" in out


def test_random_uuid_org_reads_its_own_row(pg):
    """THE REGRESSION. Before 046 this returned nothing, so org_settings read
    learning_mode/instance_seed/the GPU caps as defaults with no error."""
    out = _as(pg, STAGING_ORG, "select name from public.organizations where id = auth.uid();")
    assert "Acme (staging)" in out


def test_a_user_still_sees_only_orgs_they_belong_to(pg):
    """046 is org-scoped (id = auth.uid()), so it grants a USER nothing new: a
    stranger's own token still matches no row."""
    assert _count(_as(pg, STRANGER, "select count(*) from public.organizations;")) == 0
    # via the 006 membership policy, unchanged by 046
    assert _count(_as(pg, OWNER, "select count(*) from public.organizations;")) == 2


def test_an_org_cannot_read_another_orgs_row(pg):
    # The staging org's token satisfies neither policy for the prod row: it is not
    # its own id, and there is no membership row whose user_id is the staging org.
    out = _as(
        pg, STAGING_ORG, f"select count(*) from public.organizations where id = '{PERSONAL_ORG}';"
    )
    assert _count(out) == 0


def test_an_anonymous_caller_cannot_write_at_all(pg):
    """anon has a null auth.uid() so no policy would admit it, but 047 revokes the
    default-granted write verbs anyway — a privilege sitting next to a policy is
    how the next mistake happens."""
    for _verb, sql in (
        ("update", "update public.organizations set learning_mode = 'isolated';"),
        ("insert", "insert into public.organizations(id, name) values (gen_random_uuid(), 'x');"),
        ("delete", "delete from public.organizations;"),
    ):
        body = (
            "\\pset tuples_only on\n\\pset format unaligned\n"
            f"begin;\nset local role anon;\n{sql}\ncommit;"
        )
        pg.psql(body)
    after = pg.psql(
        "\\pset tuples_only on\n\\pset format unaligned\n"
        "select count(*) from public.organizations where learning_mode = 'isolated';"
    )
    # Only the org that legitimately set it below/above, never anon's blanket update.
    assert _count(after) <= 1


def test_an_org_can_update_its_governance_columns(pg):
    out = _as(
        pg,
        STAGING_ORG,
        "update public.organizations set learning_mode = 'isolated' "
        "where id = auth.uid() returning learning_mode;",
    )
    assert "isolated" in out


def test_an_org_cannot_rewrite_its_plan(pg):
    """The grant must be column-scoped: a blanket UPDATE lets any org promote
    itself from 'free' to 'platform'.

    This is the case that escaped into production. 046 wrote a column grant and
    assumed it narrowed things, but Supabase had already table-granted UPDATE to
    authenticated, and a column grant on top of a table grant narrows nothing —
    so `id = auth.uid()` (satisfied by a personal org owner's own browser JWT)
    could rewrite plan, name, even id. 047 revokes the blanket grant. The harness
    above now reproduces Supabase's default grants, without which this test passes
    for the wrong reason.

    Asserted on the stored value rather than on an exception: psql reports the
    denial on stderr and returns normally, and "the plan did not change" is the
    property that actually matters anyway."""
    _as(
        pg,
        STAGING_ORG,
        "update public.organizations set plan = 'platform' where id = auth.uid();",
    )
    after = pg.psql(
        "\\pset tuples_only on\n\\pset format unaligned\n"
        f"select plan from public.organizations where id = '{STAGING_ORG}';"
    )
    assert "platform" not in after
    assert "free" in after
