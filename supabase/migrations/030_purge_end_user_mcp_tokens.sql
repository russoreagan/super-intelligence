-- 030_purge_end_user_mcp_tokens.sql
-- Bulk-revoke every MCP connector an end user has, for right-to-erasure.
--
-- The GDPR purge (brain/session_turn.py) claimed to erase "every per-user table" and
-- did not touch end_user_mcp_tokens at all, so a deleted customer's vault-encrypted
-- third-party OAuth tokens survived an "irreversible" erasure. Those are live
-- credentials to the customer's own Gmail, Drive and so on — the single worst thing
-- in the system to retain past a deletion request.
--
-- It could not be a plain table delete: the row only holds a secret_id pointing into
-- Supabase Vault, so deleting the row alone ORPHANS the secret — the ciphertext stays
-- in vault.secrets with nothing left referencing it, which is worse than before
-- because now nothing can ever find it to clean up. This function walks the rows and
-- drops both halves.
--
-- Shaped exactly like delete_end_user_mcp_token (024): security definer,
-- `set search_path = ''`, and the same `coalesce(auth.uid(), p_org_id)` so it works
-- both under a real org JWT and under the service-key fallback the pod actually runs
-- in (asymmetric signing → auth.uid() is null).
--
-- This CREATES a new function rather than dropping any existing one, so the warning
-- in 026 about re-granting anon does not bite here — but the revoke below is issued
-- anyway, because a new security-definer function gets Supabase's default privileges
-- and would otherwise be anon-executable.

create or replace function public.purge_end_user_mcp_tokens(
  p_end_user_id text,
  p_org_id      uuid default null
)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_org uuid := coalesce(auth.uid(), p_org_id);
  v_row record;
  v_n   integer := 0;
begin
  if v_org is null then
    raise exception 'not authenticated';
  end if;
  for v_row in
    select server_name, secret_id
      from public.end_user_mcp_tokens
      where org_id = v_org and end_user_id = p_end_user_id
  loop
    -- Secret first: if this statement fails the row survives and the purge can be
    -- retried. Deleting the row first would strand the secret permanently.
    delete from vault.secrets where id = v_row.secret_id;
    v_n := v_n + 1;
  end loop;
  delete from public.end_user_mcp_tokens
    where org_id = v_org and end_user_id = p_end_user_id;
  return v_n;
end;
$$;

revoke all on function public.purge_end_user_mcp_tokens(text, uuid) from public;
revoke execute on function public.purge_end_user_mcp_tokens(text, uuid) from anon, public;
grant execute on function public.purge_end_user_mcp_tokens(text, uuid) to authenticated, service_role;
