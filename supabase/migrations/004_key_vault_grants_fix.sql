-- SECURITY FIX for 003: lock down the vault RPC grants.
--
-- Supabase grants EXECUTE on public-schema functions to anon + authenticated by
-- default (via default privileges), so `revoke all ... from public` in 003 was
-- NOT enough — the anon/authenticated grants survived and anon could call
-- get_user_api_keys (the decrypt path). Revoke from those roles explicitly.

-- Decrypt path: service_role ONLY. Never anon, never authenticated (a logged-in
-- user must not be able to decrypt arbitrary users' keys).
revoke execute on function public.get_user_api_keys(uuid) from anon, authenticated, public;
grant  execute on function public.get_user_api_keys(uuid) to service_role;

-- User-facing RPCs: authenticated only (auth.uid()-scoped inside). Never anon.
revoke execute on function public.set_user_api_key(text, text) from anon, public;
revoke execute on function public.delete_user_api_key(text)     from anon, public;
revoke execute on function public.get_my_api_key_status()       from anon, public;
grant  execute on function public.set_user_api_key(text, text)  to authenticated;
grant  execute on function public.delete_user_api_key(text)     to authenticated;
grant  execute on function public.get_my_api_key_status()       to authenticated;
