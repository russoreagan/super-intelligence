-- 045_org_key_vault.sql
-- BYO provider keys become an ORG resource. Today they are keyed by USER id but
-- READ by org id, and that only works because the two values are the same number.
--
-- THE BUG. An organization is the tenant unit: it owns the brain process, the
-- volume, and every per-tenant row. But 003's vault is keyed
--   user_api_keys_meta.user_id uuid references auth.users primary key
-- while every production read passes an ORG id:
--   brain/gateway/server.py  _has_anthropic  -> vault.fetch_user_keys(tenant)
--   brain/gateway/server.py  _org_has_anthropic(org)
--   brain/provisioner.py     injects the tenant's keys at spawn
-- That resolves correctly ONLY because every org alive today is a "personal org"
-- whose id was seeded equal to its owner's auth.users.id (006:49-56, re-seeded by
-- 007:273-281, hardcoded at scripts/create_user.py:83).
--
-- The moment an org exists whose id is a fresh uuid — which is exactly what a
-- second environment (staging alongside prod, one login) requires — the equality
-- breaks and the org cannot boot at all:
--   get_user_api_keys(<random uuid>) finds no row  -> {}
--   -> _has_anthropic false
--   -> the gateway catch-all redirects to /keys
--   -> the user saves a key, which lands under THEIR uid, not the org's id
--   -> the check still fails. An unbreakable loop; the brain never spawns.
-- And GET /api/keys reads the USER's row via get_my_api_key_status(), so the page
-- reports "key on file" while the spawn gate disagrees — two truths, which is the
-- mechanism that makes the loop so confusing to debug.
--
-- THE SHAPE OF THE FIX, and why it is not a backfill. A new org-keyed table, and
-- NO data movement: get_org_api_keys() reads the org row, and when there isn't one
-- falls back to reading the legacy user row IN PLACE at the same uuid. Copying the
-- legacy rows instead would give one vault secret two owners; the legacy write path
-- (set_user_api_key, still live during any rollout) would then update one pointer
-- while reads prefer the other, so a rotation would silently not take effect and a
-- delete would blank the other row's target. Reading in place cannot diverge,
-- because there is only ever one row.
--
-- DEPLOY ORDERING (the concern 036's header raises) is safe in both directions:
--   migration first, code later -> personal orgs read the identical legacy row
--     through the fallback; set_user_api_key still exists; nothing moved. Inert.
--   code first, migration later -> fetch_org_keys() catches undefined_function and
--     retries get_user_api_keys, so reads are unaffected; key WRITES raise and the
--     gateway returns its existing 502. Loud and non-destructive.
-- Rollback = restore 003's get_user_api_keys body. Since nothing was copied,
-- personal orgs are byte-identical afterwards; the only loss is writes made through
-- set_org_api_key after cutover.
--
-- Apply with `supabase db push` (numbered files) — never via the MCP apply_migration.

-- ── Org-keyed vault metadata ────────────────────────────────────────────────
-- secret_ids mirrors 003's shape: { "anthropic": "<vault uuid>", ... }. No
-- plaintext, no ciphertext — just pointers into vault.secrets.
create table if not exists public.org_api_keys_meta (
  org_id        uuid primary key references public.organizations(id) on delete cascade,
  secret_ids    jsonb not null default '{}'::jsonb,
  -- Attribution only: who typed the key in. NOT an ownership or fallback link —
  -- nothing resolves keys through this column. `on delete set null` rather than
  -- 005's cascade because an org must outlive the user who created it; deleting
  -- an employee must not delete the organization's credentials.
  owner_user_id uuid references auth.users on delete set null,
  updated_at    timestamptz not null default now()
);

alter table public.org_api_keys_meta enable row level security;
-- Deliberately NO policies and NO table grants: every access path is a SECURITY
-- DEFINER function below, which is exactly the posture 003 established for
-- user_api_keys_meta (and 026's lesson — Supabase default-grants EXECUTE to anon
-- on create, so every grant here is stated explicitly).

-- ── Decrypt path (pod boot / spawn gate, service_role ONLY) ─────────────────
create or replace function public.get_org_api_keys(p_org_id uuid)
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
  if p_org_id is null then
    return v_out;
  end if;

  select secret_ids into v_ids
    from public.org_api_keys_meta where org_id = p_org_id;

  -- LEGACY FALLBACK. Every org that exists today is a personal org seeded with
  -- id = auth.users.id, so its keys live in user_api_keys_meta under that same
  -- uuid. Read them where they are rather than copying (see the header). The join
  -- to auth.users keeps this honest: a random-uuid org matches no user and falls
  -- straight through to {}.
  if v_ids is null then
    select m.secret_ids into v_ids
      from public.user_api_keys_meta m
      join auth.users u on u.id = m.user_id
      where m.user_id = p_org_id;
  end if;

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

-- Back-compat shim. 003's entry point is misnamed (its p_uid has been an ORG id
-- at every production call site since 006) — keeping it as a delegator is what
-- makes a gateway deployed BEFORE this migration and one deployed AFTER read
-- identically. `create or replace` preserves grants, but 004's whole lesson is to
-- state them anyway; see the grant block at the end.
create or replace function public.get_user_api_keys(p_uid uuid)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select public.get_org_api_keys(p_uid)
$$;

-- ── Write path (gateway, as the authenticated USER) ─────────────────────────
-- NOTE, and it is deliberately the OPPOSITE of 024: do NOT resolve the org as
-- coalesce(auth.uid(), p_org_id). 024's callers are tenant PROCESSES holding the
-- service key, where auth.uid() is null and p_org_id is trusted. This function's
-- caller is an END USER on their own Supabase JWT: auth.uid() is their user id and
-- p_org_id is attacker-controlled request data. They are different things, so
-- p_org_id must be AUTHORIZED against auth.uid() — never substituted for it.
--
-- Admin-only on purpose: a BYO provider key is org-wide spend authority and a
-- shared vendor rate-limit pool. The 006 seed gives every existing user a real
-- role='admin' row on their personal org, so this passes for all of prod on day one.
create or replace function public.set_org_api_key(p_org_id uuid, p_provider text, p_value text)
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
  if p_org_id is null then
    raise exception 'org required';
  end if;
  if not exists (
    select 1 from public.memberships m
    where m.org_id = p_org_id and m.user_id = v_uid and m.role = 'admin'
  ) then
    raise exception 'not an admin of org %', p_org_id;
  end if;
  if not public._valid_api_provider(p_provider) then
    raise exception 'unknown provider: %', p_provider;
  end if;
  if p_value is null or length(btrim(p_value)) = 0 then
    raise exception 'empty value';  -- callers treat blank as "leave unchanged"
  end if;

  insert into public.org_api_keys_meta (org_id, owner_user_id) values (p_org_id, v_uid)
    on conflict (org_id) do nothing;

  select secret_ids ->> p_provider into v_id
    from public.org_api_keys_meta where org_id = p_org_id;

  -- A personal org's FIRST write through this path adopts the legacy pointer, so
  -- a rotation updates the existing vault secret instead of orphaning it and
  -- leaving the old value decryptable under the user row.
  if v_id is null then
    select m.secret_ids ->> p_provider into v_id
      from public.user_api_keys_meta m where m.user_id = p_org_id;
  end if;

  if v_id is null then
    v_name := 'apikey:org:' || p_org_id::text || ':' || p_provider;
    v_id := vault.create_secret(p_value, v_name, 'BYO API key (org)')::text;
  else
    perform vault.update_secret(v_id::uuid, p_value);
  end if;

  update public.org_api_keys_meta
    set secret_ids = jsonb_set(coalesce(secret_ids, '{}'::jsonb), array[p_provider], to_jsonb(v_id)),
        updated_at = now()
    where org_id = p_org_id;
end;
$$;

-- ── Delete path (gateway, as the authenticated USER) ────────────────────────
create or replace function public.delete_org_api_key(p_org_id uuid, p_provider text)
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
  if p_org_id is null then
    raise exception 'org required';
  end if;
  if not exists (
    select 1 from public.memberships m
    where m.org_id = p_org_id and m.user_id = v_uid and m.role = 'admin'
  ) then
    raise exception 'not an admin of org %', p_org_id;
  end if;

  select secret_ids ->> p_provider into v_id
    from public.org_api_keys_meta where org_id = p_org_id;

  if v_id is not null then
    delete from vault.secrets where id = v_id::uuid;
    update public.org_api_keys_meta
      set secret_ids = (secret_ids - p_provider), updated_at = now()
      where org_id = p_org_id;
    return;
  end if;

  -- Legacy row for a personal org that has never been written through the org
  -- path. Delete it where it lives, so "remove my key" is not silently a no-op.
  select m.secret_ids ->> p_provider into v_id
    from public.user_api_keys_meta m where m.user_id = p_org_id;
  if v_id is not null then
    delete from vault.secrets where id = v_id::uuid;
    update public.user_api_keys_meta
      set secret_ids = (secret_ids - p_provider), updated_at = now()
      where user_id = p_org_id;
  end if;
end;
$$;

-- ── Status path (gateway/UI, as the authenticated USER) ─────────────────────
-- Booleans only, never values or vault uuids. Readable by ANY member (a plain
-- member needs to see whether the org is configured); writes above stay admin-only.
-- This is the half that fixes the "two truths" bug: it resolves through exactly
-- the same org row + legacy fallback the spawn gate uses, so the page and the gate
-- can no longer disagree.
create or replace function public.get_org_api_key_status(p_org_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
  v_ids jsonb;
  v_updated timestamptz;
begin
  if v_uid is null then
    raise exception 'not authenticated';
  end if;
  if p_org_id is null then
    raise exception 'org required';
  end if;
  if not exists (
    select 1 from public.memberships m
    where m.org_id = p_org_id and m.user_id = v_uid
  ) then
    raise exception 'not a member of org %', p_org_id;
  end if;

  select secret_ids, updated_at into v_ids, v_updated
    from public.org_api_keys_meta where org_id = p_org_id;

  if v_ids is null then
    select m.secret_ids, m.updated_at into v_ids, v_updated
      from public.user_api_keys_meta m
      join auth.users u on u.id = m.user_id
      where m.user_id = p_org_id;
  end if;

  v_ids := coalesce(v_ids, '{}'::jsonb);
  return jsonb_build_object(
    'anthropic',  (v_ids ? 'anthropic'),
    'elevenlabs', (v_ids ? 'elevenlabs'),
    'deepgram',   (v_ids ? 'deepgram'),
    'google',     (v_ids ? 'google'),
    'updated_at', v_updated
  );
end;
$$;

-- ── Provisioning helper: copy one org's keys to another (service_role ONLY) ──
-- Creates NEW vault secrets in the target from the source's decrypted values.
-- Fresh secret uuids, never shared pointers: sharing a pointer would make a delete
-- on either side blank the other, and would make a rotation in staging silently
-- cross into prod — precisely what separate environments exist to prevent.
--
-- This is what keeps "create a staging org" from demanding the user re-enter their
-- Anthropic key, WITHOUT introducing an implicit runtime fallback to the owner's
-- personal key (which would let a runaway staging loop throttle and bill prod
-- invisibly, and would make "rotate staging" an unobservable no-op).
-- The two orgs share one vendor invoice afterwards; separate vendor keys are the
-- only way to separate that bill, and the caller's help text should say so.
create or replace function public.copy_org_api_keys(p_from_org uuid, p_to_org uuid)
returns int
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_src jsonb;
  v_provider text;
  v_value text;
  v_new text;
  v_n int := 0;
begin
  if p_from_org is null or p_to_org is null then
    raise exception 'both orgs required';
  end if;
  if p_from_org = p_to_org then
    return 0;
  end if;

  -- Resolve through the same path the spawn gate uses, so a personal source org
  -- whose keys are still in the legacy row copies correctly.
  v_src := public.get_org_api_keys(p_from_org);
  if v_src is null or v_src = '{}'::jsonb then
    return 0;
  end if;

  insert into public.org_api_keys_meta (org_id) values (p_to_org)
    on conflict (org_id) do nothing;

  for v_provider in select jsonb_object_keys(v_src) loop
    v_value := v_src ->> v_provider;
    continue when v_value is null or length(btrim(v_value)) = 0;
    -- Skip a provider the target already has: copying is a seed, not a rotation,
    -- and must never clobber a key the target org set for itself.
    if (select secret_ids ? v_provider from public.org_api_keys_meta where org_id = p_to_org) then
      continue;
    end if;
    v_new := vault.create_secret(
      v_value,
      'apikey:org:' || p_to_org::text || ':' || v_provider,
      'BYO API key (org, copied at provisioning)'
    )::text;
    update public.org_api_keys_meta
      set secret_ids = jsonb_set(coalesce(secret_ids, '{}'::jsonb), array[v_provider], to_jsonb(v_new)),
          updated_at = now()
      where org_id = p_to_org;
    v_n := v_n + 1;
  end loop;
  return v_n;
end;
$$;

-- ── Grants: least privilege ─────────────────────────────────────────────────
-- Stated explicitly rather than inherited: Supabase default-grants EXECUTE to
-- anon on create (the rule migration 026 established).
revoke all on function public.get_org_api_keys(uuid)                  from public;
revoke all on function public.get_user_api_keys(uuid)                 from public;
revoke all on function public.set_org_api_key(uuid, text, text)       from public;
revoke all on function public.delete_org_api_key(uuid, text)          from public;
revoke all on function public.get_org_api_key_status(uuid)            from public;
revoke all on function public.copy_org_api_keys(uuid, uuid)           from public;

revoke execute on function public.get_org_api_keys(uuid)        from anon, authenticated;
revoke execute on function public.get_user_api_keys(uuid)       from anon, authenticated;
revoke execute on function public.copy_org_api_keys(uuid, uuid) from anon, authenticated;

-- Decrypt + copy paths: service role only. Never granted to anon/authenticated.
grant execute on function public.get_org_api_keys(uuid)        to service_role;
grant execute on function public.get_user_api_keys(uuid)       to service_role;
grant execute on function public.copy_org_api_keys(uuid, uuid) to service_role;

-- User-facing paths: the authenticated user, authorized per-org inside each body.
grant execute on function public.set_org_api_key(uuid, text, text) to authenticated;
grant execute on function public.delete_org_api_key(uuid, text)    to authenticated;
grant execute on function public.get_org_api_key_status(uuid)      to authenticated;
