# Update plan: Eleven v4 Turbo as the voice engine

**Status:** plan, 2026-09-28 (the day of ElevenLabs' v4 launch). Nothing built yet.
**Supersedes:** "Flash stays the default" in `docs/V3_CONVERSATIONAL_SPIKE.md`. That
spike's Phase 1 transport is the foundation this plan builds on.

## What changed at ElevenLabs

| | Eleven Flash v2.5 (our default) | Eleven v3 Conversational (opt-in today) | **Eleven v4 Turbo** |
|---|---|---|---|
| model_id | `eleven_flash_v2_5` | `eleven_v3_conversational` | `eleven_v4_turbo` |
| Model latency (median) | ~75 ms | ~280 ms | ~100 ms inference, ~150 ms to first speech |
| Transport | TTS HTTP / TTS WS | Text to Dialogue WS only | **Text to Dialogue WS only** (the TTS WS returns 400) |
| Expressiveness | VoiceSettings sliders only; tags read aloud | Audio tags | Audio tags + free-text direction (`[said angrily]`), follows them "more accurately than prior models" |
| PVC voices | yes | degraded | **yes, supported again** |
| Languages | 32 | 70+ | 90+, with cross-lingual voice identity |
| SSML `<break>` | yes | n/a | **disabled**, use `[pause]` / `[long pause]` |
| Voices per socket | n/a | 1 | 1 (`eleven_v4` allows 10) |
| Price | 0.5 credits/char | billed at Flash rate (measured 2026-08-26) | "same credit pricing"; API launch discount to $11/1M chars until ~2026-10-12 |

Sibling `eleven_v4` (non-turbo) is for batch and multi-speaker work. It is only
available through Text to Dialogue, not the TTS endpoints.

**Why this is more than a model-id swap.** Until now, low latency and expressiveness
were a trade-off. We picked latency (Flash) and built a whole second prosody
dialect around it: `[mood:X]` markup mapped to per-chunk VoiceSettings buckets,
sentence chunking, and `previous_text`/`next_text` stitching. The June "v3 is not
viable" ruling (see memory `project-elevenlabs-flash-migration`) rested on two
things, chunking artifacts and speed. v3c fixed the first, and v4 Turbo fixes the
second. It also brings back the one thing v3 took away, PVCs. So the
expressive-tag path becomes the only primary path, Flash becomes a fallback, and
the transport has to move to the dialogue WebSocket everywhere, including the
hosted engine API, which today has no WebSocket transport at all.

## Where we are today (code map)

- **Model choice:** env only. `ELEVENLABS_MODEL_ID` defaults to Flash, read in three
  places: `brain/pns.py:1080`, `brain/api/audio.py:151`, `brain/ui/server.py:3073`.
  Engine aliases are `flash|v3|v3c` (`api/audio.py:52`).
- **Local `_speak`:**
  - Flash uses per-chunk HTTP (`pns.py:1500-1530`) with `_split_sentences` (`:303`),
    `_mood_segmented_chunks` (`:819`), 20 ms gaps, and stitching.
  - v3c uses the dialogue WS (`_stream_dialogue_ws`, `pns.py:907`). The model id is
    **hard-coded in the URL** (`:936`), there is one socket per utterance, the whole
    text is sent in one frame, and a breaker (`:1004`) falls back to HTTP `eleven_v3`.
- **Hosted engine API** (the path that actually ships):
  - `api/audio.py` has no WS transport. `_http_model_id` (`:155`) always downgrades
    v3c to v3.
  - On that v3 path, audio tags never reach ElevenLabs. `_mood_segmented_chunks`
    strips them and nothing re-injects them, so `api_guide.md:1298` is wrong today.
- **Speech starts only after the whole turn finishes.**
  - `brain/api/ws.py:452` calls `_ws_stream_audio(text)` after the `done` frame.
  - The SSE path works the same way (`server.py:991`).
  - No reply-text token streaming exists anywhere.
- **Echo guard / barge-in** compares against the *entire* reply text
  (`_speaking_text`, `ws.py:240`; `voice_bridge.echo_containment`). A 2 s tail
  covers late echoes.
- **Voice picker** hides PVCs for `eleven_v3*` (`ui/server.py:3045-3110`).
- **Browser path** is half-duplex (`index.html:5903-5920`).

## Target behaviour

1. **One expressive dialect.**
   - Chemistry and `[mood:X]` spans become inline audio tags on a single continuous
     stream per utterance.
   - No sentence chunking, no stitching, no per-chunk slider buckets on the primary path.
2. **Speak while thinking.**
   - The dialogue socket opens when the turn *starts*, which hides the handshake
     behind LLM time.
   - Later (Phase 3), committed reply clauses feed in as they are generated, so
     first audio arrives on the first clause instead of after the whole turn.
3. **The brain knows what it actually said.**
   - `sync_alignment` gives a spoken-so-far cursor that drives the echo guard,
     barge-in truncation and captions.
4. **Same behaviour hosted and local.**
   - One shared transport module used by `pns._speak`, `api/ws.py`, `/v1/tts` and SSE.
   - The August review found both voice upgrades finished locally and left hosted
     behind. This plan avoids repeating that.
5. **PVC persona voices are allowed again** on v4.

## Phases

### Phase 0: probe (harness only, no brain changes)

Generalise `scripts/spike_v3c_ws.py` to take `--model`. Run Flash, v3c, v4t and v4
against the same scripts and persona voices.

| # | Question | Why it matters |
|---|---|---|
| P1 | TTFA for v4t: cold socket vs socket pre-opened N seconds earlier (with `keep_alive`) | Sizes the Phase 1 "open at turn start" win |
| P2 | Billing per char on v4t (dashboard, controlled 960-char run as in Q5) | Default-flip cost, before and after the launch discount |
| P3 | Which `voice_settings` v4t honours: stability continuous or discrete? style? speed? 422 or silently ignored? | Decides whether chemistry→sliders survives or becomes tags only, and whether `_snap_v3_stability` applies |
| P4 | Output formats on the WS: `pcm_22050`, `pcm_24000`, `mp3_44100_128`, `ulaw_8000`, `opus_48000_64` | Engine API promises 7 formats (`api/audio.py:37`) |
| P5 | `sync_alignment=true` payload shape and timing accuracy vs the audio | Phase 4 depends on it |
| P6 | **Mid-utterance cancel:** does a multi-context message (`context_id` / close-context, added to the SDKs 2026-07-13) work on the dialogue WS for v4t, or is barge-in close-and-reopen? Cost of reopen? | Decides between persistent sockets and a socket per turn |
| P7 | Incremental feed: clause-sized frames vs whole text, for prosody, TTFA, and a tag split across frames. Does `new_turn` reset prosody cleanly? | Phase 3 feasibility |
| P8 | Dialogue-WS session pool size on our plan (the old open Q8) | **Hard gate** for hosted multi-tenant default, since every speaking session holds one |
| P9 | PVC persona voice on v4t (listening) | Unlocks the picker change |
| P10 | Listening A/B on persona voices with tag-rich and mood-span scripts: Flash vs v4t (the old open Q2) | Product sign-off |
| P11 | Accepted language codes (pipecat found the live list differs from the published one) | Flux `multi` + persona language |
| P12 | Fault injection: mid-stream drop, 20 s idle timeout, pool-full rejection | Breaker and fallback tests |

**Exit gate:** P1 ≤ Flash TTFA + 100 ms, P2 ≤ 1× Flash rate after the discount,
P8 ≥ our expected concurrent speaking sessions, and P10 approved by ear.

### Phase 1: shared dialogue transport + default flip

- **New `brain/tts_dialogue.py`**, lifted out of `pns._stream_dialogue_ws` and
  model-parameterised (no hard-coded id). It exposes a per-utterance session object:
  - `open(voice_id, model, fmt, alignment)` can be called early and sends `keep_alive`
    every ~10 s so a long tool-using turn doesn't hit the 20 s timeout.
  - `feed(text)` for incremental text, `finish()` which sends `flush` + `close_socket`,
    and `cancel()`.
  - It is an async iterator of `(pcm, alignment)`.
  - It carries the existing breaker (`BRAIN_TTS_DIALOGUE_WS_OPEN_TIMEOUT`,
    `_MAX_FAILURES`), now per model and process-wide.
- **Callers:**
  - `pns._speak` uses the new module for any model routed to the dialogue WS
    (`eleven_v3*`, `eleven_v4*`).
  - `api/audio.py` gains a WS branch in `_segment_stream`/`synthesize`, so `/v1/tts`,
    SSE and `api/ws.py` all get v4t. `_http_model_id` stays for v3 only.
- **Open at turn start.**
  - `api/ws.py` opens the session when `_run_turn` begins (audio enabled) and feeds
    the full text at `done`.
  - `_speak` does the same from the turn loop.
  - This is a pure latency win that needs no token streaming.
- **Default flip:** `ELEVENLABS_MODEL_ID` default becomes `eleven_v4_turbo` in all three
  readers, via one shared resolver so the three can't drift again.
  - Engine aliases become `v4t` → `eleven_v4_turbo` and `v4` → `eleven_v4`, keeping
    `flash|v3|v3c`.
  - Per the no-dark-shipping rule this ships ON, and the kill switch is the existing
    `BRAIN_TTS_DIALOGUE_WS=0`.
- **Fallback chain changes:** v4t → **Flash HTTP** (tags stripped, moods to the existing
  VoiceSettings buckets), not → `eleven_v3`.
  - v4t has no HTTP twin, and Flash is the proven low-latency path.
  - The v3c → v3 downgrade stays for v3c.
- **Tests:** extend `test_v3c_dialogue_ws.py` into `test_tts_dialogue.py`, parameterised
  over v3c/v4t. Add WS-branch cases to `test_api_audio.py`, early-open + `keep_alive` +
  cancel cases, and breaker → Flash fallback.

### Phase 2: one prosody dialect (audio tags)

- On the dialogue path, `[mood:X]…[/mood]` becomes inline tags via the existing
  `_parse_mood_markup` (`pns.py:411`) on one stream.
  - `_mood_segmented_chunks` stops being a split point on this path. It stays for
    Flash, OpenAI and Google fallbacks.
- This **fixes the engine-API gap**: hosted v4t gets the tags that hosted v3 never got.
  `affect_view` keeps its `{base_tag, segments[], markup}` contract for partners.
  Segments are derived from mood spans rather than from synth chunks, so the shape is unchanged.
- Chemistry → base tag uses `_v3_audio_tag_from_affect` (`pns.py:244`). If P3 shows
  continuous stability is honoured, also send chemistry-driven stability in the first
  message. This is the "continuous from the neuromod vector" step noted in June.
- `_shape_for_v3` em-dash breaths become `[pause]` where they were meant as pauses.
  Nothing emits SSML `<break>` today (checked), so the v4 SSML removal costs nothing.
- The Flash VoiceSettings buckets (`pns.py:622-658`) are relabelled fallback-only.
  Keep them and their tests.
- **Optional, gated on P10:** let the reply writer use v4's free-text direction tags
  (e.g. `[said quietly, half to herself]`) in addition to the `EMOTION_TAG_MAP` vocabulary.
  - Strip them on fallback, and never pass them into Flash.

### Phase 3: speak while thinking (stream reply text into the socket)

This is the biggest latency change and the biggest design change. Today no reply
text streams anywhere.

- **Add a per-turn speech queue.** The *final user-facing generation* emits committed
  clauses (clause or sentence boundary, markup parsed incrementally so a
  `[mood:X]` span or tag is never split) into the queue. `tts_dialogue.feed()`
  consumes it.
- **Scope:**
  - Only the final reply generation streams. Planner, tool and critic calls never do.
  - DMN proactive speech and one-shot `/v1/tts` keep whole-text feeding.
- **Must-resolve before building:** find the last point where reply text can still be
  rewritten or withheld: the frontal critic, answer-only guards, the `pending`
  confirmation, the lane gates, and the proactive-voice gate. Any gate that can
  change or suppress the reply *after* generation either moves before generation or
  holds the speech queue until it passes.
  - **Decision for Russ:** hold speech until gates pass (safe, smaller win), or move
    gates earlier (full win, larger refactor). See Decisions.
- **Provider support:** the final-reply call has to stream on every provider route in
  `model_router.py` (Anthropic, OpenAI, pod). Today only the pod HTTP path streams
  (`model_router.py:2804`).
- **Wire events:** new `speech_start` on the engine WS and SSE. The `done` text frame
  becomes independent of audio completion (it already is on the wire; this just
  documents it).
- **Tests:** incremental markup parser (tags split across tokens), gate-holds-speech,
  cancel mid-feed, and the answer-only turn never speaking.

### Phase 4: alignment-aware turn-taking

With `sync_alignment=true`, keep a **spoken-so-far cursor** per utterance
(characters whose audio has actually been played, not just generated). Uses:

- **Echo guard:** `echo_containment` compares against a sliding window around the
  cursor instead of the whole reply. That is sharper on long replies and needs less of
  the 2 s `_ECHO_TAIL_S` guesswork (`ws.py:40`), since the tail is computed from the
  actual end of audio.
- **Barge-in truth:** on interrupt, the transcript, episode memory and
  `session_turn` history record only what the user heard, plus an "interrupted at …"
  marker. Today the brain believes it delivered the whole reply.
- **Captions / word highlighting:** `audio_chunk` frames carry alignment for partners
  (additive field). The local UI can highlight words.
- **Follow-up (not in this plan):** browser full-duplex. It is half-duplex today and
  the cursor-based guard is what would make that safe.

### Phase 5: surface, docs, retire

- **Voice picker:** stop hiding PVCs for `eleven_v4*`; keep the filter for `eleven_v3*`
  (`ui/server.py:3093`).
- **Settings UI model picker** (the unshipped v3c Phase 2 item): expose the model
  choice next to `tts_provider`. The per-org override is a candidate, since orgs are
  environments now.
- **Multilingual:** pass the persona or detected language when P11 confirms a param
  exists. Flux `multi` is already wired on the STT side.
- **Docs:**
  - `docs/ENV_VARS.md` (fix the stale line refs too), `.env.example:96-108`.
  - `api_guide.md` `/v1/tts`: new aliases, and correct the "v3 via inline tags" claim.
  - `docs/SYSTEMS.md` §8.11 / §8.12 as a snapshot, not a changelog.
  - Mark `V3_CONVERSATIONAL_SPIKE.md` superseded.
- **Retire:** fold the v3c-specific branch into the generic dialogue path. Keep
  `eleven_v3` and Flash working as explicit choices.

## Risks

- **v4t is WebSocket-only.** Any dialogue-WS outage or pool exhaustion puts all
  primary audio onto the Flash fallback. That fallback therefore has to stay tested
  and healthy (P12, breaker tests), and the breaker must be per-process and cheap
  (3 s open timeout, as today).
- **Session pool (P8).** Opening early and holding through tool-using turns increases
  concurrent sessions. If the pool is small, open at `done` instead of turn start, and
  add a gateway-level session budget.
- **Cost after the launch discount** (P2). "Same credit pricing" is the vendor's claim;
  measure it.
- **Tags on fallback:** every non-v4/v3 path must strip tags, or Flash reads
  `[laughs]` aloud. The existing `_strip_all_tags` covers this; add a regression test
  per fallback.
- **Phase 3 gate ordering** is the one place this could speak something the brain would
  have withheld. The default is "hold until gates pass".
- **Partner contract:** `affect` segments and the `audio_chunk` shape must not change
  (additive fields only).

## Decisions for Russ

1. **Default flip timing.** Recommended: flip immediately after the Phase 0 exit gate,
   shipped ON with the kill switch (no-dark rule). The alternative is keeping v4t
   opt-in like v3c. That is what left v3c's Q2/Q8 open for a month.
2. **Phase 3 gate policy.** Recommended to start with "hold speech until post-generation
   gates pass", then move gates earlier one by one where that is safe.
3. **Fallback target.** Recommended: Flash HTTP. The alternative, `eleven_v3` HTTP, keeps
   tags but is slow and bills at 1×.

## Sources

- ElevenLabs launch post: https://elevenlabs.io/blog/eleven-v4
- v4 product page: https://elevenlabs.io/v4
- Models reference: https://elevenlabs.io/docs/overview/models
- Text to Dialogue WebSocket: https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tdd
- LiveKit agents PR (v4 rejected on the TTS WS, routed through dialogue): https://github.com/livekit/agents/pull/7516
- Pipecat PR (v4t default, live language list differs): https://github.com/pipecat-ai/pipecat/pull/5959
