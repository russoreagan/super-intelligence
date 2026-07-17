-- 026_end_user_mcp_tokens_revoke_anon.sql
-- SECURITY FIX for a hole opened by 024.
--
-- 024 made the three end_user_mcp_token RPCs resolve the org as
--   coalesce(auth.uid(), p_org_id)
-- so they work under the service-key fallback (no auth.uid()). That is safe for
-- the intended callers — the pod as service_role, or as `authenticated` carrying
-- an org JWT (auth.uid() is non-null and WINS, so p_org_id is ignored). It is NOT
-- safe for the `anon` role: anon has no auth.uid(), so coalesce() falls through to
-- a caller-supplied p_org_id, and these SECURITY DEFINER functions would then
-- read / write / delete DECRYPTED end-user MCP tokens for ANY org.
--
-- Supabase's default privileges grant EXECUTE to `anon` on every newly created
-- function, and `revoke ... from public` does NOT remove a direct grant to the
-- `anon` role — so 012's and 024's grant blocks left anon able to call these over
-- the public PostgREST endpoint (/rest/v1/rpc/...). It was harmless before 024
-- only because the old body fail-closed (auth.uid() null -> raise). 024 removed
-- that protection for anon; this migration restores it.
--
-- The pod never connects as anon (service key -> service_role; or anon key + an
-- org JWT whose role claim is 'authenticated'), so revoking anon breaks nothing.
-- Keep EXECUTE for authenticated (org-JWT mode) and service_role (fallback mode).
--
-- NOTE: any FUTURE migration that DROPs + recreates these functions will let
-- Supabase's default privileges re-grant anon — such a migration must repeat these
-- revokes. The sibling connector RPCs in 013 (get/register/delete_mcp_connector)
-- are also anon-executable but still fail-closed (auth.uid()-only); if they ever
-- gain a p_org_id fallback they need the same revoke.

revoke execute on function public.set_end_user_mcp_token(text, text, text, text, timestamptz, uuid) from anon, public;
revoke execute on function public.get_end_user_mcp_tokens(text, uuid) from anon, public;
revoke execute on function public.delete_end_user_mcp_token(text, text, uuid) from anon, public;
