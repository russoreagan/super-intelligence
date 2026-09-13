# Elyceum Engine API

Version `v1`. The full developer reference for the engine API: authentication, the cold-start
contract, every endpoint's request and response shape, the SSE and WebSocket event vocabularies,
error codes, and quotas.

**How this stays accurate.** Every endpoint card on these pages — method, path, description — is
generated from the live route table at render time (`brain/api/reference.py` introspects the real
router), so it cannot drift from what the server actually serves. The surrounding prose is written
by hand and lives in `brain/api/api_guide.md`. A test fails the build if a route ships without a
section here documenting it.

---

## 0. Getting your key

Every request needs a bearer key, and keys are minted **in the app, not over the
API** — the minting route is owner-gated, so there is no bootstrap path from an
unauthenticated request.

Ask the org admin to:

1. Sign in to the Elyceum web app.
2. Open the **API workspace → Partner keys**.
3. Click **Mint partner key**, enter a partner id (your integration's name, e.g.
   `acme`) and a label. Optionally restrict the key to specific agents — a
   restricted key can only open sessions on those agents and sees only them
   ([§25](#25-keys-and-end-user-lifecycle)).
4. Copy the token from the one-time reveal.

**The token is shown once and never again.** Only its SHA-256 hash is stored. If it
is lost, revoke it and mint a new one.

Then:

```bash
export ELYCEUM_KEY="sk_..."
```

Two grades of key exist. A **partner key** is the normal one: it can run sessions and
everything else this guide describes, scoped to its own work. An **owner key** can
additionally reach the owner-gated routes in [§3](#3-authentication). Owner keys
minted here work through the hosted API host; the `BRAIN_API_KEYS` environment
variable is a separate, per-tenant mechanism that only applies to a direct or
self-hosted connection.

Confirm it works:

```bash
curl -sS https://api.elyceum.app/v1/capabilities \
  -H "Authorization: Bearer $ELYCEUM_KEY"
```

A `503 {"status": "booting"}` here is expected on a cold org — see
[§4](#4-the-cold-start-contract). To verify the key itself without waking anything,
call [`GET /v1/whoami`](#27-lifecycle-sleep-and-status) instead: it answers during a
cold start with your org id, role and any agent restriction on the key.

---

## 1. Concepts

Read this section before writing any client code. The object model is small but not the one a
chat-completions API would lead you to expect.

| Concept | What it is |
| --- | --- |
| **Org** | The tenant unit. One org owns one brain process (or several under multi-persona routing), its personas, roles, agents, skills, and keys. Every API key belongs to exactly one org. |
| **Persona** | A durable identity: display name, disposition text written in first person, and a resting chemistry baseline. Built-ins ship with the engine; custom personas are authored at runtime. The persona is the half of an agent that *is someone*. |
| **Mandate (role)** | A reusable role spec: charter text, conduct rules, reward shaping. Org-level, assignable to any persona. Roles live outside personas — the same "research lead" role can be worn by several personas. |
| **Agent** | A persona × role pairing, addressed as `"{persona_slug}.{mandate_id}"` (for example `the_visionary.research_lead`). This is the single handle you pass when opening a session. Agents carry a display name, a permission ceiling, and a model tier. |
| **End user** | *Your* customer. A partner-chosen free-text id (`end_user_id`). The brain keys that customer's memory, relationship, and chemistry on it. It is not globally unique — the same string in another org is a different person. |
| **Session** | A conversation handle bound to exactly one `end_user_id`. Open one per customer conversation. Durable: sessions persist to Supabase and survive a brain restart. |
| **Turn** | One request/response exchange inside a session. Returns display text plus a structured affect block and a mood. Each turn has a `turn_id` you can grade later. |
| **Affect / mood** | The differentiator. `mood` is the persona's emotional output for the turn; `affect` is the per-segment prosody plan that drives TTS. The underlying neurochemical layer is deliberately **not** exposed over the API. |
| **Answer-only** | A contract flag. `answer_only=true` makes a session (or a single turn) pure synchronous Q&A: the brain drafts an answer and does nothing else — no tool or motor work, no background follow-up jobs, no confirmations. It can also be set org-wide by the owner (`PUT /v1/org/permissions`), in which case nothing a session, turn or agent says can lift it. |
| **Org ceilings** | The org-wide permission maxima (motor capability, filesystem roots, spend caps, the idle-loop switch, answer-only). Every agent's `permissions` narrows under them and can never widen them. Read by any key, written by the owner ([§21](#21-agents)). |
| **Agent allowlist** | An optional per-key restriction set at mint: the key may only open sessions on the listed agents and sees only those agents and their personas. Absent = the whole org roster ([§25](#25-keys-and-end-user-lifecycle)). |
| **Erasure** | `DELETE /v1/end_users/{id}` removes every store keyed by a customer, then keeps only a tombstone on the ownership row so the owning partner's later requests get `410` with the erasure time, while a foreign partner's still get `404` ([§25](#25-keys-and-end-user-lifecycle), [§30](#30-data-handling-and-retention)). |
| **Learning mode** | An org-level account setting. `consolidated` (default): a persona is one learning identity shared across every customer it talks to, with cross-customer transfer through the de-identification gate. `isolated`: every persona is a separate individual and nothing learned by one reaches another — one clone per purchase, first end user owns it, hard purge erases it. Read on `whoami`; switched by the owner with `confirm: true` ([§20](#20-personas)). |

### Persona slugs

Personas are addressed by slug (lowercase, underscore-separated). `GET /v1/personas` returns the
roster with `slug`, `display_name`, and `builtin`. An `agent_id` splits on the first `.` — everything
before it is the persona slug, everything after is the mandate id.

---

## 2. Base URL and transport

**Hosted (recommended):**

```
https://api.elyceum.app/v1
```

The hosted gateway resolves your bearer key to an org, spawns that org's brain on demand, warms the
GPU pod, and proxies your request. SSE and WebSocket both pass through.

`api.elyceum.app` serves the `/v1` engine API and `/health`, and nothing else — any other path
returns a JSON `404` rather than redirecting to a login page.

`https://elyceum.app/v1` remains a working alias for existing integrations. New clients should use
the API host.

**Direct / self-hosted:**

```
http://127.0.0.1:8780/v1
```

The engine API runs its own uvicorn server on `BRAIN_API_PORT` (default `8780`), separate from the
owner UI so it never inherits cookie auth. It only starts when at least one API key is configured
for the org.

### Conventions

- All request and response bodies are JSON (`Content-Type: application/json`).
- All paths are prefixed `/v1`.
- Path segments containing user-chosen ids (`end_user_id`, `skill_id`, `server_name`) must be
  URL-encoded.
- Binary audio crosses the boundary as base64 strings inside JSON, never as multipart.
- There is no pagination cursor. List endpoints that page use `?limit=`.
- Every response carries **`X-Request-Id`**. Send your own (up to 64 printable ASCII
  characters) and it is echoed back; otherwise one is generated. Log it, and quote it
  in any support request — it is the only shared handle between what you saw and what
  we recorded.

### OpenAPI

A machine-readable schema and a live Swagger UI are served on the hosted gateway:

```
https://api.elyceum.app/v1/openapi.json     # OpenAPI 3.1 document
https://api.elyceum.app/v1/docs             # Swagger UI
```

Both are public — no bearer key needed to read them (Swagger UI cannot attach one to its own schema
fetch, and the document contains no tenant data). They're generated from the live route table, so
they can't drift from what the server actually serves, and they're answered by the gateway without
spawning a brain, so hitting them never triggers a cold start.

Use the schema to generate a client. **One gap by construction:** OpenAPI has no WebSocket concept,
so `WS /v1/sessions/{session_id}/stream` does not appear in it. [§11](#11-streaming-websocket) is its
reference.

The engine also serves Swagger at `/v1/docs` on its own port for direct/self-hosted connections.

---

## 3. Authentication

Every request carries a bearer token:

```
Authorization: Bearer sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**`Bearer` is required.** A bare token with no scheme is rejected, as is any other
scheme. Matching is case-insensitive and surrounding whitespace is ignored.

Auth is **fail-closed**: if no keys are configured for the org, every request is denied. An
accidentally exposed server is not open by default. If the key store itself is
unreachable, requests fail with `503`, never with a guess about who you are.

### Kinds of key

| Kind | Where it lives | `owner` | Scope |
| --- | --- | --- | --- |
| **Partner key** | Row in the `api_keys` table, minted in the app ([§0](#0-getting-your-key)) | `false` | Its own sessions, skills, end users and their connectors. Metered. |
| **Owner key** (`role: owner`) | Same table, minted the same way | `true` | Everything, including the owner-gated routes below. Never metered. |
| **Env owner key** | `BRAIN_API_KEYS` / `BRAIN_API_KEY` env, or the `api_keys` setting | `true` | Everything, but **direct/self-hosted only** — see below. |

Keys are stored as a SHA-256 hash. The plaintext token is returned **once**, at creation, and
is never recoverable. Tokens are prefixed `sk_`.

> **Hosted clients.** The gateway maps a bearer token to an org by looking it up across
> all orgs in the `api_keys` table. The **env** owner key lives in per-tenant environment
> variables and is not resolvable there, so it does not work through
> `https://api.elyceum.app/v1`. If you need owner access on the hosted API, mint a key
> with `role: owner` — it lives in the table and resolves normally.

### Owner-gated routes

These require an owner credential and return `403 owner credential required` for a
partner key:

- `GET|PUT /v1/dmn`
- `GET /v1/admin/skills/flagged`, `POST /v1/admin/skills/{skill_id}/approve`, `POST /v1/admin/skills/{skill_id}/reject`
- `GET|POST /v1/partner_keys`, `DELETE /v1/partner_keys/{key_id}`
- **Org configuration writes**: `PUT|DELETE` on `/v1/mandates/{id}`,
  `/v1/personas/{persona}`, `/v1/personas/{persona}/mandates/{id}`, and
  `/v1/agents/{id}`
- `POST /v1/sleep` (gateway)

### Partner scoping

This is the isolation model. Read it before assuming anything about what a co-tenant
partner in the same org can and cannot see.

**Scoped to you** — another partner's is invisible or refused:

| Resource | Rule |
| --- | --- |
| **Sessions** | A session records the `partner_id` that opened it. Another partner's session returns `403 session belongs to another partner`. Legacy sessions with no `partner_id` are owner-scoped. |
| **End users** | The first partner to use an `end_user_id` owns it. Opening a session as another partner's customer returns `403`. |
| **MCP tokens** | Reading, writing or deleting connectors for another partner's customer returns `404` (not `403` — the API does not confirm whether the id exists). |
| **Erasure** | You may erase your own customers; another partner's returns `404`. |
| **Agent allowlist** | A partner key minted with `allowed_agents` (see [§25](#25-keys-and-end-user-lifecycle)) can only open sessions on those agents (`agent_id` becomes **required**; any other id returns `404`, exactly like an unknown one), and `GET /v1/agents`, `GET /v1/agents/{id}`, `GET /v1/personas` and `GET /v1/personas/{p}` are filtered to those agents and their personas. A key minted without it sees the whole org roster, as before. Check your own key with [`GET /v1/whoami`](#27-lifecycle-sleep-and-status). |
| **Allowlist pins** | An `allowed_agents` entry may be a **pin** instead of an agent id, so one key covers a whole family of clones without re-minting per purchase: `template:<slug>` allows every persona cloned from `<slug>` on any mandate, `template:<slug>.<mandate_id>` restricts that to one mandate, and `prefix:<p>` allows every persona whose slug starts with `<p>`. The template itself is not covered by its pin (list it by id if the key needs it). Each pin counts as one entry against the 200-entry allowlist cap. For a key whose entries are all template pins, `GET /v1/personas` lists that template's clones by default (clones are otherwise hidden — see [§20](#20-personas)). Example: `{"partner_id": "shop", "allowed_agents": ["template:concierge.sales", "prefix:shop_"]}` opens sessions on `concierge_order42.sales` and `shop_vip.support`, and refuses `concierge_order42.support` with `404`. |
| **Persona ownership** (isolated orgs) | In an org whose `learning_mode` is `isolated`, the first `end_user_id` to open a session on a persona owns it. Another end user's session on that persona returns `404`, exactly like an unknown agent — nothing about the persona is revealed. The home persona and owner keys are exempt; consolidated orgs never enforce ([§20](#20-personas)). |
| **Skills** | `GET /v1/skills` filters to your own submissions. Fetching, updating or deleting another partner's skill returns `403`. |
| **Approvals** | An owner key additionally sees and can resolve the *autonomous* lane — actions the brain queued while unattended. Partner keys never do. |
| **Learning** | `?persona=` is honored only for owner keys. A partner key always reads the org's home persona. |
| **Audio quota** | Metered per `partner_id`. Owner keys are never metered. |

**Org-wide** — shared with every partner in the org, by design:

| Resource | What that means |
| --- | --- |
| **Mandates, personas, agents** | Readable by any partner in the org (you need the roster to resolve an `agent_id`). **Writable only by the owner**, so no partner can change the role text, persona self-model or agent wiring that another partner's live sessions run on. |
| **Cloud budget** | The daily USD ceiling is per **org**, not per partner. One partner exhausting it affects everyone in the org — see [§7](#7-quotas-budgets-and-metering). |
| **The brain process and GPU pod** | Shared per org. `POST /v1/sleep` is owner-gated precisely because it stops both for every partner at once. |

---

## 4. The cold-start contract

**Every hosted client must implement this.** It is the single most common integration bug.

Brain processes are spawned on demand and reaped when idle. When you call `/v1/...` and your org's
brain is not running, the gateway starts it, warms the GPU pod, and immediately returns:

```
HTTP/1.1 503 Service Unavailable
Content-Type: application/json

{"status": "booting"}
```

This is **not** an error. It means "come back shortly". The spawn is idempotent — concurrent calls
await a single spawn. The booting response carries `Retry-After: 2`.

**Handling:** retry the request with backoff until you get a non-503. Boot typically completes in
seconds; allow up to roughly a minute on a cold pod. Distinguish this 503 from other 503s by the body:
a booting response has `{"status": "booting"}`, while a capability 503 has `{"detail": "..."}`.

**At capacity.** If the host cannot start another brain (the platform-wide brain cap, or your org's
dedicated-persona cap), the **first** request already answers
`503 {"status": "at_capacity", "detail": "..."}` with `Retry-After: 30` — never `booting`. Back off
much harder than for booting and alert someone: capacity frees as other tenants idle out, not because
you retried. The caps are on **running processes**, not on how many personas or agents exist; see
[§28](#28-multi-persona-routing).

Two things never spawn a brain and always answer during a cold start: [`GET /v1/status`](#27-lifecycle-sleep-and-status)
and [`GET /v1/whoami`](#27-lifecycle-sleep-and-status).

The WebSocket equivalent is a close with code **1013 (Try Again Later)**, sent for the same reason.
Reconnect with backoff.

```python
import time, requests

def call(method, path, **kw):
    for attempt in range(12):
        r = requests.request(method, f"{BASE}{path}",
                             headers={"Authorization": f"Bearer {KEY}"}, **kw)
        if r.status_code == 503 and r.json().get("status") == "booting":
            time.sleep(min(2 ** attempt, 10))
            continue
        return r
    raise TimeoutError("brain did not boot")
```

Waking is implicit: any `/v1` call respawns the brain. There is no explicit wake endpoint. See
[§27](#27-lifecycle-sleep-and-status) for putting it back to sleep deliberately.

---

## 5. Quickstart

Open a session, run a turn, grade it.

```bash
# 1. Open a session for your customer on an agent.
curl -sS https://api.elyceum.app/v1/sessions \
  -H "Authorization: Bearer $ELYCEUM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"end_user_id": "u_8821", "agent_id": "the_visionary.research_lead"}'
```

```json
{
  "session_id": "9f2c1ab7d4e05b63",
  "end_user_id": "u_8821",
  "agent_id": "the_visionary.research_lead",
  "mandate_id": "research_lead",
  "skills": [],
  "answer_only": false
}
```

```bash
# 2. Run a turn.
curl -sS https://api.elyceum.app/v1/sessions/9f2c1ab7d4e05b63/turns \
  -H "Authorization: Bearer $ELYCEUM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "What changed in the market today?"}'
```

```json
{
  "session_id": "9f2c1ab7d4e05b63",
  "end_user_id": "u_8821",
  "response": "Two things moved, and only one of them matters.",
  "affect": {
    "base_tag": "[confident]",
    "segments": [
      {"seq": 0, "text": "Two things moved,", "mood": "confident", "tag": "[confident]"},
      {"seq": 1, "text": "and only one of them matters.", "mood": null, "tag": "[confident]"}
    ]
  },
  "mood": {"emotion": "confident", "user_emotion": "curious"},
  "turn_id": "t_01J8XYZ..."
}
```

```bash
# 3. Send the thumbs verdict back. This is the one reward signal grounded outside
#    the agent's own appraisal, and it is what makes the brain learn from real use.
curl -sS https://api.elyceum.app/v1/sessions/9f2c1ab7d4e05b63/turns/t_01J8XYZ.../grade \
  -H "Authorization: Bearer $ELYCEUM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"grade": 1, "source": "thumbs"}'
```

```json
{"ok": true, "grade": 1.0, "applied_live": true}
```

---

## 6. Errors

Errors use standard HTTP status codes with a FastAPI-shaped body:

```json
{"detail": "message (non-empty string) is required"}
```

The one exception is the gateway's boot response, which is `{"status": "booting"}` (see
[§4](#4-the-cold-start-contract)).

| Status | Meaning | What to do |
| --- | --- | --- |
| `400` | Malformed body — missing required field, wrong type, invalid base64, both `message` and `audio_input` sent, unknown audio format. | Fix the request. Never retry unchanged. |
| `401` | Missing, malformed, unknown, or revoked bearer key. | Check the key. A revoked key fails immediately. |
| `402` | Cloud spend is over the org's daily USD ceiling. | Stop and raise the budget, or wait for the window to roll. Surfaced explicitly instead of an opaque 500 so you can tell it apart from a fault. |
| `403` | Authenticated but not permitted: an owner-gated route with a partner key, or another partner's session or skill. | Do not retry. |
| `404` | Unknown `session_id`, `job_id`, `turn_id` (for this session), agent, persona, mandate, skill, or key — **or an `end_user_id` owned by another partner**. | Note that grading a turn from a *different* session returns 404, deliberately indistinguishable from a turn that never existed. The same applies to another partner's end user: a `403` would confirm the id exists. |
| `413` | Request body, `/v1/extract` `input`, or `/v1/extract` `schema` over the limit. | See the limits table in [§8](#8-capabilities-and-limits). Never retry unchanged. |
| `429` | Audio quota exhausted, **or** the rate limit. | Both carry `Retry-After`. Rate-limited responses also carry `X-RateLimit-*`; back off until the window rolls. |
| `409` | State conflict: `POST /confirm` with nothing pending, or an `agent_id` whose persona lives in a different process. | Re-read state. |
| `410` | An `end_user_id` **you own** that has already been erased. Body is `{"detail": "end_user erased", "erased_at": "<ISO timestamp>"}`. Returned by `DELETE /v1/end_users/{id}` and the three MCP-token routes. | Record `erased_at` as proof of erasure. Opening a new session for the same id starts the customer afresh and clears the tombstone (see [§25](#25-keys-and-end-user-lifecycle)). Another partner's erased id is still `404`. |
| `422` | `audio_input` contained no detectable speech. | Prompt the user to speak again. |
| `500` | Storage fault (MCP token read/write). | Retry with backoff. |
| `501` | The capability is not wired on this server — grading, consolidation, approvals, extraction, job history, learning, TTS, STT, or event streaming. | A deployment fact, not a transient one. Do not retry. Feature-detect at startup. |
| `503` | The brain is booting (`{"status": "booting"}` — retry); the host is at capacity (`{"status": "at_capacity"}` — back off hard and tell someone); a dependency is missing, e.g. no provider key for audio or the Supabase backend absent (`{"detail": "..."}` — do not retry); or the key store is unreachable (`auth backend unavailable` — retry). | Branch on the body. |

**501 vs 503 matters.** `501` means the runner was never wired into this deployment. `503` with a
`detail` means it exists but a key or backend is missing. Neither is worth retrying; both are worth
surfacing to whoever operates the deployment. Call
[`GET /v1/capabilities`](#8-capabilities-and-limits) once at startup to learn which
subsystems this deployment has, instead of discovering it from a `501` in production.

### Rate limits

`/v1` is rate-limited per key, and failed authentication is limited per client IP.
Over the limit returns `429` with `Retry-After`. Successful responses carry
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (the epoch
second at which the current window rolls) so a client can pace itself against the
clock rather than discovering the ceiling by hitting it. The `429` carries
`X-RateLimit-Reset` too.

### Errors inside a stream

Once an SSE stream is open, the HTTP status is already `200`. Failures arrive as frames:

- `event: error` with `{"detail": "..."}` — the turn failed.
- `event: audio_error` with `{"turn_id", "detail"}` — synthesis failed, but the **text was already
  sent**. Degrade to text, do not treat the turn as failed.

On the WebSocket, errors arrive as `{"type": "error", "detail": "...", "code": <http-ish code>}`.

---

## 7. Quotas, budgets, and metering

### Audio quotas

STT and TTS hit paid third parties, so partner keys are metered the way those services bill:

| Meter | Unit | Cap setting |
| --- | --- | --- |
| `tts_chars` | Characters synthesised | `audio_tts_chars_per_window` |
| `stt_seconds` | Seconds of input audio | `audio_stt_seconds_per_window` |

The window length is `audio_quota_window_s` (default 86400s). A cap of `0` means unlimited. Owner
keys are never metered.

Enforcement makes no cost prediction. A call is refused only when you are *already* at or over the
cap; actual usage is recorded after the call succeeds. A single request can therefore overshoot
slightly before you are blocked.

Over the cap:

- `POST /v1/tts` and `POST /v1/stt` → `429` with detail
  `audio quota reached (N characters per Ns)`.
- SSE turn audio → an `audio_error` frame instead of audio. **The text still streams.**
- WebSocket audio-in → `{"type": "error", "code": 429}`.

### Cloud budget

Cloud model spend is capped daily (UTC). There are two ceilings and the **tighter one
binds**:

- A **per-partner** cap (`partner_cloud_daily_usd_budget`). Your key's spend is
  metered against your own partner budget, so another partner's usage never consumes
  yours.
- A **per-org** cap (`cloud_daily_usd_budget`) as a backstop across the whole org.

Both are org ceilings the owner sets via `PUT /v1/org/permissions` ([§21](#21-agents)) or the
console's Account limits page; `GET /v1/agents` reports the current values under `ceilings`.

Over your cap, **every partner key gets `402`** with the budget message as `detail`,
on any deployment tier. A partner is never silently downgraded — if you are paying for
cloud-tier answers and the budget is spent, the call stops rather than quietly serving
a weaker local model. This covers turns and `POST /v1/extract`.

(The owner lane behaves differently and is not something a partner integration
observes: an over-budget owner-lane call on a full brain reroutes to local rather than
failing. Partner traffic always errors.)

Enforcement makes no cost prediction — a call is refused only once you are already at
or over the cap, so a single request can overshoot slightly. `GET /v1/capabilities`
reports the effective ceiling in `limits.cloud`.

### Persona capacity

`GET /v1/personas` returns a `limits` block:

```json
{"max_dedicated_instances": 3, "max_live_brains": 25, "max_personas": 10000}
```

Beyond `max_dedicated_instances`, additional persona *processes* are refused. Plan concurrent
multi-persona scenes (a six-way debate, for example) inside that cap. `max_personas` caps the custom
persona *specs* an org may hold (built-in overrides excluded): `POST /v1/personas/{template}/clone`
returns `409` at the cap. A clone costs no process — clones bind per turn on the org's shared brain —
so the cap is about catalogue size, not compute. Sized for a marketplace (the cap check is a single
indexed count, not a scan of the catalogue); raise it on the deployment (`BRAIN_MAX_PERSONAS`) if you
need more.

`max_dedicated_instances` comes from your org record when set there ([§20 Placement](#placement)),
else the deployment default.

### GPU capacity

Memory, idle thinking, self-reflection and sleep run on a **pool of GPU pods** shared by every
org on the deployment: one queue, one to `max_pods` identical pods. Each brain process is assigned
to one pod (sticky; it moves only if its pod dies or is drained), the pool adds a pod when a pod
stays busy or its callers keep queueing for several minutes, and removes one after a long idle
spell. There is nothing to request: capacity follows load. What you can observe is the shape of
saturation — it shows up as **latency on background work, never as errors**: a pod at capacity
queues calls behind a per-process concurrency limit rather than failing them, and the pool never
falls back to the cloud for work that is designed to run locally. Owner keys see the pool on
[`GET /v1/status`](#27-lifecycle-sleep-and-status). Pool usage is billed as shared pod time
(`pod_s` in agent usage); a standalone pod per persona or per org is the planned premium tier.

### GPU: pool share vs dedicated hours

Two kinds of GPU time show up on the bill, and they are metered differently:

- **Pool share** (basic tier, every persona). Your personas' local model calls run on the platform
  GPU pool. What is metered is *inference seconds* per persona (`pod_s` in the usage ledger), reported
  as `pod_hours_shared` and priced as hours × `rate_per_hr`. Idle thinking on the shared brain is a
  time-slice of one loop, so it is near-free at any persona count.
- **Dedicated hours** (premium tier, placed personas with `pod: "standalone"` or `"org"`). A pod of
  the org's own bills for *uptime*, whether or not anyone is inferring, so it is metered as wall-clock
  per tick into its own ledger and reported as `pod_hours_dedicated` with the real `gpu_usd`. The
  org's `gpu_daily_usd_budget` caps it per UTC day; when the day's budget is spent the org's pods
  sleep and its dedicated instances fall back to the pool until midnight UTC. `0` = the org may hold
  no standalone pods at all.

### `GET /v1/usage`

**Owner credential required.**

```
GET /v1/usage?since=2026-09-06&until=2026-09-13
```

Both parameters optional (`YYYY-MM-DD` or ISO-8601, UTC). Default: the last 7 UTC days including
today. `until` is exclusive; the window is capped at 92 days; `400` for a malformed or empty window.

```json
{
  "since": "2026-09-06T00:00:00+00:00",
  "until": "2026-09-13T00:00:00+00:00",
  "rate_per_hr": 0.44,
  "days": [
    {
      "day": "2026-09-12",
      "personas": {
        "captain_ahab_purchase_8821": {
          "calls": 412, "cloud_calls": 30, "cloud_usd": 0.81,
          "pod_hours_shared": 0.0, "pod_usd_shared": 0.0,
          "pod_hours_dedicated": 23.9, "gpu_usd": 10.52
        },
        "the_visionary": {
          "calls": 1188, "cloud_calls": 91, "cloud_usd": 2.14,
          "pod_hours_shared": 1.37, "pod_usd_shared": 0.6,
          "pod_hours_dedicated": 0.0, "gpu_usd": 0.0
        }
      },
      "totals": {"calls": 1600, "cloud_calls": 121, "cloud_usd": 2.95, "pod_hours_shared": 1.37, "pod_usd_shared": 0.6, "pod_hours_dedicated": 23.9, "gpu_usd": 10.52}
    }
  ],
  "personas": {"captain_ahab_purchase_8821": {"…": "…"}, "the_visionary": {"…": "…"}},
  "totals": {"…": "…"},
  "budgets": {
    "cloud_daily_usd_budget": 20.0,
    "partner_cloud_daily_usd_budget": 5.0,
    "gpu_daily_usd_budget": 12.0,
    "gpu_usd_today": 10.52
  }
}
```

| Field | Meaning |
| --- | --- |
| `calls`, `cloud_calls` | Model calls the persona drove (all lanes, idle thinking included) and how many went to the cloud. |
| `cloud_usd` | Metered cloud spend. |
| `pod_hours_shared`, `pod_usd_shared` | The persona's inference time on the platform pool, and that time at `rate_per_hr`. |
| `pod_hours_dedicated`, `gpu_usd` | Wall-clock of standalone / org pods attributed to the persona (an `org` pod's tick is split evenly across the org's dedicated instances) and what it cost. |
| `rate_per_hr` | What the pool bills at right now (the live pod rate when known). |
| `budgets.gpu_usd_today` | Dedicated-pod spend so far today, against `gpu_daily_usd_budget`. |

A persona key of the org's home persona carries the shared brain's own usage; days with no rows are
omitted. Empty `days` before the usage ledgers exist on a deployment.

---

## 8. Capabilities and limits

### `GET /v1/capabilities`

Call this once at startup. Several subsystems are optional and return `501` when they
were never wired into a deployment, and this is how you find out which — without
probing endpoints that cost money or have side effects.

```json
{
  "api_version": "v1",
  "capabilities": {
    "turns": true,
    "streaming_sse": true,
    "streaming_ws": true,
    "grading": true,
    "consolidation": true,
    "confirmations": true,
    "approvals": true,
    "extraction": true,
    "job_history": true,
    "learning": true,
    "erasure": true,
    "tts": true,
    "stt": true,
    "stt_live": true,
    "skills_screening": true,
    "org_config": true,
    "mcp_tokens": true,
    "partner_keys": true
  },
  "limits": { }
}
```

Every flag is read from the same wiring the routes themselves check, so it cannot
claim a capability the server does not have. A `false` flag means the matching
endpoints return `501` — a deployment fact, not a transient one.

`tts` and `stt` additionally require a configured provider key, because those routes
answer `503` rather than `501` when the key is missing. The flag reflects "the call
will work", not "the code is present".

On a cold gateway this route follows the usual [`503 booting`](#4-the-cold-start-contract)
contract, like any other engine route.

### Limits

| Limit | Value | Applies to |
| --- | --- | --- |
| `max_body_bytes` | 10485760 (10 MB) | Any `/v1` request body. Over → `413`. Audio crosses as base64 inside JSON, so it counts against this. |
| `max_ws_frame_bytes` | 8388608 (8 MB) | A single WebSocket frame. |
| `extract_max_input_chars` | 100000 | `POST /v1/extract` `input`. Over → `413`. |
| `extract_max_schema_bytes` | 16384 | `POST /v1/extract` `schema`, serialised. Over → `413`. |
| `max_pinned_skills` | 32 | The `skills` array on `POST /v1/sessions`. |
| `grade_source_max_chars` | 64 | `source` on a grade. Clamped, not rejected; non-ASCII is stripped. |
| `end_user_id_pattern` | `^[A-Za-z0-9._@+-]{1,128}$` | Every `end_user_id`. Over or outside → `400`. |

The `limits` block in the response carries these same numbers plus two runtime blocks:
`audio` (your own quota caps, window, and current usage) and `cloud` (the daily USD
budget). The table above is generated from the same constants the server enforces, so
it cannot drift.

**`end_user_id` is validated.** It is deliberately wider than a slug — emails and
UUIDs are fine — but whitespace, quotes, colons, slashes and control characters are
refused rather than sanitised. Normalising them would silently merge two different
customers into one identity, which would merge their memory, chemistry and connector
tokens.

---

## 9. Sessions and turns

### `POST /v1/sessions`

Open a session for one end user.

**Body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `end_user_id` | string | **yes** | Non-empty. Your customer's id. Whitespace-trimmed. |
| `agent_id` | string | no* | `"{persona}.{mandate_id}"`. Resolves the role. Preferred over `mandate_id`. *Required for a key minted with an agent allowlist (`400` without it); an id outside the allowlist returns `404`. |
| `mandate_id` | string | no | Raw role id, for callers not using agents. Ignored when `agent_id` resolves. |
| `skills` | string[] | no | App-provided skill ids pinned into every turn of this session. Unknown or non-enabled ids are silently ignored at turn time — a pin cannot conjure an unscreened skill. |
| `answer_only` | boolean | no | Default `false`. Declares the whole session synchronous Q&A. A turn body can override per turn. The effective flag is **org setting OR session OR turn OR agent permission** — any one of them restricts, none can widen. What it gates: no tool/motor dispatch, no muscle-memory open-loop, no follow-up jobs, no `confirmation`, and no job-originated events on the streams. An agent whose permissions carry the flag also runs no background work at all: its idle-thought ideas are never queued and its assigned projects wait on the ledger until the flag is cleared. What it does **not** gate: memory, learning and consolidation still run for the turn. |

**Response `200`**

```json
{
  "session_id": "9f2c1ab7d4e05b63",
  "end_user_id": "u_8821",
  "agent_id": "the_visionary.research_lead",
  "mandate_id": "research_lead",
  "skills": ["house_policy_v2"],
  "answer_only": false
}
```

**Errors** — `400` bad field type, missing `end_user_id`, or a key with an agent allowlist that
sent no `agent_id`; `404` unknown `agent_id` (or one outside your key's allowlist); `409` the
agent's persona runs in a different process (the gateway routes there; see
[§28](#28-multi-persona-routing)).

Sessions have no explicit close and no TTL. They persist to the `api_sessions` table and are
read-through on a cache miss, so a `session_id` stays valid across brain restarts. Open one per
customer conversation and keep it.

### `POST /v1/sessions/{session_id}/turns`

Run one turn.

**Body** — send **either** `message` **or** `audio_input`, never both.

| Field | Type | Notes |
| --- | --- | --- |
| `message` | string | Non-empty. The user's text. |
| `audio_input` | object | Voice-in. `{data: <base64>, mimetype?: "audio/wav", model?: string}`. Transcribed through the same path as `POST /v1/stt`, then run as a normal turn. |
| `answer_only` | boolean | Overrides the session default for this turn only — upward. `false` cannot lift an org-wide or agent-level `answer_only`. |

**Response `200`**

| Field | Type | Notes |
| --- | --- | --- |
| `session_id` | string | |
| `end_user_id` | string | |
| `response` | string | Clean display text. All mood markup and reaction tags stripped. Safe for chat UI and captions. |
| `affect` | object | Prosody plan. See below. |
| `mood` | object | `{emotion: string, user_emotion?: string}`. The mood **output** only. |
| `turn_id` | string | Present when the turn produced one. The handle for `POST .../grade`. |
| `elapsed_s` | number | Wall time of the turn in seconds (the same number `turn_end` carries on the streams). |
| `llm_calls` | integer | Model calls the turn made, background calls excluded. The count is adaptive (draft competition, quiescence), not fixed per tier — tier changes *which* models answer, not how many calls it takes. |
| `transcript` | string | Present only for `audio_input` — what we heard. |
| `confirmation` | object | Present only when a cloud write is parked: `{required: true, description: string}`. Never present on an `answer_only` turn. |

**The affect block**

```json
{
  "base_tag": "[warm]",
  "segments": [
    {"seq": 0, "text": "I looked into it.", "mood": "warm", "tag": "[warm]"},
    {"seq": 1, "text": "You were right.", "mood": "proud", "tag": "[proudly]"}
  ],
  "markup": "[mood:warm] I looked into it. [mood:proud] You were right."
}
```

- `base_tag` — whole-utterance inflection, or `null`.
- `segments` — 1:1 with the TTS chunk plan. Drive your own TTS from these, or pass the turn's text to
  `POST /v1/tts` and let the affect→voice mapping do it (strictly better; it is the part you cannot
  replicate client-side).
- `markup` — present only when the raw text actually carries mood spans worth handing back.

The neurochemical layer behind the mood (neuromodulator and hormonal channels, enrollment, appraisal)
is never exposed. Only the mood output crosses the boundary.

**Errors** — `400` neither or both inputs, bad base64; `403` another partner's session; `404` unknown
session; `422` no speech detected; `501` STT not wired (for `audio_input`); `402` over cloud budget.

### Concurrency and cancellation

**Turns serialize behind one lock per brain process.** The lock is process-global: it spans every
session of your org's brain, not just the session you are calling on. A second turn — on the same
session or another — queues behind the first and runs when it finishes. There is no `409`, no queue
cap and no per-session parallelism today. Plan throughput accordingly: one turn at a time per org
process, with `elapsed_s` as your measure of how long each holds the lock.

**There is no cancel endpoint.** Disconnecting a streaming request (SSE) or the WebSocket cancels the
in-flight turn task; a `POST /turns` request cannot be cancelled once accepted. The semantics of a
cancelled turn are:

- the `agent_turns` row (the durable record used for grading) is written only after `done`, so a
  cancelled turn is never gradable;
- episodic memory written before the cancel point survives — the brain may remember the start of a
  turn whose reply you never received;
- a cloud write that was already dispatched is not rolled back.

**No incremental text.** Articulation completes before `done`; the streams carry the brain's inner
life (thoughts, mood shifts, region activations), not response tokens. `turn_end.response` is the
whole reply in one frame. Token-level streaming is not on the current roadmap.

---

## 10. Streaming: SSE

### `POST /v1/sessions/{session_id}/turns/stream`

Same body as `POST /turns`, plus:

| Field | Type | Notes |
| --- | --- | --- |
| `audio` | object | `{enabled: true, voice_id?, model?, format?, provider?}`. When enabled, audio frames follow the `done` frame. `voice_id` defaults to the session persona's configured voice. |

Responds `200` with `Content-Type: text/event-stream`. Frames are standard SSE: a named `event:` and
a JSON `data:` line. Comment keep-alives (`: keep-alive`) are sent during quiet periods — ignore them.

**Event order:** `open` → interleaved inner-life events → `done` → optional `audio_meta`,
`audio_chunk`×N, `audio_end`.

Text is sent before audio deliberately: render the reply immediately, then let audio stream in.

| Event | Payload |
| --- | --- |
| `open` | `{session_id, end_user_id, transcript?}` |
| `turn_start` | `{turn_id, user_input, session_id, ts}` |
| `activation` | `{cluster, intensity, note, turn_id, ts}` — a brain region firing. |
| `stream_thought` | `{thought, chem_delta, proactive, ts, salience?, urgency?, from_job?}` — inner monologue. |
| `emotion` | `{emotion, intensity?}` — the persona's mood shifting mid-turn. |
| `user_emotion` | `{emotion}` — the brain's read of *your user's* emotional state. |
| `turn_end` | `{turn_id, response, elapsed_s, llm_calls, ts}` |
| `done` | `{response, affect, mood, elapsed_s, llm_calls, confirmation?}` — the authoritative result, identical in shape to the non-streaming turn response. `confirmation` never appears on an `answer_only` turn. |
| `audio_meta` | `{turn_id, format, voice_id, model, sample_rate}` |
| `audio_chunk` | `{turn_id, seq, text, mood, voice_settings?, data}` — `data` is base64 audio for one segment. |
| `audio_end` | `{turn_id, chunks, duration_s, chars}` |
| `audio_error` | `{turn_id, detail}` — synthesis failed; text already sent. |
| `error` | `{detail}` — the turn failed. |

Every event carries its own `type` field mirroring the event name. Events are filtered to this
session — you never see another partner's turn or the brain's idle inner life.

**Treat `done` as authoritative.** `turn_end.response` is the raw text still carrying markup;
`done.response` is the cleaned display text with the curated affect block and any pending
confirmation.

**Disconnecting cancels the turn.** Closing the stream before `done` cancels the in-flight turn task.
No `agent_turns` row is written (it lands only after `done`), so the turn cannot be graded; episodic
writes made before the cancel point survive. The stream carries inner life, not response tokens — the
reply arrives whole in `turn_end`/`done` (see [§9](#9-sessions-and-turns), *Concurrency and
cancellation*). Under `answer_only`, inner-thought frames that belong to a background job
(`from_job: true`) are not forwarded.

**This is a server-to-server API.** There is no CORS on any origin, so a browser
cannot call it directly — and it should not: your key is a long-lived secret with
access to every one of your customers, and putting it in client-side code publishes
it. Proxy through your own backend and stream to the browser from there.

```python
import json, httpx

with httpx.stream(
    "POST",
    f"{BASE}/v1/sessions/{sid}/turns/stream",
    headers={"Authorization": f"Bearer {KEY}"},
    json={"message": "Summarise the thread", "audio": {"enabled": True}},
    timeout=None,
) as r:
    event = None
    for line in r.iter_lines():
        if line.startswith(":"):          # comment keep-alive
            continue
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
            if event == "done":           # authoritative result
                print(payload["response"])
```

---

## 11. Streaming: WebSocket

### `WS /v1/sessions/{session_id}/stream`

Full-duplex realtime: PCM16 audio in with live STT, inner-life events and TTS chunks back, with
barge-in.

Keep streaming mic audio while the reply plays — that is what makes interruption work. The server
runs the same barge policy as the local voice path: explicit barge keywords ("stop", "wait", …) cut
instantly, other speech needs at least two real words, and anything that is mostly an echo of what
is currently being spoken is dropped. On headphones the echo guard never fires; on open speakers it
is what stops a reply from interrupting and then answering itself.

**Auth** is checked on the `Authorization` header of the **upgrade request**, before `accept()`.
Unknown or unauthorised connections are closed **1008**. A brain that is not yet up closes **1013** —
reconnect with backoff.

On connect the server sends:

```json
{"type": "ready", "session_id": "9f2c1ab7d4e05b63", "expects": "pcm_16000"}
```

All frames are JSON. Audio is base64 inside the JSON payload, consistent with the SSE `audio_chunk`
shape.

**Client → server**

| `type` | Payload | Notes |
| --- | --- | --- |
| `audio` | `{data: <base64 PCM16 @ 16 kHz>}` | Stream chunks as captured, including during playback. The live STT session opens on the first chunk and stays open across turns. Barge-in fires on *transcribed speech*, not on audio arrival: an interim transcript that passes the barge policy cancels in-flight TTS within ~300 ms, and the turn then dispatches on the final. Speech that is mostly the words currently being spoken is treated as speaker echo — it neither interrupts nor starts a turn. |
| `audio_end` | — | Close the live STT session. A new one opens on the next `audio` chunk; you do not need to send this between turns. |
| `text` | `{message: string, audio?: {enabled, voice_id?, model?, format?, provider?, proactive?}}` | Text-in turn. The `audio` block is remembered for subsequent turns. `proactive: false` mutes audio for out-of-band results while keeping reply audio. |
| `ping` | — | Server replies `{"type": "pong"}`. |

**Server → client**

| `type` | Payload |
| --- | --- |
| `ready` | `{session_id, expects}` |
| `transcript` | `{text, is_final, seq, duration_s?}` — interim and final STT results. A final result triggers a turn unless it is speaker echo. `duration_s` is present on finals and is the STT quota meter: seconds of audio fed for that turn, the same unit `POST /v1/stt` reports. |
| `thought` | The `stream_thought` payload (renamed on this transport). |
| `turn_start` | `{turn_id, user_input, session_id, ts}` |
| `emotion` / `user_emotion` | `{emotion, intensity?}` |
| `done` | `{response, affect, mood, elapsed_s, llm_calls, transcript?, confirmation?}` — `confirmation` never appears on an `answer_only` session. |
| `audio_meta` / `audio_chunk` / `audio_end` / `audio_error` | Same payloads as SSE. `audio_end` carries `cancelled: true` when barge-in interrupted synthesis. |
| `proactive` | `{text, ts, affect?}` — **out-of-band**. A backgrounded job's result, delivered after `turn_end`. |
| `task_outcome` | `{job_id, state, reason_human, summary, goal}` — terminal outcome of an autonomous job. Gate-independent, so you see terminal state even when spoken delivery is suppressed. |
| `error` | `{detail, code}` |
| `pong` | — |

**The WebSocket is the only transport that survives `turn_end`,** and it behaves differently because
of it. On the request/response transports a tool's result is resolved inline before the reply
returns. Here, a deferred tool's result arrives later as a `proactive` frame. If your product needs
"the agent comes back to you with an answer", this is the transport.

Turns are serialised behind a lock — concurrent sends queue rather than interleave. The lock is the
brain process's, shared across every session of your org (see [§9](#9-sessions-and-turns),
*Concurrency and cancellation*); closing the socket cancels the in-flight turn. On an `answer_only`
session, `proactive`, `task_outcome` and job-originated `thought` frames are not forwarded — there is
no background work to report.

---

## 12. Approvals and confirmations

Two distinct mechanisms. Do not conflate them.

### Confirmations — one parked cloud write, per session

When a turn wants to perform a cloud write that needs sign-off, it parks it and the turn response
carries:

```json
{"confirmation": {"required": true, "description": "Send the summary email to the client list"}}
```

### `POST /v1/sessions/{session_id}/confirm`

Body `{"approve": true}` (defaults to `true`).

```json
{
  "session_id": "9f2c1ab7d4e05b63",
  "end_user_id": "u_8821",
  "approved": true,
  "response": "Sent.",
  "mood": {"emotion": "satisfied"}
}
```

`409 no action awaiting confirmation` when nothing is parked. The pending action is stored per
session, so concurrent sessions never collide. Agents configured to auto-confirm never reach this
path — the write already ran.

### Approvals — a queue of sensitive actions, per end user

### `GET /v1/sessions/{session_id}/approvals`

```json
{
  "session_id": "9f2c1ab7d4e05b63",
  "end_user_id": "u_8821",
  "approvals": [
    {
      "id": "a_7c19",
      "tool": "send_email",
      "signature": "9f31c0aa5b2e7d14",
      "reason": "sends mail on the user's behalf",
      "preview": "to=client@example.com, 812 chars",
      "turn_id": "t_01J8XYZ",
      "end_user_id": "u_8821",
      "status": "pending",
      "created_at": 1754400000.0,
      "resolved_at": null
    }
  ]
}
```

Returns `{"approvals": []}` rather than `501` when the runner is not wired.

### `POST /v1/sessions/{session_id}/approvals/{approval_id}/resolve`

Body `{"approve": true}`.

```json
{"session_id": "...", "end_user_id": "u_8821", "approved": true}
```

Plus whatever the resolver returns.

Pending approvals expire after `BRAIN_APPROVAL_PENDING_TTL_S` (default 24h). An owner key also sees
and can resolve the autonomous lane — actions the brain queued while unattended — so a single-tenant
owner app can offer "approve from when I was away". Partner keys never see that lane.

---

## 13. Grading and consolidation

### `POST /v1/sessions/{session_id}/turns/{turn_id}/grade`

The external reward signal. This is the one grade grounded outside the agent's own appraisal, and
wiring it up is the highest-leverage integration you can do.

**Body**

| Field | Type | Notes |
| --- | --- | --- |
| `grade` | number \| boolean | `+1`/`-1` or `true`/`false` for thumbs; any number normalised to `[-1, +1]`. |
| `source` | string | Provenance, default `"api"`. Clamped to 64 printable characters — it lands in the eval log verbatim. |

**Response `200`**

```json
{"ok": true, "grade": 1.0, "applied_live": true}
```

When the turn has already left the live trace buffer (consolidation or a restart happened):

```json
{"ok": true, "grade": 1.0, "applied_live": false, "reason": "turn_not_live"}
```

That is a success, not a failure — the grade is recorded for audit, but the learning half missed its
window. An async grader should check `applied_live` and tighten its latency if it sees
`turn_not_live` regularly.

**Contracts worth knowing:**

- `turn_id` must belong to **this** session. Grading a turn from another session returns `404`,
  deliberately indistinguishable from a turn that never existed.
- Chemistry moves at most once per `turn_id`. A re-grade applies only the bounded difference from the
  previous grade, so repeated posts cannot pump the reward signal. An identical repeat is a no-op.
- The nudge lands on **that end user's** chemistry, not the process resting state. A partner's grade
  moves that customer's mood.

**Errors** — `400` missing grade; `404` unknown session or unknown turn for this session; `501`
grading not wired.

### `POST /v1/sessions/{session_id}/consolidate`

Run the session-end Hebbian/sleep pass now and persist learning, without tearing the brain down.

Body: `{"reason": "debate_end"}` (defaults to `"api"`).

```json
{"session_id": "9f2c1ab7d4e05b63", "consolidation": { }}
```

A checkpoint: idempotent and single-flight. Learning is bound to the *session's* persona, so in a
multi-persona scene each participant's learning lands on its own graph.

The pass is **grouped by persona**: the buffer of un-consolidated turns is process-wide, so each
persona's turns are replayed under its own binding, and the passes that used to scan every persona
on disk (learning stories, angle synonyms, reflex mining) are bounded to the personas in the batch.
In a **consolidated** org whose batch carried engine-lane turns, the self-model rewrite (History
summary, Stable preferences) and the inner-life digest pass through the de-identification gate
before they land in `self.md`, and a passage the gate rejects is not written. In an **isolated**
org the cross-learning step is skipped and no self-authored skill is drafted
([§20 Learning mode](#learning-mode)). `409` when the session's persona is served by a dedicated
instance ([§28](#28-multi-persona-routing)).

Call it when a long-running agent should durably commit learning between sessions, or at the end of a
multi-agent debate for every participant. If you never call it, consolidation still happens on the
normal session-end path — this endpoint only lets you force the checkpoint early.

---

## 14. Utility: structured extraction

### `POST /v1/extract`

Sessionless structured extraction: one forced cheap model call returning JSON matching your schema.
No session, persona, memory, motor, or DMN. Built for high-volume utility classification that must
never pay for — or be unreliably answered by — a full conversational turn.

**Body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `input` | string | **yes** | Non-empty source text. |
| `schema` | object | **yes** | A non-empty JSON Schema object. |
| `instructions` | string | no | Extraction guidance. |
| `name` | string | no | Tool name, default `"extract"`. |

**Response `200`** — `{"data": { }}` matching your schema. `data` is `{}` if the model returned
nothing usable.

```bash
curl -sS https://api.elyceum.app/v1/extract \
  -H "Authorization: Bearer $ELYCEUM_KEY" -H "Content-Type: application/json" \
  -d '{
        "input": "Apple beat earnings expectations…",
        "schema": {"type":"object","properties":{"ticker":{"type":"string"},"sentiment":{"type":"string"}}},
        "instructions": "Pull the tradeable signal."
      }'
```

**Errors** — `400` missing or wrong-typed `input`/`schema`; `402` over the daily USD ceiling; `501`
extraction not wired.

---

## 15. Jobs

Durable, pollable outcomes of autonomous background work. A client that was disconnected while a job
ran still reads its result — job history is independent of any streaming gate.

### `GET /v1/jobs`

Query: `?limit=` (default 20, clamped to 200), `?state=`.

```json
{"jobs": [
  {
    "job_id": "j_01J8...",
    "goal": "Draft the weekly market note",
    "state": "completed",
    "reason_code": "",
    "reason_human": "Produced the note and cited four sources.",
    "summary": "…",
    "source": "self",
    "agent_id": "the_visionary.research_lead",
    "productive_steps": 6,
    "stories_completed": 3,
    "stories_total": 3,
    "cloud_usd": 0.41,
    "source_links": ["https://…"],
    "written_files": ["notes/weekly.md"],
    "created_at": "…", "updated_at": "…", "completed_at": "…"
  }
]}
```

Returns `{"jobs": []}` rather than `501` when job history is not wired.

**Job states**

| State | Meaning |
| --- | --- |
| `running` | In-progress checkpoint; partial results persisted. |
| `completed` | Produced real, verified-enough work. |
| `deferred` | Paused, will resume. Carries a defer reason. |
| `awaiting_approval` | An external side effect is waiting on approval. |
| `failed` | Ran but produced nothing usable. Carries a reason. |
| `stopped_budget` | Hit the hard budget cap; not retried today. |

`reason_human` and `summary` are guaranteed non-empty on every terminal state — there is no silent
empty-success.

### `GET /v1/jobs/{job_id}`

Full record: steps, results, plan, source links, written files, summary. `404` for an unknown id
(or another partner's job); `501` when job history is not available.

A partner key sees only its own jobs on both routes; the owner sees all.

---

## 16. Webhooks

Register an endpoint and the engine POSTs there when an autonomous job reaches a
terminal state, so you don't have to hold a WebSocket open or poll `GET /v1/jobs`.
Requires the Supabase backend.

**Routing.** A webhook registered by a partner key receives only that partner's jobs.
An owner-registered webhook receives every job in the org, including the brain's own
self-directed work.

**Payload.** The body carries the outcome summary and the job id — not the full record.
Fetch `GET /v1/jobs/{job_id}` for steps and results.

```json
{
  "event": "job.completed",
  "data": {
    "job_id": "j_01J8...",
    "state": "completed",
    "reason_human": "Produced the note and cited four sources.",
    "summary": "…",
    "goal": "Draft the weekly market note"
  }
}
```

Terminal states delivered: `job.completed`, `job.failed`, `job.deferred`,
`job.awaiting_approval`, `job.stopped_budget`. (An internal rate-limit *decline* is not
a delivered event — the job never ran.)

**Signature.** Every delivery carries an `Elyceum-Signature` header:

```
Elyceum-Signature: t=1754467200,v1=<hex hmac_sha256(secret, "<t>.<body>")>
```

Verify it against the exact received bytes. Reject a signature whose `t` is more than
five minutes from now (replay protection), and compare `v1` with a constant-time
compare. Deliveries also carry a stable event id you can dedupe on. **Do not** treat the
secret as a bearer token — it is a signing key, never sent as `Authorization`.

```python
import hmac, hashlib, time

def verify(secret: str, body: bytes, header: str, tolerance=300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if abs(time.time() - int(parts["t"])) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f'{parts["t"]}.'.encode() + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts["v1"], expected)
```

**Delivery is at-least-once.** Retries use jittered backoff; a webhook that dead-letters
repeatedly is auto-disabled (visible as `active: false` with a `disabled_reason`).
Dedupe on the event id.

**The target must be a public HTTPS URL.** Registration rejects `http`, private,
loopback, and link-local addresses, and the check is re-run on every delivery attempt
(so DNS rebinding cannot smuggle a request to an internal address).

### `POST /v1/webhooks`

Body `{url, events?}`. Returns `{id, url, events, partner_id, secret}` — the `secret` is
shown **once**. `400` on a non-https or private-address URL; `503` without Supabase.

### `GET /v1/webhooks`

Your registered webhooks (metadata only — never the secret).

### `POST /v1/webhooks/{webhook_id}/rotate`

Mints a new secret (shown once); the old one stops verifying. `404` for another
partner's webhook.

### `GET /v1/webhooks/{webhook_id}/deliveries`

Recent delivery attempts — `state`, `last_status`, `last_error`, `attempts` — the
failure-visibility surface. `404` for another partner's webhook.

### `DELETE /v1/webhooks/{webhook_id}`

Deletes the webhook and its secret. `404` for another partner's webhook.

---

## 17. Learning surface

Read-only windows into what the brain has learned. Owner keys may pass `?persona=`; without it the
org's home persona is read. All three return `501` when the learning surface is not wired.

**Content policy.** What a persona learned is narrated from its conversations, so these reads pass
the same read-path content policy as the persona's living self-model and user-model
([§20](#20-personas)): in an isolated org a non-home persona is withheld with `403 isolated_persona`;
a partner key is not an org admin and gets `403 org_admin_required` on all three routes (they are
effectively **owner-key** reads); every allowed read leaves a governance line in the org's audit log.

### `GET /v1/learning/stories`

Query: `?persona=` (owner only), `?limit=` (default 50).

```json
{
  "stories": [ ],
  "generated_on_read": false,
  "personas": ["the_visionary", "the_adversary"]
}
```

Plain-language accounts of what the brain learned in each session, with structured evidence
citations. This is the surface to show an end user or an operator — it is written to be read, not
parsed.

### `GET /v1/learning/wiring`

Query: `?persona=` (owner only), `?edge=src→tgt`.

```json
{
  "top": [{"edge": "frontal→hippocampus", "weight": 1.284}],
  "deltas": [ ],
  "edge_count": 72
}
```

`?edge=` adds that edge's drift series across consolidation snapshots plus its recent update records.
Note the arrow is a literal `→` (U+2192) and must be URL-encoded.

### `GET /v1/learning/summary`

Query: `?persona=` (owner only).

```json
{
  "persona": "the_visionary",
  "plasticity": { },
  "reward_mix": { },
  "switches": { },
  "chunks": { },
  "predictor": { },
  "gates": { },
  "structure": { }
}
```

Learning vitals: plasticity per session, the reward-source mix (self-graded versus external
percentage), switch efficacy within safety bands, motor chunks, thought-sequence predictor stats,
gate activity, and structural growth.

**`reward_mix` is your integration health check.** If the external share is near zero, nobody is
calling the grade endpoint and the brain is learning only from its own appraisal.

---

## 18. Audio: TTS and STT

Both are stateless — no session needed. Both return `501` when the runner is not wired and `503`
when the provider key is not configured.

### `POST /v1/tts`

Text-to-speech with the affect→voice mapping. Pass the `affect` from a turn to get mood-driven
prosody.

**Body**

| Field | Type | Notes |
| --- | --- | --- |
| `text` | string | Required, non-empty. Pass the **raw** turn text (markup intact) so mood spans drive per-chunk prosody. |
| `affect` | object | The turn's affect. Drives the mapping. |
| `voice_id` | string | Provider voice. Defaults to the persona's configured voice on session paths. |
| `model` | string | Alias `flash` (prosody via voice settings) or `v3` (prosody via inline tags), or a raw provider model id. |
| `format` | string | `mp3_44100_128` (default), `mp3_22050_32`, `pcm_16000`, `pcm_22050`, `pcm_24000`, `opus_48000`. |
| `provider` | string | `elevenlabs` (default), `openai`, `google`. Falls back to the `TTS_PROVIDER` env. |

**Response `200`**

```json
{
  "format": "mp3_44100_128",
  "voice_id": "…",
  "model": "eleven_flash_v2_5",
  "data": "<base64>",
  "duration_s": 3.42,
  "chars": 88,
  "segments": [{"seq": 0, "text": "…", "mood": "warm"}]
}
```

`chars` is the provider-billed unit and what the quota meters.

**Errors** — `400` empty text, non-object `affect`, unknown format or model; `429` quota; `503` no
provider key.

### `POST /v1/stt`

**Body**

| Field | Type | Notes |
| --- | --- | --- |
| `audio` | string | Required. Base64 audio. |
| `mimetype` | string | Default `audio/wav`. |
| `diarize` | boolean | Default `false`. Speaker separation. |
| `model` | string | Provider model override. |

**Response `200`**

```json
{
  "transcript": "what changed in the market today",
  "words": [ ],
  "duration_s": 2.8,
  "segments": [{"transcript": "…", "is_final": true}]
}
```

Providers: `deepgram` (default) and `google`, selected by the `STT_PROVIDER` env. Every provider
returns the same shape. `duration_s` is the STT quota meter.

**Errors** — `400` missing or invalid base64; `429` quota; `503` no provider key.

---

## 19. Mandates (roles)

The org's role library. All mandate routes require the Supabase backend and return
`503 mandates require the Supabase storage backend` without it.

### `GET /v1/mandates`

Query: `?include_inactive=true` includes deactivated roles. Returns `{"mandates": [...]}`.

### `PUT /v1/mandates/{mandate_id}`

Create or update a role. Idempotent.

| Field | Type | Required |
| --- | --- | --- |
| `role_text` | string | **yes** |
| `conduct_rules` | any | no |
| `reward_weights` | any | no |

`conduct_rules` and `reward_weights` are accepted and stored so a partner whose source of truth lives
in their own app can sync full rows, but **the brain does not consume them yet**. Do not expect them
to change behaviour.

### `DELETE /v1/mandates/{mandate_id}`

Soft-delete. Assignments stop resolving; the record survives so `?include_inactive=true` still lists
it. `404` if unknown. Returns `{"ok": true, "mandate_id": "...", "active": false}`.

### Assignments

These three are the low-level primitive behind agents. Most callers should use `/v1/agents` instead —
same underlying pairing, but it speaks in agent ids and lets you set a name, permissions, and tier in
one call.

### `GET /v1/personas/{persona}/mandates`

Returns `{"persona": "...", "assignments": [...]}`, in order.

### `PUT /v1/personas/{persona}/mandates/{mandate_id}`

Assign a role to a persona. Idempotent. Body: `{"sort_order": 0}`.

### `DELETE /v1/personas/{persona}/mandates/{mandate_id}`

Unassign. `404` if no such assignment.

---

## 20. Personas

### Learning mode

Every persona in an org follows the org's **learning mode**, an account setting on the
organizations record (`learning_mode`, default `consolidated`), read by
[`GET /v1/whoami`](#27-lifecycle-sleep-and-status) and
[`GET /v1/org/permissions`](#21-agents) and switched with `PUT /v1/org/permissions` or the
console's Account limits page. There is no per-persona override; an org that wants both models
needs two orgs.

**Consolidated** (the default): a persona is one individual doing a job for your organization.
It learns across everyone it talks to — wiring, self-model, stories, chunks, stances — and what one
customer teaches it may shape how it treats the next, after passing the de-identification gate.
Each customer keeps a private silo (memory recall, speaker profile, chemistry pair, approvals,
sessions), and in engine lanes structural recall, the speaker-profile grep and the DMN memory seed
are scoped to the bound end user. The self-model rewrite and the inner-life digest are
de-identified before they land in `self.md` when a batch carried engine-lane turns. Right for
support desks, care teams, advisors, tutors.

**Isolated**: every persona is a separate individual and nothing learned by one may reach
another. Each purchase, client or project gets its own [clone](#post-v1personaspersonaclone) of
a template persona. What `isolated` means for the whole org:

1. Learning is per persona: wiring, self.md, stories, chunks, stances, chemistry, DMN state, open
   threads. Nothing crosses personas.
2. No cross-learning: the private rumination → de-id gate → hypothesis store chain is skipped at
   sleep, and established principles are never injected into a turn.
3. In engine lanes, structural recall, the speaker-profile grep, and the DMN memory seed are
   scoped to the bound end user. On the idle loop a persona's memory seed samples its recorded
   owner's episodes (ownership binding), so a companion remembers its buyer spontaneously; an
   unowned persona seeds from nothing.
4. Idle thinking is per persona on the org's one shared loop. Each persona a human has talked to
   in the last `dmn_active_roster_days` (default 7) keeps its own idle thinking, self-tasks and
   projects, in its own stores only; one that nobody has talked to for longer leaves the roster
   (its open threads persist in its own ledger and resume when someone comes back). Cadence thins
   with the active count: the loop rotates at `max(dmn_min_tick_interval, interval / active)`,
   so 200 active personas each think about every 17 minutes. The console's `dmn_isolated_roster`
   setting picks the rule: `active` (default), `home` (the home persona only — purchase personas
   never think idle and get no `dmn_state` row), or `all` (every full-tier persona, as a
   consolidated org). Always-on dedicated instances, with a full-cadence loop of their own, are a
   separate placement tier, not a roster mode.
5. No self-authored skills. Partner-authored skills stay org-level and apply per the normal
   agent mapping.
6. Muscle memory neither records nor recalls for non-home personas.
7. Sleep passes that scan all personas are bounded to the personas in the batch.
8. Persona hard purge (`DELETE /v1/personas/{p}?purge=true`) and the isolation audit
   (`GET /v1/personas/{p}/isolation`) are available.
9. **Persona ownership binding**: the first `end_user_id` to open a session on a persona owns it;
   a session for any other end user on that persona returns `404`, exactly like an unknown agent.
   The home persona and owner keys are exempt. A "shared avatar" (one character, many fans) is
   therefore impossible in an isolated org and needs a consolidated org.
10. **What the org can see.** Neither the console nor the owner key returns a non-home persona's
    learned content: engine-lane transcripts, jobs (goal, steps, results), approval payloads, idle
    thoughts, the living `self.md` or the user-model. Those reads answer `403 isolated_persona` or a
    content-free projection (state, counts, cost, timestamps). The persona's dials, Seed self-model
    and chemistry state (resting and current) are configuration, not content, and stay readable and
    editable. In a consolidated org the same reads are open to org admins only and each one is
    written to the governance log; `GET /v1/personas/{p}/isolation` reports `content_reads_30d`.

**Changing the setting** is a governance event: owner key or org admin only, `confirm: true`
required, audit-logged (who, when, from, to, `instance_seed`), and it takes effect on the next
turn and the next sleep pass without a restart (a 60 s cache). The response lists exactly what
changed.

| | consolidated → isolated | isolated → consolidated |
| --- | --- | --- |
| Existing personas | Stay as they are and become **templates**; each purchase is a fresh clone. You choose the org's `instance_seed` at the switch (**required**, `400` without it): `default` = spec only, a freshly composed self.md, baseline wiring, no chunks or stances — the template's learning reaches nobody; `current` = the template's learned competence is carried at clone time (wiring weights, motor chunks, sequence weights, ignition tally, and the self.md History summary and Stable preferences after the de-identification gate). Chemistry starts at the resting baseline, never the template's live mood. Never carried: episodes, `user.md` and speaker profiles, open threads, chemistry pairs, DMN state, learning ledger and stories, jobs. The response lists the personas that hold learned state so you can purge any you do not want to serve as templates. | **No merge.** Per-purchase personas stay separate individuals; nothing is folded into a template. They will now contribute de-identified principles to the shared store and receive them. Because that breaks the promise buyers were sold, the switch is **refused with `409 isolated personas exist: purge or archive first`** while any non-home persona holds learned state, unless `force: true` — audit-logged with the persona list. |
| Hypothesis store | Injection stops immediately. `hypotheses.json` is retained but inert; [`DELETE /v1/org/hypotheses`](#21-agents) purges it on request, and the response says whether it exists. | Resumes from whatever is there (empty for an org that started isolated). |
| End-user silos | Unchanged. | Unchanged. |
| DMN | The roster becomes home + the recently active personas within 60 s (`dmn_isolated_roster`); a persona nobody has talked to for `dmn_active_roster_days` leaves it, its open threads persisting in its own ledger. | Every full-tier persona re-enters the roster regardless of activity. |
| Ownership binding | Starts for new sessions; personas with more than one end user in `api_sessions` are reported as `multi-owner, cannot be bound` and stay unbound until purged. | Rows are kept but no longer enforced. |
| Erasure | Persona hard purge is the primary erasure for a purchase. | End-user purge remains; consolidated state is de-identified, not per-user erasable. |

```json
PUT /v1/org/permissions
{"learning_mode": "isolated", "confirm": true, "instance_seed": "current"}
```

`instance_seed` can be changed later on its own (no `confirm` needed); a clone body's `seed` overrides
it per clone.

### `GET /v1/personas`

```json
{
  "personas": [
    {"slug": "the_visionary", "display_name": "The Visionary", "builtin": true},
    {"slug": "captain_ahab", "display_name": "Captain Ahab", "builtin": false}
  ],
  "total": 2,
  "limit": 200,
  "offset": 0,
  "next_offset": null,
  "limits": {"max_dedicated_instances": 3, "max_live_brains": 25, "max_personas": 10000}
}
```

`?q=` searches slug and display name; `?limit=` (default 200, max 1000) and `?offset=` page.
Served from the personas index (one ranged query) when it is live, so the call costs the same
for an org with ten personas or ten thousand.

Built-ins first, then custom specs in slug order. Respect `limits` when planning concurrent
multi-persona scenes; `max_personas` caps the custom specs an org may hold (see
[§7](#7-quotas-budgets-and-metering)).

| Query | Default | Notes |
| --- | --- | --- |
| `include_clones` | `false` | Clones (personas created by the clone route, carrying a `template` field) are **hidden by default** — a marketplace org has thousands, and the roster you resolve agents against is the templates. |
| `template` | — | List one template's clones (implies clones). Each row carries `template` and `seed`. |
| `limit` | `200` | Page size, capped at `1000`. |
| `offset` | `0` | Page start. `total` counts the filtered set; `next_offset` is `null` on the last page. |

### `GET /v1/personas/{persona}`

A custom persona's stored spec, or a built-in's canonical profile (overlaid with its override
spec when one is saved — the response carries an `overridden` flag). `404` if unknown.

### `PUT /v1/personas/{persona}`

Create or update a persona spec. Idempotent. All body fields are optional and merge over the
stored spec.

A **built-in** slug is accepted as an **override spec**: `baseline`, `tag`, `note` and `vals`
apply on top of the canonical persona, while identity (`display_name`, `disposition`,
`personality`, `speaking`) stays canonical and is refused (`400`). Unset baseline channels on a
built-in default to its canonical chemistry. `DELETE` the built-in's spec to restore defaults.

| Field | Type | Notes |
| --- | --- | --- |
| `display_name` | string | Custom personas only. |
| `disposition` | string | Custom only. Identity text, written **as the persona, in first person**. It becomes the persona's self-model. |
| `personality` | string | Same role as `disposition`. |
| `speaking` | string | Voice and cadence notes. Bullet lines work well. |
| `baseline` | object | Resting chemistry. Channels `DA`, `ACh`, `GABA`, `Glu`, `NE`, `5HT`, `CORT`, `OXT`, `AEA`, each in `[0, 0.8]` — resting is the setpoint the brain relaxes toward, and live dynamics need headroom above it, so higher values clamp to `0.8`; `GABA` is floored at `0.12` so inhibition can never be authored out of reach. Unset channels default to a neutral profile (custom) or the canonical chemistry (built-in override). |
| `tag` | string | Short catalogue subtitle shown in the owner UI. |
| `note` | string | Catalogue blurb shown in the owner UI. |
| `vals` | object | A saved settings-knob setup (`settings-key → scalar`). Read only by the owner UI; the brain itself never reads it. |

The baseline is the temperament the persona's brain boots with and relaxes toward. It influences
expression; it does not determine it. Expression variance under a fixed chemistry is intended, not a
bug.

```json
{
  "display_name": "Captain Ahab",
  "disposition": "Captain Ahab — consumed, magnetic, unbending. The whale took my leg and I will have my reckoning…",
  "speaking": "- Grand, biblical cadence; oaths and omens\n- Commands, never asks",
  "baseline": {"DA": 0.45, "NE": 0.55, "CORT": 0.3, "GABA": 0.18, "5HT": 0.3}
}
```

A created persona is immediately resolvable — the gateway spawns any persona slug on demand, subject
to the dedicated-instance cap.

### `POST /v1/personas/{persona}/clone`

**Owner credential required.**

Clone a custom template persona into a new persona. This is the isolated-org primitive
([Learning mode](#learning-mode)): one clone per purchase, client or project, each its own
learning identity. Custom templates only — a built-in slug is `400`, an unknown one `404`.

```json
{"suffix": "purchase_8821", "display_name": "Captain Ahab", "copy_agents": true, "seed": "default"}
```

| Field | Type | Notes |
| --- | --- | --- |
| `slug` \| `suffix` | string | One is required. With `suffix`, the slug is `<template>_<suffix>` (the canonical slugifier collapses runs of `_`, so `__` cannot be a store key). |
| `display_name` | string | Defaults to the template's. |
| `copy_agents` | boolean | Default `true`: the template's agents (mandate assignments with name, enabled flag, sort order, permissions, tier) and their skill mappings are copied to the clone. Agent errors land in `errors[]`; the spec is written first. |
| `seed` | `current` \| `default` | Per-clone override of the org's `instance_seed`. |
| `tag`, `note` | string | Catalogue metadata; default to the template's. |

**What a clone starts with.** `default`: the template's spec (disposition, personality,
speaking, baseline), a freshly composed self-model, baseline wiring, no chunks or stances.
`current`: additionally the template's learned competence — wiring weights, motor chunks,
sequence weights, ignition tally, angle synonyms — and its self-model's History summary and
Stable preferences **after the de-identification gate** (a section the gate rejects is not
carried and is listed in `errors[]`). In both cases chemistry starts at the resting baseline,
never the template's live mood, and these are **never** copied: episodes, `user.md` and
speaker profiles, open threads and the open-questions ledger, chemistry pairs, DMN state,
learning ledger and stories, jobs.

```json
{
  "created": true,
  "persona": {"slug": "captain_ahab_purchase_8821", "template": "captain_ahab", "seed": "default", "…": "…"},
  "template": "captain_ahab",
  "seed": "default",
  "copied": {"agents": {"agents": 2, "agent_skills": 1}},
  "errors": []
}
```

Idempotent: the same slug for the same template returns `created: false` with the existing
spec. `409` when the slug belongs to a different persona, or when the org is at `max_personas`.

### `DELETE /v1/personas/{persona}`

Deletes a custom persona's spec, chemistry, and identity document. For a **built-in** slug this
**restores defaults**: the override spec is removed and resting chemistry reset to canonical —
the persona itself, its evolved mood and its grown self-model stay (`404` when no override
exists). `404` if unknown.

**Learned state is not deleted.** Episodes and wiring stay keyed under the slug and go dormant.
Re-creating the same slug resurrects that history. Delete the persona's agents separately via
`DELETE /v1/agents/{agent_id}`.

**`?purge=true` — the persona hard purge.** Owner credential. The primary erasure for a purchase in
an isolated org: every store keyed by the persona goes. Refused for built-ins and the home persona
(`400` — the home persona's state root is the org's volume root); `404` for an unknown persona;
`501` when the server has no brain attached.

| Removed | Kept |
| --- | --- |
| Supabase rows keyed `(org_id, persona)`: `episodes`, `wiring_edges`, `wiring_snapshots`, `dmn_state`, `brain_schemas` (self.md, user model, open-questions ledger), `tasks`, `agent_turns`, `agent_projects`, `agents`; `api_sessions` and `agent_jobs` of its agents (`agent_id` prefix); its `persona_owners` row. | `agent_usage` (the org's spend record — billing history, not the persona's memory) and `speaker_profiles` (keyed by the customer; the end-user purge erases them). |
| Files: the persona's state root (wiring, chunks, sequence weights, ignition tally, ledgers, stories, chemistry pairs, DMN state), its catalogue dir (spec + chemistry), its stamped local job records; its rows in the eval log (best-effort rewrite). | — |
| In-process: its sessions, un-consolidated traces and their journal lines, chemistry and profile caches, wiring graph, DMN bundle, agent caches. | — |

Serialized on the turn lock and the consolidation lock so a purge can never race a turn or a
sleep pass. The response is a per-step summary and `ok` is `false` if any step failed — a partial
purge is visible and retryable.

```json
{"ok": true, "persona": "captain_ahab_purchase_8821", "deleted": {"episodes": 412, "wiring_edges": 96, "agents": 2, "api_sessions": 3, "state_root": ["…/personas/captain_ahab_purchase_8821"], "kept": ["agent_usage", "speaker_profiles"], "…": "…"}}
```

Re-cloning the same slug afterwards yields a fresh persona with a new fingerprint.

### `GET /v1/personas/{persona}/isolation`

**Owner credential required.**

The isolation audit snapshot — the partner-facing proof that nothing crosses personas.

```json
{
  "persona": "captain_ahab_purchase_8821",
  "learning_mode": "isolated",
  "owner_end_user_id": "u_8821",
  "in_dmn_roster": true,
  "last_human_turn_ts": 1789312501.4,
  "is_home": false,
  "files": {"wiring.json": {"sha256": "…", "bytes": 4210, "mtime": 1789312541.2}, "chunks.json": null, "…": "…"},
  "documents": {"self.md": {"sha256": "…", "bytes": 2210}, "open_questions.md": {"sha256": "…", "bytes": 0}, "user_model": {"files": 1, "sha256": "…"}},
  "ledgers": {"learning_ledger.jsonl": 18, "learning_stories.jsonl": 3},
  "client_chem_pairs": 1,
  "counts": {"episodes": 412, "wiring_edges": 96, "wiring_snapshots": 2, "dmn_state": 0, "tasks": 0, "agent_turns": 412, "agent_projects": 0},
  "fingerprint": "sha256 over the canonical stores"
}
```

| Field | Meaning |
| --- | --- |
| `files` | sha256, size and mtime of each learned-state file under the persona's state root (`null` when absent). |
| `documents` | sha256 + size of `self.md` and the open-questions ledger; the user model as a count of speaker files plus a hash over their hashes. |
| `ledgers` | Line counts of the learning ledger and stories. |
| `counts` | Exact head counts of every `(org_id, persona)`-keyed table. `"error: …"` for a store that could not be counted. |
| `in_dmn_roster` | Whether this process's idle loop rotates into the persona: home always; in an isolated org, per `dmn_isolated_roster` — under `active` only while `last_human_turn_ts` is within `dmn_active_roster_days`. |
| `last_human_turn_ts` | Wall-clock of the last human turn with this persona (`null` if never), the per-persona activity stamp behind the `active` rule. Not part of the fingerprint. |
| `fingerprint` | sha256 over content hashes, counts and line counts only — never mtimes — so it is byte-stable while the persona is untouched and changes the moment any learned store does. |

**Verify recipe.** Snapshot B → talk to A (turns, then `POST /v1/sessions/{A}/consolidate`) →
snapshot B again. `fingerprint`, `documents.self.md.sha256`, `files.wiring.json.sha256` and
`ledgers` must be identical; A's own fingerprint must have changed.

### `POST /v1/personas/reindex`

**Owner credential required.**

Rebuild the persona index — the `personas` table that backs listing, search and the fleet
rollups — from this org's spec files, built-ins and activity stamps, and re-evaluate which
personas hold learned state. Every persona write keeps the index in step on its own; this is the
backfill after the migration that introduced it, or the repair after a volume restore. The brain
also runs it once at boot when the index holds fewer custom personas than the volume.

Synchronous: the request returns when the rebuild is done (batches of 200 rows; a few seconds
per thousand personas).

```json
{"indexed": 4213, "learned": 3907, "elapsed_s": 6.4}
```

| Field | Meaning |
| --- | --- |
| `indexed` | Rows upserted: every built-in plus every custom persona whose spec is on the volume (soft-deleted personas are resurrected only if their spec still exists). |
| `learned` | Custom personas found holding learned state (a learned file, ledger, episode or wiring rows, or a self-model that differs from its authored seed). |
| `elapsed_s` | Wall-clock of the rebuild. |

`503` while the index is unavailable — turned off (`persona_index_enabled`), no database backend,
or the migration not yet applied. No body.

### Placement

Where a persona *runs* is separate from what it *learns* ([Learning mode](#learning-mode) above
decides that). By default every persona is **shared**: it binds per turn on your org's one brain
process, and that process's local model calls ride the platform GPU pool with everyone else's.
Idle thinking for a shared persona is a time-slice of the shared loop.

A **placement** is the premium tier: the persona gets a brain process of its own (`mode:
"dedicated"`), pinned so the idle reaper never stops it, with a full-cadence idle mind. `pod` says
which GPU that process talks to:

| `pod` | What it buys |
| --- | --- |
| `pool` | The platform GPU pool, shared with every other tenant. A full-cadence idle mind on a shared card. |
| `standalone` | A GPU pod of its own: full-cadence idle thinking on its own GPU, never queued behind another tenant. `gpu_type` (a RunPod `gpu_type_id`) picks the card and lifts the pool's price ceiling. |
| `org` | One pod shared by *this org's* dedicated instances — a middle price point. |

**Reaching the instance.** Once a persona is placed and its instance is running, send
`X-Brain-Persona: <slug>` on every request for it — `POST /v1/sessions`, turns, streams and the
WebSocket upgrade ([§28](#28-multi-persona-routing)). The header is honoured only for a persona
with a running dedicated instance; a request for a placed persona that arrives without it reaches the
shared brain and is refused with `409` (the cross-process guard), so the persona's state is never
written by two processes.

Two org-level caps govern placements, both on the organizations record and both reported by
[`GET /v1/org/permissions`](#21-agents):

- `max_dedicated_instances` — how many dedicated placements the org may hold (`0` = the
  deployment default, `BRAIN_MAX_DEDICATED`). Over it → `409`.
- `gpu_daily_usd_budget` — the org's daily (UTC) ceiling on standalone / org pod spend, set with
  `PUT /v1/org/permissions {"gpu_daily_usd_budget": 12.0}`. `0` means the org may not hold
  standalone pods: `pod: "standalone"` or `"org"` → `402` until it is set. When the day's budget is
  spent, the org's pods sleep and its dedicated instances fall back to the pool until midnight UTC.

A placement whose `paid_until` has passed is **demoted**: the instance is consolidated and stopped
on the next tick, the persona returns to the shared brain, and the row reads as `mode: "shared",
expired: true`. Removing the row does the same immediately. Every placement write is
audit-logged like the learning-mode switch. Metering is in [§7](#7-quotas-budgets-and-metering):
pool time as `pod_hours_shared`, standalone / org pod wall-clock as `pod_hours_dedicated`.

### `GET /v1/personas/{persona}/placement`

**Owner credential required.**

```json
{
  "persona": "captain_ahab_purchase_8821",
  "mode": "dedicated",
  "pod": "standalone",
  "gpu_type": null,
  "always_on": true,
  "paid_until": "2026-12-31T00:00:00+00:00",
  "placed": true,
  "expired": false,
  "live": {"instance": "dedicated", "pod_state": "ready", "host_kind": "standalone"}
}
```

| Field | Meaning |
| --- | --- |
| `mode`, `pod`, `gpu_type`, `always_on`, `paid_until` | The placement as written. An unplaced persona reads `mode: "shared", pod: "pool", placed: false`. |
| `expired` | `paid_until` has passed; `mode` then reads `shared`. |
| `live.instance` | `dedicated` when the persona's own process is running right now, else `shared`. Lags a write by up to a minute (the gateway's tick). |
| `live.pod_state` | The GPU that instance talks to: `off`, `resuming`, `warming`, `ready`, `failed`, or `fallback_pool` while a standalone pod could not be created and the instance is consuming the pool. |
| `live.host_kind` | `pool`, `standalone` or `org`. |
| `live.reason` | Only with `pod_state: fallback_pool`: why the instance is on the pool — `booting`, `fallback_pool` (could not be created), `no_budget`, `budget_spent`, `platform_cap`, `idle`, `slept`. |

`404` for an unknown persona.

### `POST /v1/personas/{persona}/placement`

**Owner credential required.** Upsert.

```json
{"mode": "dedicated", "pod": "standalone", "gpu_type": null, "always_on": true, "paid_until": "2026-12-31T00:00:00Z"}
```

All fields optional: `mode` defaults to `dedicated`, `pod` to `pool`, `always_on` to `true`,
`paid_until` to open-ended. Returns the same shape as `GET`.

| Status | Meaning |
| --- | --- |
| `400` | A built-in persona (clone it first), the org's home persona (it already *is* the shared process), or a malformed body. |
| `404` | Unknown persona. |
| `409` | The org's `max_dedicated_instances` is reached. Re-placing an already-placed persona does not count against it. |
| `402` | `pod` is `standalone` or `org` and the org's `gpu_daily_usd_budget` is `0`. |
| `503` | The placement registry is unavailable on this deployment (migration pending). Nothing was written. |

### `DELETE /v1/personas/{persona}/placement`

**Owner credential required.**

Removes the placement. The persona returns to the shared brain and the pool; the gateway
consolidates and stops its dedicated instance and pauses its standalone pod on the next tick.
Idempotent (`removed: 0` for an unplaced persona); `404` for an unknown one, `400` for the home
persona.

```json
{"ok": true, "persona": "captain_ahab_purchase_8821", "removed": 1, "mode": "shared", "pod": "pool", "placed": false, "live": {"…": "…"}}
```

### `GET /v1/personas/{persona}/self-model`

**Owner credential required.**

The persona's self-model (`self.md`) — the identity document it authors and re-authors about
itself. Sleep consolidation rewrites its History summary and Stable preferences from lived
sessions, so diffing this over time is the primary read for "how has this persona changed".
`404` if unknown.

```json
{"persona": "the_visionary", "display_name": "The Visionary", "content": "# Self\n…"}
```

### `GET /v1/personas/{persona}/user-model`

**Owner credential required.**

The persona's model of the people it talks to: `content` is `user.md` (speakerless turns), and
`speakers` holds one entry per identified speaker — engine turns key speakers by `end_user_id` —
with the relationship fields the turn path itself reads back. Untouched templates are filtered
out: an empty `speakers` list means nothing has been learned about anyone yet, not an error.

```json
{
  "persona": "the_visionary",
  "display_name": "The Visionary",
  "content": "# User\n…",
  "speakers": [
    {
      "file": "user_the_adversary_1a2b3c4d.md",
      "name": "the_adversary",
      "content": "# User: the_adversary\n- Score: 12\n- Familiarity: acquainted\n…",
      "affection": 12,
      "familiarity": "acquainted"
    }
  ]
}
```

### `GET /v1/personas/{persona}/chemistry`

**Owner credential required.**

The persona's chemistry state: `resting` (temperament) and `current` channels, plus one pair per
end-user it has met — engine mode seeds a per-customer mood that decays toward temperament in
their absence. Owner-only by design: the public surface deliberately curates affect down to
`mood`, so the internal chemistry model is not partner-observable; this read exists for the org
owner's own instrumentation. Pair writes are throttled — a pair snapshot can lag the turn that
moved it by a turn.

```json
{
  "persona": "the_visionary",
  "display_name": "The Visionary",
  "resting": {"DA": 0.62, "ACh": 0.55, "GABA": 0.35, "Glu": 0.5, "NE": 0.45, "5HT": 0.4, "CORT": 0.3, "OXT": 0.45, "AEA": 0.4},
  "current": {"DA": 0.66, "…": 0.0},
  "updated": "2026-08-15T21:04:11+00:00",
  "pairs": [
    {"end_user_id": "the_adversary", "snapshot": {"nm": {"DA": 0.58, "…": 0.0}}, "last_seen": 1786312541.2}
  ]
}
```

---

## 21. Agents

The persona × role pairing your end users actually talk to. Requires the Supabase backend.

### `GET /v1/agents`

```json
{
  "agents": [
    {
      "agent_id": "the_visionary.research_lead",
      "persona": "the_visionary",
      "mandate_id": "research_lead",
      "name": "Research Lead",
      "enabled": true,
      "permissions": {"cloud_writes": false},
      "sort_order": 0,
      "tier": "full"
    }
  ],
  "ceilings": { }
}
```

`?persona=<slug>` filters to one persona; `?limit=` and `?offset=` page server-side (no limit =
every row).

`ceilings` are the account-level permission maxima. A per-agent `permissions` map can only **narrow**
them, never widen. A key minted with an agent allowlist ([§25](#25-keys-and-end-user-lifecycle))
sees only its own agents here. The same map, with its write side, is served by
`GET|PUT /v1/org/permissions` below.

### Org ceilings and the narrowing rules

The exact key names, what each governs, and how an agent's `permissions` value combines with
the org ceiling:

| Key | Values | Org × agent |
| --- | --- | --- |
| `motor_enable_shell`, `motor_enable_network`, `motor_enable_cloud_actions` | `0`/`1` | AND — an agent can switch a capability **off**, never on. |
| `motor_user_writes`, `motor_user_network`, `motor_self_writes`, `motor_self_network`, `motor_auto_confirm_writes` | `0`/`1` | AND. `user_*` govern jobs a customer asked for; `self_*` the brain's own self-directed work. |
| `motor_user_cloud`, `motor_self_cloud` | `off` / `ro` / `full` | The more restrictive wins. |
| `motor_allowed_dirs`, `motor_read_only_dirs` | newline-separated absolute paths | Agent roots must sit **inside** an org root (path containment). **Empty = no filesystem access** (fail closed). On the hosted API a root outside your tenant's own volume is dropped on write and reported. |
| `motor_allowed_commands` | newline-separated command names | Set intersection with the org list (or with the built-in default set when the org list is empty). |
| `motor_user_connectors`, `motor_self_connectors` | newline-separated connector names | Intersection. Empty = all configured connectors. |
| `ralph_max_total_attempts`, `motor_max_concurrent_jobs`, `motor_max_jobs_per_window`, `motor_max_jobs_per_day`, `motor_max_jobs_per_session`, `bg_cloud_max_tokens_per_call`, `local_max_concurrent`, `cloud_max_concurrent`, `bg_cloud_max_concurrent` | numbers | Minimum wins. |
| `cloud_daily_usd_budget` | USD | Minimum wins. The org's daily cloud ceiling ([§7](#7-quotas-budgets-and-metering)). |
| `partner_cloud_daily_usd_budget` | USD | Org-wide only (no per-agent form): the cap each partner key is metered against; a partner is charged against the tighter of this and the org budget and gets `402` over it. |
| `dmn_enabled` | `0`/`1` | Org-wide only: the idle-thought loop kill switch (same as `PUT /v1/dmn`). |
| `answer_only` | `0`/`1` | OR — the org switch, the session/turn flag or the agent permission: any one restricts, none widens. See [§9](#9-sessions-and-turns). |
| `engine_lane_scoping` | `0`/`1` | Org-wide only. `1` (default): structural recall, the speaker-profile grep and the DMN memory seed read only the bound customer's material on the agent lane. `0` restores the persona-wide reads (kill switch). |
| `self_model_deid` | `0`/`1` | Org-wide only. `1` (default): in a consolidated org, the self-model rewrite and inner-life digest are de-identified before landing in `self.md` when a batch carried partner customers. `0` writes the raw rewrite (kill switch). |
| `persona_ownership_binding` | `0`/`1` | Org-wide only. `1` (default): in an isolated org the first end user to open a session on a persona owns it. `0` leaves isolation to your one-persona-per-purchase discipline ([§20](#20-personas)). |

`permissions: {}` on an agent means **inherit every ceiling**. There is no deny-all shortcut; to
deny everything, set the capability flags to `0`, `answer_only: 1`, and leave the directory roots
empty.

### `GET /v1/org/permissions`

Any key. `{"permissions": {<key>: <value>, …}, "keys": [<every ceiling key>]}` — the current org
value of every key in the table above.

### `PUT /v1/org/permissions`

Owner credential. Partial update: only the keys you send change. Booleans are accepted for the
`0`/`1` keys; a list of strings is accepted for the newline-separated keys.

```json
{"answer_only": 1, "motor_enable_shell": 0, "partner_cloud_daily_usd_budget": 5.0}
```

Returns `{"permissions": {…all keys…}, "dropped_paths": [...], "learning_mode": "…",
"instance_seed": "…", "hypotheses_present": false, "max_dedicated_instances": 0,
"gpu_daily_usd_budget": 0}` — `dropped_paths` lists any filesystem roots
refused because they lie outside the tenant's own volume. `400` for a key that is not a ceiling or
a value that cannot be coerced; `403` for a partner key. The console's Account limits page edits the
same keys; non-admin console members cannot write any of them.

**The learning-mode switch rides the same body.** `learning_mode`, `instance_seed`, `confirm` and
`force` are not ceiling keys: they are split off and applied first, as one governance event, before
any ceiling in the body is written. The full semantics are in [§20 "Learning mode"](#20-personas).

```json
{"learning_mode": "isolated", "instance_seed": "current", "confirm": true}
```

**The GPU budget rides the same body too.** `gpu_daily_usd_budget` (USD per UTC day, `>= 0`)
lives on the organizations record, not in the ceilings, so it reads identically from the shared
brain, every dedicated instance and the gateway. `0` means the org may hold no standalone pods
([§20 Placement](#placement)). Audit-logged; `400` for a non-number, `503` when the column is not
yet available on this deployment.

```json
{"gpu_daily_usd_budget": 12.0}
```

| Field | Notes |
| --- | --- |
| `learning_mode` | `consolidated` \| `isolated`. Requires `confirm: true` (`400` otherwise). |
| `instance_seed` | `current` \| `default`. **Required** when switching to `isolated` (`400` without it); optional afterwards to change the seeding policy alone (no `confirm` needed for a seed-only change). |
| `confirm` | Must be `true` to change the mode. |
| `force` | `isolated → consolidated` only: override the `409` refused while any non-home persona holds learned state. Audit-logged with the persona list. |

The response carries a `switch` block: `learning_mode`, `instance_seed`, `previous`, `changed` (what
took effect), `personas_with_learned_state` (the personas that become templates, or that blocked the
switch back), `multi_owner_personas` (personas with more than one end user in `api_sessions` — they
cannot be bound to a single owner), `hypotheses_present`. A refused switch returns a flat body:
`{"detail": "…", "personas": [...]}` with `400` or `409`, and applies nothing. `503` when the
organizations row cannot be written (migration 037 not applied on the deployment).

### `DELETE /v1/org/hypotheses`

**Owner credential required.**

Purge the org's shared hypothesis store (`hypotheses.json`): the de-identified, corroborated
principles cross-customer learning admitted through the de-identification gate. In an isolated org
the store is already inert (injection stopped at the switch) and this removes it; in a consolidated
org learning starts again from an empty store. Audit-logged.

```json
{"ok": true, "purged": true, "path": "…/hypotheses.json"}
```

### `GET /v1/agents/{agent_id}`

One agent. `404` if unknown — or outside your key's agent allowlist.

### `PUT /v1/agents/{agent_id}`

Create or update. The `agent_id` is split on the first `.` into persona and mandate id, and the
pairing is created or re-enabled idempotently. Only the keys present in the body are applied.

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | Display name. |
| `permissions` | object | Per-agent narrowing. Unknown keys are dropped. `answer_only: true` here marks the agent answer-only for every session. |
| `tier` | string | `"lite"` or `"full"`. Anything else is a `400`. |

Returns the updated agent row.

**Tier is about how the persona is run, not about what it can learn.** Learning is tier-agnostic — a
`lite` agent still learns.

### `DELETE /v1/agents/{agent_id}`

Unassigns the pairing. The id stops resolving for new sessions; the underlying role stays in the
library. `404` if unknown. Returns `{"ok": true, "agent_id": "..."}`.

---

## 22. Skills

App-provided skills: partner-supplied guidance injected into the agent's prompt. Because it is
partner-supplied content that reaches the prompt, **every submission is screened** before it can go
live. Requires the Supabase backend.

Screening is **defence in depth, not a security boundary**. It combines deterministic
checks with an LLM judge, and like any such judge it can be wrong in both directions.
Treat it as a filter that catches obvious problems, not as a guarantee that anything
`enabled` is safe. Submit skills you would be willing to run.

### Statuses

| Status | Meaning |
| --- | --- |
| `enabled` | Screened as obviously safe. Live — injected into turns after the next rewarm. |
| `flagged` | Anything in question. Awaiting owner review. **Not live.** |
| `rejected` | Reviewed and refused. Never goes live. |

If no screener is wired the submission fails safe to `flagged` with
`screen_notes.judge.reasons = ["screener_not_configured"]`. Nothing auto-enables without a screener.

### `PUT /v1/skills/{skill_id}`

Submit or update. Runs the screener synchronously.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `body` | string | **yes** | The guidance text. |
| `description` | string | no | Short summary, used for relevance selection. |
| `display_name` | string | no | |
| `keywords` | string[] | no | Retrieval hints. |
| `allowed_tools` | string[] | no | |
| `tier` | integer | no | Default `2`. |

**Response `200`**

```json
{"id": "house_policy_v2", "status": "flagged", "version": 3, "screen_notes": { }}
```

**Check `status` on every submission.** A `200` does not mean the skill is live. Poll
`GET /v1/skills/{skill_id}` until it reads `enabled`.

### `GET /v1/skills`

Query: `?status=`, `?include_inactive=`. Returns `{"skills": [...]}`. A partner key sees only its own
submissions.

### `GET /v1/skills/{skill_id}`

The full row including body text, status, and screener notes. `404` unknown, `403` another partner's.

### `DELETE /v1/skills/{skill_id}`

Leaves the live index on the next rewarm and stops being injected. `404` unknown, `403` another
partner's. Returns `{"ok": true, "skill_id": "...", "active": false}`.

### Pinning

Pass `skills: ["id", ...]` on `POST /v1/sessions` to force skills into every turn of that session, on
top of normal relevance selection. Ids that are unknown or not `enabled` are silently ignored at turn
time — a pin cannot bypass the screener.

---

## 23. Admin: skill review

**Owner credential required.** `403` for a partner key.

### `GET /v1/admin/skills/flagged`

Returns `{"skills": [...]}` awaiting review.

### `POST /v1/admin/skills/{skill_id}/approve`

The skill goes live and a rewarm is triggered. Returns `{"id", "status"}`. `404` on an unknown skill
id.

### `POST /v1/admin/skills/{skill_id}/reject`

Body `{"reason": "..."}`, recorded in `screen_notes.review`. The skill never goes live. `404` on an
unknown skill id.

---

## 24. Brain controls: DMN

**Owner credential required.**

The DMN is the idle-thought loop — the brain's inner life when nobody is talking to it.

One loop per brain process rotates across the personas on its **roster**, binding one persona per
tick so its thoughts, open threads and self-tasks land only in that persona's stores. In a
consolidated org the roster is every enabled full-tier persona. In an [isolated](#learning-mode) org
it is the home persona plus each persona a human has talked to in the last `dmn_active_roster_days`
(default 7; `dmn_isolated_roster` = `active` | `home` | `all`), so a purchased companion keeps its own
idle thinking while its owner is around and drops off the roster, not the org, when they stop.
Cadence thins with the roster size, down to the `dmn_min_tick_interval` floor. Two gates sit above
the roster: the kill switch below, and org-level dormancy — no human turn on any agent for
`dmn_pause_after_idle_s` (default three days) pauses the whole loop, self-tasks and project clock-in
until the next turn. Always-on dedicated instances (a full-cadence loop of their own) are a separate
placement tier.

### `GET /v1/dmn`

Returns `{"enabled": true}`.

### `PUT /v1/dmn`

Body `{"enabled": false}` → `{"enabled": false}`. `400` when `enabled` is missing or not a boolean.

This is a **kill switch, not an enable switch.** The loop checks the setting each cycle, so a `PUT`
takes effect on the next tick and re-enabling needs no restart. But it can only stop a running loop —
it can never start one that the `BRAIN_DMN` environment gate has disabled. The setting is persisted
and survives a restart.

---

## 25. Keys and end-user lifecycle

**Owner credential required for the key routes.** Erasure (`DELETE /v1/end_users/{id}`)
is partner-callable for a partner's own customers — see below.

### `POST /v1/partner_keys`

Body: `{"partner_id": "acme", "label": "Acme production", "role"?: "partner"|"owner", "allowed_agents"?: [...]}`.

```json
{"id": "3f9a1c2b7e04d5a6", "partner_id": "acme", "label": "Acme production", "role": "partner", "allowed_agents": ["the_visionary.research_lead"], "token": "sk_…"}
```

**`token` is returned once and never again.** Only its SHA-256 hash is stored. If it is lost, revoke
and mint a new one.

**`allowed_agents`** pins a partner key to a list of agent ids (`"{persona}.{mandate_id}"`, up to
200, well-formed but not required to exist yet). A restricted key must send `agent_id` when opening
a session and may only name one on its list (`404` otherwise, indistinguishable from an unknown
agent); `GET /v1/agents`, `/v1/agents/{id}`, `/v1/personas` and `/v1/personas/{p}` are filtered to
those agents and their personas. Omit it (or `null`) for an unrestricted key — the behaviour every
key had before. It cannot be set on a `role: owner` key. A key's list is fixed at mint: to change
it, mint a new key and revoke the old one.

`400` when `partner_id` is missing, `allowed_agents` is malformed or empty, `allowed_agents` is given
with `role: owner`, or the Supabase backend is absent.

### `GET /v1/partner_keys`

`{"keys": [{"id", "partner_id", "label", "active", "role", "created_ts", "allowed_agents"}]}`.
Metadata only — never the token or its hash. `allowed_agents` is `null` for an unrestricted key.

### `DELETE /v1/partner_keys/{key_id}`

Deactivates immediately; requests bearing the key are rejected from that moment. The metadata row is
kept for audit. `404` unknown. Returns `{"ok": true, "id": "...", "active": false}`.

### `DELETE /v1/end_users/{end_user_id}`

GDPR right-to-erasure. **A partner may erase its own customers** — you are the data
controller for them. The owner credential can erase any customer in the org. Another
partner's customer returns `404`.

Erases, in this order: in-memory caches (first, so a concurrent turn cannot
repopulate from a row about to be deleted); the durable chemistry snapshot for every
persona the customer has spoken to; pending approvals, including the `tool_input` of
any parked action; the customer's un-consolidated turns (the in-memory trace buffers
and their crash-safe journal, so the next learning pass cannot relearn them); local
background-job records; then the durable rows —

| Store | What it holds | Summary key |
| --- | --- | --- |
| `episodes` | Episodic memory | `episodes` |
| `agent_turns` | Verbatim prompt and response text | `agent_turns` |
| `agent_jobs` | Background-job outcomes: goal, tool outputs, results | `agent_jobs` |
| Local job files | The file mirror of `agent_jobs` on the brain's volume | `local_jobs` (`"skipped"` when the deployment has no motor cortex) |
| `brain_schemas` | The user model, including the per-speaker profile document | `brain_schemas`, `speaker_schema` |
| `speaker_profiles` | Voice/speaker identification | `speaker_profiles` |
| `tasks`, `dmn_state` | Queued work and idle-loop state | `tasks`, `dmn_state` |
| `api_sessions` | Session handles | `api_sessions` |
| `end_user_mcp_tokens` | Connector credentials, including the Vault secrets themselves | `end_user_mcp_tokens` |
| Trace journal | Un-consolidated turns awaiting the next learning pass | `session_traces`, `trace_journal` |
| Eval log | Per-turn evaluation rows. Engine-lane rows are written with the text already replaced by a digest, so there is nothing verbatim to erase | `eval_log` (`"redacted_at_write"`, or `"verbatim"` if the operator opted in) |

**What erasure does not reach.** Cross-customer *principles* (the hypothesis store)
are org-attributed, de-identified conclusions that passed the de-identification
gate at consolidation; they carry no customer identifier and are not per-user
erasable. Persona-level learning (wiring weights, the persona's own self-model
notes) is likewise not keyed by customer. Langfuse export is **off** for partner
turns by default (see [§30](#30-data-handling-and-retention)); if a deployment has
opted in, Langfuse holds its own copy under its own retention.

Returns a per-step summary plus `erased_at`. **Check `ok`** — it is `false` if any
step failed, and `failed` names which. A partial erasure is reported as a partial
erasure and can be retried: the customer stays owned until the pass completes.

**Tombstone semantics.** A successful erasure records `erased_at` on the ownership
row instead of dropping it, so:

- a second `DELETE` of the same id by its owner returns `410` with `erased_at` —
  distinguishable from the `404` a foreign id gets;
- the three MCP-token routes also answer `410` for an erased id (you cannot write a
  connector onto an erased customer);
- **opening a new session** for the same `end_user_id` starts the customer afresh —
  the tombstone is cleared, memory and chemistry begin from zero — and a later
  erasure works again.

`400` invalid id; `404` another partner's customer; `410` already erased; `501` when
the purge runner is not wired.

The data erasure is irreversible. There is no undo and no soft-delete of the data;
only the ownership handle is kept, so the id can be answered honestly afterwards.

---

## 26. MCP tokens

Per-end-user connector credentials, so managed agents can act through each of your users' own MCP
servers. Tokens are vault-encrypted at rest. Requires the Supabase backend (`503` otherwise).

Call these after **your** app completes the OAuth flow with the third-party service — the engine does
not run OAuth for you.

### `POST /v1/mcp/tokens`

| Field | Type | Required |
| --- | --- | --- |
| `end_user_id` | string | **yes** |
| `server_name` | string | **yes** |
| `server_url` | string | **yes** |
| `access_token` | string | **yes** |
| `expires_at` | timestamp \| null | no |

Returns `{"ok": true, "end_user_id": "...", "server_name": "..."}`. `400` on any missing field, `500`
on a storage fault.

### `GET /v1/mcp/tokens/{end_user_id}`

```json
{"end_user_id": "u_8821", "connections": [
  {"server_name": "gmail", "server_url": "https://mcp.example.com", "expires_at": null}
]}
```

Metadata only — never the token.

### `DELETE /v1/mcp/tokens/{end_user_id}/{server_name}`

The disconnect path when an end user unlinks a service. Agents lose access on their next vault build,
not instantly. `404` when the connection does not exist.

The primary key is `(org_id, end_user_id, server_name)`. `end_user_id` is your free text and is not
globally unique, so the same string legitimately exists in other orgs — every read is org-scoped.

---

## 27. Lifecycle: sleep and status

These two live on the **gateway**, not the brain router, so they are absent from the generated
OpenAPI schema and carry a `gateway` chip here. They are hosted-only and authenticate with a partner
key. Both are `/v1` paths, so they are served on the API host alongside everything else.

### `GET /v1/status`

```json
{
  "brain": "awake",
  "pod": {
    "state": "ready",
    "pods": [{"pod_id": "abc123", "index": 0, "state": "ready", "consumers": 2, "busy_frac_5m": 0.31}],
    "ready": 1,
    "assignments": 2,
    "max_pods": 3
  },
  "sleep": null
}
```

| Field | Values |
| --- | --- |
| `brain` | `awake`, `booting`, `asleep` — is your org's per-request compute running? |
| `pod` | **Owner keys only.** The platform GPU pool as your org sees it — the main cost driver for the basic tier. `state` is pod 0's boot phase (`off`, `resuming`, `warming`, `ready`, …), `pods[]` lists every pod currently held (slot `index`, `state`, `consumers`, 1-minute `busy_frac_5m`, `cost_per_hr`), `ready` counts serving pods, `assignments` counts brain processes placed on a pod, `max_pods` is the pool ceiling. Partner keys do not receive this field — the pool is shared across orgs. A placed persona's own pod is reported on [`GET /v1/personas/{p}/placement`](#placement) instead. |
| `sleep` | `null`, or `{"state": "asleep" \| "consolidating" \| …, "pod": "…"}` for the last transition. |

`401` on an unresolvable key.

### `GET /v1/whoami`

Who your key is. Answered at the gateway from the key row alone — **during a cold start too** —
and never spawns a brain or touches the pod, so it is the right first call to verify a credential
and learn your org id before sending traffic.

```json
{
  "org_id": "4bc6e95b-4977-431a-8765-5aa0422a7ff8",
  "partner_id": "acme",
  "role": "partner",
  "key_id": "3f9a1c2b7e04d5a6",
  "allowed_agents": null,
  "learning_mode": "consolidated",
  "instance_seed": "default"
}
```

| Field | Values |
| --- | --- |
| `org_id` | The org this key belongs to — the tenant unit for isolation, budgets and erasure. |
| `partner_id` | The partner the key was minted for; `null` on an owner key. |
| `role` | `partner` or `owner`. |
| `key_id` | The public key id (the one `GET /v1/partner_keys` lists); `null` for the env owner key on a self-hosted deployment. |
| `allowed_agents` | The key's agent allowlist, or `null` when unrestricted ([§25](#25-keys-and-end-user-lifecycle)). |
| `learning_mode` | The org's learning mode, `consolidated` or `isolated` ([§20](#20-personas)). `unknown` means the account record could not be read (the engine twin has not yet read it; the gateway could not reach it) — treat it as isolated. |
| `instance_seed` | What a persona clone starts with in this org: `default` or `current` ([§20](#20-personas)); `null` alongside an `unknown` learning mode. |

The engine serves the same path directly on a self-hosted deployment. `401` on an unresolvable key;
`503 auth backend unavailable` when the key store is down (retry).

### `POST /v1/sleep`

Turns off the cost-generating parts — the brain process and the GPU pod — for your org.

```json
{"ok": true, "state": "sleeping"}
```

Returns immediately; the sleep runs asynchronously. In-flight consolidation is allowed to finish (up
to `BRAIN_SLEEP_CONSOLIDATE_WAIT_S`, default 90s) before the process is reaped, so learning is not
cut short.

**There is no wake endpoint.** Waking is implicit: any other `/v1` call respawns the brain and kicks
the pod. Expect the [`503 booting`](#4-the-cold-start-contract) contract on the first call after
sleeping.

---

## 28. Multi-persona routing

You do not route. Every `/v1` request goes to your org's **shared** brain instance — the one process
that serves every persona on the roster and binds the session's persona per turn. A persona that has
been placed on its own dedicated instance ([§20 Placement](#placement)) is served by that instance
instead, and you reach it with the header below; when the instance is not running (over budget,
`paid_until` passed, a pod that could not be created) the shared brain serves the persona and nothing
on your side changes.

**The `X-Brain-Persona` header** is optional and narrow. When the deployment enables it
(`BRAIN_MULTI_PERSONA`), a header naming a persona that **currently runs on its own dedicated
instance** pins the request to that instance. A header naming any other persona is **ignored** and
the request goes to the shared brain: the header never starts a process, so a partner key cannot
spawn a persona instance by naming it — dedicated instances are created by the placement, not by
traffic. When the flag is off the header is ignored entirely. The same rule applies to the WebSocket
upgrade request.

```
X-Brain-Persona: captain_ahab_purchase_8821
```

The header value is normalised to a persona slug (lowercased, non-alphanumerics folded to `_`) and
must match `^[a-z0-9][a-z0-9_]{0,63}$` afterwards. Anything else degrades to the shared route.

**Cross-process guard.** A persona is served by exactly one process at a time. If a request reaches
the shared brain for a persona that has its own instance (a client that did not send the header, a
placement that has just come up) you get `409` on `POST /v1/sessions` and on turns; retry with the
`X-Brain-Persona` header naming that persona. The first request after a placement may still see the
[`503 booting`](#4-the-cold-start-contract) contract while the instance comes up.

---

## 29. Versioning and changes

`/v1` is the stable contract.

- **Additive changes** — new endpoints, new optional request fields, new response
  fields — ship without notice. Your client must tolerate unknown fields in a
  response and must not depend on the ordering of object keys.
- **Breaking changes** get a new version path (`/v2`) and at least 90 days' notice.
  We do not repurpose an existing field's meaning in place.
- **Deprecations** are announced with a removal date, and the old behaviour keeps
  working until it.

Two things that look like part of the contract and are not: the wording of `detail`
strings on errors (branch on the status code, not the message), and the exact
composition of the `capabilities` block, which grows as subsystems are added.

### Recent additive changes (2026-09)

All backward-compatible; existing clients need no change.

- `GET /v1/whoami` (gateway and engine) — [§27](#27-lifecycle-sleep-and-status).
- `GET|PUT /v1/org/permissions` and an org-wide `answer_only` ceiling — [§21](#21-agents), [§9](#9-sessions-and-turns).
- Per-key agent allowlist (`allowed_agents` on `POST /v1/partner_keys`) — [§25](#25-keys-and-end-user-lifecycle).
- `DELETE /v1/end_users/{id}` tombstones: `410` for an already-erased own customer, `erased_at` in the response; the purge now covers `agent_jobs`, local job records and un-consolidated turns — [§25](#25-keys-and-end-user-lifecycle).
- `elapsed_s` and `llm_calls` on `POST /turns`, SSE `done` and WS `done` — [§9](#9-sessions-and-turns).
- `Retry-After: 2` on the booting `503`; `at_capacity` on the first over-cap request; `X-RateLimit-Reset` — [§4](#4-the-cold-start-contract), [§6](#6-errors).
- New section [§30 Data handling and retention](#30-data-handling-and-retention).
- **Learning mode** (`organizations.learning_mode`, `consolidated` | `isolated`) on `whoami` and
  `GET /v1/org/permissions`; switched via `PUT /v1/org/permissions` with `confirm` + `instance_seed`
  — [§20](#20-personas), [§21](#21-agents). New owner routes: `POST /v1/personas/{template}/clone`,
  `GET /v1/personas/{p}/isolation`, `DELETE /v1/personas/{p}?purge=true`, `DELETE /v1/org/hypotheses`.
- `GET /v1/personas` hides clones by default; `?include_clones=`, `?template=`, `?limit=`,
  `?offset=`, `total`, `next_offset`; `limits.max_personas` — [§20](#20-personas), [§7](#7-quotas-budgets-and-metering).
- In consolidated orgs, engine-lane structural recall, the speaker-profile grep and the DMN memory
  seed are scoped to the bound end user, and the self-model rewrite is de-identified — [§13](#13-grading-and-consolidation).
- Persona ownership binding in isolated orgs (`404` for a second end user) — [§3](#3-authentication).
- `409` on turns for a persona served by a dedicated instance — [§28](#28-multi-persona-routing).
- The `/v1` lane answers `403 no_anthropic_key` instead of spawning a brain for an org with no
  Anthropic key on file — [§4](#4-the-cold-start-contract).
- **Placement** (premium tier): `GET|POST|DELETE /v1/personas/{p}/placement` (owner) — a dedicated
  brain instance per persona on the pool, a standalone GPU pod or one pod per org; org caps
  `max_dedicated_instances` and `gpu_daily_usd_budget` on `GET /v1/org/permissions`, the budget set
  via `PUT` — [§20](#20-personas), [§21](#21-agents).

### Deprecated

| What | Status |
| --- | --- |
| `https://elyceum.app/v1` as the API base | Working alias. Use `https://api.elyceum.app/v1`; the alias will be removed no earlier than 2027-01-01. |
| `Authorization` without the `Bearer ` prefix | **Removed.** Send `Bearer <token>`. |

---

## 30. Data handling and retention

What the platform stores about your end users, where, and for how long. This is the
factual basis for your own privacy documentation; it is not legal advice.

### Credentials

- Partner keys are stored as a SHA-256 hash only. The token is shown once at mint.
- End-user connector tokens (`POST /v1/mcp/tokens`) are vault-encrypted at rest and
  removed via the vault RPC on erasure — the ciphertext goes with the row.

### Logging

- No request/response body logging and no access log on the API path. Server logs
  carry ids (session, turn, agent), timings and error classes. Where the brain logs a
  slice of a goal, reason or summary, engine-lane text — and, in an isolated org, any
  text produced while bound to a non-home persona — is written as a `sha256:…/<len>`
  digest, never the words; owner-lane text (the org's own person in the console) may
  appear in the process log.
- Console and owner-key reads of learned content (transcripts, jobs, approvals,
  thoughts, the living self-model, the user-model) follow the org's learning mode:
  consolidated → org admins only, each read written to the governance log; isolated →
  never for a non-home persona. `GET /v1/personas/{p}/isolation` reports
  `content_reads_30d` so a partner can verify it stayed at zero.
- Secret redaction is applied to text the brain writes to its own notes (API keys,
  tokens, card-shaped numbers). It is **secret** redaction, not PII redaction: names
  and facts your customer volunteers are, by design, what the brain remembers.

### Where verbatim text lives

| Store | Contents | Per-customer erasable |
| --- | --- | --- |
| `agent_turns` | Every engine turn's prompt and response | Yes |
| `episodes` | Episodic memory (text + embedding) | Yes |
| `brain_schemas` | The per-customer profile document | Yes |
| `agent_jobs` + local job files | Goal, tool outputs and results of background work | Yes |
| Trace journal (`pending_traces*.jsonl`) | Un-consolidated turns, until the next learning pass | Yes |
| Eval log (`eval/turns.jsonl`) | Per-turn evaluation rows. Engine-lane text is replaced at write by `sha256:<16 hex>/<len>` digests; the operator can opt into verbatim with `BRAIN_EVAL_LOG_AGENT_TEXT=verbatim` | Nothing verbatim to erase by default |
| Langfuse | Optional tracing export. **Off for partner turns by default**; a deployment opts in with `BRAIN_LANGFUSE_AGENT_LANE=true`, in which case Langfuse (default host `cloud.langfuse.com`) is a sub-processor with its own retention | No — managed on the Langfuse side |
| Hypothesis store | De-identified, org-attributed principles that passed the de-identification gate at consolidation | No — carries no customer identifier |

### Retention

- **No automatic retention window today.** Data persists until erased through
  `DELETE /v1/end_users/{id}` or until the org is deleted.
- Erasure removes the live rows immediately. Database backups follow the hosting
  provider's point-in-time-recovery window and age out on their schedule; they are
  not rewritten on erasure.
- Persona-level learning (wiring weights, self-model notes) is not keyed by customer
  and is not per-customer erasable. In a **consolidated** org it carries no customer
  identifier: the self-model rewrite is de-identified when a batch carried partner
  customers, and cross-customer principles pass the de-identification gate. In an
  **isolated** org the persona *is* the purchase, and `DELETE /v1/personas/{p}?purge=true`
  erases everything it learned ([§20](#20-personas)).

### Termination

Erasure endpoints stay callable for as long as a partner key is active. On contract
end, ask for the org to be deleted: the control-plane tables cascade; the brain's
volume directory and any opted-in Langfuse project are removed as a manual step.

### Roadmap (not a commitment)

Per-org retention windows for `agent_turns`, `episodes` and the eval log, and a
region statement, are being scoped. Until they ship, the statements above are the
contract.
