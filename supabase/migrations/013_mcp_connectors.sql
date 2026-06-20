-- Org-scoped MCP connector registry for the CMA executor.
--
-- A connector is an MCP tool server the brain's agent can call (scheduler,
-- trading, …). Registering one generates a shared secret used both as the
-- single-user bearer and as the HMAC signing key for per-end-user identity
-- tokens. The secret is org-level — like partner_keys and account limits — and
-- must NOT live in the persona-namespaced second_brain volume (where it would be
-- lost on persona switch). This table is the org-level home.
--
-- Storage mirrors end_user_mcp_tokens: the secret bytes live in Supabase Vault
-- (AEAD-encrypted at rest); this table holds the opaque vault secret UUID and
-- metadata only. The decrypted secret is returned ONLY through get_mcp_connectors,
-- which is callable solely by the pod under its org JWT — never the browser.

create table if not exists public.mcp_connectors (
  org_id       uuid        not null,
  name         text        not null,
  url          text        not null,
  display_name text,
  secret_id    uuid        not null,  -- vault.secrets.id (shared connector secret)
  created_ts   timestamptz not null default now(),
  updated_ts   timestamptz not null default now(),
  primary key (org_id, name)
);

alter table public.mcp_connectors enable row level security;

-- Pod reads/writes under its org JWT (auth.uid() = org_id).
create policy "org can manage own connectors"
  on public.mcp_connectors
  using (auth.uid() = org_id)
  with check (auth.uid() = org_id);

-- ── Write path: register a new connector ─────────────────────────────────────
-- Creates the vault secret and inserts the metadata row. Raises if a connector
-- with this name already exists for the org (registration is create-only; use
-- delete + register to rotate).
create or replace function public.register_mcp_connector(
  p_name         text,
  p_url          text,
  p_secret       text,
  p_display_name text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := auth.uid();
  v_sid uuid;
  v_vname text;
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
  if p_secret is null or length(btrim(p_secret)) = 0 then
    raise exception 'empty connector secret';
  end if;

  if exists (
    select 1 from public.mcp_connectors where org_id = v_org and name = p_name
  ) then
    raise exception 'connector % already exists', p_name;
  end if;

  v_vname := 'mcpconn:' || v_org::text || ':' || p_name;
  v_sid := vault.create_secret(p_secret, v_vname, 'MCP connector secret')::uuid;

  insert into public.mcp_connectors (org_id, name, url, display_name, secret_id)
  values (v_org, p_name, p_url, nullif(btrim(coalesce(p_display_name, '')), ''), v_sid);
end;
$$;

-- ── Read path: fetch connectors with decrypted secrets ───────────────────────
-- Called by CMAExecutor inside the pod (under the org JWT). Returns
-- [{name, url, display_name, token}]. The token is the decrypted shared secret;
-- the Python layer strips it before anything reaches the browser.
create or replace function public.get_mcp_connectors()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := auth.uid();
  v_out jsonb := '[]'::jsonb;
  v_row record;
  v_tok text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  for v_row in
    select name, url, display_name, secret_id
      from public.mcp_connectors
      where org_id = v_org
      order by name
  loop
    select ds.decrypted_secret into v_tok
      from vault.decrypted_secrets ds
      where ds.id = v_row.secret_id;
    v_out := v_out || jsonb_build_object(
      'name',         v_row.name,
      'url',          v_row.url,
      'display_name', v_row.display_name,
      'token',        v_tok
    );
  end loop;
  return v_out;
end;
$$;

-- ── Delete path: remove a connector ──────────────────────────────────────────
create or replace function public.delete_mcp_connector(
  p_name text
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
    from public.mcp_connectors
    where org_id = v_org and name = p_name;
  if v_sid is null then
    return false;
  end if;
  delete from vault.secrets where id = v_sid;
  delete from public.mcp_connectors where org_id = v_org and name = p_name;
  return true;
end;
$$;

-- ── Grants ───────────────────────────────────────────────────────────────────
revoke all on function public.register_mcp_connector(text, text, text, text) from public;
revoke all on function public.get_mcp_connectors() from public;
revoke all on function public.delete_mcp_connector(text) from public;

-- Pods authenticate as 'authenticated' (org JWT); these RPCs are the only paths.
grant execute on function public.register_mcp_connector(text, text, text, text) to authenticated;
grant execute on function public.get_mcp_connectors() to authenticated;
grant execute on function public.delete_mcp_connector(text) to authenticated;
