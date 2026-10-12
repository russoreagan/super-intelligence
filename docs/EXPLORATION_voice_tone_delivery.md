# Voice tone vs mood chemistry: exploration (2026-10-11)

> **Decision (Russ, 2026-10-12):** go with the felt/expressed split. Pride and the other self-directed appraisals keep shaping chemistry but never set how the agent talks. The existing deliberate-expression path is extended: the drafter opens a reply with a bare `[mood:X]`. The console shows felt and expressed separately; partners see one layer. Built in `brain/expressed_emotion.py` (see `docs/SYSTEMS.md` §8.11). The prototype and replay script referenced below stayed on the exploration branch and were not merged; the embedding tone reader was not pursued.
>
> **Found while building:** `/v1/tts` ignored the `base_tag` in a replayed affect block, so Haven's stored tags (including the off ones in this sample) were never spoken. That is fixed in the same change.

Branch `explore/voice-tone-delivery`. Nothing is wired and production behaviour is unchanged. This is waiting on a decision.

**Question.** How can the tone a reply actually has drive the voice tag, while keeping the full range of tags? Is the problem the chemistry, or how it is read into a tag?

**Answer.** It is the reading. The_empath's chemistry behaved sensibly through the session. All six bad tags came from three interpretation faults, and none of them looks at the reply that gets spoken.

## Evidence

The sample is the Haven session of 2026-10-11 (19:13 to 19:34 UTC): 24 listener replies. Three sources were joined by turn time:

- the stored `affect.base_tag` from Haven's `therapy_messages`;
- the logged turn emotion and metacognition appraisals from Railway;
- the per-turn neuromod snapshots from `episodes`, in the Elyceum project's Supabase.

| Off tag | Count | Root cause | Chemistry or interpretation? |
|---|---|---|---|
| `[proudly]` | 4 | Metacognition's **intrinsic-pride** appraisal of the *previous* reply | Neither. It is a self-appraisal leaking into the voice |
| `[excited]` | 1 | Effective DA crossed the 0.6 bucket edge with ACh high, landing in the `excitement` cell | Interpretation (bucket edge) |
| `[urgently]` | 1 | `engaged` has no tag, so it fell through to PNS's raw neuromod threshold fallback | Interpretation (map gap) |

### 1. `[proudly]` is not chemistry

`brain/metacognition.py:408-424` (rule 4) returns `proud` whenever the selected draft's `overall` score is above `self_standard_gate` (0.85). Every scored turn in the session was between 0.90 and 0.95, so pride fired every time it was off cooldown (9 appraisals).

- It is published at the end of turn N (`:495`) and drained by the hypothalamus as turn N+1's emotion (`brain/clusters/hypothalamus.py:612-628`).
- The payload says `ttl_turns: 1`, but nothing reads that field. The message expires on the bus default **30 s wall clock** (`brain/bus.py:21,61`).
- So it landed exactly when Russ replied within about 30 s. That happened 4 of 4 times (19:14:16, 19:15:19, 19:20:06, 19:21:09). Every gap longer than 30 s dropped it.
- In each of those four turns, chemistry alone read `connected`, `connected`, `curious`, `curious` (replayed with `brain/clusters/hypothalamus.py` math), which map to `[warmly]`, `[warmly]`, `[curious]`, `[curious]`.

The other side of the same fault: **`sympathetic` fired 3 times (user "struggling") and never landed.** It is computed from the current turn's `user_emotion`, applied one turn late, and every one of those gaps was over 30 s. The appropriate appraisal is dropped, and whether the inappropriate one lands depends on how fast the user replies.

There is a secondary fault: `refresh_emotion()` after recall affect (`brain/session_turn.py:1696-1703`) re-derives the label without the override and overwrites it. So an override also depends on whether recall carried affect. The 19:23:58 `excitement` turn had a pride override inside its TTL, so this is the likely reason it was lost there.

### 2. `[excited]` is a bucket edge, not bad chemistry

The_empath's effective DA at rest is about 0.585: raw 0.45, plus 5HT×0.12, OXT×0.05 and AEA×0.04 lifts. The "high" bucket starts at 0.60. Any reward bump crosses it.

With the empath's high attention (ACh 0.6 to 0.9), the label flickers between `curious` (mid,low,high,mid) and `excitement` (high,low,high,mid). The replay shows that flip on 19:23:28, 19:23:58 and 19:24:43.

The chemistry trajectory itself was right for a listener:

- DA fell when the user got frustrated (somber, `[softly]`).
- ACh stayed high (curious).
- GABA climbed from 0.12 to 0.49, and Glu from 0.23 to 0.54, as the conversation got heavier.

That is an empath growing concerned. The coarse four-channel hard-bucket table is what turns "warm, attentive, slightly rewarded" into "excitement".

### 3. `[urgently]`: 11 labels have no tag

These labels can be emitted but have no entry and no tag-bearing feeling-wheel ancestor:

`engaged`, `composed`, `settled`, `stirred`, `cautious-warm`, `alert-curious`, `scattered`, `concerned`, `down`, `wary`

They fall to `PNS._v3_audio_tag_from_affect`'s raw-neuromod thresholds (`brain/pns.py:288-319`). In this session `engaged` voiced as `[curious]`, `[softly]`, natural, and `[urgently]`: four tags for one label. The `[urgently]` came from Glu > 0.55 with GABA > 0.35.

Two further labels inherit the wrong tag through the hierarchy. `eased` and `serene` become `[happy]`, because the empty `peaceful` entry is skipped and the lookup falls through to the `happy` core.

### Other things checked

- **Per-end-user chemistry** exists (`brain/client_chem.py`, keyed `persona:end_user_id`, bound in `brain/session_turn.py:1005-1028`). Post-draft rewards carry into the next turn, as CONSTITUTION's two-phase rule intends. It was not a driver here; the per-user pair tracked the conversation plausibly.
- **Lite brains** never instantiate metacognition (`brain/session_setup.py:993-1003`). The pride leak and the dropped sympathetic appraisal only affect full brains.
- **The resting chemistry** (`brain/persona_chem.py` "The Empath") needs **no change**. Lowering DA to move off the 0.6 edge would change the felt state the drafter sees everywhere, to fix a voice-mapping problem.

## Options

The latencies below are measured on this sample. "Fixed" counts the 6 off tags; "churn" counts changes to the 18 tags Russ found fine.

| Option | Cost | Latency | Fidelity | Notes |
|---|---|---|---|---|
| Re-derive emotion after drafting | free | 0 | **low** | It is not text-aware. Post-draft chemistry is reward, so this would add pride/DA bumps, and it would break CONSTITUTION's two-phase rule ("post-draft updates must never re-color the CURRENT response"). Rejected. |
| Blend the appraisal into this turn + display rules (V1) | free | 0 | medium: **5/6 fixed**, 5 churn | Other-directed appraisals (sympathetic) use the current `user_emotion`, which is known pre-draft. Self-directed ones (proud, embarrassed, relieved) still set `affect["emotion"]` but don't pick the voice. It can't fix `[excited]`, because that is the felt state. |
| Richer chemistry → tag map | free | 0 | fixes `[urgently]` only | Fill the 11 gaps and fix `eased`/`serene`. Required under any option. More v3 tags (`[reassuringly]`, `[tenderly]`) would need an ear test first. |
| Cheap classifier: embedding exemplar bank, seeds only (V2) | 1 local nomic embed per sentence (~15-25 ms each, ~100 ms per reply) | ~0.1 s | **poor as seeded** | It reads topic, not tone ("work troubles" scored as `firm`, frustration as `grieving`). It is only viable once an LLM teacher has grown the bank (the `intent_detector.py` pattern). |
| LLM names the delivery family (V3: Haiku reader as proxy for critic/drafter naming) | ~430 input tokens per turn as a separate call; ~5 output tokens if folded into the drafter or critic | 0.87 s p50 as a separate call; **0** if the drafter or critic emits it | high: **6/6 fixed**, 1 churn | Folded into the drafter, it costs no added latency and covers every turn. The critic only runs with 2 or more drafts and the predictor can skip it, so coverage there is partial. |
| **V4: display rules + text gate (recommended)** | as above | 0 if drafter-named | **6/6 fixed**, 3 churn (all defensible) | See below. |

### Before / after (stored vs V4)

| Time | Reply opens | Logged | Stored | V4 |
|---|---|---|---|---|
| 19:14:16 | "Good to hear — and yeah…" | proud | `[proudly]` | `[warmly]` |
| 19:15:19 | "I hear you — that's a real weight…" | proud | `[proudly]` | `[warmly]` |
| 19:20:06 | "That's a key thing to know…" | proud | `[proudly]` | `[curious]` |
| 19:21:09 | "That's a big thing to notice…" | proud | `[proudly]` | `[curious]` |
| 19:23:58 | "That's it — both…" | excitement | `[excited]` | `[curious]` |
| 19:32:29 | "That's a real specific thing…" | engaged | `[urgently]` | `[curious]` |
| 19:28:12 | "Right — so you're caught…" | engaged | `[softly]` | natural *(churn)* |
| 19:28:59 | "Yeah, so you're not fighting…" | engaged | natural | `[curious]` *(churn)* |
| 19:33:43 | "I think I'm hearing something important…" | curious-uncertain | `[curious]` | `[gently]` *(churn: same-turn sympathetic)* |

The other 15 replies are unchanged (`[warmly]` ×3, `[softly]` ×3, `[curious]` ×9).

## Recommendation: change the interpretation, not the chemistry

Split the **felt** emotion from the **expressed** one. Psychology calls this step display rules. The brain feels what the chemistry and appraisals say, and the voice expresses the closest part of that state which the reply actually supports.

1. **Felt label for voice.** Apply other-directed appraisals (sympathetic) on the *same* turn. Self-directed appraisals (proud, embarrassed, relieved) keep driving `affect["emotion"]`, so the drafter, memory, rewards and the mood API are unchanged, but they don't choose the voice on their own. Fix the override delivery while there: honour `ttl_turns` instead of 30 s wall clock, and keep an override through the post-recall refresh.
2. **Complete map.** Every emittable label resolves explicitly, so the PNS threshold fallback is never reached from a named label.
3. **Text gate.** The drafter emits a delivery *family* for the whole reply (comforting / affirming / inquiring / reflective / celebrating / playful / firm / grieving / uneasy).
   - The resolver keeps the felt tag when that family supports it.
   - Otherwise it picks the supported tag nearest the felt PAD state (`compute_affect_dims`).
   - Text decides the *kind* of delivery; chemistry still decides the *flavour* within it.
   - **The full range stays.** Every one of the ~30 tags belongs to some family (enforced by `test_full_tag_range_is_reachable`). `[proudly]` and `[excited]` still voice when the reply celebrates (`test_pride_voiced_when_reply_celebrates`).
   - Fallback when the drafter omits the family: no gate (felt tag), as today. Later, an embedding bank taught by these drafter labels can fill that gap at ~0.1 s, so the LLM stays off the critical path.

### What a change would touch

- `brain/delivery_tone.py`: promote the prototype (families, resolver, `LABEL_TAG_FILL`). The fill should probably fold into `EMOTION_TAG_MAP` in `brain/emotion_presets.py`.
- `brain/api/audio.py` `affect_view`: `base_tag` comes from `resolve_delivery(...)` instead of `PNS._v3_audio_tag_from_affect`. This is the single API choke point for SSE, WS (`brain/api/_affect.py`) and `/v1/tts` callers that pass a stored affect.
- `brain/pns.py` `_speak` path (local voice), so owner voice and API voice agree.
- `brain/clusters/frontal.py` `_build_drafter_prompt` (~3341-3390): ask for a leading `[tone:<family>]`, and strip it like `[mood:X]` everywhere (`PNS._strip_all_tags`).
- `brain/metacognition.py` and `brain/clusters/hypothalamus.py`: same-turn sympathetic, self- vs other-directed split, and turn-count TTL.
- `brain/session_turn.py:1696-1703`: keep an override through the refresh.
- A settings kill switch (default ON, per the no-dark-shipping rule) and a test for the off state.
- Haven: no change. `/api/therapy/speak` passes `affect` through, and the new `base_tag` arrives in the same field.

**Open questions for Russ**

- Is a drafter-emitted family acceptable, or should it live in the critic, with partial coverage?
- Should `[confidently]` stay in the `reflective` family?
- Ear-test new v3 tags before adding them, or after?

## Reproduce

```bash
PYTHONPATH=. .venv/bin/python scripts/replay_delivery_tone.py <sample.json> --llm
```

The sample JSON is not committed because it contains a real conversation. It was built from the three sources above. Each row holds `turn, t, user, reply, stored_tag, emotion, user_emotion, nm{DA,GABA,ACh,Glu,NE}`. The embedding read needs `ollama serve` with `nomic-embed-text`.
