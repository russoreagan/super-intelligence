# Super Intelligence App

A biologically-inspired multi-agent system whose architecture mirrors the human brain. Clusters of switch neurons (cheap, deterministic) and integrator agents (LLM, expensive) produce conversational behaviour. Intelligence emerges from wiring, modulation, and memory — not from a single orchestrator.

See [PLAN.md](PLAN.md) for the original design doc and [brain/CONSTITUTION.md](brain/CONSTITUTION.md) for the philosophical commitments.

## Quick start

```bash
# One-shot launch (Ollama + brain + browser UI with all features)
./start.sh
```

Then visit http://localhost:8765.

The first run will pull Ollama models (`qwen2.5:7b`, `nomic-embed-text`) and may take a few minutes. Subsequent runs reuse cached models.

### Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| [`uv`](https://github.com/astral-sh/uv) | Python env + dep manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Ollama](https://ollama.com) | Local LLM + embeddings | `brew install ollama` |
| API keys | Cloud LLM, voice | See `.env.example` |

Copy `.env.example` → `.env` and fill in the keys you have. Anthropic + Google are needed for the cloud integrators; Deepgram + ElevenLabs only when voice mode is on; Langfuse is optional (the local decisions log works without it).

## Launch modes

`start.sh` takes a `FEATURES` env-var preset:

```bash
./start.sh                       # full stack (default)
FEATURES=standard ./start.sh     # UI + DMN + metacognition + motor (no voice)
FEATURES=minimal ./start.sh      # text-only UI
FEATURES=custom BRAIN_EXTRA_ARGS="--ui --voice" ./start.sh
```

Full stack enables:
- `--ui` — browser at :8765
- `--voice` — Deepgram streaming mic + ElevenLabs TTS
- `--ears` — auditory cortex (speaker enrollment, prosody, song fingerprint)
- `--dmn` — Default Mode Network: idle thinking every 15 s
- `--metacognition` — self-monitoring cell: reflects on behaviour every 30 s
- `--motor` — tool use (file I/O + shell commands, sandboxed to `BRAIN_MOTOR_PATHS`)

You can also run `brain.run` directly with any subset of those flags. CLI flags and `BRAIN_*` env vars are equivalent.

## Architecture at a glance

```
PNS ──► sensory.text ──► Temporal ──► Hypothalamus ──► Frontal ──► Brainstem ──► PNS
                       │             │              │           (articulation)
                       └► Hippocampus│              │
                                     └► Parietal ───┘
```

Each cluster has:
- **Switch neurons** (`brain/neuron.py`) — deterministic code, cheap, the connective tissue. ~80% excitatory, ~20% inhibitory.
- **Integrator cells** (`brain/cell.py`) — LLM-backed, fire only at convergence zones where reasoning genuinely needs to happen.
- **Predictor** (`brain/predictor.py`) — anticipates the cluster's output; integrators stay asleep on low surprise. **Emotion-aware veto** forces integrators awake when the entity or user is in a non-routine emotional state.
- **Hebbian edge weights** (`brain/wiring.py`) — connections between cells/clusters carry weights nudged by outcome at sleep consolidation. Weighted routing decides which drafters fire, which temporal switches evaluate first, and how memory recall fans out.

Long-term state lives in `second_brain/`:
- `schema/self.md`, `schema/user.md` — human-readable, hand-editable
- `episodes/episodes.lance/` — vector-indexed turn summaries
- `wiring.json` — Hebbian edge weights (persists across sessions)
- `wiring_history/{session_id}.json` — per-session snapshots for evolution charting

## What's new in this iteration

**Predict-and-surprise gating** (Active Inference applied at cluster level):
- `PredictorSwitch` in Temporal — already-shipped, skips understanding integrator on routine input
- `CompositePredictor` in Frontal — skips executive when response shape is predictable; skips critic when past scores on similar shapes were consistently high
- Encoder gate in Hippocampus — skips LLM summarisation on low-surprise/low-DA turns; episode still gets stored
- Recall reuse — near-identical recall queries reuse the previous result

**Hebbian wiring**:
- Composite outcome signal: `0.5 * DA_delta + 0.3 * critic_score + 0.2 * user_emotion_delta`
- Plasticity modulator scales the learning rate by session-averaged DA + ACh (sad/flat sessions learn slowly; engaged sessions learn faster)
- Gentle homeostatic decay (1% toward resting weight 1.0) prevents lock-in
- Tendency-match bonus reinforces coherence between emotional state and behavioural choice
- Updates applied at sleep consolidation; weights consulted live for drafter selection, switch ordering, and recall fan-out (ε-greedy exploration)

**Emotion-aware veto** (the "don't gate during emotional moments" rule):
Gating is bypassed when `high_GABA`, when the entity's emotion is reactive (`angry`, `defensive`, `frustrated`, `sympathetic`, etc.), when the user's emotion needs care (`distressed`, `sad`, `hostile`, etc.), or when the vocal tone is `stressed`/`whisper`. Hebbian also skips turns that ended in the defuse path — those are reactive, not representative.

**Skip rules for Hebbian**:
- Outcome too small to read a signal from (`|outcome| < 0.05`)
- Turn was a defuse response (high GABA + single draft)
- Entity was `confused`/`flat` with negative user emotion

## Observability

Everything routes through one append-only JSONL stream + the browser UI. Three record types in `eval/turns.jsonl`:

| Type | What |
|------|------|
| `turn` | Full `TurnTrace` (response, emotion, neuromod, draft scores, **fired_path**, **predictor_outcomes**, **llm_calls_saved**) |
| `decision` | Every predict-and-surprise and Hebbian decision with the reason |
| `eval_patch` | Async patches from baseline runner / post-hoc scorer |

Decision flavours include:

- `skip_executive_integrator` / `skip_critic` / `skip_temporal_integrator` / `skip_encoder` / `reuse_recent_recall`
- `gate_bypassed_emotional` (with the bypass reason)
- `weighted_drafter_selection` (picked drafter, weights, ε-greedy roll, whether it diverged from uniform)
- `weighted_switch_order`, `weighted_recall_fanout`
- `hebbian_update_applied` (per-edge with from/to weight + delta)
- `hebbian_update_skipped` (with reason)
- `session_plasticity_summary` (session totals, top gainers/losers)

### Tail the decisions stream

```bash
tail -f eval/turns.jsonl | jq 'select(.type=="decision")'
```

### Browser UI

http://localhost:8765 has a **Plasticity** panel on the right column showing:
- Cumulative LLM calls saved this session
- Predictor accuracy (rolling 50-prediction window)
- Plasticity modulator (current learning rate scalar)
- Top edges by weight with session deltas
- Live colour-coded decisions feed
- `[FROZEN]` tag when `BRAIN_WIRING_FROZEN=true`

### Eval comparison

```bash
uv run python -m eval.compare
```

Buckets recent turns by whether gating actually fired (proxied via `llm_calls_saved > 0` or `gating_bypassed_count > 0`) and reports avg LLM calls per turn + quality delta. Pass criterion: ≥25% call reduction with no statistically significant quality drop.

## Escape hatches

- `BRAIN_WIRING_FROZEN=true` — disables weighted routing everywhere (drafter selection, switch order, recall fan-out revert to uniform / declaration order). Hebbian learning still runs at sleep so weights keep accumulating. Use if behaviour drifts strangely.
- `BRAIN_DISABLE_PREDICT_GATING=true` — forces every integrator to wake regardless of predictor confidence. For A/B eval comparisons.
- Edit `second_brain/wiring.json` directly to nudge or zero specific edges. The bootstrap step is idempotent — re-adding an existing edge is a no-op, so you can hand-tune without fear of wipes.
- Edit `second_brain/schema/self.md` or `user.md` directly. The sleep consolidation rewrites only specific sections (History summary, Stable preferences); your other edits stick.

## Testing

```bash
uv run pytest                                           # full suite (310+)
uv run pytest tests/test_predictor_gating.py            # predictor + bypass helper
uv run pytest tests/test_hebbian_pass.py                # wiring + sleep Hebbian pass
uv run pytest tests/test_decisions_log.py               # decisions → disk + UI
```

## Project layout

```
brain/
  bus.py                  — topic-tagged pub/sub blackboard + neuromodulator state
  brainstem.py            — heartbeat, budget enforcer, articulation gate
  cell.py                 — IntegratorCell (LLM-backed, firing-path hook)
  neuron.py               — SwitchNeuron + StatefulSwitch (firing-path hook)
  predictor.py            — PredictorSwitch + CompositePredictor + emotion-aware bypass
  wiring.py               — Edge graph + Hebbian update + decay + history snapshots
  wiring_bootstrap.py     — declares the initial cluster-to-cluster edge graph
  pns.py                  — peripheral I/O (text in, TTS out)
  streaming_mic.py        — Deepgram streaming session
  model_router.py         — cloud-or-local LLM dispatch by model key
  security.py             — egress pseudonymisation + log redaction
  dmn.py                  — Default Mode Network: idle thinking loop
  metacognition.py        — self-monitor cell publishing meta.* topics
  sleep.py                — consolidation: episode synthesis + self-model update + Hebbian pass
  emotion_hierarchy.py    — 3-tier feeling-wheel taxonomy
  emotion_vocabulary.py   — neuromod → (emotion, tendency) lookup
  clusters/
    temporal.py           — language understanding + predictor + weighted switch order
    frontal.py            — Multiple Drafts engine + executive/critic predictors + weighted drafter selection
    hippocampus.py        — episodic + schema memory + recall reuse + weighted fan-out
    hypothalamus.py       — neuromod state + emotion naming + appraisal override
    thalamus.py           — attention spotlight
    parietal.py           — session state ring buffer
    occipital.py          — vision integrator (VLM)
    motor_cortex.py       — tool use (file I/O + shell, sandboxed)
    cloud_executor.py     — Claude Code subprocess for cloud actions
    audio_dsp.py          — pitch/pace/voice feature extraction
    auditory_cortex.py    — speaker enrollment, prosody, song fingerprint
  observability/
    timeline.py           — TurnTrace + Langfuse adapter
    firing_path.py        — context-var binding for the per-turn firing path
    decisions.py          — unified decision log (disk + UI)
  ui/
    server.py             — FastAPI WebSocket server
    index.html            — visualizer + Plasticity panel
    emitter.py            — async queue bridging brain → WebSocket
second_brain/
  schema/self.md          — entity's autobiography (hand-editable)
  schema/user.md          — per-user model + affection score
  episodes/               — LanceDB vector store
  wiring.json             — Hebbian edge weights (persists)
  wiring_history/         — per-session snapshots
eval/
  turns.jsonl             — append-only event stream (turns + decisions + patches)
  compare.py              — gating on/off comparison runner
  baseline.py             — baseline-model runner for eval
  scorer.py               — post-hoc judge scorer
tests/                    — pytest suite
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Ollama embedding service unreachable` | `ollama serve` not running | `ollama serve` (start.sh handles this) |
| `Motor cortex enabled but BRAIN_MOTOR_PATHS is not set` | Tool sandbox not configured | Set `BRAIN_MOTOR_PATHS=/abs/path[:/other/path]` in `.env` |
| Browser shows no decision events | UI WebSocket not connected, or session has had no turns yet | Reload tab; check `ws-indicator` in header turns green |
| Brain hangs on voice input | Deepgram key missing / mic permission denied | Check `DEEPGRAM_API_KEY`; grant mic to your terminal in System Settings → Privacy & Security |
| Weights never change between sessions | Sessions are too short (no turn → no firing path), or every turn hit a skip rule | Have a few-turn conversation; check `tail eval/turns.jsonl \| jq 'select(.decision=="hebbian_update_skipped")'` for reasons |
| Strange behaviour drift after many sessions | Hebbian over-fit on early outliers | `BRAIN_WIRING_FROZEN=true ./start.sh` to isolate; if it normalises, hand-edit `second_brain/wiring.json` |
