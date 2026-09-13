-- 041_fleet_persona_counts.sql
-- Superadmin fleet view (brain/gateway/fleet_orgs.py): ONE grouped query for the
-- per-org persona counts instead of three `count=exact, head=true` selects per
-- org on every 30 s refresh. Same three numbers the head counts produced:
--
--   persona_count       live custom personas   (builtin = false)
--   clone_count         of those, clones       (template <> '')
--   active_roster_size  any persona with a human turn since p_active_since
--                       (fleet_orgs.ACTIVE_DAYS = 7; builtin included, as before)
--
-- Soft-deleted rows (deleted_at set) are excluded throughout. Orgs with no
-- personas produce no row; the caller treats a missing org as zeros.
--
-- SECURITY DEFINER because personas is RLS-scoped to auth.uid() = org_id and the
-- gateway is not pinned to an org. Only the service_role key may call it: the
-- gateway is the sole caller, and the function reads every org at once. anon is
-- revoked explicitly — Supabase's default privileges grant EXECUTE to anon on
-- every new public function and `revoke ... from public` does not remove that
-- direct grant (see 040). Idempotent; safe to re-run.
--
-- The gateway falls back to the per-org head counts while this is unapplied.

create or replace function public.fleet_persona_counts(p_active_since timestamptz)
returns table (
  org_id uuid,
  persona_count bigint,
  clone_count bigint,
  active_roster_size bigint
)
language sql
stable
security definer
set search_path = public
as $$
  select p.org_id,
         count(*) filter (where not p.builtin)                          as persona_count,
         count(*) filter (where not p.builtin and p.template <> '')     as clone_count,
         count(*) filter (where p.last_human_turn_ts >= p_active_since) as active_roster_size
    from public.personas p
   where p.deleted_at is null
   group by p.org_id
$$;

revoke all on function public.fleet_persona_counts(timestamptz) from public;
revoke execute on function public.fleet_persona_counts(timestamptz) from anon, authenticated, public;
grant execute on function public.fleet_persona_counts(timestamptz) to service_role;
