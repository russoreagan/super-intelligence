-- Per-end-user MCP OAuth tokens for the engine API.
--
-- When a partner's end-user connects their own app (Jira, Google Workspace, etc.)
-- via OAuth, the partner stores the resulting token here via POST /v1/mcp/tokens.
-- CMAExecutor reads these at turn-execution time and creates a per-user Anthropic
-- Vault so each end-user's MCP sessions are credential-isolated.
--
-- Storage mirrors user_api_keys_meta: actual token bytes live in Supabase Vault
-- (AEAD-encrypted at rest); this table holds the opaque vault secret UUID and
-- metadata only. No plaintext is ever returned through the GET endpoints.

create table if not exists public.end_user_mcp_tokens (
  org_id      uuid        not null,
  end_user_id text        not null,
  server_name text        not null,
  server_url  text        not null,
  secret_id   uuid        not null,  -- vault.secrets.id
  expires_at  timestamptz,
  created_ts  timestamptz not null default now(),
  updated_ts  timestamptz not null default now(),
  primary key (org_id, end_user_id, server_name)
);

alter table public.end_user_mcp_tokens enable row level security;

-- Pod reads/writes under its org JWT (auth.uid() = org_id).
create policy "org can manage own end-user tokens"
  on public.end_user_mcp_tokens
  using (auth.uid() = org_id)
  with check (auth.uid() = org_id);

-- ── Write path: store/update one end-user token ─────────────────────────────
-- Called by the engine API server (inside the pod, under the org JWT).
-- Creates or rotates the vault secret and upserts the metadata row.
create or replace function public.set_end_user_mcp_token(
  p_end_user_id text,
  p_server_name text,
  p_server_url  text,
  p_token       text,
  p_expires_at  timestamptz default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := auth.uid();
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
-- Called by CMAExecutor inside the pod (under the org JWT).
-- Returns [{server_name, server_url, token, expires_at}].
create or replace function public.get_end_user_mcp_tokens(
  p_end_user_id text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org  uuid := auth.uid();
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
  p_server_name text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := auth.uid();
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
revoke all on function public.set_end_user_mcp_token(text, text, text, text, timestamptz) from public;
revoke all on function public.get_end_user_mcp_tokens(text) from public;
revoke all on function public.delete_end_user_mcp_token(text, text) from public;

-- Pods authenticate as 'authenticated' (org JWT); these RPCs are the only paths.
grant execute on function public.set_end_user_mcp_token(text, text, text, text, timestamptz) to authenticated;
grant execute on function public.get_end_user_mcp_tokens(text) to authenticated;
grant execute on function public.delete_end_user_mcp_token(text, text) to authenticated;
