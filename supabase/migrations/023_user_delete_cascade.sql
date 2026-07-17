-- 023_user_delete_cascade.sql
-- Deleting an auth user was impossible — the dashboard failed with "Database
-- error deleting user", and the admin API 500'd.
--
-- Cause: two of our tables referenced auth.users with the DEFAULT delete rule
-- (NO ACTION), so Postgres correctly refused to remove a row that memberships /
-- user_profiles still pointed at. Every OTHER reference was already fine:
-- auth's own tables cascade, and 005 gave user_api_keys_meta the same treatment
-- this migration now applies to the last two holdouts. Nothing about the delete
-- was ever going to work until these constraints changed.
--
-- Ordering note: these are the only two blockers as of this migration. Any NEW
-- table keyed by a USER (rather than by org_id, which already cascades from
-- organizations) must specify on delete cascade, or user deletion breaks again.

alter table user_profiles
  drop constraint if exists user_profiles_id_fkey,
  add constraint user_profiles_id_fkey
    foreign key (id) references auth.users(id) on delete cascade;

alter table memberships
  drop constraint if exists memberships_user_id_fkey,
  add constraint memberships_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

-- ── Personal orgs die with their owner ───────────────────────────────────────
-- A personal org's id IS its owner's user id (the 006 seed and create_user.py
-- both establish this). organizations deliberately has no FK to auth.users — a
-- multi-member B2B org outlives any one member — so the cascades above would
-- leave the deleted user's personal org behind, and with it every episode,
-- wiring edge, agent, api key, session and job that cascades from it: an
-- unreachable island of that person's data, kept forever after they were
-- deleted. Drop the personal org with the user; the existing on-delete-cascade
-- FKs from organizations(id) clear everything beneath it.
--
-- A multi-member org is untouched: its id is a fresh uuid, so it matches no
-- user id and this delete finds nothing.
create or replace function public.drop_personal_org_on_user_delete()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  delete from public.organizations where id = old.id;
  return old;
end;
$$;

revoke all on function public.drop_personal_org_on_user_delete() from public;

drop trigger if exists on_auth_user_deleted on auth.users;
create trigger on_auth_user_deleted
  after delete on auth.users
  for each row execute function public.drop_personal_org_on_user_delete();
