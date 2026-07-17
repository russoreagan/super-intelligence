"""
SQL-level org isolation for the per-end-user MCP token RPCs (migration 024).

This loads the REAL migration file into a REAL Postgres (via the `pgserver`
pip package, which bundles a postgres binary) and exercises the three
SECURITY DEFINER RPCs under two request identities:

  • a service-role JWT  → role=service_role, NO `sub` → auth.uid() IS NULL
    (exactly the mode production runs in once the Supabase project signs JWTs
    asymmetrically and the pod falls back to the service key)
  • a real org JWT       → sub = org id → auth.uid() = org id

It proves the migration's core guarantees:
  1. Under service-role, an explicit p_org_id resolves the org (feature works).
  2. Cross-org reads/deletes are impossible (isolation holds via the in-body
     `where org_id = v_org` scoping).
  3. Under a real org JWT, auth.uid() WINS over a caller-supplied p_org_id — a
     tenant cannot name another org.
  4. With no identity at all (no JWT, no p_org_id) the RPCs still fail closed.

Skipped automatically when `pgserver` isn't installed (it is not a hard test
dependency); the API-level wiring is covered by tests/test_api_mcp_tokens.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

_MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
# Load 012 (the auth.uid()-only originals) THEN 024, so the test also exercises
# 024's real production upgrade path: drop the old signatures, replace in place.
MIGRATION_012 = _MIGRATIONS / "012_end_user_mcp_tokens.sql"
MIGRATION_024 = _MIGRATIONS / "024_end_user_mcp_tokens_org_param.sql"

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
SERVICE_ROLE = '{"role":"service_role","iss":"supabase"}'  # no sub
JWT_A = f'{{"sub":"{ORG_A}","role":"authenticated","aud":"authenticated"}}'

# Roles, Supabase's auth.uid()/auth.role(), a faithful `vault` stub, and the 012
# table — everything migration 024 assumes already exists.
PRELUDE = """
do $$ begin
  if not exists (select from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists (select from pg_roles where rolname='service_role')  then create role service_role;  end if;
end $$;

create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $fn$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid $fn$;
create or replace function auth.role() returns text language sql stable as $fn$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )::text $fn$;

create schema if not exists vault;
create table if not exists vault.secrets (
  id uuid primary key default gen_random_uuid(),
  name text, description text, secret text
);
create or replace view vault.decrypted_secrets as
  select id, name, description, secret as decrypted_secret from vault.secrets;
create or replace function vault.create_secret(p_secret text, p_name text default null, p_description text default null)
  returns uuid language plpgsql as $fn$
  declare v_id uuid; begin
    insert into vault.secrets(secret, name, description) values (p_secret, p_name, p_description)
      returning id into v_id; return v_id; end; $fn$;
create or replace function vault.update_secret(p_id uuid, p_secret text)
  returns void language plpgsql as $fn$
  begin update vault.secrets set secret = p_secret where id = p_id; end; $fn$;

create table if not exists public.end_user_mcp_tokens (
  org_id      uuid        not null,
  end_user_id text        not null,
  server_name text        not null,
  server_url  text        not null,
  secret_id   uuid        not null,
  expires_at  timestamptz,
  created_ts  timestamptz not null default now(),
  updated_ts  timestamptz not null default now(),
  primary key (org_id, end_user_id, server_name)
);
"""

# Sentinel wrappers around the REAL migration functions so RAISEs surface as a
# returned string rather than being lost to stderr.
WRAPPERS = """
create or replace function public.t_set(p_eu text, p_sn text, p_url text, p_tok text, p_org uuid) returns text
language plpgsql as $fn$ begin
  perform public.set_end_user_mcp_token(p_eu, p_sn, p_url, p_tok, null, p_org); return 'OK';
exception when others then return 'RAISE:'||SQLERRM; end; $fn$;

create or replace function public.t_get(p_eu text, p_org uuid) returns text
language plpgsql as $fn$ begin
  return 'OK:'||public.get_end_user_mcp_tokens(p_eu, p_org)::text;
exception when others then return 'RAISE:'||SQLERRM; end; $fn$;

create or replace function public.t_del(p_eu text, p_sn text, p_org uuid) returns text
language plpgsql as $fn$ begin
  return 'OK:'||public.delete_end_user_mcp_token(p_eu, p_sn, p_org)::text;
exception when others then return 'RAISE:'||SQLERRM; end; $fn$;
"""

_TAGS = {"SET", "BEGIN", "COMMIT", "ROLLBACK", "DO", ""}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    data = tmp_path_factory.mktemp("pg_org_scoping")
    pg = pgserver.get_server(data)
    try:
        pg.psql(PRELUDE)
        pg.psql(MIGRATION_012.read_text())  # originals present…
        pg.psql(MIGRATION_024.read_text())  # …then 024 drops + replaces them
        pg.psql(WRAPPERS)

        def call(sql: str, claims: str | None = None) -> str:
            """Run one scalar select, optionally as a request carrying `claims`.
            Each psql() is a fresh session, so a plain SET scopes to this call."""
            body = "\\pset tuples_only on\n\\pset format unaligned\n"
            if claims is not None:
                body += f"set request.jwt.claims = '{claims}';\n"
            body += sql
            lines = [ln.strip() for ln in pg.psql(body).splitlines()]
            vals = [ln for ln in lines if ln not in _TAGS and not ln.startswith("NOTICE")]
            return vals[-1] if vals else ""

        yield call
    finally:
        pg.cleanup()


def _sq(s: str) -> str:  # single-quote for embedding in SQL literal
    return "'" + s.replace("'", "''") + "'"


def test_service_role_can_store_and_read_with_explicit_org(db):
    # Store for org A as a service-role request (no auth.uid()).
    assert db(f"select public.t_set('u1','jira','https://x','tok-A',{_sq(ORG_A)}::uuid);", SERVICE_ROLE) == "OK"
    # Read it back scoped to A → token present.
    got = db(f"select public.t_get('u1',{_sq(ORG_A)}::uuid);", SERVICE_ROLE)
    assert got.startswith("OK:") and "tok-A" in got and "jira" in got


def test_cross_org_read_is_empty(db):
    db(f"select public.t_set('u1','jira','https://x','tok-A',{_sq(ORG_A)}::uuid);", SERVICE_ROLE)
    # Org B asks for u1's tokens → sees nothing of A's.
    got = db(f"select public.t_get('u1',{_sq(ORG_B)}::uuid);", SERVICE_ROLE)
    assert got == "OK:[]"


def test_real_org_jwt_overrides_spoofed_param(db):
    db(f"select public.t_set('u1','jira','https://x','tok-A',{_sq(ORG_A)}::uuid);", SERVICE_ROLE)
    # Caller authenticated as org A but passes p_org_id = B → auth.uid() (A) wins,
    # so it reads A's tokens, NOT B's. A tenant cannot name another org.
    got = db(f"select public.t_get('u1',{_sq(ORG_B)}::uuid);", JWT_A)
    assert got.startswith("OK:") and "tok-A" in got


def test_no_identity_fails_closed(db):
    # No JWT claims AND no p_org_id → still 'not authenticated'.
    assert db("select public.t_get('u1', null);").startswith("RAISE:not authenticated")
    assert db(
        "select public.t_set('u1','jira','https://x','tok', null);"
    ).startswith("RAISE:not authenticated")


def test_cross_org_delete_cannot_touch_another_org(db):
    db(f"select public.t_set('u2','slack','https://s','tok-A',{_sq(ORG_A)}::uuid);", SERVICE_ROLE)
    # Org B tries to delete A's token → not found (scoped away), A's row survives.
    assert db(f"select public.t_del('u2','slack',{_sq(ORG_B)}::uuid);", SERVICE_ROLE) == "OK:false"
    assert "tok-A" in db(f"select public.t_get('u2',{_sq(ORG_A)}::uuid);", SERVICE_ROLE)
    # Org A deletes its own → gone.
    assert db(f"select public.t_del('u2','slack',{_sq(ORG_A)}::uuid);", SERVICE_ROLE) == "OK:true"
    assert db(f"select public.t_get('u2',{_sq(ORG_A)}::uuid);", SERVICE_ROLE) == "OK:[]"
