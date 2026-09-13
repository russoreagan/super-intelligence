-- 043_mcp_connectors_auth_modes.sql
-- Connectors grow two more ways to authenticate, alongside the original shared
-- secret (brain/clusters/cma_executor.py, brain/connectors/oauth.py):
--
--   shared_secret  the brain generates a secret; the connector verifies brain-
--                  minted per-end-user HMAC bearers with it (unchanged — for
--                  servers the org hosts itself).
--   api_key        the org pastes a bearer the third-party server issued (Zapier,
--                  a GitHub PAT, …). Sent as-is on every call.
--   oauth          the org clicks Connect; the brain runs the MCP authorization
--                  flow (RFC 9728 discovery → RFC 7591 dynamic registration →
--                  PKCE → code exchange) and keeps the access + refresh tokens.
--
-- All secret material stays in Supabase Vault exactly like 013: this table
-- holds vault ids and metadata only. `secret_id` is now nullable because an
-- OAuth connector has no secret until its callback lands; for api_key it holds
-- the pasted key, for shared_secret the generated one, for oauth the ACCESS
-- token (the refresh token and the DCR client secret get their own vault rows).
--
-- The three 013 RPCs are dropped and recreated with the new columns and an
-- org fallback `coalesce(auth.uid(), p_org_id)` (024's pattern — the pod may
-- run on the service key under asymmetric JWT signing, where auth.uid() is
-- NULL). Signature changes stale 040's revoke list, so every function here
-- re-issues `revoke … from anon, public` itself (026's standing rule).
-- Idempotent; safe to re-run.

alter table public.mcp_connectors
  add column if not exists description             text,
  add column if not exists auth_mode               text not null default 'shared_secret',
  add column if not exists catalog_id              text,
  add column if not exists oauth_client_id         text,
  add column if not exists oauth_client_secret_id  uuid,
  add column if not exists oauth_token_endpoint    text,
  add column if not exists oauth_scope             text,
  add column if not exists oauth_refresh_secret_id uuid,
  add column if not exists oauth_expires_at        timestamptz,
  add column if not exists oauth_status            text not null default '',
  add column if not exists oauth_error             text,
  add column if not exists connected_ts            timestamptz;

alter table public.mcp_connectors alter column secret_id drop not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'mcp_connectors_auth_mode_check'
  ) then
    alter table public.mcp_connectors
      add constraint mcp_connectors_auth_mode_check
      check (auth_mode in ('shared_secret', 'api_key', 'oauth'));
  end if;
end $$;

-- ── helpers ──────────────────────────────────────────────────────────────────
-- Create-or-update one vault secret; returns the (possibly new) vault id.
create or replace function public._mcpconn_put_secret(
  p_existing uuid,
  p_value    text,
  p_name     text,
  p_desc     text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_id uuid := p_existing;
begin
  if p_value is null then
    return v_id;
  end if;
  if v_id is not null and exists (select 1 from vault.secrets where id = v_id) then
    perform vault.update_secret(v_id, p_value);
    return v_id;
  end if;
  return vault.create_secret(p_value, p_name, p_desc)::uuid;
end;
$$;

revoke all on function public._mcpconn_put_secret(uuid, text, text, text) from anon, authenticated, public;

-- ── register ─────────────────────────────────────────────────────────────────
drop function if exists public.register_mcp_connector(text, text, text, text);

create or replace function public.register_mcp_connector(
  p_name         text,
  p_url          text,
  p_secret       text default null,
  p_display_name text default null,
  p_description  text default null,
  p_auth_mode    text default 'shared_secret',
  p_catalog_id   text default null,
  p_org_id       uuid default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_sid uuid;
  v_mode text := coalesce(nullif(btrim(p_auth_mode), ''), 'shared_secret');
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  if p_name is null or length(btrim(p_name)) = 0 then
    raise exception 'empty connector name';
  end if;
  if p_url is null or length(btrim(p_url)) = 0 then
    raise exception 'empty connector url';
  end if;
  if v_mode not in ('shared_secret', 'api_key', 'oauth') then
    raise exception 'unknown auth_mode %', v_mode;
  end if;
  if v_mode <> 'oauth' and (p_secret is null or length(btrim(p_secret)) = 0) then
    raise exception 'empty connector secret';
  end if;
  if exists (
    select 1 from public.mcp_connectors where org_id = v_org and name = p_name
  ) then
    raise exception 'connector % already exists', p_name;
  end if;

  if p_secret is not null then
    v_sid := vault.create_secret(
      p_secret, 'mcpconn:' || v_org::text || ':' || p_name, 'MCP connector secret'
    )::uuid;
  end if;

  insert into public.mcp_connectors
    (org_id, name, url, display_name, description, auth_mode, catalog_id, secret_id, oauth_status)
  values (
    v_org, p_name, p_url,
    nullif(btrim(coalesce(p_display_name, '')), ''),
    nullif(btrim(coalesce(p_description, '')), ''),
    v_mode,
    nullif(btrim(coalesce(p_catalog_id, '')), ''),
    v_sid,
    case when v_mode = 'oauth' then 'pending' else '' end
  );
end;
$$;

-- ── read (decrypted — pod only) ──────────────────────────────────────────────
drop function if exists public.get_mcp_connectors();

create or replace function public.get_mcp_connectors(
  p_org_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_out jsonb := '[]'::jsonb;
  v_row record;
  v_tok text;
  v_csec text;
  v_rtok text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  for v_row in
    select name, url, display_name, description, auth_mode, catalog_id, secret_id,
           oauth_client_id, oauth_client_secret_id, oauth_token_endpoint, oauth_scope,
           oauth_refresh_secret_id, oauth_expires_at, oauth_status, oauth_error,
           connected_ts, created_ts
      from public.mcp_connectors
      where org_id = v_org
      order by name
  loop
    v_tok := null; v_csec := null; v_rtok := null;
    if v_row.secret_id is not null then
      select ds.decrypted_secret into v_tok
        from vault.decrypted_secrets ds where ds.id = v_row.secret_id;
    end if;
    if v_row.oauth_client_secret_id is not null then
      select ds.decrypted_secret into v_csec
        from vault.decrypted_secrets ds where ds.id = v_row.oauth_client_secret_id;
    end if;
    if v_row.oauth_refresh_secret_id is not null then
      select ds.decrypted_secret into v_rtok
        from vault.decrypted_secrets ds where ds.id = v_row.oauth_refresh_secret_id;
    end if;
    v_out := v_out || jsonb_build_object(
      'name',           v_row.name,
      'url',            v_row.url,
      'display_name',   v_row.display_name,
      'description',    v_row.description,
      'auth_mode',      v_row.auth_mode,
      'catalog_id',     v_row.catalog_id,
      'token',          v_tok,
      'created_ts',     v_row.created_ts,
      'connected_ts',   v_row.connected_ts,
      'oauth', jsonb_build_object(
        'client_id',      v_row.oauth_client_id,
        'client_secret',  v_csec,
        'token_endpoint', v_row.oauth_token_endpoint,
        'scope',          v_row.oauth_scope,
        'refresh_token',  v_rtok,
        'expires_at',     v_row.oauth_expires_at,
        'status',         v_row.oauth_status,
        'error',          v_row.oauth_error
      )
    );
  end loop;
  return v_out;
end;
$$;

-- ── delete ───────────────────────────────────────────────────────────────────
drop function if exists public.delete_mcp_connector(text);

create or replace function public.delete_mcp_connector(
  p_name   text,
  p_org_id uuid default null
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_row record;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  select secret_id, oauth_client_secret_id, oauth_refresh_secret_id into v_row
    from public.mcp_connectors
    where org_id = v_org and name = p_name;
  if not found then
    return false;
  end if;
  -- Secrets before the row (030's rule): a failure here leaves the row so the
  -- next delete still finds the vault ids.
  delete from vault.secrets
    where id in (v_row.secret_id, v_row.oauth_client_secret_id, v_row.oauth_refresh_secret_id);
  delete from public.mcp_connectors where org_id = v_org and name = p_name;
  return true;
end;
$$;

-- ── rotate the bearer / shared secret / pasted API key ───────────────────────
create or replace function public.set_mcp_connector_secret(
  p_name   text,
  p_secret text,
  p_org_id uuid default null
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_sid uuid;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  if p_secret is null or length(btrim(p_secret)) = 0 then
    raise exception 'empty connector secret';
  end if;
  select secret_id into v_sid
    from public.mcp_connectors where org_id = v_org and name = p_name;
  if not found then
    return false;
  end if;
  v_sid := public._mcpconn_put_secret(
    v_sid, p_secret, 'mcpconn:' || v_org::text || ':' || p_name, 'MCP connector secret'
  );
  update public.mcp_connectors
    set secret_id = v_sid, updated_ts = now()
    where org_id = v_org and name = p_name;
  return true;
end;
$$;

-- ── OAuth state: client registration, tokens, status ─────────────────────────
-- NULL arguments leave the corresponding field untouched, so the callback can
-- store tokens without re-sending the client registration and a refresh can
-- rotate the access token alone. p_status/p_error are always written.
create or replace function public.set_mcp_connector_oauth(
  p_name           text,
  p_status         text,
  p_error          text default null,
  p_client_id      text default null,
  p_client_secret  text default null,
  p_token_endpoint text default null,
  p_scope          text default null,
  p_access_token   text default null,
  p_refresh_token  text default null,
  p_expires_at     timestamptz default null,
  p_org_id         uuid default null
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_row record;
  v_sid uuid;
  v_cid uuid;
  v_rid uuid;
  v_base text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  select secret_id, oauth_client_secret_id, oauth_refresh_secret_id into v_row
    from public.mcp_connectors
    where org_id = v_org and name = p_name and auth_mode = 'oauth';
  if not found then
    return false;
  end if;
  v_base := 'mcpconn:' || v_org::text || ':' || p_name;
  v_sid := public._mcpconn_put_secret(v_row.secret_id, p_access_token, v_base, 'MCP OAuth access token');
  v_cid := public._mcpconn_put_secret(v_row.oauth_client_secret_id, p_client_secret, v_base || ':client', 'MCP OAuth client secret');
  v_rid := public._mcpconn_put_secret(v_row.oauth_refresh_secret_id, p_refresh_token, v_base || ':refresh', 'MCP OAuth refresh token');
  update public.mcp_connectors
    set secret_id               = v_sid,
        oauth_client_secret_id  = v_cid,
        oauth_refresh_secret_id = v_rid,
        oauth_client_id         = coalesce(p_client_id, oauth_client_id),
        oauth_token_endpoint    = coalesce(p_token_endpoint, oauth_token_endpoint),
        oauth_scope             = coalesce(p_scope, oauth_scope),
        oauth_expires_at        = coalesce(p_expires_at, case when p_access_token is null then oauth_expires_at else null end),
        oauth_status            = coalesce(nullif(btrim(p_status), ''), oauth_status),
        oauth_error             = p_error,
        connected_ts            = case when p_status = 'connected' and p_access_token is not null then now() else connected_ts end,
        updated_ts              = now()
    where org_id = v_org and name = p_name;
  return true;
end;
$$;

-- ── grants ───────────────────────────────────────────────────────────────────
-- Supabase's default privileges grant EXECUTE to anon on every new public
-- function and `revoke … from public` does not remove that direct grant (040),
-- so anon is revoked by name on each signature created above.
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.register_mcp_connector(text, text, text, text, text, text, text, uuid)',
    'public.get_mcp_connectors(uuid)',
    'public.delete_mcp_connector(text, uuid)',
    'public.set_mcp_connector_secret(text, text, uuid)',
    'public.set_mcp_connector_oauth(text, text, text, text, text, text, text, text, text, timestamptz, uuid)'
  ] loop
    execute format('revoke all on function %s from anon, public', fn);
    execute format('grant execute on function %s to authenticated, service_role', fn);
  end loop;
end $$;
