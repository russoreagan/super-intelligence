-- 042_gateway_db_webhook.sql
--
-- The gateway's reconciler is event-driven (brain/gateway/reconciler.py). The
-- owner API already nudges it when it writes a placement or a budget, but a row
-- edited any other way — the Supabase dashboard, a SQL migration, a partner
-- script against PostgREST — was only noticed at the 15-minute resync. This
-- makes the DATABASE the source of that edge: a trigger on persona_placement
-- (insert/update/delete) and on the organizations caps posts to the gateway's
-- public webhook route (POST /__nudge/db) through pg_net, asynchronously, with
-- a shared secret. The gateway wakes and re-reads; nothing else changes.
--
-- Configuration lives in Vault, never in this file:
--   vault.create_secret('https://<gateway host>/__nudge/db', 'gateway_nudge_url');
--   vault.create_secret('<BRAIN_DB_WEBHOOK_SECRET value>', 'gateway_nudge_secret');
-- With either missing the trigger is a no-op (it returns before posting), so this
-- migration is safe to apply before the gateway or the secrets exist. pg_net is
-- asynchronous: a failed post lands in net._http_response and never fails the
-- write that caused it.
--
-- Apply with `supabase db push` (numbered files), after 041 — never via the MCP
-- apply_migration.

create extension if not exists pg_net with schema extensions;

create or replace function public.gateway_nudge() returns trigger
language plpgsql
security definer
set search_path = public, extensions, vault, net
as $$
declare
  v_url text;
  v_secret text;
  v_reason text;
  v_org text;
  v_persona text;
  v_payload jsonb;
begin
  select decrypted_secret into v_url
    from vault.decrypted_secrets where name = 'gateway_nudge_url' limit 1;
  select decrypted_secret into v_secret
    from vault.decrypted_secrets where name = 'gateway_nudge_secret' limit 1;
  if v_url is null or v_url = '' or v_secret is null or v_secret = '' then
    return null;  -- not configured: silently no edge (the resync still covers it)
  end if;
  if tg_table_name = 'persona_placement' then
    v_reason := 'placement';
    v_org := coalesce(new.org_id, old.org_id)::text;
    v_persona := coalesce(new.persona, old.persona);
  else
    v_reason := 'budget';
    v_org := coalesce(new.id, old.id)::text;
    v_persona := null;
  end if;
  v_payload := jsonb_build_object(
    'reason', v_reason,
    'table', tg_table_name,
    'op', tg_op,
    'org', v_org,
    'persona', v_persona,
    'ts', extract(epoch from now())
  );
  perform net.http_post(
    url := v_url,
    body := v_payload,
    headers := jsonb_build_object(
      'content-type', 'application/json',
      'x-brain-webhook-secret', v_secret
    ),
    timeout_milliseconds := 3000
  );
  return null;
exception when others then
  -- A webhook must never break the write that caused it.
  raise notice 'gateway_nudge skipped: %', sqlerrm;
  return null;
end
$$;

revoke all on function public.gateway_nudge() from public;
revoke execute on function public.gateway_nudge() from anon, authenticated;

drop trigger if exists persona_placement_gateway_nudge on public.persona_placement;
create trigger persona_placement_gateway_nudge
  after insert or update or delete on public.persona_placement
  for each row execute function public.gateway_nudge();

drop trigger if exists organizations_gateway_nudge on public.organizations;
create trigger organizations_gateway_nudge
  after update of gpu_daily_usd_budget, max_dedicated_instances, learning_mode, instance_seed
  on public.organizations
  for each row execute function public.gateway_nudge();

comment on function public.gateway_nudge() is
  'Posts a placement/budget edge to the gateway''s POST /__nudge/db via pg_net; '
  'reads gateway_nudge_url / gateway_nudge_secret from Vault; no-op when unset.';
