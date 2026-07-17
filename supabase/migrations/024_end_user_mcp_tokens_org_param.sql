-- 024_end_user_mcp_tokens_org_param.sql
-- The per-end-user MCP token feature was INERT in production.
--
-- Cause: the three SECURITY DEFINER RPCs in 012 derive the caller's org from
--   v_org uuid := auth.uid();
-- and `raise exception 'not authenticated'` when it is NULL. That worked while
-- the Supabase project signed JWTs with the legacy shared HS256 secret: the
-- gateway minted a per-tenant org token whose `sub` IS the org id, so auth.uid()
-- returned the org (see brain/gateway/org_token.py).
--
-- The live project has since migrated to Supabase's asymmetric JWT signing
-- (ES256/RSA — now the default). The gateway cannot mint an asymmetric token
-- (only Supabase holds the private key), so mint_org_token() returns "" and the
-- provisioner falls back to giving each tenant the SERVICE-ROLE key. A
-- service-role JWT carries `role: service_role` and NO `sub` claim, so
-- auth.uid() IS NULL — and all three RPCs fail closed:
--   set_end_user_mcp_token     ← POST   /v1/mcp/tokens                       (500)
--   delete_end_user_mcp_token  ← DELETE /v1/mcp/tokens/{end_user}/{server}   (500)
--   get_end_user_mcp_tokens    ← per-end-user Anthropic Vault build fails
-- (Confirmed empirically against a real Postgres: with request.jwt.claims set to
-- a service-role claim blob, auth.uid() returns NULL and each RPC raises.)
--
-- Fix: resolve the org as coalesce(auth.uid(), p_org_id) from an explicit
-- caller-supplied p_org_id, and only raise when BOTH are null. This is safe in
-- BOTH signing modes:
--   • Under a real org JWT, auth.uid() is non-null and WINS — the caller-supplied
--     p_org_id is ignored, so a tenant on an org token cannot name another org.
--   • Under the service-key fallback there is no JWT identity, so p_org_id is
--     used. That grants no new power: a process holding the service key already
--     bypasses RLS and could read any org's rows regardless. Tenancy in that mode
--     rests on the in-query org scoping the callers already apply (the pod passes
--     its OWN org id, supabase_client.get_org_id()).
-- Keeping the raise when both are null preserves fail-closed with no org at all.
--
-- p_org_id is added as a trailing parameter with a default of null so any caller
-- that omits it still fails closed rather than silently acting org-less. The old
-- signatures are dropped so exactly one function of each name remains.
--
-- NOTE ON THE DEEPER TRADEOFF: this restores the feature but does NOT restore RLS
-- (layer 1). While the project signs asymmetrically the gateway cannot mint a
-- token PostgREST will accept, so tenants keep the service key and isolation
-- rests entirely on in-query org scoping (layer 2). Re-enabling RLS is a Supabase
-- dashboard/ops decision (JWT signing keys), tracked separately.

-- ── Drop the auth.uid()-only signatures from 012 ─────────────────────────────
drop function if exists public.set_end_user_mcp_token(text, text, text, text, timestamptz);
drop function if exists public.get_end_user_mcp_tokens(text);
drop function if exists public.delete_end_user_mcp_token(text, text);

-- ── Write path: store/update one end-user token ──────────────────────────────
-- Called by the engine API server (POST /v1/mcp/tokens). Creates or rotates the
-- vault secret and upserts the metadata row, scoped to the resolved org.
create or replace function public.set_end_user_mcp_token(
  p_end_user_id text,
  p_server_name text,
  p_server_url  text,
  p_token       text,
  p_expires_at  timestamptz default null,
  p_org_id      uuid        default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := coalesce(auth.uid(), p_org_id);
  v_sid  uuid;
  v_name text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  if p_token is null or length(btrim(p_token)) = 0 then
    raise exception 'empty token';
  end if;

  select secret_id into v_sid
    from public.end_user_mcp_tokens
    where org_id = v_org and end_user_id = p_end_user_id and server_name = p_server_name;

  v_name := 'mcp:' || v_org::text || ':' || p_end_user_id || ':' || p_server_name;

  if v_sid is null then
    v_sid := vault.create_secret(p_token, v_name, 'end-user MCP token')::uuid;
    insert into public.end_user_mcp_tokens
      (org_id, end_user_id, server_name, server_url, secret_id, expires_at)
    values
      (v_org, p_end_user_id, p_server_name, p_server_url, v_sid, p_expires_at);
  else
    perform vault.update_secret(v_sid, p_token);
    update public.end_user_mcp_tokens
      set server_url = p_server_url,
          expires_at = p_expires_at,
          updated_ts = now()
      where org_id = v_org and end_user_id = p_end_user_id and server_name = p_server_name;
  end if;
end;
$$;

-- ── Read path: fetch decrypted tokens for one end-user ───────────────────────
-- Called by CMAExecutor when building a per-end-user Anthropic Vault.
-- Returns [{server_name, server_url, token, expires_at}], scoped to the org.
create or replace function public.get_end_user_mcp_tokens(
  p_end_user_id text,
  p_org_id      uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := coalesce(auth.uid(), p_org_id);
  v_out  jsonb := '[]'::jsonb;
  v_row  record;
  v_tok  text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  for v_row in
    select server_name, server_url, secret_id, expires_at
      from public.end_user_mcp_tokens
      where org_id = v_org and end_user_id = p_end_user_id
  loop
    select ds.decrypted_secret into v_tok
      from vault.decrypted_secrets ds
      where ds.id = v_row.secret_id;
    if v_tok is not null then
      v_out := v_out || jsonb_build_object(
        'server_name', v_row.server_name,
        'server_url',  v_row.server_url,
        'token',       v_tok,
        'expires_at',  v_row.expires_at
      );
    end if;
  end loop;
  return v_out;
end;
$$;

-- ── Delete path: revoke one end-user token ───────────────────────────────────
create or replace function public.delete_end_user_mcp_token(
  p_end_user_id text,
  p_server_name text,
  p_org_id      uuid default null
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
  select secret_id into v_sid
    from public.end_user_mcp_tokens
    where org_id = v_org and end_user_id = p_end_user_id and server_name = p_server_name;
  if v_sid is null then
    return false;
  end if;
  delete from vault.secrets where id = v_sid;
  delete from public.end_user_mcp_tokens
    where org_id = v_org and end_user_id = p_end_user_id and server_name = p_server_name;
  return true;
end;
$$;

-- ── Grants ───────────────────────────────────────────────────────────────────
-- The old signatures were dropped, so their grants are gone; re-issue for the
-- new ones. `authenticated` covers a real org JWT; `service_role` covers the
-- asymmetric-signing fallback where the pod holds the service key (Supabase's
-- default privileges grant execute to service_role on create, but we grant it
-- explicitly so the fallback path does not depend on that default).
revoke all on function public.set_end_user_mcp_token(text, text, text, text, timestamptz, uuid) from public;
revoke all on function public.get_end_user_mcp_tokens(text, uuid) from public;
revoke all on function public.delete_end_user_mcp_token(text, text, uuid) from public;

grant execute on function public.set_end_user_mcp_token(text, text, text, text, timestamptz, uuid) to authenticated, service_role;
grant execute on function public.get_end_user_mcp_tokens(text, uuid) to authenticated, service_role;
grant execute on function public.delete_end_user_mcp_token(text, text, uuid) to authenticated, service_role;
