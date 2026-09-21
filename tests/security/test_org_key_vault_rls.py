"""
Authorization inside the org key-vault RPCs (migration 045), against real Postgres.

These functions are SECURITY DEFINER and take the target org as a PARAMETER, which
is the shape that makes them worth testing directly: `p_org_id` arrives as request
data from an end user's browser, so the only thing standing between a user and
another org's credentials is the membership check inside each body.

It is deliberately the inverse of migration 024's template. 024's callers are
tenant PROCESSES holding the service key, where auth.uid() is null and p_org_id is
trusted, so it resolves `coalesce(auth.uid(), p_org_id)`. Here the caller is an end
user on their own JWT: auth.uid() is who they are, p_org_id is what they asked for,
and conflating the two would hand anyone the keys to any org by id.

Also covers the legacy fallback that makes the migration a no-op for existing
orgs: every org alive today is a "personal org" whose id equals its owner's user
id, so its keys live in 003's user_api_keys_meta and must still resolve.

Skipped when `pgserver` isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

MIGRATION_045 = (
    Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "045_org_key_vault.sql"
)

# A personal org (006's seed shape): org id == owner's user id, keys in the legacy table.
PERSONAL_ORG = "11111111-1111-1111-1111-111111111111"
OWNER = PERSONAL_ORG
# The same person's second environment: a fresh uuid.
STAGING_ORG = "5a5a5a5a-0000-4000-8000-000000000001"
# A different org the owner has no membership in at all.
FOREIGN_ORG = "ffffffff-0000-4000-8000-000000000002"
# A plain member of the staging org — may read status, may not write keys.
PLAIN = "22222222-2222-2222-2222-222222222222"

LEGACY_SECRET = "aaaaaaaa-1111-4000-8000-00000000000a"

# Stand-ins for the pieces 045 leans on that plain Postgres has no notion of:
# Supabase Vault, auth.users, and the roles RLS/grants are written against.
PRELUDE = f"""
do $$ begin
  if not exists (select from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists (select from pg_roles where rolname='anon') then create role anon; end if;
  if not exists (select from pg_roles where rolname='service_role') then create role service_role; end if;
end $$;

create schema if not exists auth;
create schema if not exists vault;
create or replace function auth.uid() returns uuid language sql stable as $fn$
  select (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid $fn$;
grant usage on schema auth to authenticated, service_role;
grant execute on function auth.uid() to authenticated, service_role;

create table auth.users (id uuid primary key);

-- Minimal Vault: secrets by id, and a decrypted view over them. Enough to prove
-- the RPCs create/update/read the RIGHT pointer; the real Vault's crypto is not
-- what these tests are about.
create table vault.secrets (
  id uuid primary key default gen_random_uuid(),
  secret text not null,
  name text,
  description text
);
create or replace view vault.decrypted_secrets as
  select id, secret as decrypted_secret, name, description from vault.secrets;
create or replace function vault.create_secret(p_secret text, p_name text, p_desc text)
returns uuid language sql as $fn$
  insert into vault.secrets(secret, name, description)
  values (p_secret, p_name, p_desc) returning id $fn$;
create or replace function vault.update_secret(p_id uuid, p_secret text)
returns void language sql as $fn$
  update vault.secrets set secret = p_secret where id = p_id $fn$;

create table public.organizations (id uuid primary key, name text not null);
create table public.memberships (
  org_id uuid not null, user_id uuid not null, role text not null default 'member',
  primary key (org_id, user_id)
);
-- 003's table, with one legacy row so the fallback path has something to find.
create table public.user_api_keys_meta (
  user_id uuid references auth.users primary key,
  secret_ids jsonb not null default '{{}}'::jsonb,
  updated_at timestamptz not null default now()
);
create or replace function public._valid_api_provider(p text)
returns boolean language sql immutable as $fn$
  select p in ('anthropic', 'elevenlabs', 'deepgram', 'google') $fn$;

insert into auth.users(id) values ('{OWNER}'), ('{PLAIN}');
insert into public.organizations(id, name) values
  ('{PERSONAL_ORG}', 'Acme'), ('{STAGING_ORG}', 'Acme (staging)'), ('{FOREIGN_ORG}', 'Other Co');
insert into public.memberships(org_id, user_id, role) values
  ('{PERSONAL_ORG}', '{OWNER}', 'admin'),
  ('{STAGING_ORG}',  '{OWNER}', 'admin'),
  ('{STAGING_ORG}',  '{PLAIN}', 'member');

insert into vault.secrets(id, secret, name) values ('{LEGACY_SECRET}', 'sk-legacy', 'legacy');
insert into public.user_api_keys_meta(user_id, secret_ids)
  values ('{OWNER}', '{{"anthropic": "{LEGACY_SECRET}"}}'::jsonb);
"""


def _as_user(pg, sub: str, sql: str) -> str:
    """Run `sql` as the authenticated role with a JWT whose sub is `sub`."""
    body = (
        "\\pset tuples_only on\n\\pset format unaligned\n"
        "begin;\n"
        f'set local request.jwt.claims = \'{{"sub":"{sub}"}}\';\n'
        "set local role authenticated;\n"
        f"{sql}\n"
        "commit;"
    )
    return pg.psql(body)


def _json(out: str) -> dict:
    """The single JSON object psql printed, out of its BEGIN/SET/COMMIT chatter."""
    import json

    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            return json.loads(ln)
    raise AssertionError(f"no json object in psql output: {out!r}")


def _as_service(pg, sql: str) -> str:
    """Run `sql` as service_role — the tier the decrypt and copy paths are for."""
    body = (
        "\\pset tuples_only on\n\\pset format unaligned\n"
        "begin;\nset local role service_role;\n"
        f"{sql}\ncommit;"
    )
    return pg.psql(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg_org_vault"))
    try:
        server.psql(PRELUDE)
        server.psql(MIGRATION_045.read_text())
        yield server
    finally:
        server.cleanup()


# ── the read path, and the legacy fallback that makes 045 inert for prod ─────


def test_personal_org_still_reads_its_legacy_keys(pg):
    """Every org alive today is personal, with keys under 003's user-keyed table.
    They must resolve through the new entry point with nothing migrated."""
    out = _as_service(pg, f"select public.get_org_api_keys('{PERSONAL_ORG}');")
    assert "sk-legacy" in out


def test_a_fresh_org_reads_nothing_rather_than_someone_elses(pg):
    out = _as_service(pg, f"select public.get_org_api_keys('{STAGING_ORG}');")
    assert "sk-legacy" not in out
    assert "{}" in out


def test_the_legacy_entry_point_delegates(pg):
    """003's get_user_api_keys is kept as a shim so a gateway deployed before this
    migration and one deployed after read identically during a rollout."""
    out = _as_service(pg, f"select public.get_user_api_keys('{PERSONAL_ORG}');")
    assert "sk-legacy" in out


# ── the write path: p_org_id is authorized, never trusted ────────────────────


def test_an_admin_can_set_their_own_orgs_key(pg):
    _as_user(pg, OWNER, f"select public.set_org_api_key('{STAGING_ORG}', 'anthropic', 'sk-stg');")
    out = _as_service(pg, f"select public.get_org_api_keys('{STAGING_ORG}');")
    assert "sk-stg" in out


def test_an_admin_cannot_set_a_key_on_an_org_they_do_not_belong_to(pg):
    """THE POINT OF THIS FILE. p_org_id is request data; membership is the gate."""
    _as_user(pg, OWNER, f"select public.set_org_api_key('{FOREIGN_ORG}', 'anthropic', 'stolen');")
    out = _as_service(pg, f"select public.get_org_api_keys('{FOREIGN_ORG}');")
    assert "stolen" not in out
    assert "{}" in out


def test_a_plain_member_cannot_set_a_key(pg):
    """A BYO provider key is org-wide spend authority, so writing one is admin-only
    even for a legitimate member of that org."""
    _as_user(pg, PLAIN, f"select public.set_org_api_key('{STAGING_ORG}', 'deepgram', 'nope');")
    out = _as_service(pg, f"select public.get_org_api_keys('{STAGING_ORG}');")
    assert "nope" not in out


def test_a_plain_member_may_read_status(pg):
    """Booleans only, and readable by any member — a member needs to know whether
    the org is configured without being able to change it."""
    status = _json(_as_user(pg, PLAIN, f"select public.get_org_api_key_status('{STAGING_ORG}');"))
    assert status["anthropic"] is True
    assert status["deepgram"] is False
    # Booleans only — a value or a vault uuid must never reach the browser.
    assert "sk-stg" not in str(status)


def test_status_for_an_org_you_are_not_in_is_refused(pg):
    out = _as_user(pg, PLAIN, f"select public.get_org_api_key_status('{FOREIGN_ORG}');")
    assert "true" not in out


def test_rotation_updates_the_legacy_secret_in_place(pg):
    """A personal org's first write through the org path must adopt the pointer it
    already has, or the old value stays decryptable under the user row while the
    user believes they rotated it."""
    _as_user(pg, OWNER, f"select public.set_org_api_key('{PERSONAL_ORG}', 'anthropic', 'sk-new');")
    out = _as_service(pg, f"select public.get_org_api_keys('{PERSONAL_ORG}');")
    assert "sk-new" in out
    still = pg.psql(
        "\\pset tuples_only on\n\\pset format unaligned\n"
        f"select secret from vault.secrets where id = '{LEGACY_SECRET}';"
    )
    assert "sk-legacy" not in still  # the same secret was rotated, not orphaned


# ── copy: how a new environment starts life able to boot ─────────────────────


def test_copy_makes_fresh_secrets_not_shared_pointers(pg):
    """Sharing a vault pointer between two orgs would make a delete on either side
    blank the other, and a rotation in staging silently reach prod."""
    target = "c0c0c0c0-0000-4000-8000-000000000003"
    pg.psql(f"insert into public.organizations(id, name) values ('{target}', 'Copy target');")
    n = _as_service(pg, f"select public.copy_org_api_keys('{PERSONAL_ORG}', '{target}');")
    assert "1" in n

    out = _as_service(pg, f"select public.get_org_api_keys('{target}');")
    assert "sk-new" in out  # same VALUE

    ids = pg.psql(
        "\\pset tuples_only on\n\\pset format unaligned\n"
        "select (secret_ids->>'anthropic') from public.org_api_keys_meta "
        f"where org_id in ('{PERSONAL_ORG}', '{target}') order by org_id;"
    )
    pointers = [ln.strip() for ln in ids.splitlines() if "-" in ln and len(ln.strip()) == 36]
    assert len(pointers) == 2 and pointers[0] != pointers[1]  # different SECRETS


def test_copy_never_clobbers_a_key_the_target_already_set(pg):
    """Copying is a seed for a brand-new org, not a rotation."""
    n = _as_service(pg, f"select public.copy_org_api_keys('{PERSONAL_ORG}', '{STAGING_ORG}');")
    assert "0" in n
    out = _as_service(pg, f"select public.get_org_api_keys('{STAGING_ORG}');")
    assert "sk-stg" in out  # staging kept its own key


def test_copy_is_not_reachable_by_an_ordinary_user(pg):
    """It decrypts, so it is service-role only — same tier as get_org_api_keys."""
    out = _as_user(
        pg, OWNER, f"select public.copy_org_api_keys('{PERSONAL_ORG}', '{FOREIGN_ORG}');"
    )
    assert "permission denied" in out.lower() or "1" not in out
