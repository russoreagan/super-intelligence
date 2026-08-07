# Elyceum Engine API — Developer Reference

Version `v1`. This document is the full developer reference for the engine API: authentication, the
cold-start contract, every endpoint's request and response shape, the SSE and WebSocket event
vocabularies, error codes, and quotas.

The Reference page in the API workspace lists endpoints and is generated from the route code
(`brain/api/reference.py` introspects the real router). This document covers everything that page
cannot: bodies, response fields, error semantics, transport protocols, and the operational contracts
you must handle in a client.

**Source of truth.** Endpoint paths and one-line descriptions are generated from
`brain/api/server.py` docstrings. If this document ever disagrees with the Reference page on a path
or method, the Reference page wins and this document is stale.

---

## Contents

1. [Concepts](#1-concepts)
2. [Base URL and transport](#2-base-url-and-transport)
3. [Authentication](#3-authentication)
4. [The cold-start contract](#4-the-cold-start-contract)
5. [Quickstart](#5-quickstart)
6. [Errors](#6-errors)
7. [Quotas, budgets, and metering](#7-quotas-budgets-and-metering)
8. [Sessions and turns](#8-sessions-and-turns)
9. [Streaming: SSE](#9-streaming-sse)
10. [Streaming: WebSocket](#10-streaming-websocket)
11. [Approvals and confirmations](#11-approvals-and-confirmations)
12. [Grading and consolidation](#12-grading-and-consolidation)
13. [Utility: structured extraction](#13-utility-structured-extraction)
14. [Jobs](#14-jobs)
15. [Learning surface](#15-learning-surface)
16. [Audio: TTS and STT](#16-audio-tts-and-stt)
17. [Mandates (roles)](#17-mandates-roles)
18. [Personas](#18-personas)
19. [Agents](#19-agents)
20. [Skills](#20-skills)
21. [Admin: skill review](#21-admin-skill-review)
22. [Brain controls: DMN](#22-brain-controls-dmn)
23. [Keys and end-user lifecycle](#23-keys-and-end-user-lifecycle)
24. [MCP tokens](#24-mcp-tokens)
25. [Lifecycle: sleep and status](#25-lifecycle-sleep-and-status)
26. [Multi-persona routing](#26-multi-persona-routing)
27. [Appendix: full endpoint index](#appendix-full-endpoint-index)

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
| **Answer-only** | A contract flag. `answer_only=true` makes a session (or a single turn) pure synchronous Q&A: the brain drafts an answer and does nothing else — no tool or motor work, no background follow-up jobs. |

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

### OpenAPI

The engine app serves Swagger UI at `/v1/docs` on the API port. Behind the hosted gateway the schema
document itself is served at the origin root and is not proxied with your bearer key, so Swagger is
reliable only on a direct connection. Treat this document as the reference.

---

## 3. Authentication

Every request carries a bearer token:

```
Authorization: Bearer sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The header is also accepted without the `Bearer ` prefix, but send the prefix.

Auth is **fail-closed**: if no keys are configured for the org, every request is denied. An
accidentally exposed server is not open by default.

### Two kinds of key

| Kind | Where it lives | `partner_id` | `owner` | Scope |
| --- | --- | --- | --- | --- |
| **Org owner key** | `BRAIN_API_KEYS` / `BRAIN_API_KEY` env, or the `api_keys` setting | `null` | `true` | Everything, including owner-gated routes. Never metered. |
| **Partner key** | Row in the `api_keys` table, minted via `POST /v1/partner_keys` | your partner id | `false` | Only sessions this key opened; only skills this key submitted. Metered. |

Partner keys are stored as a SHA-256 hash. The plaintext token is returned **once**, at creation, and
is never recoverable. Tokens are prefixed `sk_`.

> **Important for hosted clients.** The hosted gateway maps a bearer token to an org by looking it up
> across all orgs in the `api_keys` table. Owner keys live in per-tenant environment variables and
> are **not** resolvable there. Through `https://api.elyceum.app/v1` you must use a per-partner key. Owner
> keys work on a direct connection to a single-tenant or standalone deployment.

### Owner-gated routes

These require `owner: true` and return `403` for a partner key:

- `GET|PUT /v1/dmn`
- `GET /v1/admin/skills/flagged`, `POST /v1/admin/skills/{skill_id}/approve`, `POST /v1/admin/skills/{skill_id}/reject`
- `GET|POST /v1/partner_keys`, `DELETE /v1/partner_keys/{key_id}`
- `DELETE /v1/end_users/{end_user_id}`

### Partner scoping

A partner key sees a narrowed world, enforced per request:

- **Sessions.** A session records the `partner_id` that opened it. Any session route called with a
  different partner key returns `403 session belongs to another partner`. Legacy sessions with no
  `partner_id` are owner-scoped.
- **Skills.** `GET /v1/skills` filters to your own submissions. Fetching, updating, or deleting
  another partner's skill returns `403`.
- **Approvals.** An owner key additionally sees and can resolve the *autonomous* lane — actions the
  brain queued while unattended. Partner keys never do, which is what preserves cross-tenant
  isolation.
- **Learning.** `?persona=` is honored only for owner keys. A partner key always reads the org's home
  persona.

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
await a single spawn.

**Handling:** retry the request with backoff until you get a non-503. Boot typically completes in
seconds; allow up to roughly a minute on a cold pod. Distinguish this 503 from other 503s by the body:
a booting response has `{"status": "booting"}`, while a capability 503 has `{"detail": "..."}`.

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
[§25](#25-lifecycle-sleep-and-status) for putting it back to sleep deliberately.

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
| `404` | Unknown `session_id`, `job_id`, `turn_id` (for this session), agent, persona, mandate, skill, or key. | Note that grading a turn from a *different* session returns 404, deliberately indistinguishable from a turn that never existed. |
| `409` | State conflict: `POST /confirm` with nothing pending, or an `agent_id` whose persona lives in a different process. | Re-read state. |
| `422` | `audio_input` contained no detectable speech. | Prompt the user to speak again. |
| `429` | Audio quota exhausted for the rolling window. | Back off until the window rolls. Detail names the cap and window. |
| `500` | Storage fault (MCP token read/write). | Retry with backoff. |
| `501` | The capability is not wired on this server — grading, consolidation, approvals, extraction, job history, learning, TTS, STT, or event streaming. | A deployment fact, not a transient one. Do not retry. Feature-detect at startup. |
| `503` | Either the brain is booting (`{"status": "booting"}` — retry) or a dependency is missing: no provider key configured for audio, or the Supabase backend is required and absent (`{"detail": "..."}` — do not retry). | Branch on the body. |

**501 vs 503 matters.** `501` means the runner was never wired into this deployment. `503` with a
`detail` means it exists but a key or backend is missing. Neither is worth retrying; both are worth
surfacing to whoever operates the deployment.

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

When the org exceeds its daily USD ceiling, any endpoint that would make a cloud model call returns
`402` with the budget message as `detail`. This includes turns and `POST /v1/extract`.

### Persona capacity

`GET /v1/personas` returns a `limits` block:

```json
{"max_dedicated_instances": 3, "max_live_brains": 25}
```

Beyond `max_dedicated_instances`, additional personas are refused. Plan concurrent multi-persona
scenes (a six-way debate, for example) inside that cap.

---

## 8. Sessions and turns

### `POST /v1/sessions`

Open a session for one end user.

**Body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `end_user_id` | string | **yes** | Non-empty. Your customer's id. Whitespace-trimmed. |
| `agent_id` | string | no | `"{persona}.{mandate_id}"`. Resolves the role. Preferred over `mandate_id`. |
| `mandate_id` | string | no | Raw role id, for callers not using agents. Ignored when `agent_id` resolves. |
| `skills` | string[] | no | App-provided skill ids pinned into every turn of this session. Unknown or non-enabled ids are silently ignored at turn time — a pin cannot conjure an unscreened skill. |
| `answer_only` | boolean | no | Default `false`. Declares the whole session synchronous Q&A. A turn body can override per turn. |

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

**Errors** — `400` bad field type or missing `end_user_id`; `404` unknown `agent_id`; `409` the
agent's persona runs in a different process (the gateway routes there; see
[§26](#26-multi-persona-routing)).

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
| `answer_only` | boolean | Overrides the session default for this turn only. |

**Response `200`**

| Field | Type | Notes |
| --- | --- | --- |
| `session_id` | string | |
| `end_user_id` | string | |
| `response` | string | Clean display text. All mood markup and reaction tags stripped. Safe for chat UI and captions. |
| `affect` | object | Prosody plan. See below. |
| `mood` | object | `{emotion: string, user_emotion?: string}`. The mood **output** only. |
| `turn_id` | string | Present when the turn produced one. The handle for `POST .../grade`. |
| `transcript` | string | Present only for `audio_input` — what we heard. |
| `confirmation` | object | Present only when a cloud write is parked: `{required: true, description: string}`. |

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

---

## 9. Streaming: SSE

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
| `done` | `{response, affect, mood, confirmation?}` — the authoritative result, identical in shape to the non-streaming turn response. |
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

```javascript
const res = await fetch(`${BASE}/v1/sessions/${sid}/turns/stream`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ message: 'Summarise the thread', audio: { enabled: true } }),
});

const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
// ... parse `event:` / `data:` pairs, switch on the event name.
```

---

## 10. Streaming: WebSocket

### `WS /v1/sessions/{session_id}/stream`

Full-duplex realtime: PCM16 audio in with live STT, inner-life events and TTS chunks back, with
barge-in.

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
| `audio` | `{data: <base64 PCM16 @ 16 kHz>}` | Stream chunks as captured. The live STT session opens on the first chunk. Audio arriving during playback triggers barge-in: in-flight TTS is cancelled and a fresh utterance begins. |
| `audio_end` | — | Close the current STT utterance. |
| `text` | `{message: string, audio?: {enabled, voice_id?, model?, format?, provider?, proactive?}}` | Text-in turn. The `audio` block is remembered for subsequent turns. `proactive: false` mutes audio for out-of-band results while keeping reply audio. |
| `ping` | — | Server replies `{"type": "pong"}`. |

**Server → client**

| `type` | Payload |
| --- | --- |
| `ready` | `{session_id, expects}` |
| `transcript` | `{text, is_final, seq, duration_s?}` — interim and final STT results. A final result triggers a turn. |
| `thought` | The `stream_thought` payload (renamed on this transport). |
| `turn_start` | `{turn_id, user_input, session_id, ts}` |
| `emotion` / `user_emotion` | `{emotion, intensity?}` |
| `done` | `{response, affect, mood, transcript?, confirmation?}` |
| `audio_meta` / `audio_chunk` / `audio_end` / `audio_error` | Same payloads as SSE. `audio_end` carries `cancelled: true` when barge-in interrupted synthesis. |
| `proactive` | `{text, ts, affect?}` — **out-of-band**. A backgrounded job's result, delivered after `turn_end`. |
| `task_outcome` | `{job_id, state, reason_human, summary, goal}` — terminal outcome of an autonomous job. Gate-independent, so you see terminal state even when spoken delivery is suppressed. |
| `error` | `{detail, code}` |
| `pong` | — |

**The WebSocket is the only transport that survives `turn_end`,** and it behaves differently because
of it. On the request/response transports a tool's result is resolved inline before the reply
returns. Here, a deferred tool's result arrives later as a `proactive` frame. If your product needs
"the agent comes back to you with an answer", this is the transport.

Turns are serialised behind a lock — concurrent sends queue rather than interleave.

---

## 11. Approvals and confirmations

Two distinct mechanisms. Do not conflate them.

### Confirmations — one parked cloud write, per session

When a turn wants to perform a cloud write that needs sign-off, it parks it and the turn response
carries:

```json
{"confirmation": {"required": true, "description": "Send the summary email to the client list"}}
```

**`POST /v1/sessions/{session_id}/confirm`** — body `{"approve": true}` (defaults to `true`).

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

**`GET /v1/sessions/{session_id}/approvals`**

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

**`POST /v1/sessions/{session_id}/approvals/{approval_id}/resolve`** — body `{"approve": true}`.

```json
{"session_id": "...", "end_user_id": "u_8821", "approved": true}
```

Plus whatever the resolver returns.

Pending approvals expire after `BRAIN_APPROVAL_PENDING_TTL_S` (default 24h). An owner key also sees
and can resolve the autonomous lane — actions the brain queued while unattended — so a single-tenant
owner app can offer "approve from when I was away". Partner keys never see that lane.

---

## 12. Grading and consolidation

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

Call it when a long-running agent should durably commit learning between sessions, or at the end of a
multi-agent debate for every participant. If you never call it, consolidation still happens on the
normal session-end path — this endpoint only lets you force the checkpoint early.

---

## 13. Utility: structured extraction

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

## 14. Jobs

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

Full record: steps, results, plan, source links, written files, summary. `404` for an unknown id;
`501` when job history is not available.

---

## 15. Learning surface

Read-only windows into what the brain has learned. Owner keys may pass `?persona=`; partner keys are
pinned to the org's home persona. All three return `501` when the learning surface is not wired.

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

## 16. Audio: TTS and STT

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

## 17. Mandates (roles)

The org's role library. All mandate routes require the Supabase backend and return
`503 mandates require the Supabase storage backend` without it.

| Endpoint | Notes |
| --- | --- |
| `GET /v1/mandates` | `?include_inactive=true` includes deactivated roles. Returns `{"mandates": [...]}`. |
| `PUT /v1/mandates/{mandate_id}` | Create or update. Idempotent. |
| `DELETE /v1/mandates/{mandate_id}` | Soft-delete. Assignments stop resolving; the record survives so `?include_inactive=true` still lists it. `404` if unknown. |

**`PUT` body**

| Field | Type | Required |
| --- | --- | --- |
| `role_text` | string | **yes** |
| `conduct_rules` | any | no |
| `reward_weights` | any | no |

`conduct_rules` and `reward_weights` are accepted and stored so a partner whose source of truth lives
in their own app can sync full rows, but **the brain does not consume them yet**. Do not expect them
to change behaviour.

`DELETE` returns `{"ok": true, "mandate_id": "...", "active": false}`.

### Assignments

| Endpoint | Notes |
| --- | --- |
| `GET /v1/personas/{persona}/mandates` | `{"persona": "...", "assignments": [...]}`, in order. |
| `PUT /v1/personas/{persona}/mandates/{mandate_id}` | Assign. Idempotent. Body: `{"sort_order": 0}`. |
| `DELETE /v1/personas/{persona}/mandates/{mandate_id}` | Unassign. `404` if no such assignment. |

These are the low-level primitive. Most callers should use `/v1/agents` instead — same underlying
pairing, but it speaks in agent ids and lets you set a name, permissions, and tier in one call.

---

## 18. Personas

### `GET /v1/personas`

```json
{
  "personas": [
    {"slug": "the_visionary", "display_name": "The Visionary", "builtin": true},
    {"slug": "captain_ahab", "display_name": "Captain Ahab", "builtin": false}
  ],
  "limits": {"max_dedicated_instances": 3, "max_live_brains": 25}
}
```

Built-ins first, then custom specs in slug order. Respect `limits` when planning concurrent
multi-persona scenes.

### `GET /v1/personas/{persona}`

A custom persona's stored spec, or a built-in's canonical profile. `404` if unknown.

### `PUT /v1/personas/{persona}`

Create or update a **custom** persona. Idempotent. Built-in slugs are refused (`400`). All body
fields are optional and merge over the stored spec.

| Field | Type | Notes |
| --- | --- | --- |
| `display_name` | string | |
| `disposition` | string | Identity text, written **as the persona, in first person**. It becomes the persona's self-model. |
| `personality` | string | Same role as `disposition`. |
| `speaking` | string | Voice and cadence notes. Bullet lines work well. |
| `baseline` | object | Resting chemistry. Channels `DA`, `ACh`, `GABA`, `Glu`, `NE`, `5HT`, `CORT`, `OXT`, `AEA`, each in `[0, 1]`. Unset channels default to a neutral profile. |

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

### `DELETE /v1/personas/{persona}`

Deletes the spec, chemistry, and identity document. Built-ins cannot be deleted (`400`). `404` if
unknown.

**Learned state is not deleted.** Episodes and wiring stay keyed under the slug and go dormant.
Re-creating the same slug resurrects that history. Delete the persona's agents separately via
`DELETE /v1/agents/{agent_id}`.

---

## 19. Agents

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

`ceilings` are the account-level permission maxima. A per-agent `permissions` map can only **narrow**
them, never widen.

### `GET /v1/agents/{agent_id}`

One agent. `404` if unknown.

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

## 20. Skills

App-provided skills: partner-supplied guidance injected into the agent's prompt. Because it is
partner-supplied content that reaches the prompt, **every submission is screened** before it can go
live. Requires the Supabase backend.

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

## 21. Admin: skill review

**Owner credential required.** `403` for a partner key.

| Endpoint | Notes |
| --- | --- |
| `GET /v1/admin/skills/flagged` | `{"skills": [...]}` awaiting review. |
| `POST /v1/admin/skills/{skill_id}/approve` | Goes live; triggers a rewarm. Returns `{"id", "status"}`. |
| `POST /v1/admin/skills/{skill_id}/reject` | Body `{"reason": "..."}`, recorded in `screen_notes.review`. Never goes live. |

Both `404` on an unknown skill id.

---

## 22. Brain controls: DMN

**Owner credential required.**

The DMN is the idle-thought loop — the brain's inner life when nobody is talking to it.

| Endpoint | Body | Response |
| --- | --- | --- |
| `GET /v1/dmn` | — | `{"enabled": true}` |
| `PUT /v1/dmn` | `{"enabled": false}` | `{"enabled": false}` |

`400` when `enabled` is missing or not a boolean.

This is a **kill switch, not an enable switch.** The loop checks the setting each cycle, so a `PUT`
takes effect on the next tick and re-enabling needs no restart. But it can only stop a running loop —
it can never start one that the `BRAIN_DMN` environment gate has disabled. The setting is persisted
and survives a restart.

---

## 23. Keys and end-user lifecycle

**Owner credential required for all of these.**

### `POST /v1/partner_keys`

Body: `{"partner_id": "acme", "label": "Acme production"}`.

```json
{"id": "3f9a1c2b7e04d5a6", "partner_id": "acme", "label": "Acme production", "token": "sk_…"}
```

**`token` is returned once and never again.** Only its SHA-256 hash is stored. If it is lost, revoke
and mint a new one.

`400` when `partner_id` is missing or the Supabase backend is absent.

### `GET /v1/partner_keys`

`{"keys": [{"id", "partner_id", "label", "active", "created_ts"}]}`. Metadata only — never the token
or its hash.

### `DELETE /v1/partner_keys/{key_id}`

Deactivates immediately; requests bearing the key are rejected from that moment. The metadata row is
kept for audit. `404` unknown. Returns `{"ok": true, "id": "...", "active": false}`.

### `DELETE /v1/end_users/{end_user_id}`

GDPR right-to-erasure. Erases the end user's memory and state across every per-user table and drops
in-memory caches, so a later turn cannot run as a half-erased customer.

`400` empty id; `501` when the purge runner is not wired. Returns the deletion summary.

This is irreversible. There is no undo and no soft-delete.

---

## 24. MCP tokens

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

## 25. Lifecycle: sleep and status

These two live on the **gateway**, not the brain router, so they do not appear in the generated
Reference page. They are hosted-only and authenticate with a partner key. Both are `/v1` paths, so
they are served on the API host alongside everything else.

### `GET /v1/status`

```json
{
  "brain": "awake",
  "pod": {"state": "ready"},
  "sleep": null
}
```

| Field | Values |
| --- | --- |
| `brain` | `awake`, `booting`, `asleep` — is your org's per-request compute running? |
| `pod` | Shared GPU pod state: `off`, `resuming`, `warming`, `ready`, … This is the main cost driver. |
| `sleep` | `null`, or `{"state": "asleep" \| "consolidating" \| …, "pod": "…"}` for the last transition. |

`401` on an unresolvable key.

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

## 26. Multi-persona routing

When `BRAIN_MULTI_PERSONA` is enabled on the deployment, the gateway routes each `/v1` request to the
persona named in a header:

```
X-Brain-Persona: the_adversary
```

Each named persona gets its own brain process, so one org can run several personas concurrently — a
six-persona debate, for example. Omit the header to use the org's default process.

When the flag is off the header is ignored entirely and the deployment behaves exactly as
single-process. The header also works on the WebSocket upgrade request.

Concurrency is bounded by `max_dedicated_instances` from `GET /v1/personas`. Beyond it, additional
personas are refused.

**Cross-process agents.** If you open a session with an `agent_id` whose persona lives in a different
process than the one handling the request, you get `409`. Route the request with the right
`X-Brain-Persona` header instead.

---

## Appendix: full endpoint index

47 routes. `owner` marks routes requiring the owner credential.

### Sessions

| Method | Path | Scope |
| --- | --- | --- |
| `POST` | `/v1/sessions` | |
| `POST` | `/v1/sessions/{session_id}/turns` | |
| `POST` | `/v1/sessions/{session_id}/turns/stream` | SSE |
| `WS` | `/v1/sessions/{session_id}/stream` | WebSocket |
| `POST` | `/v1/sessions/{session_id}/turns/{turn_id}/grade` | |
| `POST` | `/v1/sessions/{session_id}/consolidate` | |
| `POST` | `/v1/sessions/{session_id}/confirm` | |
| `GET` | `/v1/sessions/{session_id}/approvals` | |
| `POST` | `/v1/sessions/{session_id}/approvals/{approval_id}/resolve` | |

### Utility

| Method | Path |
| --- | --- |
| `POST` | `/v1/extract` |

### Jobs

| Method | Path |
| --- | --- |
| `GET` | `/v1/jobs` |
| `GET` | `/v1/jobs/{job_id}` |

### Learning

| Method | Path |
| --- | --- |
| `GET` | `/v1/learning/stories` |
| `GET` | `/v1/learning/summary` |
| `GET` | `/v1/learning/wiring` |

### Audio

| Method | Path |
| --- | --- |
| `POST` | `/v1/tts` |
| `POST` | `/v1/stt` |

### Mandates

| Method | Path |
| --- | --- |
| `GET` | `/v1/mandates` |
| `PUT` | `/v1/mandates/{mandate_id}` |
| `DELETE` | `/v1/mandates/{mandate_id}` |

### Personas

| Method | Path |
| --- | --- |
| `GET` | `/v1/personas` |
| `GET` | `/v1/personas/{persona}` |
| `PUT` | `/v1/personas/{persona}` |
| `DELETE` | `/v1/personas/{persona}` |
| `GET` | `/v1/personas/{persona}/mandates` |
| `PUT` | `/v1/personas/{persona}/mandates/{mandate_id}` |
| `DELETE` | `/v1/personas/{persona}/mandates/{mandate_id}` |

### Agents

| Method | Path |
| --- | --- |
| `GET` | `/v1/agents` |
| `GET` | `/v1/agents/{agent_id}` |
| `PUT` | `/v1/agents/{agent_id}` |
| `DELETE` | `/v1/agents/{agent_id}` |

### Skills

| Method | Path |
| --- | --- |
| `GET` | `/v1/skills` |
| `GET` | `/v1/skills/{skill_id}` |
| `PUT` | `/v1/skills/{skill_id}` |
| `DELETE` | `/v1/skills/{skill_id}` |

### Admin

| Method | Path | Scope |
| --- | --- | --- |
| `GET` | `/v1/admin/skills/flagged` | owner |
| `POST` | `/v1/admin/skills/{skill_id}/approve` | owner |
| `POST` | `/v1/admin/skills/{skill_id}/reject` | owner |

### Brain controls

| Method | Path | Scope |
| --- | --- | --- |
| `GET` | `/v1/dmn` | owner |
| `PUT` | `/v1/dmn` | owner |

### Keys and end users

| Method | Path | Scope |
| --- | --- | --- |
| `GET` | `/v1/partner_keys` | owner |
| `POST` | `/v1/partner_keys` | owner |
| `DELETE` | `/v1/partner_keys/{key_id}` | owner |
| `DELETE` | `/v1/end_users/{end_user_id}` | owner |

### MCP tokens

| Method | Path |
| --- | --- |
| `POST` | `/v1/mcp/tokens` |
| `GET` | `/v1/mcp/tokens/{end_user_id}` |
| `DELETE` | `/v1/mcp/tokens/{end_user_id}/{server_name}` |

### Gateway (hosted only, not in the generated reference)

| Method | Path |
| --- | --- |
| `GET` | `/v1/status` |
| `POST` | `/v1/sleep` |
