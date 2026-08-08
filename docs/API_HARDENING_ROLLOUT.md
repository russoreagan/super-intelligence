# API hardening — rollout and next steps

Branch `api-hardening`. 2826 tests green, ruff clean, nothing committed yet.
Background: `docs/API_REVIEW_third_party.md`. The two follow-up deliverables
(per-partner cloud budgets, signed webhooks) are now **implemented** on this branch;
their design is in `.claude/plans/put-together-a-plan-cryptic-pike.md`.

---

## 0. Blocked on you: apply the migrations

`supabase db push` is refused by the Claude Code permission classifier (mutating
Supabase CLI commands are blocked; read-only ones pass). Verified state: remote is
clean at `001…027`, and `028`–`032` are local-only. A dry run confirms exactly those
would be pushed.

| Migration | Adds |
| --- | --- |
| `028_api_keys_role_and_hash_index` | `api_keys.role` + key_hash index |
| `029_end_users` | ownership registry + backfill |
| `030_purge_end_user_mcp_tokens` | GDPR vault-purge RPC |
| `031_partner_cloud_usage` | per-partner spend table + atomic bump RPC |
| `032_partner_webhooks` | webhooks + deliveries + Vault RPCs + `agent_jobs` attribution cols |

All additive; `db push` in order. Do **not** use the Supabase MCP `apply_migration`
(timestamp versions → the 2026-07-17 split-brain).

Run from the repo root (the CLI is linked here, not in a worktree):

```bash
supabase db push
```

**Do not apply these through the Supabase MCP `apply_migration`.** It mints a
timestamp version in `schema_migrations` while the repo tracks numbers, which is
exactly the split-brain that had to be reconciled on 2026-07-17. One mechanism only:
numbered files plus `db push`.

What lands:

| Migration | Effect | Risk |
| --- | --- | --- |
| `028_api_keys_role_and_hash_index` | Adds `api_keys.role` (default `'partner'`) + an index on `key_hash` alone | Additive. Grants nobody anything: every existing row stays `partner`. |
| `029_end_users` | New ownership table, RLS, and a backfill from `api_sessions` | Additive. The backfill is `on conflict do nothing`. |
| `030_purge_end_user_mcp_tokens` | New security-definer RPC for erasure | New function; the `revoke ... from anon, public` is in the file. |

Verify after applying:

```bash
supabase migration list          # expect 028/029/030 in both columns
```

Then confirm in SQL that `end_users` has RLS enabled, its policy is
`auth.uid() = org_id`, and `purge_end_user_mcp_tokens` is not executable by `anon`.

**Ordering is safe either way.** The auth lookups deliberately `select("*")` rather
than naming `role`, because PostgREST errors on an unknown column — naming it would
have made a code-ahead-of-migration deploy return 503 for every request. Applying the
migration first is still the tidier order.

---

## 1. Review and merge

The diff is ~1550 insertions across 18 modified files plus 11 new ones. Worth reading
closely, in this order:

1. `brain/api/auth.py` and `_require` in `brain/api/server.py` — the escalation fix.
   Note the deliberately unreachable test-only branch and why it is static.
2. `brain/api/end_users.py` — first-writer-wins, and why it is never an upsert.
3. `brain/session_turn.py::api_purge_end_user` — the rewritten erasure.
4. `brain/api/rate_limit.py` — fixed windows and the negative cache.

Three behaviour changes that are breaking by intent (no live partners):

- `Authorization` without `Bearer ` no longer authenticates.
- Org config writes (mandates, personas, agents) now require an owner credential.
- `POST /v1/sleep` now requires an owner credential.

---

## 2. Deploy and smoke test

Railway auto-deploys from `main`; builds take ~20–25 minutes and queue serially, so
budget for that. `/health` carries the running commit sha — confirm it matches before
trusting any result below.

Then, against the deployed branch:

1. Mint one **owner-role** key and one **partner** key from the API workspace.
2. `GET /v1/capabilities` with each. Partner must not see `spent_usd_today`.
3. Partner `PUT /v1/mandates/<id>` → `403`; owner → not 403.
4. Partner `POST /v1/sleep` → `403`. Owner → `200`, and the org actually sleeps.
5. `GET /v1/openapi.json` unauthenticated → contains `/v1/sessions`, does **not**
   contain `/v1/partner_keys` or the mandates `put`.
6. Any response → confirm `X-Request-Id` is present.
7. Open a session, run a turn, grade it — the Quickstart path still works end to end.

### Two side effects to expect

**Existing per-speaker profiles are orphaned.** `speaker_filename` now appends a
digest of the raw id, so previously written `user_<slug>.md` documents no longer
resolve and a fresh profile is created on the customer's next contact. This is
deliberate: the old name was lossy, so two different customers could share one
profile, and no backfill can un-merge them. Learned profiles regenerate; nothing else
is lost.

**`end_users` is backfilled only from `api_sessions`.** That is the only table
carrying both `end_user_id` and `partner_id`. Any customer not represented there
lands unowned, which means owner-only until a partner claims them. Fail-closed, and
the right direction — attributing a customer to the wrong partner would hand over the
data this table exists to protect.

---

## 3. Check this before anything else ships

`brain/ui/emitter.py:47-79` and `:222-283` POST to `AGENT_WORK_WEBHOOK_URL` /
`AGENT_WEBHOOK_URL`, a **single deployment-wide URL**. Under the multi-tenant gateway
that means every tenant's proactive output goes to whichever partner's endpoint is
configured. It also sends the secret as a bearer token (replayable by anyone who sees
it), has no signature, and no SSRF guard.

**Check whether either variable is set in the Railway environment.** If it is, that is
a live cross-tenant leak and should be unset now, ahead of the webhook work below.
If it is not set, the path is inert and can wait for §5.

---

## 4. Next: per-partner cloud budgets

The daily USD ceiling is still per org, so one partner can exhaust it for its
siblings. The guide says so, accurately, but it is the remaining isolation gap.

- Migration `031`: `partner_cloud_usage (org_id, partner_id, usage_date, usd)` plus an
  atomic `bump_partner_cloud_usd(...)` doing `on conflict do update set usd = usd +
  excluded.usd`. The atomic increment is the point — it also fixes the lost-update bug
  where every dedicated persona instance full-dict-overwrites the same
  `cloud_usage.json`, under-counting spend whenever multi-persona is on.
- Thread `partner_id` through the existing `brain/turn_ctx.py` contextvar, bound where
  `ctx.get("partner_id")` is already in scope. Thread-pool workers do not inherit
  contextvars — resolve on the event loop and pass the value in.
- Change the full-tier behaviour **for partner lanes only**: raise
  `CloudBudgetExceeded` → 402 rather than silently rerouting to local models. A partner
  paying for cloud-tier answers should be told it stopped, not quietly served a worse
  model. Keep the reroute for the owner lane, where a human sees the change.
- Then update guide §7, which currently documents the per-org behaviour.

Estimated: one migration, ~4 files, a day.

---

## 5. Next: signed webhooks, and delete the unsigned path

`GET /v1/jobs` polling or a held-open WebSocket are the only ways to learn a job
finished, which is awkward for the product the engine is actually selling.

- Migration `032`: `partner_webhooks` (secret in Vault, mirroring `012`),
  `webhook_deliveries`, and `agent_jobs.partner_id`.
- **Decide attribution first.** Jobs run owner-lane with no partner today. Rule:
  partner-registered hooks receive only their own partner's jobs; owner-registered
  hooks receive everything including self-directed work. Without this, one partner's
  job goals leak to another.
- **Enqueue in the brain, retry in the gateway.** The brain sleeps, and "the job
  finished while you were disconnected" is precisely when it idles out — a
  brain-resident retry loop would lose every backoff. Delivery must not wake the brain.
- Sign Stripe-scheme: `t=<ts>,v1=<hmac_sha256(secret, "t.body")>` over the exact bytes,
  with a stable event id for receiver dedupe. Never the secret as a bearer token.
- SSRF guard extracted from `brain/clusters/motor_dispatcher.py:451-475` so there is
  one copy: resolve every A/AAAA record, reject private/loopback/link-local
  (169.254.0.0/16 is the metadata endpoint), no redirects, and **re-check on every
  attempt** — registration-time validation alone loses to DNS rebinding.
- Ship the outcome summary plus a job link, never `steps_json`/`results_json`.
- **Then delete the env path** and its four variables from `docs/ENV_VARS.md`. Two
  webhook systems, one signed and one not, is the bad outcome.

Estimated: the largest remaining item. Two or three days.

---

## 6. Smaller things worth doing

- **Idempotency keys.** §4 tells clients to retry aggressively while `POST /v1/sessions`
  has no idempotency, so a timed-out create silently duplicates. The guide is honest
  about this now; the fix touches session creation, turns, grading and the store, so it
  wants its own pass.
- **Thread `end_user_id` through `brain_schemas`.** The writer hardcodes `''`
  (`store.py:682`) and four reads pin the same constant. The purge works around it by
  deleting on the derived filename; doing it properly needs a backfill plan or every
  existing profile is orphaned a second time.
- **Prune `select("*")` in auth** once `028` is applied everywhere, back to naming the
  columns.
- **Docstring pass on public routes.** They feed the public OpenAPI `description`, and
  several read as internal architecture notes rather than partner-facing prose.
