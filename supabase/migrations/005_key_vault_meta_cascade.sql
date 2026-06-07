-- Delete a user → drop their key metadata. (Vault secrets are removed via
-- delete_user_api_key during normal use; orphan cleanup on hard user-delete is
-- deferred hardening.)
alter table user_api_keys_meta
  drop constraint if exists user_api_keys_meta_user_id_fkey,
  add constraint user_api_keys_meta_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;
