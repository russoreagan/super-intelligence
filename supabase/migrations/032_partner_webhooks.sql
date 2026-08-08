-- 032_partner_webhooks.sql
-- Signed outbound webhooks for autonomous job outcomes, and the attribution needed to
-- route them.
--
-- Today the only way a partner learns a background job finished is to hold a WebSocket
-- open or poll GET /v1/jobs. There is an OLDER env-var webhook path
-- (brain/ui/emitter.py) but it is a single deployment-wide URL — every tenant posts to
-- it — unsigned, secret-as-bearer, no SSRF guard. This migration backs the replacement,
-- which is deleted-and-signed: per-partner registration, HMAC signatures, a delivery
-- ledger with retries. The env path is removed in the same change.
--
-- Three parts:
--   1. agent_jobs attribution. The table records only agent_id, so which PARTNER (or
--      end user, or session) a job belongs to is unrecoverable — and a webhook must
--      route to the initiating partner and no other. Add the columns; the store fills
--      them from the (now partner-aware) turn context at write time.
--   2. partner_webhooks. Registration metadata; the signing secret lives in Supabase
--      Vault (secret_id only here), mirroring 012_end_user_mcp_tokens.
--   3. webhook_deliveries. The durable outbox/retry ledger. The brain writes a row when
--      a job reaches a terminal state; the gateway sweeper (always up, service role)
--      retries it — the brain sleeps, so it cannot own a multi-hour backoff schedule.
--
-- SECURITY: the Vault RPCs are security-definer with the `p_org_id` service-key
-- fallback (the gateway signs asymmetrically, so auth.uid() is null there) and carry
-- the `revoke ... from anon, public` that migration 026 requires of every such
-- function. The signing secret is never returned by a metadata read — only
-- get_partner_webhook_secret returns plaintext, and only the signer calls it.

-- ── 1. Attribution on agent_jobs ────────────────────────────────────────────
alter table agent_jobs add column if not exists partner_id text not null default '';
alter table agent_jobs add column if not exists end_user_id text not null default '';
alter table agent_jobs add column if not exists origin_session_id text not null default '';
create index if not exists agent_jobs_org_partner_idx on agent_jobs(org_id, partner_id);

-- ── 2. partner_webhooks ─────────────────────────────────────────────────────
create table if not exists public.partner_webhooks (
  org_id     uuid        references organizations(id) on delete cascade not null,
  id         text        not null,                     -- public handle, wh_<hex>
  partner_id text        not null default '',          -- '' = owner-registered (org-wide)
  url        text        not null,
  secret_id  uuid        not null,                     -- vault.secrets.id
  events     jsonb       not null default '["job"]'::jsonb,
  active     boolean     not null default true,
  disabled_reason text   not null default '',
  consecutive_failures int not null default 0,
  created_ts timestamptz not null default now(),
  updated_ts timestamptz not null default now(),
  primary key (org_id, id)
);
alter table public.partner_webhooks enable row level security;
create policy "org can manage own partner_webhooks" on public.partner_webhooks for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
create index if not exists partner_webhooks_org_partner_idx
  on public.partner_webhooks(org_id, partner_id);
grant select, insert, update, delete on public.partner_webhooks to authenticated;

-- Create-or-update a webhook and create/rotate its Vault secret in one call, like
-- set_end_user_mcp_token. secret_id never leaves the database.
create or replace function public.set_partner_webhook(
  p_id         text,
  p_partner_id text,
  p_url        text,
  p_events     jsonb,
  p_secret     text,
  p_org_id     uuid default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_sid uuid;
  v_name text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  if p_secret is null or length(btrim(p_secret)) = 0 then
    raise exception 'empty secret';
  end if;
  select secret_id into v_sid
    from public.partner_webhooks where org_id = v_org and id = p_id;
  v_name := 'webhook:' || v_org::text || ':' || p_id;
  if v_sid is null then
    v_sid := vault.create_secret(p_secret, v_name, 'partner webhook signing secret')::uuid;
    insert into public.partner_webhooks (org_id, id, partner_id, url, secret_id, events)
      values (v_org, p_id, coalesce(p_partner_id, ''), p_url, v_sid,
              coalesce(p_events, '["job"]'::jsonb));
  else
    perform vault.update_secret(v_sid, p_secret);
    update public.partner_webhooks
      set url = p_url,
          events = coalesce(p_events, events),
          active = true,
          disabled_reason = '',
          consecutive_failures = 0,
          updated_ts = now()
      where org_id = v_org and id = p_id;
  end if;
end;
$$;

-- Read the plaintext signing secret. Called ONLY by the signer (the gateway sweeper,
-- under service role). Never surfaced through a metadata endpoint.
create or replace function public.get_partner_webhook_secret(
  p_id     text,
  p_org_id uuid default null
)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_sid uuid;
  v_secret text;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  select secret_id into v_sid
    from public.partner_webhooks where org_id = v_org and id = p_id;
  if v_sid is null then
    return null;
  end if;
  select decrypted_secret into v_secret from vault.decrypted_secrets where id = v_sid;
  return v_secret;
end;
$$;

-- Delete a webhook and its Vault secret together (an orphaned secret is worse than
-- the row — nothing can ever find it to clean up).
create or replace function public.delete_partner_webhook(
  p_id     text,
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
  select secret_id into v_sid
    from public.partner_webhooks where org_id = v_org and id = p_id;
  if v_sid is null then
    return false;
  end if;
  delete from vault.secrets where id = v_sid;
  delete from public.partner_webhooks where org_id = v_org and id = p_id;
  return true;
end;
$$;

-- ── 3. webhook_deliveries ───────────────────────────────────────────────────
create table if not exists public.webhook_deliveries (
  org_id     uuid        references organizations(id) on delete cascade not null,
  id         text        not null,                     -- delivery id, dlv_<hex>
  webhook_id text        not null,
  event_id   text        not null,                     -- stable across retries; dedupe key
  event_type text        not null,                     -- job.completed | job.failed | ...
  payload    jsonb       not null default '{}'::jsonb,
  state      text        not null default 'pending',   -- pending|sending|delivered|failed|dead_letter
  attempts   int         not null default 0,
  last_status int,
  last_error text        not null default '',
  next_attempt_ts timestamptz not null default now(),
  created_ts timestamptz not null default now(),
  updated_ts timestamptz not null default now(),
  primary key (org_id, id)
);
alter table public.webhook_deliveries enable row level security;
create policy "org can manage own webhook_deliveries" on public.webhook_deliveries for all
  using (auth.uid() = org_id) with check (auth.uid() = org_id);
-- Per-webhook history for the debugging endpoint.
create index if not exists webhook_deliveries_org_webhook_idx
  on public.webhook_deliveries(org_id, webhook_id, created_ts desc);
-- The gateway sweeper claim scan, deliberately NOT org-prefixed — it runs cross-org
-- under service role. Partial, so it only indexes rows still needing work.
create index if not exists webhook_deliveries_due_idx
  on public.webhook_deliveries(next_attempt_ts)
  where state in ('pending', 'failed');
grant select, insert, update, delete on public.webhook_deliveries to authenticated;

-- ── Grants / revokes (026 convention) ───────────────────────────────────────
revoke all on function public.set_partner_webhook(text, text, text, jsonb, text, uuid) from public;
revoke all on function public.get_partner_webhook_secret(text, uuid) from public;
revoke all on function public.delete_partner_webhook(text, uuid) from public;
revoke execute on function public.set_partner_webhook(text, text, text, jsonb, text, uuid) from anon, public;
revoke execute on function public.get_partner_webhook_secret(text, uuid) from anon, public;
revoke execute on function public.delete_partner_webhook(text, uuid) from anon, public;
grant execute on function public.set_partner_webhook(text, text, text, jsonb, text, uuid) to authenticated, service_role;
grant execute on function public.get_partner_webhook_secret(text, uuid) to authenticated, service_role;
grant execute on function public.delete_partner_webhook(text, uuid) to authenticated, service_role;
