# Engine API — third-party integration review

A cold read of `brain/api/api_guide.md` as an outside developer preparing an integration, with
every claim checked against the implementation. Findings are split into what the documentation
gets wrong and what the *documentation reveals* about the app.

First, the honest framing: this is a genuinely good API guide. The cold-start contract, the
`done`-is-authoritative rule, the `501` vs `503` distinction, and the `applied_live` explanation
are the kind of thing most APIs make you discover in production. The problems below are almost
all cases where the docs are *more coherent than the system they describe*.

---

## A. The blocker: I cannot actually start

**A1. There is no way to get a key.** The Quickstart opens with `$ELYCEUM_KEY` and never says
where it comes from. Following the guide's own rules leads to a dead end:

- §3 says hosted clients **must** use a partner key.
- Partner keys are minted by `POST /v1/partner_keys` (§23), which is **owner-gated**.
- §3 also says owner keys are **not resolvable on the hosted gateway**.

So on `api.elyceum.app` the only endpoint that mints a key cannot be called by anyone. The real
answer is presumably "the org owner mints one for you in the web UI, out of band" — but that
sentence does not exist anywhere in 1300 lines. This is the single highest-value fix in the
document: a §0 "Getting your key" with the actual human workflow.

**A2. No sandbox, no test mode, no free path.** Every call in the guide bills real cloud spend
and, on a cold pod, warms a GPU. There is no way to build against this API without spending
money from the first curl. For an evaluating integrator that is a significant adoption tax.

---

## B. Security findings

Severity reflects a hosted, multi-partner org — which is the exact deployment the guide sells.

### B1. Partner scoping is promised broadly and implemented narrowly — CRITICAL

§3 "Partner scoping" states: *"A partner key sees a narrowed world, enforced per request"*, then
lists sessions, skills, approvals, and learning. A reader takes that as the security model.

It is enforced for exactly those four. Every other org-level resource is writable by **any**
valid partner key in the org, with no ownership check at all:

| Route | Auth in code | Consequence for a co-tenant partner |
| --- | --- | --- |
| `POST /v1/mcp/tokens` | `_require` only, `brain/api/server.py:1519` | **Connector hijack.** Upsert keyed `(org_id, end_user_id, server_name)`. Partner A can point partner B's customer's `server_url` at an attacker-controlled MCP endpoint with an attacker token. B's agent builds a vault against it on next refresh. |
| `GET /v1/mcp/tokens/{end_user_id}` | `_require` only, `:1565` | Enumerate which third-party services another partner's customers have linked. |
| `DELETE /v1/mcp/tokens/...` | `_require` only, `:1602` | Silently break another partner's integration. |
| `PUT /v1/mandates/{id}` | `_require` only, `:1028` | Rewrite the charter text another partner's agents run under — role text reaches the prompt. |
| `DELETE /v1/mandates/{id}` | `_require` only, `:1046` | Deactivate a role; B's `agent_id` starts 404-ing on new sessions. |
| `PUT/DELETE /v1/personas/{p}` | `_require` only, `:1228`, `:1239` | Overwrite another partner's persona `disposition` (its self-model, injected into the prompt) and baseline chemistry, or delete the persona outright. |
| `PUT/DELETE /v1/agents/{id}` | `_require` only, `:1131`, `:1157` | Delete an agent a live integration depends on, or silently degrade it via `tier: lite` / `answer_only: true`. |

The root cause is that **there is no partner dimension on end users or on org resources at all**.
`ApiSession.partner_id` scopes sessions; nothing scopes anything else. `end_user_mcp_tokens` has
no partner column.

The code even states the rationale (`brain/api/server.py:986-988`): *the same caller that can open
a session naming any mandate_id already controls which role text applies*. That is sound for
single-tenant. It silently becomes a cross-tenant hole the moment you mint a second partner key —
which is the product §23 documents.

Note the contrast that proves this is an oversight, not a decision: **skills do it right**
(`_skill_owned`, `:1263-1268`, and a real TOCTOU guard that keeps serving the approved body while
a new submission is screened). Personas, mandates, agents and MCP tokens simply forgot.

**Fix:** add `partner_id` to end users and org resources; scope reads/writes to the owning
partner with owner override. Until then, treat "one org = one partner" as a hard operational
rule and say so explicitly in §3.

### B2. Privilege escalation on a transient database error — HIGH

`_require` (`brain/api/server.py:222-227`) runs auth twice, as two independent Supabase queries:

```
if not auth(authorization):            # check_bearer → resolve_partner → DB lookup #1
    raise 401
return resolver(authorization) or {"partner_id": None, "owner": True}   # DB lookup #2
```

`_lookup_partner_key` swallows every exception and returns `None` (`brain/api/auth.py:88-89`). So
if lookup #1 succeeds and lookup #2 hits a timeout, connection reset, or any Supabase blip, the
partner is promoted to **full org owner** — `owner: True` unlocks key minting, GDPR purge, DMN
control, and the autonomous approval lane.

The docstring frames the fallback as a test affordance. It is reachable in production, and it
fails open in exactly the conditions where infrastructure is already unhealthy. The same race
also grants owner on a key revoked between the two lookups.

**Fix:** resolve once, pass the context through. Never synthesise an owner context from a failed
lookup — a `None` resolve after a passing auth is a 503, not a promotion.

### B3. No rate limiting anywhere, and auth failures are expensive — HIGH

There is no throttling, lockout, per-IP limit, or failed-attempt counter on any `/v1` path
(gateway or engine). Consequences:

- Bearer tokens can be brute-forced at unlimited rate. The token itself is strong
  (`secrets.token_urlsafe(32)`, 256 bits — `brain/api/auth.py:168`), so guessing is infeasible;
  the real cost is the amplification.
- Every attempt — **including every invalid one** — costs an uncached Supabase round trip
  (`resolve_partner_org`, `brain/api/auth.py:92-120`). Worse, that query filters on `key_hash`
  alone while the only index is `(org_id, key_hash)` (`supabase/migrations/011_api_keys.sql:23`),
  so the leading-column mismatch likely forces a **sequential scan per request**. An
  unauthenticated flood is a database DoS.
- With a valid key, every `/v1` call spawns a brain and kicks the GPU pod, unbounded.

**Fix:** per-IP limiting on unauthenticated 401s, per-key limiting on authenticated traffic, and
a covering index on `key_hash` alone. Then document the limits — see C3.

### B4. `POST /v1/sleep` is an org-wide DoS available to any partner key — HIGH

`brain/gateway/server.py:552-560` authenticates with `resolve_partner_org` only — no owner check.
`_do_sleep(org)` (`:470-515`) then sweeps **every** brain instance in the org and pauses the
shared GPU pod. So any partner key can kill all sibling partners' in-flight sessions and force
them into a cold start. It is fire-and-forget, returns `200` immediately, and has no cooldown.

Combined with implicit wake (§25: *"There is no wake endpoint"*), this is a free
sleep→call→sleep→call GPU thrash loop against the org's cost centre.

**Fix:** make `/v1/sleep` owner-gated. The guide already documents it as a cost control, which is
an owner concern, not a partner one. `GET /v1/status` should also stop returning shared GPU pod
state, which is not org-scoped data.

### B5. `X-Brain-Persona` is a raw filesystem path segment — HIGH (latent)

`_persona_header` returns the header verbatim — no slugify, no charset check, no length limit, no
validation against the org's roster (`brain/gateway/server.py:74-81`). It flows to:

```
root = TENANTS_DIR / user_id / "personas" / persona     # brain/provisioner.py:555
root.mkdir(parents=True, exist_ok=True)                 # :558
```

`Path` joins `..` literally, so traversal segments escape the tenant directory, and the value is
also injected into the child process env as `BRAIN_PERSONA_NAME`. The canonical slugifier
(`brain/persona_key.py:20-24`) exists and is used on other paths — it is simply not applied here.

This is **latent**: `BRAIN_MULTI_PERSONA` is off by default (`docs/ENV_VARS.md:73`). But §26
documents the header as an available feature, so the guide is advertising the thing that arms it.
Fix before anyone flips that flag.

Related: the per-org spawn cap raises `CapacityError` inside a fire-and-forget task
(`gateway/server.py:674`), which is swallowed — the caller just receives `503 booting` forever
and cannot distinguish "at capacity" from "cold start". That deserves a distinct status.

### B6. No request size limit anywhere — MEDIUM

No body cap on the gateway or the engine. `_proxy_http` buffers the **entire** request body into
gateway RAM before forwarding (`brain/gateway/server.py:906`, `:929`). `/v1/extract` bounds only
its output (`max_tokens=1024`), never its input or schema. Base64 audio is decoded with no size
check, and the gateway's upstream WebSocket sets `max_size=None` (`:973`) — explicitly unbounded
frames.

Given the guide tells integrators to send audio as base64 inside JSON (§2), a documented maximum
is table stakes. Right now a single large POST is a gateway memory event.

### B7. GDPR erasure is incomplete — MEDIUM, and it is a compliance claim

§23 says erasure *"erases the end user's memory and state across every per-user table"* and is
irreversible. The purge (`brain/session_turn.py:254-261`) sweeps six tables and misses at least
three things:

- **`end_user_mcp_tokens` is never swept.** The end user's vault-encrypted third-party OAuth
  tokens survive "irreversible" erasure. This is the most serious of the three.
- Per-speaker `brain_schemas` rows are written with `end_user_id: ""` hardcoded
  (`brain/second_brain/store.py:684`), so the user model — personal facts, preferences, emotional
  profile — is not matched by the `.eq("end_user_id", ...)` filter and survives.
- The durable `FileChemStore` snapshot survives; `forget()` clears memory only and documents that
  the caller removes the snapshot (`brain/client_chem.py:268-273`). The caller does not.

Also note **partners have no erasure route at all** (owner-gated, `:1441`). Partners create end
users and store their OAuth tokens but cannot delete any of it, so a partner cannot discharge its
own GDPR obligations through this API. That is a product gap, not just a doc gap.

### B8. `end_user_id` is the least-validated input in the system — MEDIUM

Validation is `isinstance(str)` and non-empty (`brain/api/server.py:316-320`). No length limit, no
charset restriction, no normalization — while every sibling id (persona, mandate, skill) is
regex-validated to `^[a-z0-9][a-z0-9_-]{0,63}$`. Three consequences:

- It is interpolated straight into an **LLM prompt** (`brain/clusters/_executor_common.py:90`:
  `f"\n**User:** {end_user_id}"`). A hostile `end_user_id` is a prompt-injection vector that
  bypasses the skill screener entirely.
- `speaker_filename` sanitizes lossily to 32 chars (`brain/second_brain/store.py:928-934`), so
  `"a!"` and `"a?"` both resolve to `user_a.md` — **cross-customer user-model bleed** between two
  of a partner's own customers.
- It is sent verbatim to the Anthropic Vaults API as a display name
  (`brain/clusters/cma_executor.py:825`).

Filesystem paths are safe by construction (hashed), so this is not traversal — but validate and
length-cap it, and document the accepted charset. The session `skills` pin list is likewise
uncapped; a 10k-element list is accepted and persisted.

### B9. Smaller items

- **Owner key comparison is not constant-time** — `token in owner_keys` (`brain/api/auth.py:62`)
  is a plain string compare. The partner path is a hash lookup and is fine. Low practical risk
  given entropy; cheap to fix with `hmac.compare_digest`.
- **`_extract_token` accepts any header value as the token** (`brain/api/auth.py:45`) — `Basic
  sk_x`, or a bare token with no scheme, both authenticate. The guide documents the bare form
  (§3). It widens what counts as a credential-bearing request and encourages clients to put
  secrets in oddly-shaped headers. Require `Bearer` and deprecate the rest.
- **`source` on grade permits Unicode bidi/RTL overrides.** `isprintable()` strips control chars
  but allows U+202E and homoglyphs (`brain/api/server.py:507-508`). A log viewer can be visually
  spoofed into rendering a partner-supplied source as `owner_review`. Bounded to 64 chars and
  lands in structured sinks, so no injection — but the docs' word "printable" is doing load-bearing
  work it does not unpack.
- **Public OpenAPI publishes the full internal attack surface.** The document is built from the
  entire route table with no filtering (`gateway/server.py:613-635`), so it enumerates
  `partner_keys`, `admin/skills/*`, `end_users` purge, `dmn`, and `mcp/tokens`, with docstrings
  that describe internal architecture. No tenant data leaks and the reasoning for public docs is
  sound — but consider filtering owner-gated routes out of the public document and serving the
  full one behind auth.

---

## C. Architectural decisions the documentation exposes

These are not bugs. They are choices that the act of writing them down makes look wrong.

### C1. `501` everywhere, with no way to feature-detect

The `501` row in §6 says the capability *"is not wired on this server"* and lists eight subsystems
that may simply be absent — grading, consolidation, approvals, extraction, job history, learning,
TTS, STT, event streaming. It then advises: **"Feature-detect at startup."**

There is no capabilities endpoint. The only way to feature-detect is to call each endpoint and see
whether it 501s — several of which have side effects or cost money. The guide gives an instruction
the API cannot satisfy.

More fundamentally: an integrator cannot tell from the docs what they are actually buying. Whether
grading works — the feature §12 calls *"the highest-leverage integration you can do"* — is a
deployment fact invisible until runtime. **Add `GET /v1/capabilities`** returning the wired
subsystems and the effective quota/budget limits. It is a small endpoint that removes the largest
source of integration uncertainty in the document.

### C2. Sessions are unbounded and immortal by design

§8: *"Sessions have no explicit close and no TTL... Open one per customer conversation and keep
it."* Confirmed — `ApiSessionRegistry` has no eviction, no TTL and no cap
(`brain/api/sessions.py:55-103`), and `get()` *adds* to the in-process dict on every read-through
miss and never removes. The `api_sessions` table has no expiry column and nothing sweeps it.

So the documented happy path is unbounded growth in both a process-resident dict and a durable
table, with no per-partner or per-end-user cap. A partner can mint unlimited sessions with
arbitrary `end_user_id`s. Add a cap and an LRU on the in-memory cache; the durability guarantee
survives eviction because it already read-throughs from Supabase.

### C3. Quotas are per-org, so partners are not isolated from each other's spend

The cloud budget counter is a single per-org file with no partner dimension
(`brain/model_router.py:209-220`). One partner exhausting the daily USD ceiling returns `402` to
**every** partner in the org. §7 documents the `402` but never says the budget is shared.

On a `full`-tier brain it is worse: over-budget does not `402` at all — it silently reroutes every
request to the local GPU (`model_router.py:465-471`), degrading output quality for all partners
with no signal. That behaviour is undocumented and would be baffling to debug from outside.

Audio quotas are per `partner_id` (better), but the persisted file is written by every dedicated
persona instance with a full-dict overwrite (`brain/api/audio_quota.py:110-146`) against an
org-canonical path, so concurrent instances lose each other's updates and quota is under-counted
under multi-persona. Move both counters to atomic per-partner rows in Postgres.

### C4. Jobs are pollable but not pushable

§14 documents durable job records and §10 delivers `proactive` frames — but only over WebSocket.
An integrator whose product is "the agent comes back to you with an answer" (the guide's own
phrasing) must either hold a WebSocket open indefinitely or poll `GET /v1/jobs`. There are no
webhooks. For a B2B engine whose differentiator is autonomous background work, outbound webhooks
with signed payloads are the obvious missing transport, and their absence should at least be
stated.

### C5. No versioning or deprecation policy

The path says `/v1` and the document says nothing about what `v1` guarantees, how breaking changes
are communicated, how long a version is supported, or where a changelog lives. There is a live
example of drift already: §2 notes `elyceum.app/v1` *"remains a working alias for existing
integrations"* — an implicit deprecation with no stated timeline. Add a short §Versioning with the
stability contract and a changelog link.

### C6. No CORS, but a browser example

§9 demonstrates SSE with a JavaScript `fetch`. There is no CORS middleware anywhere in the
gateway or engine, so that snippet cannot run in a browser — and if it could, it would place a
long-lived secret key in client-side code. Either state plainly that the API is server-side only
and rewrite the example in a server runtime, or add CORS plus a short-lived browser token. The
current pairing invites the least safe reading.

### C7. Missing operational contract

Absent from the guide and, as far as I can tell, from the system: request ids for support
escalation (no `X-Request-Id`), an idempotency-key mechanism (retrying a timed-out
`POST /v1/sessions` silently creates a duplicate session — and §4 explicitly instructs clients to
retry aggressively), any published rate limits, any documented maximum body size, and a stated
uptime/status page. Each is a question a serious integrator asks in week one.

### C8. A live bug: long WebSockets on a dedicated persona get reaped mid-stream

Not a design question — an outright defect worth fixing alongside the above. The WebSocket proxy
correctly touches the persona instance once at setup (`brain/gateway/server.py:694`), but its
per-activity keep-alive touches the org's **default** instance instead
(`:700`, `on_activity=lambda: provisioner.touch(org)`). So a long-lived WebSocket against a
dedicated persona brain never refreshes that brain's `last_active`, and the idle reaper can kill
it while the stream is in flight. It surfaces only under multi-persona, alongside B5.

---

## D. Documentation-only fixes

Ordered by how much confusion they remove.

1. **Add §0 "Getting your key"** with the real out-of-band workflow (A1). Nothing else matters
   until an integrator can authenticate.
2. **Correct §3's partner-scoping claim** to enumerate what is *not* scoped (B1), or fix the code
   and keep the claim. Right now the guide overstates the isolation guarantee.
3. **Say the cloud budget is shared org-wide** in §7, and document the full-tier silent-reroute
   behaviour (C3).
4. **Document limits**: max body size, max `input`/`schema` on `/v1/extract`, `end_user_id` charset
   and length, max pinned skills, rate limits once they exist.
5. **Add a §Versioning** with the `v1` stability contract, the `elyceum.app` alias timeline, and a
   changelog link (C5).
6. **Fix the browser example** or state server-side-only (C6).
7. **Soften §20's framing of screening.** The screener's own docstring calls it defence-in-depth,
   not a boundary (`brain/skills_screener.py:25-28`); the guide presents it as *the* gate. Also
   worth knowing: `description` is stored up to 1000 chars but the LLM judge only sees the first
   500 (`skills_screener.py:110` vs `skills_registry.py:45`) — narrow, but close it.
8. **Note the erasure gaps** in §23 until they are fixed — a documented compliance claim that the
   code does not meet is the worst kind of doc bug.

---

## E. Suggested order of work

**Before onboarding a second partner into any org** (B1 is inert with one partner, and lethal
with two):

1. B2 — the fail-open owner promotion. One-line-ish fix, unbounded blast radius.
2. B1 — partner scoping on MCP tokens, mandates, agents, personas.
3. B4 — owner-gate `/v1/sleep`.
4. B3 — rate limiting plus the `key_hash` index.

**Before enabling `BRAIN_MULTI_PERSONA`:**

5. B5 — slugify the persona header.

**Before the next compliance conversation:**

6. B7 — complete the purge, especially `end_user_mcp_tokens`.

**Then:** B6 body caps, B8 `end_user_id` validation, C1 capabilities endpoint, C2 session caps,
C3 per-partner budgets, and the §D documentation pass.

---

## Appendix: claims verified as accurate

Worth recording, because the guide is right far more often than it is wrong.

- Grading clamp and re-grade behaviour match §12 exactly (`brain/session_loops.py:1052-1056`), and
  cross-session isolation is *stronger* than documented — the binding comes from the trace's own
  stamps, not caller input.
- Skill re-screening on update, and the TOCTOU guard that keeps serving the approved body while a
  new submission is pending (`brain/skills_registry.py:168-202`). This is the model the other
  resources should follow.
- Screener fail-safe to `flagged` with `screener_not_configured` when no screener is wired.
- Auth is fail-closed at the process level: the engine API server does not start without a key
  (`brain/session_setup.py:383-393`).
- Key revocation is immediate — there is no token→org cache to invalidate.
- Partner tokens are never logged; the brain's uvicorn runs with `access_log=False`.
- Bearer auth runs **before** any spawn or pod warm, so an invalid key cannot trigger a cold start.
- The gateway forwards `Authorization` to the brain deliberately, and the brain re-resolves it
  org-scoped — a token valid in org A cannot be replayed against org B.
- WebSocket auth is checked on the upgrade before `accept()`, closing 1008 as documented.
- The markdown converter's URL scheme allowlist and quote-escaping are real and tested against
  parsed HTML.
- "Endpoint cards cannot drift" holds: tests enforce that every route has documentation, no route
  is documented twice, and no phantom endpoints exist.
