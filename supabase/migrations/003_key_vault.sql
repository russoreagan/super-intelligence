-- Per-user API-key vault (BYO keys) on top of Supabase Vault.
-- Run via: supabase db push  (or paste into the SQL editor).
--
-- Design (see plan: Secrets — Supabase Vault):
--   * Provider keys are stored as Supabase Vault secrets (AEAD-encrypted at rest;
--     the encryption key lives in Supabase's backend, never in the DB or our code).
--   * user_api_keys_meta maps each user → the Vault secret UUIDs they own + a
--     timestamp. RLS lets a user see only their own row. It holds NO secret values
--     (only opaque vault UUIDs), so even the meta row leaks nothing usable.
--   * Two least-privilege SECURITY DEFINER RPCs are the ONLY access paths:
--       - set_user_api_key / delete_user_api_key : write-only, scoped to auth.uid()
--         (the gateway proxies the user's authenticated call; it can store/clear a
--         key but can never read one back).
--       - get_user_api_keys : decrypt path, service_role ONLY (pod-boot). The pod
--         passes its own BRAIN_USER_ID.
--   * get_my_api_key_status returns booleans only, so the settings UI can show
--     "key on file" without ever seeing a value or even a vault UUID.

create extension if not exists supabase_vault with schema vault;

-- ── Per-user vault metadata ─────────────────────────────────────────────────
-- secret_ids: { "anthropic": "<uuid>", "deepgram": "<uuid>", ... }. Presence of a
-- provider key = that provider is set. No plaintext, no ciphertext — just the
-- pointers into vault.secrets.
create table if not exists user_api_keys_meta (
  user_id uuid references auth.users primary key,
  secret_ids jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table user_api_keys_meta enable row level security;
-- Read-only own-row policy. Writes go exclusively through the SECURITY DEFINER
-- RPCs below (which run as the table owner), so we deliberately grant no
-- insert/update/delete policy to users.
create policy "users can read own key metadata"
  on user_api_keys_meta for select
  using (auth.uid() = user_id);

-- The set of providers we accept. Keep in sync with settings.API_KEY_ENV.
create or replace function public._valid_api_provider(p text)
returns boolean language sql immutable as $$
  select p in ('anthropic', 'elevenlabs', 'deepgram', 'google')
$$;

-- ── Write path (gateway, as the authenticated user) ─────────────────────────
-- Upserts the user's Vault secret for one provider. Creates on first set,
-- updates in place thereafter. Returns nothing — there is no read-back path.
create or replace function public.set_user_api_key(p_provider text, p_value text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
  v_id  text;
  v_name text;
begin
  if v_uid is null then
    raise exception 'not authenticated';
  end if;
  if not public._valid_api_provider(p_provider) then
    raise exception 'unknown provider: %', p_provider;
  end if;
  if p_value is null or length(btrim(p_value)) = 0 then
    raise exception 'empty value';  -- callers treat blank as "leave unchanged" before reaching here
  end if;

  insert into public.user_api_keys_meta (user_id) values (v_uid)
    on conflict (user_id) do nothing;

  select secret_ids ->> p_provider into v_id
    from public.user_api_keys_meta where user_id = v_uid;

  if v_id is null then
    v_name := 'apikey:' || v_uid::text || ':' || p_provider;
    v_id := vault.create_secret(p_value, v_name, 'BYO API key')::text;
  else
    perform vault.update_secret(v_id::uuid, p_value);
  end if;

  update public.user_api_keys_meta
    set secret_ids = jsonb_set(coalesce(secret_ids, '{}'::jsonb), array[p_provider], to_jsonb(v_id)),
        updated_at = now()
    where user_id = v_uid;
end;
$$;

-- ── Delete path (gateway, as the authenticated user) ────────────────────────
create or replace function public.delete_user_api_key(p_provider text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
  v_id  text;
begin
  if v_uid is null then
    raise exception 'not authenticated';
  end if;
  select secret_ids ->> p_provider into v_id
    from public.user_api_keys_meta where user_id = v_uid;
  if v_id is not null then
    delete from vault.secrets where id = v_id::uuid;
    update public.user_api_keys_meta
      set secret_ids = (secret_ids - p_provider), updated_at = now()
      where user_id = v_uid;
  end if;
end;
$$;

-- ── Status path (gateway/UI, as the authenticated user) ─────────────────────
-- Returns booleans only: { "anthropic": true, "deepgram": false, ... }. The UI
-- shows asterisks for true; never sees values or vault UUIDs.
create or replace function public.get_my_api_key_status()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
  v_ids jsonb;
begin
  if v_uid is null then
    raise exception 'not authenticated';
  end if;
  select coalesce(secret_ids, '{}'::jsonb) into v_ids
    from public.user_api_keys_meta where user_id = v_uid;
  return jsonb_build_object(
    'anthropic',  (v_ids ? 'anthropic'),
    'elevenlabs', (v_ids ? 'elevenlabs'),
    'deepgram',   (v_ids ? 'deepgram'),
    'google',     (v_ids ? 'google'),
    'updated_at', (select updated_at from public.user_api_keys_meta where user_id = v_uid)
  );
end;
$$;

-- ── Decrypt path (pod boot, service_role ONLY) ──────────────────────────────
-- Returns the user's decrypted keys as { provider: value }. Restricted so only
-- the operator tier (service role, held only by gateway+pods) can call it.
create or replace function public.get_user_api_keys(p_uid uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_ids jsonb;
  v_out jsonb := '{}'::jsonb;
  v_provider text;
  v_secret text;
begin
  select secret_ids into v_ids
    from public.user_api_keys_meta where user_id = p_uid;
  if v_ids is null then
    return v_out;
  end if;
  for v_provider in select jsonb_object_keys(v_ids) loop
    select ds.decrypted_secret into v_secret
      from vault.decrypted_secrets ds
      where ds.id = (v_ids ->> v_provider)::uuid;
    if v_secret is not null then
      v_out := jsonb_set(v_out, array[v_provider], to_jsonb(v_secret));
    end if;
  end loop;
  return v_out;
end;
$$;

-- ── Grants: least privilege ─────────────────────────────────────────────────
revoke all on function public.set_user_api_key(text, text)   from public;
revoke all on function public.delete_user_api_key(text)      from public;
revoke all on function public.get_my_api_key_status()        from public;
revoke all on function public.get_user_api_keys(uuid)        from public;

grant execute on function public.set_user_api_key(text, text) to authenticated;
grant execute on function public.delete_user_api_key(text)    to authenticated;
grant execute on function public.get_my_api_key_status()      to authenticated;
-- Decrypt path: service role only. Never granted to anon/authenticated.
grant execute on function public.get_user_api_keys(uuid)      to service_role;
