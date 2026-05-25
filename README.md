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
                                                    │
                                              Motor Cortex
```

Each cluster has:
- **Switch neurons** (`brain/neuron.py`) — deterministic code, cheap, the connective tissue. ~80% excitatory, ~20% inhibitory.
- **Integrator cells** (`brain/cell.py`) — LLM-backed, fire only at convergence zones where reasoning genuinely needs to happen.
- **Predictor** (`brain/predictor.py`) — anticipates the cluster's output; integrators stay asleep on low surprise. **Emotion-aware veto** forces integrators awake when the entity or user is in a non-routine emotional state.
- **Hebbian edge weights** (`brain/wiring.py`) — connections between cells/clusters carry weights nudged by outcome at sleep consolidation. Weighted routing decides which drafters fire, which temporal switches evaluate first, and how memory recall fans out.

Long-term state lives in `second_brain/`:
- `schema/self.md`, `schema/user.md` — human-readable, hand-editable; routed per recognised speaker
- `episodes/episodes.lance/` — vector-indexed turn summaries
- `episodes/procedures.lance/` — procedural memory (muscle memory)
- `wiring.json` — Hebbian edge weights (persists across sessions)
- `wiring_history/{session_id}.json` — per-session snapshots for evolution charting

## Signal model

### Neuromodulators (fast, per-turn)

| Signal | Role |
|--------|------|
| DA | Reward / positive valence |
| ACh | Curiosity, attentional engagement |
| GABA | Inhibition, de-escalation |
| Glu | General arousal, salience |
| NE | Focused alertness — sharp attentional spotlight, rises with surprise and threat |

### Hormonal channels (slow, session-level)

| Signal | Role |
|--------|------|
| OXT | Oxytocin — builds on warm exchange; buffers CORT |
| CORT | Cortisol — sustained social-threat accumulation |
| 5HT | Serotonin — slow lift from rewarding interaction |
| AEA | Anandamide — homeostatic buffer; rises when Glu+NE arousal exceeds threshold; antagonised by CORT |

Hormonal channels decay on a time-weighted wall-clock schedule, not per-turn. OXT and AEA cross-antagonise CORT. Hormonal state is shown in the browser UI and logged to Langfuse.

### Continuous affect dimensions

Hypothalamus maps the neuromodulator state to continuous **valence** and **arousal** dimensions before naming an emotion. This separates the two axes of Russell's circumplex model and prevents the emotion label from being the only signal downstream clusters receive.

## Predict-and-surprise gating

Active Inference applied at cluster level: each integrator has a predictor that fires first. If surprise is low, the integrator stays asleep.

| Gate | What it guards |
|------|---------------|
| `PredictorSwitch` in Temporal | skips understanding integrator on routine input |
| `CompositePredictor` in Frontal | skips executive when response shape is predictable; skips critic when past scores on similar shapes were consistently high |
| Encoder gate in Hippocampus | skips LLM summarisation on low-surprise/low-DA turns; episode still stored |
| Recall reuse | near-identical recall queries reuse the previous result |

**Emotion-aware veto** bypasses gating when `high_GABA`, when the entity's emotion is reactive (`angry`, `defensive`, `frustrated`, `sympathetic`, etc.), when the user's emotion needs care (`distressed`, `sad`, `hostile`, etc.), or when the vocal tone is `stressed`/`whisper`.

## Hebbian wiring

Composite outcome signal: `0.5 * DA_delta + 0.3 * critic_score + 0.2 * user_emotion_delta`

- **Plasticity modulator** scales the learning rate by session-averaged DA + ACh (sad/flat sessions learn slowly; engaged sessions learn faster)
- **Homeostatic decay** (1% toward resting weight 1.0) prevents lock-in
- **Tendency-match bonus** reinforces coherence between emotional state and behavioural choice
- Updates applied at sleep consolidation; weights consulted live for drafter selection, switch ordering, and recall fan-out (ε-greedy exploration)

**Skip rules**: outcome too small (`|outcome| < 0.05`), turn was a defuse response, or entity was `confused`/`flat` with negative user emotion.

## Frontal subsystem architecture

The frontal lobe dispatches to pluggable **FrontalSubsystem** implementations (`brain/clusters/frontal_subsystem.py`). The executive runs first and classifies intent; the first matching subsystem handles it; conversational drafting (Multiple Drafts engine) is the fallback.

Current subsystems:

| Subsystem | Trigger | What it does |
|-----------|---------|--------------|
| `FrontalTaskSubsystem` | `requires_action=True` | Extracts goal → deposits in `PendingTask` for motor cortex |

To add a subsystem: implement `FrontalSubsystem`, register it in `FrontalCluster.__init__`.

### FollowThrough (SMA loop)

`brain/clusters/follow_through.py` — after the drafter finalises a response, this module detects spoken commitments ("let me grab that", "I'll go check") and re-queues them as synthetic self-directed turns. The executive then classifies the turn as a task and the motor cortex executes it. This closes the loop between intention and action without requiring an external trigger.

## Motor cortex

Tool use is sandboxed to `BRAIN_MOTOR_PATHS`. Motor cortex supports pluggable **MotorSubsystem** implementations (`brain/clusters/motor_subsystem.py`):

- `before_plan()` — inject context into the planner prompt
- `recall_procedure()` — check procedural memory for a high-confidence prior
- `predict_outcome()` — forward model: predict tool-call output before execution
- `after_job()` — record completed jobs for learning

### Muscle memory (ProcedureStore)

`brain/clusters/motor_memory.py` — completed motor jobs are stored as vector-indexed procedures in `second_brain/episodes/` (same LanceDB database as episodic memory, separate table). On subsequent similar tasks:
- Above `_SIMILARITY_THRESHOLD` (0.75): prior steps are prepended as context
- Above `_OPEN_LOOP_THRESHOLD` (0.90) with ≥2 prior successes: motor runs the procedure without LLM re-planning (open-loop execution)

The cloud executor (`brain/clusters/cloud_executor.py`) has **WebSearch** and **WebFetch** tools enabled for cloud-side tool calls.

## Skill injection

`brain/skill_loader.py` reads `.md` files from `brain/skills/` and injects them into system prompts for **local (Ollama) model calls only**. Cloud calls are unaffected. To add a skill: drop a `.md` file in `brain/skills/`. To clone a skill from your Claude Code installation: `python brain/skill_loader.py clone <name>`.

## Auditory cortex and speaker routing

With `--ears` enabled:
- **Speaker enrollment** — voice fingerprints are registered on first contact
- **Per-speaker schema routing** — each recognised speaker gets their own `schema/user_<id>.md`; unknown voices fall back to `schema/user.md`
- **Prosody extraction** — pitch, pace, voice features extracted and logged to Langfuse
- **Response-length mirroring** — the brain roughly matches its output length to the user's input length
- **Song fingerprint** — detects music in the audio stream

## Image pipeline

Upload images via the browser UI (`/upload_image`). Occipital cortex passes them to a VLM integrator; the description is injected into the drafter context.

## Default Mode Network

With `--dmn`:
- Idle thoughts fire every ~15 s when no user input is active
- **Bidirectional coupling**: DMN thoughts influence neuromodulator state; neuromodulator state gates DMN firing
- **Feedback loops**: thoughts can re-trigger downstream emotion/memory updates
- **Thought deduplication**: near-duplicate thoughts are suppressed; recent unique thoughts are surfaced
- **Idle gate**: DMN is suppressed when the brain is actively processing user input

## Observability

Everything routes through one append-only JSONL stream + the browser UI. Three record types in `eval/turns.jsonl`:

| Type | What |
|------|------|
| `turn` | Full `TurnTrace` (response, emotion, neuromod, draft scores, **fired_path**, **predictor_outcomes**, **llm_calls_saved**) |
| `decision` | Every predict-and-surprise and Hebbian decision with the reason |
| `eval_patch` | Async patches from baseline runner / post-hoc scorer |

Decision flavours include:

- `skip_executive_integrator` / `skip_critic` / `skip_temporal_integrator` / `skip_encoder` / `reuse_recent_recall`
- `gate_bypassed_emotional` (with bypass reason)
- `weighted_drafter_selection` (picked drafter, weights, ε-greedy roll, divergence flag)
- `weighted_switch_order`, `weighted_recall_fanout`
- `hebbian_update_applied` (per-edge with from/to weight + delta)
- `hebbian_update_skipped` (with reason)
- `session_plasticity_summary` (session totals, top gainers/losers)

### Tail the decisions stream

```bash
tail -f eval/turns.jsonl | jq 'select(.type=="decision")'
```

### Browser UI (http://localhost:8765)

- **Neural Map** — live wiring graph showing cluster activations and edge weights
- **Plasticity panel** — cumulative LLM calls saved, predictor accuracy (rolling 50), plasticity modulator, top edges with session deltas, colour-coded decisions feed, `[FROZEN]` tag when `BRAIN_WIRING_FROZEN=true`
- **Neuromodulator panel** — live DA, ACh, GABA, Glu, NE bars
- **Hormonal panel** — live OXT, CORT, 5HT, AEA bars with time-decay visualisation
- **Activity log** — left panel, per-turn event stream

### Session learning evaluation

`eval/learning_monitor.py` — structural learning metrics computed from `TurnTrace` data without an LLM. Per-turn scores sent to Langfuse (`learning.predictor_accuracy`, `learning.gating_efficiency`, `learning.avg_surprise`, etc.). Session summary: predictor accuracy trend, gating efficiency trend, surprise trend, Hebbian edge change count and magnitude, cross-session weight drift.

### Eval comparison

```bash
uv run python -m eval.compare
```

Buckets recent turns by whether gating fired (proxied via `llm_calls_saved > 0` or `gating_bypassed_count > 0`) and reports avg LLM calls per turn + quality delta. Pass criterion: ≥25% call reduction with no statistically significant quality drop.

## Escape hatches

- `BRAIN_WIRING_FROZEN=true` — disables weighted routing everywhere. Hebbian learning still runs at sleep so weights accumulate. Use if behaviour drifts strangely.
- `BRAIN_DISABLE_PREDICT_GATING=true` — forces every integrator to wake regardless of predictor confidence. For A/B eval comparisons.
- Edit `second_brain/wiring.json` directly to nudge or zero specific edges. Bootstrap is idempotent — re-adding an existing edge is a no-op.
- Edit `second_brain/schema/self.md` or `user.md` directly. Sleep consolidation rewrites only specific sections (History summary, Stable preferences); your other edits stick.

## Testing

```bash
uv run pytest                                           # full suite (310+)
uv run pytest tests/test_predictor_gating.py            # predictor + bypass helper
uv run pytest tests/test_hebbian_pass.py                # wiring + sleep Hebbian pass
uv run pytest tests/test_decisions_log.py               # decisions → disk + UI
uv run pytest tests/test_motor_cortex.py                # motor cortex + muscle memory
uv run pytest tests/test_hormonal_system.py             # endocrine channels
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
  pns.py                  — peripheral I/O (text in, TTS out, voice shaping)
  streaming_mic.py        — Deepgram streaming session (with KeepAlive)
  model_router.py         — cloud-or-local LLM dispatch by model key
  security.py             — egress pseudonymisation + log redaction
  skill_loader.py         — loads brain/skills/*.md and injects into local model prompts
  skills/                 — skill markdown files (injected into Ollama calls only)
  dmn.py                  — Default Mode Network: idle thinking loop
  metacognition.py        — self-monitor cell publishing meta.* topics
  sleep.py                — consolidation: episode synthesis + self-model update + Hebbian pass
  emotion_hierarchy.py    — 3-tier feeling-wheel taxonomy
  emotion_vocabulary.py   — neuromod → (emotion, tendency) lookup
  clusters/
    temporal.py           — language understanding + predictor + weighted switch order
    frontal.py            — Multiple Drafts engine + executive/critic predictors + weighted drafter selection
    frontal_subsystem.py  — FrontalSubsystem ABC + SubsystemResult
    frontal_task.py       — FrontalTaskSubsystem + PendingTask (intent → motor handoff)
    follow_through.py     — SMA loop: spoken commitments → self-directed motor tasks
    hippocampus.py        — episodic + schema memory + recall reuse + weighted fan-out
    hypothalamus.py       — neuromod state + hormonal channels + affect dims + emotion naming
    thalamus.py           — attention spotlight
    parietal.py           — session state ring buffer
    occipital.py          — vision integrator (VLM)
    motor_cortex.py       — tool use (file I/O + shell, sandboxed)
    motor_subsystem.py    — MotorSubsystem ABC (before_plan / after_job hooks)
    motor_memory.py       — MuscleMemorySubsystem + ProcedureStore (LanceDB-backed)
    cloud_executor.py     — Claude Code subprocess for cloud actions (WebSearch + WebFetch)
    audio_dsp.py          — pitch/pace/voice feature extraction
    auditory_cortex.py    — speaker enrollment, prosody, per-speaker schema routing
  observability/
    timeline.py           — TurnTrace + Langfuse adapter
    firing_path.py        — context-var binding for the per-turn firing path
    decisions.py          — unified decision log (disk + UI)
  ui/
    server.py             — FastAPI WebSocket server (image upload, hormonal push)
    index.html            — visualizer (Neural Map, Plasticity, neuromod, hormonal panels)
    emitter.py            — async queue bridging brain → WebSocket
second_brain/
  schema/self.md          — entity's autobiography (hand-editable)
  schema/user.md          — per-user model + affection score
  schema/user_<id>.md     — per-speaker schema files (created automatically)
  episodes/               — LanceDB vector store (episodes + procedures tables)
  wiring.json             — Hebbian edge weights (persists)
  wiring_history/         — per-session snapshots
eval/
  turns.jsonl             — append-only event stream (turns + decisions + patches)
  learning_monitor.py     — structural learning metrics (no LLM, per-turn + session)
  learning_judge.py       — LLM-backed qualitative assessment of learning
  compare.py              — gating on/off comparison runner
  baseline.py             — baseline-model runner for eval
  scorer.py               — post-hoc judge scorer
tests/                    — pytest suite (310+ tests)
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Ollama embedding service unreachable` | `ollama serve` not running | `ollama serve` (start.sh handles this) |
| `Motor cortex enabled but BRAIN_MOTOR_PATHS is not set` | Tool sandbox not configured | Set `BRAIN_MOTOR_PATHS=/abs/path[:/other/path]` in `.env` |
| Browser shows no decision events | UI WebSocket not connected, or session has had no turns yet | Reload tab; check `ws-indicator` in header turns green |
| Brain hangs on voice input | Deepgram key missing / mic permission denied | Check `DEEPGRAM_API_KEY`; grant mic to your terminal in System Settings → Privacy & Security |
| Weights never change between sessions | Sessions too short (no firing path), or every turn hit a skip rule | Have a few-turn conversation; check `tail eval/turns.jsonl \| jq 'select(.decision=="hebbian_update_skipped")'` for reasons |
| Strange behaviour drift after many sessions | Hebbian over-fit on early outliers | `BRAIN_WIRING_FROZEN=true ./start.sh` to isolate; hand-edit `second_brain/wiring.json` if needed |
| Hormonal panel missing in UI | Old browser tab cached before server added hormonal push | Hard-refresh the tab |
| Open-loop motor execution behaves incorrectly | Procedure store has a stale high-similarity record | Inspect `second_brain/episodes/` procedures table; delete or mark stale records |
