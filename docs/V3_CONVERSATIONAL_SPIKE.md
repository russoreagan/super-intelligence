# Spike: eleven_v3_conversational over the Text to Dialogue WebSocket

**Goal:** decide whether the v3 branch of the TTS pipeline should move from
per-chunk HTTP streaming (`eleven_v3` / Flash 2.5) to a single Text to Dialogue
WebSocket session per utterance using `eleven_v3_conversational` (~280ms model
latency, full audio-tag support, continuous prosody).

**Non-goal:** replacing Flash 2.5 as the default. Flash stays the default and
the fallback throughout; this is an opt-in alternate branch selected by model id.

## Why now (2026-08 changelog review)

- New model `eleven_v3_conversational`: realtime v3, ~280ms, audio tags.
- New WS endpoint `wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input`:
  incremental text input, buffers ~40 chars / 8 words then emits partial audio,
  `flush` / `keep_alive` / `close_socket` controls, 20s receive timeout.
  Default model is `eleven_v3_conversational`; conversational allows exactly
  **one** registered voice per session (fine: one persona per utterance).
- One WS session = one continuous prosody stream → obsoletes, for this branch:
  `_split_sentences` chunking, `previous_text`/`next_text` stitching (never
  worked on v3 anyway), the 20ms inter-chunk silence, and one-HTTP-request-per-chunk.
- Still true (verified in docs 2026-08-26): PVC voices degraded on v3 (keep the
  voice-picker filtering in `ui/server.py`); stability is still discrete
  Creative/Natural/Robust (keep `_snap_v3_stability`); style/speed params still
  not honored (speed via audio tags); `voice_settings` accepted **only on the
  WS session's first message** — no per-chunk VoiceSettings on this path.

## Open questions the spike must answer

| # | Question | How measured |
|---|----------|--------------|
| Q1 | Real TTFA (text sent → first PCM byte) vs Flash's current per-utterance TTFA | harness timings + existing `TTS first audio chunk in %.2fs` log line |
| Q2 | Expressiveness with our persona voices (IVC/premade, **not** PVC) — do `emotion_presets.py` tags land? | A/B listening: same 10 scripted utterances × {flash, v3 http, v3c ws} |
| Q3 | Short interjections ("Sure.", "On it.") — does the 40-char/8-word buffer delay or distort them? Does immediate `flush` fix it? | harness case set |
| Q4 | Output formats on the WS — is `pcm_22050` available? (local playback assumes it) If only `pcm_24000`, reuse `_pcm_resample`. | harness |
| Q5 | Cost per character vs Flash (Flash is advertised 50% cheaper; v3c pricing not in public docs) | dashboard usage after a metered harness run |
| Q6 | Long utterances (multi-paragraph DMN musings) — early termination? HTTP dialogue caps at 2,000 chars/request; does the WS stream past that cleanly? | harness with 3–5k char inputs |
| Q7 | Does the Python SDK (>=2.56) expose a dialogue-WS client, or do we speak raw WS? (SDKs added "multi-context text-to-dialogue WebSocket message types" 2026-07-13; we already do raw WS for Deepgram, so raw is acceptable) | read SDK source |
| Q8 | Session-pool concurrency: dialogue WS sessions come from a separate pool. How many does our plan tier get? Hosted multi-tenant implication. | dashboard Analytics → TTD Websocket Sessions during harness run |
| Q9 | Failure modes: mid-stream socket drop, 20s idle timeout, `too_many_concurrent_requests` | harness fault injection |

## Phase 0 — RESULTS (run 2026-08-26, voice: the-analyst/Jarnathan)

Harness: `scripts/spike_v3c_ws.py`. All 11 runs clean, zero errors.

| Q | Verdict | Evidence |
|---|---------|----------|
| Q1 TTFA | **PASS — parity with Flash** | v3c WS 0.28–0.41s vs Flash HTTP 0.24–0.52s (text sent → first PCM) |
| Q3 short replies | **PASS with `flush`** | "On it." without flush: audio only at socket close (buffered 6s+). With flush: 0.28s. → Phase 1 must always send `flush` after the final text. |
| Q4 formats | **PASS** | `pcm_22050` accepted natively on the WS — no resample needed |
| Q5 cost | **PASS — same rate as Flash** | controlled 960-char run billed 480 on BOTH v3c and Flash (0.5 credits/char). The v3 1x-rate objection does not apply to v3_conversational. |
| Q6 long-form | **PASS** | 3,832 chars in one session → 247s audio, no early termination. Generation ≈ 4.9× realtime (Flash ≈ 30×) — slower to finish (wall 50s vs 7.4s) but always ahead of playback, and TTFA unaffected. |
| Q7 SDK | raw WS | `websockets` 15 direct; protocol is 4 message types. SDK client unnecessary. |
| Q2 expressiveness | **needs ears** | A/B WAV pairs generated (medium_tags, mood_span) — v3c output runs ~15% longer for the same text (expressive pacing/pauses) |
| Q8 session pool | open | check dashboard Analytics → "TTD Websocket Sessions" during load |
| Q9 fault injection | open | mid-stream drop / idle-timeout / concurrency-rejection not yet exercised — fold into Phase 1 tests |

**Phase 0 exit gate: PASSED.** Proceed to Phase 1 pending the listening check (Q2).

## Phase 0 — standalone harness (no brain changes)

`scripts/spike_v3c_ws.py` (new, standalone, run manually):

1. Connect to `wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input`
   with `xi-api-key`, first message: `{voices: [<persona voice id>],
   voice_settings: {stability: <snapped>, similarity_boost: 0.80,
   use_speaker_boost: true}}` + requested `output_format`.
2. Case set (each written to a WAV in the scratch dir + timing JSON):
   - short interjection (< 40 chars) with and without `flush`
   - medium utterance with inline audio tags from `emotion_presets.py`
     (`[warmly]`, `[excited]`, `[sighs]`, `[drawn out]`…)
   - mood-span utterance run through the real `_parse_mood_markup` output
   - 3–5k char long-form text
   - incremental feed (simulate LLM cadence: 80-char sends every 150ms) vs
     one-shot send + flush
3. Record per case: TTFA, total wall time, audio duration, bytes, chars billed.
4. Run the identical case set through the current Flash path
   (`text_to_speech.stream`, per-chunk) for the A/B baseline.

**Exit gate for Phase 0:** Q1–Q6 answered. If TTFA > ~1.5× Flash's, tags don't
land on our voices, or cost is prohibitive → stop, write findings to memory,
keep Flash. Otherwise proceed.

## Phase 1 — DONE (2026-08-26, uncommitted)

Implemented as planned, verified by a live end-to-end smoke through the real
`_speak` path: first audio **0.44s** over the dialogue WS, correct model
logged, single-session transport.

- `brain/pns.py`: new `_stream_dialogue_ws()` (one socket per utterance,
  mandatory `flush`, base64 → existing `audio_queue`, interrupt closes the
  socket, websockets v13/v14 header-kwarg compat); `_use_dialogue_ws` transport
  flag; all `model_id == "eleven_v3"` gates widened to `startswith("eleven_v3")`;
  v3c skips `_split_sentences` (single chunk); producer attempts WS first and
  on pre-first-audio failure re-splits and falls back to per-chunk HTTP
  `eleven_v3` (never a silent turn; mid-stream failure surfaces `tts_error`
  and never re-synthesizes).
- Kill switch `BRAIN_TTS_DIALOGUE_WS=0` downgrades v3c → `eleven_v3` HTTP.
- `brain/ui/server.py`: PVC voice filtering now covers all `eleven_v3*` models.
- `pyproject.toml` + `uv.lock`: `websockets>=13,<16` promoted to a direct dep.
- `docs/ENV_VARS.md`: new row + updated `ELEVENLABS_MODEL_ID` row.
- Tests: `tests/test_v3c_dialogue_ws.py` (5) — transport selection, frame
  order incl. flush, HTTP fallback on WS failure, kill switch, Flash default
  untouched, barge-in stops the drain. TTS regression subset: 111 passed.

Opt-in remains `ELEVENLABS_MODEL_ID=eleven_v3_conversational`; Flash 2.5 stays
the default.

## Phase 1 — integrate as a `_speak` branch in `brain/pns.py` (original plan)

Model selection stays env/settings-driven: `ELEVENLABS_MODEL_ID=eleven_v3_conversational`
(add alias `v3c` next to `flash`/`v3` in `api/audio.py:_MODEL_ALIASES`).
Branch condition: `model_id.startswith("eleven_v3")` routes text shaping to the
existing v3 path; `model_id == "eleven_v3_conversational"` (or a
`BRAIN_TTS_DIALOGUE_WS=1` guard) selects WS transport.

Reuse unchanged:
- Text shaping: `_v3_audio_tag_from_affect` → `_parse_mood_markup` →
  `_shape_for_v3` (the v3 branch already produces tag-inlined `tts_text`).
- Chemistry baseline: `_blend_voice_from_chem` → `_snap_v3_stability` → session
  `voice_settings` in the WS first message.
- The consumer loop, watchdog (`BRAIN_TTS_CHUNK_TIMEOUT`), browser WS routing,
  mute/cost gates, `meta.mood_expression` events.

Replace in the producer (WS variant):
- Instead of iterating `chunked` with one HTTP stream per chunk: open the
  dialogue WS, send the whole shaped utterance (single `inputs` entry, one
  voice), then `flush` + `close_socket`; pump received base64 audio into the
  existing `audio_queue`. No inter-chunk silence, no stitching, no
  `_split_sentences` on this branch.
- Barge-in: `self._interrupt_event` → close the socket (replaces breaking out
  of the chunk loop).
- Failure fallback: any WS setup/mid-stream error before first audio →
  log + fall back to the existing per-chunk HTTP v3 path for that utterance
  (never a silent no-audio turn); after first audio, abort as today
  (`_emit_tts_error`). Circuit breaker: `BRAIN_TTS_MAX_CHUNK_FAILURES`
  consecutive WS failures flips the session back to Flash until restart.

Tests (extend `tests/test_emotion_expression.py` pattern, WS mocked):
- shaped text reaches the WS payload with tags inline and markup stripped from
  display text
- interrupt closes the socket
- WS failure falls back to HTTP path
- settings flip (`ELEVENLABS_MODEL_ID`) actually changes the transport
  (per the settings-schema-whitelist lesson: test that flipping the setting
  changes behavior).

## Phase 1.5 — hardening (2026-08-26, post-review)

The holistic voice-pipeline review after Flux + v3c landed found that both
upgrades had finished the local server-mic path and left the hosted engine-API
path behind. Fixed in the same pass:

- **Q9 fault injection — CLOSED.** A pre-first-audio WS failure fell back to
  HTTP per utterance and retried the WS on the next one, so a structural
  failure (full session pool, network stall) paid a 10s connect timeout of dead
  air *every reply*. Now: `BRAIN_TTS_DIALOGUE_WS_OPEN_TIMEOUT` (default 3s) and
  a circuit breaker, `BRAIN_TTS_DIALOGUE_WS_MAX_FAILURES` (default 3), that
  pins the process to HTTP `eleven_v3` and surfaces a `tts_error` rather than
  degrading silently. A delivered stream resets the counter, so a transient
  blip doesn't accumulate. Mid-stream drop and idle-timeout behaviour were
  already correct (never re-synthesize after first audio).
- **`brain/api/audio.py` was the last `model_id == "eleven_v3"` exact match**
  the Phase 1 `startswith` sweep missed. Since `ELEVENLABS_MODEL_ID` is shared
  with the engine API, flipping a tenant to `eleven_v3_conversational` sent
  `style`/`speed` (422 on v3) *and* an unroutable model id to
  `/v1/text-to-speech/stream` — all partner audio died. Fixed, plus the `v3c`
  alias Phase 2 called for and an explicit v3c→v3 downgrade on that transport.

**Q8 (dialogue-WS session pool) remains open** — it needs a dashboard read
under load (Analytics → TTD WebSocket Sessions). Until Q8 and Q2 (the listening
check) are closed, `eleven_v3_conversational` stays opt-in and Flash 2.5 stays
the default.

### Deferred: Deepgram eager end-of-turn

Flux's `eager_eot_threshold` enables `EagerEndOfTurn` / `TurnResumed`, which let
the agent start drafting before the turn is confirmed and cancel when the user
resumes — Deepgram puts it at hundreds of ms of end-to-end latency, at a cost of
**+50–70% LLM calls**. Deliberately not built (2026-08-26): the hosted cost
baseline isn't known yet. Wiring point when it is: set `eager_eot_threshold` in
`connect_kwargs` in `brain/api/stt_live.py` behind
`BRAIN_STT_EAGER_EOT_THRESHOLD` (unset = off), then branch on the event in
`_handle_turn_info` and cancel the speculative `_run_turn` in
`brain/api/ws.py`.

## Phase 2 — surface + hosted (only after Phase 1 verified locally)

- Engine API: accept `model: v3c` alias in `POST /v1/tts` and the realtime WS
  (`api/ws.py`); `api/audio.py` maps segments → single dialogue-WS session.
- Voice picker (`ui/server.py`): keep hiding PVC voices for any
  `eleven_v3*` model (limitation confirmed still present).
- Settings UI: expose model choice per persona (existing
  `persona_voice_<slug>` pattern covers voice; model stays a tenant setting).
- Quota: chars metered identically (`audio_quota.py` counts input chars —
  unchanged).

## Deliberately out of scope

- Feeding LLM tokens straight into the WS (true streaming synthesis while the
  model is still writing). Big latency win, but `_speak` currently receives
  complete text; wiring token-level streaming touches `session_turn` and the
  mood-markup parser (tags arrive incrementally). Note as follow-up if the
  spike succeeds.
- Multi-speaker dialogue (plain `eleven_v3`, up to 10 voices) — interesting for
  Agent Salon later, not this spike.
- Scribe STT provider work (separate track, already scoped).

## Decision criteria (end of Phase 1)

Promote `eleven_v3_conversational` to the recommended "expressive" persona
setting if ALL of: TTFA ≤ 1.5× Flash on medium utterances; short interjections
clean with flush; no dropped/garbled audio across ≥ 50 consecutive utterances;
cost within budget (know the number first); barge-in latency unchanged.
Otherwise: keep Flash default, keep findings, revisit next model rev.
