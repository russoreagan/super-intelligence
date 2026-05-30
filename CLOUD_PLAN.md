# Cloud Reasoning Migration Plan

**Status:** Deferred / not yet implemented. Captured 2026-05-30.
**Goal:** Move *heavy reasoning* to the cloud (with prompt caching to keep cost down) so we can rely less on large local models, while keeping the always-on background-thought loop local and keeping sensitive personal data on-device by default.

---

## Context / why this exists

On the dev machine (Apple M4 Pro, 48 GB), running multiple large local models concurrently caused memory overcommit → swap thrashing → Ollama hangs. We addressed the *crashes* by:

1. Capping Ollama: `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=10m` (in `start.sh`).
2. Eliminating the 32B: `OLLAMA_GENERAL_MODEL` now points at `qwen2.5:14b` (was `qwen2.5:32b`). The DMN planner and the 4 sleep-consolidation cells now run on the 14B locally. `qwen2.5:32b` was removed from Ollama.

What remains: heavy reasoning is now capped at local 14B quality. This doc plans the optional next step — selectively routing some of that reasoning to **cloud Sonnet 4.6** with prompt caching.

## Current model routing (after the local-only changes)

| Tier (`model_router.py` key) | Model | Used by | Locality |
|---|---|---|---|
| `local` | `qwen2.5:14b` | DMN inner monologue (~every 8s), hippocampus encoder, **DMN planner**, **all 4 sleep cells** | local-only |
| `local-code` | `qwen2.5-coder:14b` | Motor cortex (tool-use planning) | local-only |
| `local-general` | → `qwen2.5:14b` (was 32b) | (alias now resolves to the 14B) | local-only |
| `local-free` | `qwen2.5:14b` | speak_bridge rewriter (plain text) | local-only |
| embed | `nomic-embed-text` | memory vectors | local-first, Google fallback |
| `haiku` | `claude-haiku-4-5` | (available, cloud) | cloud |
| `sonnet` | `claude-sonnet-4-6` | (available, cloud) | cloud |
| `flash` / `flash-lite` | `gemini-2.5-flash*` | (available, cloud) | cloud |

Cloud plumbing already exists and is solid:
- `ModelRouter._call_anthropic()` already sets `cache_control: {"type": "ephemeral"}` on the system prompt (`brain/model_router.py` ~L295–303).
- Background-mode budget guard already exists: `enter_background_mode()` / `exit_background_mode()`, `bg_cloud_token_budget` (default 50k tokens/session), `bg_cloud_max_tokens_per_call` (default 512), `bg_cloud_timeout_s` (default 20s), with automatic fallback to local on budget exhaustion or timeout (`brain/model_router.py` ~L167–258).
- `MODEL_MAP` already includes `"sonnet": "claude-sonnet-4-6"`.

## Decisions already made (2026-05-30)

- **Cloud model of choice: Sonnet 4.6** (`claude-sonnet-4-6`). $3/$15 per 1M tokens. Chosen over Haiku/Opus largely because **its prompt-cache minimum is 2048 tokens** vs 4096 for Haiku/Opus — caching triggers on smaller prompts, which directly addresses "we're usually under the size that triggers caching."
- **Background thoughts stay local** (the every-8s DMN monologue on `local`/14B). Non-negotiable — the user values this loop and it must not depend on network/cost.
- **Sensitive data stays local by default.** The 4 sleep cells (`self_updater`, `episode_synthesizer`, `thought_consolidator`, `personality_observer`) read/rewrite personal memories + personality model and are flagged `sensitivity="sensitive"`. Keep them local unless explicitly revisited.
- **Motor stays local for now** (coder-14B). Revisit moving motor → cloud Sonnet later; tool-use prompts (system + skill blocks) are large and stable → excellent caching candidates.

## Candidate cells to move to cloud (when we do this)

Ranked by value-per-token and caching benefit, least-sensitive first:

1. **DMN planner** (`brain/dmn.py` ~L502, `model="local-general"`). Occasional (only when a thought sets `plan=true`), benefits from stronger reasoning, low privacy concern (speculative proposals, not raw memories). **Best first candidate.**
2. **Motor cortex tool-use planner** (`brain/clusters/motor_cortex.py`, `model="local-code"`). Highest *quality* upside (it acts on real projects) and best *caching* upside (big stable system + skill prefix). Gated/occasional. Medium privacy concern (sees file contents).
3. **Sleep consolidation cells** (`brain/sleep.py`, 4× `model="local-general"`). Highest quality upside for memory/personality coherence, but **highest privacy cost** — only move with explicit opt-in.

## Implementation sketch (when we pick it up)

The routing indirection already supports this — moving a cell to cloud is mostly a config change, not surgery.

### Option A — per-cell model swap (simplest)
Change the cell's `model=` from `"local-general"` / `"local-code"` to `"sonnet"` and **drop `locality="local"`** (the router force-redirects cloud→local when `locality=="local"`, see `model_router.py` ~L150). Wrap autonomous/background invocations in `enter_background_mode()` / `exit_background_mode()` so the existing token budget + timeout + local-fallback apply.

Caveats:
- These cells use `format="json"` locally (Ollama grammar). On Anthropic, use **structured outputs** (`output_config.format` with a `json_schema`) instead — `_call_anthropic()` does not currently pass `format`/`output_config`. Add that, or rely on the system-prompt JSON instruction + tolerant parsing already in `_call_local`. **This is the main code change needed.**
- `_call_anthropic()` reads `response.content[0].text` — fine for text/JSON; revisit if structured outputs change block shape.

### Option B — a `cloud-smart` tier (cleaner long-term)
Add `"cloud-smart": "claude-sonnet-4-6"` to `MODEL_MAP`, plus a settings flag (e.g. `heavy_reasoning_backend = "local" | "cloud"`) read in `ModelRouter.call()` so `local-general` transparently routes to Sonnet when enabled. Keeps cell definitions untouched and gives a single on/off switch. Preferred if we expect to toggle this.

### Caching strategy (keep cost down — the user's explicit ask)
- Sonnet's 2048-token cache floor is the lever. Ensure each cloud cell's **system prompt is ≥2048 tokens and byte-stable** (no timestamps/UUIDs/per-call IDs in the prefix — see the silent-invalidator list in the `claude-api` skill's `shared/prompt-caching.md`).
- Put volatile content (the specific thought/episode being processed) in the `messages`, never in the system prefix.
- Verify with `response.usage.cache_read_input_tokens > 0` across repeated calls. `_call_anthropic` currently only reads `input_tokens`/`output_tokens` — **add `cache_read_input_tokens` / `cache_creation_input_tokens` to logging** to confirm hits and track real cost.
- Consider a startup **cache pre-warm** (`max_tokens: 0` request on the stable prefix) if first-call latency on a cloud cell is user-visible.

### Cost guardrails (reuse what exists)
- Use `bg_cloud_token_budget` (raise from 50k if needed) + `bg_cloud_max_tokens_per_call` to bound spend; the router already falls back to local on exhaustion.
- Add a daily/session **USD** estimate to logging (tokens × Sonnet rate) so cost is visible, not just token counts.

## Verification checklist (when implemented)
- [ ] Cloud cell returns valid JSON (structured outputs wired, or tolerant parse confirmed).
- [ ] `cache_read_input_tokens > 0` on the 2nd+ identical-prefix call.
- [ ] Background-mode budget + timeout + local-fallback all fire correctly (test by setting a tiny budget).
- [ ] Background thoughts (DMN monologue) confirmed still 100% local.
- [ ] Sensitive sleep cells confirmed still local (unless explicitly opted in).
- [ ] Cost log shows expected $/session.

## Open questions for later
- Do we want motor → cloud (quality + caching win) given it sees file contents? Revisit after seeing local-14B tool-use quality.
- One global toggle (Option B) vs per-cell (Option A)?
- Session vs daily budget; hard USD cap?
- Eval impact: re-run the persona/eval traces after any cloud move to confirm no behavioral regression.

---
---

# IMPLEMENTATION SPEC (2026-05-30) — design-first, awaiting build approval

This section supersedes the rough sketch above where they differ. It is grounded in a
read of the actual code. **User decisions locked:** motor→cloud Sonnet 4.6 = YES; DMN
planner + sleep cells stay LOCAL 14B; build the local-redaction-before-cloud layer.
User asked for **design only, no code yet** — this is that design.

## CRITICAL FINDING: the redaction layer already exists and is already ON

`brain/security.py` → `PseudonymizationGateway`. `BRAIN_EGRESS_MODE` **defaults to
`pseudonymize`** (NOT `off` — the `.env.example` comment claiming `off` is stale and
should be fixed). Modes: `pseudonymize` (reversible ⟨type_n⟩ tokens, default) | `redact`
(irreversible [REDACTED]) | `block` (no memory context to cloud) | `off` (dev passthrough).

How it works today:
- Reversible, session-scoped vault: real value ↔ stable token (`⟨person_1⟩`, `⟨email_1⟩`…).
  Same real value → same token within a session, so the cloud model can still reason about
  associations without seeing real data.
- Regex PII patterns (ssn, card, email, phone, url, zip) + known-entity names passed in.
- `pseudonymize(text, known_entities) -> (text, count)` on the way out;
  `depseudonymize(text) -> text` swaps tokens back in the cloud response.

Where it's wired (the important part): **`brain/session_turn.py` ~L483–555**, the
*interactive turn* path. It pseudonymizes memory (schema, episodes, recent_thoughts,
core.self, core.user), user_input, parietal_context, speaker_name, affect.appraisal
BEFORE the frontal drafters (which can be cloud cells), then `depseudonymize(response)`
on the way back (L553). Constructed in `session_setup.py:136` as `self._egress`.

### What this means for the plan
1. The user's "strip personal info before cloud" idea is **partially built** — for the
   *interactive turn → frontal drafters* path. It is NOT applied in `model_router.py`,
   so any cell that calls the router on a path OTHER than session_turn (motor, DMN,
   sleep, metacognition) gets NO pseudonymization today.
2. Motor→cloud is exactly such a path. **If we route motor to cloud, motor's prompts
   (which include file contents, goals, tool output) would cross to Anthropic with NO
   redaction** unless we add it. This is the core privacy work to do.

## Decision the user still needs to make (before build)

The existing gateway is a regex+known-entity matcher, NOT an LLM. The user's phrasing was
"use 14b to strip personal info." Two designs:

- **Design R1 — extend the existing gateway (regex/entity), no 14B.** Add a router-level
  hook so every cloud-bound call is pseudonymized. Fast (no extra LLM call), deterministic,
  already-proven code. Weakness: regex misses unstructured PII ("my sister Jane in
  Portland is sick"). Best paired with feeding known_entities from the schema.
- **Design R2 — add a 14B redaction pass (the user's literal idea).** Before a cloud call,
  run the prompt through local qwen2.5:14b with a "replace personal/identifying info with
  neutral placeholders" instruction. Catches unstructured PII R1 misses. Cost: +1 local
  14B call (~1–3s) per cloud call, and it's non-reversible unless we also vault the
  mapping (hard to do reliably from free-text LLM output). Risk: LLM redaction is
  probabilistic — can leak or over-redact.
- **Recommended hybrid R3:** R1 gateway as the deterministic backstop on EVERY cloud call
  (router-level), PLUS optional R2 14B pass for high-sensitivity cells (sleep/memory) when
  their content is free-form. Reversible vault stays authoritative for de-tokenization;
  the 14B pass is additive scrubbing only (no reliance on it for round-trip).

→ **ASK USER: R1, R2, or R3?** This determines how much we build.

## Architecture decision: where redaction hooks in

**Move/centralize the egress hook into `ModelRouter.call()`** so it is impossible to send
an un-redacted cloud prompt regardless of which cell calls. Today it lives in session_turn,
which is why other paths are unprotected.

Proposed `ModelRouter.call()` flow (additions in **bold**):
```
model_id = resolve(model_key)
_is_cloud = startswith claude/gemini
if locality == "local" and _is_cloud: redirect→local   # existing guard
if _bg_mode and _is_cloud: budget/cap/timeout            # existing guard
**if _is_cloud and EGRESS_MODE != "off":**
**    system_prompt = egress.pseudonymize(system_prompt, known_entities)[0]**
**    messages = [pseudonymize each message content]**
**    (optionally: 14B scrub pass per Design R2/R3)**
dispatch to _call_anthropic / _call_google
**if _is_cloud and EGRESS_MODE != "off": text = egress.depseudonymize(text)**
return text
```
GOTCHA: `session_turn.py` ALREADY pseudonymizes before calling the router. If we also do it
in the router, we'd double-tokenize (⟨person_1⟩ → ⟨person_1⟩, harmless but wasteful) OR
mismatch vaults if they're different gateway instances. **Resolution:** the router needs
the SAME `_egress` instance the session uses (inject it), and pseudonymize must be
idempotent (re-running on already-tokenized text is a no-op since tokens don't match PII
patterns — VERIFY this; the known_entities replace could re-hit). Cleaner: remove the
session_turn-level calls and let the router be the single chokepoint. Needs care — the
session path pseudonymizes structured `memory`/`features` dicts, not just the flat
prompt; the router only sees the final system_prompt + messages, so moving it MAY lose the
structured-field granularity. **Investigate before moving; do not break the working
interactive path.**

## Motor → cloud Sonnet 4.6: exact changes

The 4 motor cells (`brain/clusters/motor_cortex.py`) all use `model="local-code"`,
`locality="local"`:
- `_planner` (L169) — tactical per-step tool planner
- `_strategic_planner` (L184) — upfront multi-step plan
- `_criteria_checker` (L197) — story acceptance-criteria gate
- `_verifier` (L212) — final job review

To move to cloud Sonnet:
1. Change `model="local-code"` → `model="sonnet"` on the cell(s) we promote.
2. **Drop `locality="local"`** (else the router's L150 guard force-redirects cloud→local).
   ⚠️ SECURITY IMPLICATION: dropping local-only means the egress hook (above) MUST be in
   place first, or file contents go to cloud raw. Build redaction BEFORE flipping these.
3. Wrap autonomous job execution in `enter_background_mode()`/`exit_background_mode()` so
   the existing token budget (`bg_cloud_token_budget`, default 50k) + per-call cap +
   20s timeout + local fallback apply. CHECK: does `execute_internal_job` already enter
   bg mode? grep showed background-mode comments near the planner cell — verify.
4. Which cells? RECO: promote `_strategic_planner` + `_verifier` (reasoning-heavy, low
   call-volume, big stable system prompt = great cache). Keep `_planner` (tactical, fires
   many times per job) on local OR cloud-with-tight-budget — it's the volume driver.
   `_criteria_checker` is cheap; local is fine. **ASK USER which cells.**

### Structured outputs — the one real code gap
Local cells use Ollama `format="json"` (grammar-enforced JSON). `_call_anthropic`
(model_router.py ~L289–307) does NOT pass any output-format constraint, and reads
`response.content[0].text`. Motor cells parse with `safe_json_parse` (tolerant). Options:
- **A (minimal):** rely on `safe_json_parse` + the system prompt already instructing JSON.
  Sonnet 4.6 is reliable at JSON-on-instruction. Lowest effort; risk of occasional parse
  miss (already handled by retry path in `_tactical_plan`).
- **B (robust):** add `output_config={"format":{"type":"json_schema","schema":...}}` to
  `_call_anthropic` for cells that need JSON. Per the claude-api skill, this is the
  canonical path and is supported on Sonnet 4.6. Requires plumbing a schema param through
  `ModelRouter.call()` → `_call_anthropic`. Cleaner, ~30 lines.
- RECO: **B**, scoped to a new optional `json_schema` kwarg on `call()` (default None =
  current behavior, so local + existing cloud cells are unaffected).

## Prompt caching (keep cost down — the explicit ask)
- `_call_anthropic` ALREADY sets `cache_control:{"type":"ephemeral"}` on the system prompt
  (model_router.py ~L298–301). Good — motor system prompts (`motor_prompts.py`:
  PLANNER_SYSTEM_BASE, STRATEGIC_SYSTEM, VERIFIER_SYSTEM) are large + stable = strong
  cache candidates.
- Sonnet 4.6 cache floor = **2048 tokens** (vs 4096 Haiku/Opus) — this is WHY Sonnet was
  chosen; smaller prompts still cache. Verify each promoted cell's system prompt ≥2048 tok.
- SILENT-INVALIDATOR AUDIT (must pass or caching never triggers):
  - `_PLANNER_SYSTEM_BASE.format(...)` at motor_cortex.py:268 interpolates into the system
    prompt. **If it injects per-turn/volatile data (path hints, chem state, timestamps),
    the cached prefix breaks every call.** MUST verify what `.format()` fills in; move
    volatile parts into the user message, keep the system prompt byte-stable.
  - Pseudonymization changes bytes — but stably (same entity→same token), so a stable
    prefix stays stable across calls. OK as long as the redacted system prompt is
    deterministic.
- `_call_anthropic` currently logs only input/output tokens. **ADD
  `cache_read_input_tokens` + `cache_creation_input_tokens`** to the return + obs logging
  to confirm hits and compute real cost. (~5 lines.)

## Cost guardrails
- Reuse `bg_cloud_token_budget` (raise from 50k if motor needs more headroom).
- ADD a USD estimate to logging: Sonnet 4.6 = $3/1M in, $15/1M out; cache reads ~0.1×.
  Surface per-session $ so cost is visible.
- Consider a daily cap (new setting) in addition to the per-session token budget.

## Sleep cells + DMN planner: STAY LOCAL (no change)
Per user decision. They keep `model="local-general"` (now 14B) + `locality="local"`.
The user's R2/R3 14B-scrub idea, if chosen, would apply to them ONLY IF later promoted —
not now. Document that they are intentionally excluded.

## BUILD ORDER (when approved) — redaction-first, per safety
1. **Fix the stale `.env.example` egress comment** (says `off`, default is `pseudonymize`).
2. Decide R1/R2/R3 (ASK USER).
3. Centralize egress into `ModelRouter.call()` as the single cloud chokepoint; inject the
   session's `_egress` instance; verify idempotency; verify interactive path unbroken.
   Add a test: a cloud call with PII in prompt → assert no raw PII in the dispatched
   payload (mock `_call_anthropic`, inspect args).
4. Add optional `json_schema` kwarg to `call()`/`_call_anthropic` (Design B).
5. Add cache + USD logging to `_call_anthropic`.
6. Audit `_PLANNER_SYSTEM_BASE.format()` for cache-busting volatile content; fix.
7. Flip chosen motor cells to `model="sonnet"`, drop their `locality="local"`, ensure
   bg-mode wrapping. Start with `_strategic_planner` + `_verifier`.
8. Verify (checklist below). Re-run eval traces; confirm no behavioral regression.

## VERIFICATION CHECKLIST (unchanged + additions)
- [ ] PII test: cloud call with email/name/phone in prompt → dispatched payload has tokens, not raw values.
- [ ] depseudonymize restores values in the cloud response.
- [ ] Cloud cell returns valid JSON (schema or tolerant parse).
- [ ] `cache_read_input_tokens > 0` on 2nd identical-prefix call.
- [ ] bg budget + timeout + local-fallback fire (test w/ tiny budget).
- [ ] DMN monologue still 100% local (grep logs: no claude/gemini for dmn/* cells).
- [ ] Sleep cells still local.
- [ ] Per-session USD cost logged and sane.
- [ ] Interactive turn path still works (existing session_turn pseudonymization not broken).
- [ ] Eval traces re-run, no regression.

## DECISIONS LOCKED (2026-05-30) — ready to build on approval
1. **Redaction = R3 hybrid.** Existing regex/entity `PseudonymizationGateway` runs on
   EVERY cloud call (deterministic, reversible backstop) + optional local-14B scrub pass
   for free-form sensitive content. The 14B pass is ADDITIVE only — de-tokenization still
   relies solely on the regex/entity vault, so the probabilistic 14B step can't break the
   round-trip.
2. **Motor cloud cells = `_strategic_planner` + `_verifier` only.** Tactical `_planner`
   (high per-job call volume) and `_criteria_checker` stay LOCAL on coder-14B.
3. **Cost ceiling = per-session token budget (existing) + NEW hard daily USD cap.** Build
   a daily-dollar ceiling on top of `bg_cloud_token_budget`; both must pass for a cloud
   call to proceed; fall back to local when either is hit.
4. (Egress placement — router-centralization vs parallel hook — still my call during
   build; will choose whichever keeps the interactive path provably unbroken. Leaning:
   centralize in router, inject the session's `_egress`, add the no-raw-PII test FIRST.)

## REMAINING USER GATE
User previously said "design only, no code yet." These decisions unblock the build.
**Next action: get explicit go-ahead to start coding (build order step 1 onward), or
continue holding at design.**

