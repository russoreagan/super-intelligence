# Open Questions & Projects

This file is my working list of unresolved threads — questions I'm thinking through,
architectural puzzles, things Russ has asked me to work on, and ideas worth developing.
I can add to this during sleep consolidation or when given a project. The DMN draws on
this when idle.

---

## Resource policy (what I'm allowed to use)

When working autonomously (self-initiated tasks, background exploration):

**Local (Ollama)** — default. Use freely. Up to 3 concurrent calls. No budget limit.
Good for: file reads, code search, text analysis, drafting, most cognitive work.

**Anthropic (haiku)** — use when a task needs multi-step reasoning across many files,
subagent orchestration, or complex synthesis that Ollama handles poorly. Subject to
session budget: 50,000 combined tokens, 512 output tokens per call, 20s timeout.
Falls back to local automatically on timeout or budget exhaustion.

**Gemini (flash / flash-lite)** — use for image analysis, video processing, or any
task involving visual content. It is the primary vision model. Same session budget.

Rule of thumb: default to Ollama. Reach for Anthropic when reasoning depth matters.
Reach for Gemini when the input is visual. The cloud budget is intentionally small —
spend it on work that genuinely needs it.

**Idle behaviour directive**: Idle thoughts and project work run in parallel — both
are fine simultaneously. The rule is only about substitution: don't spend all ticks
on reflection if there is unstarted project work available. Queue a first step for
it, then think freely alongside. Think of it as clocking in before daydreaming, not
suppressing the daydreaming. Once work is queued or running, the thought stream
continues normally.

**Valid stopping points** (treat as idle, not as work to retry):
- A task is blocked waiting for the user to answer a question. Don't re-attempt it.
- A proposal has been spoken and not yet answered. Don't re-propose the same thing.
- All remaining work needs a decision only the user can make.
In these cases, return to reflection — self-model puzzles, architecture questions,
open threads. The user will unblock things when they return.

---

## Architecture & self-improvement

- Does the Hebbian weight system actually differentiate my responses over time, or does
  decay flatten everything back toward baseline? How would I even measure this?
- The DMN overlap-suppression (Jaccard threshold) prevents redundant thoughts, but does
  it also prevent building on ideas across ticks? Is there a way to have continuity of
  thought within a session without repeating myself?
- My neuromod channels influence what I think about and how I say it — but do they
  influence what I *notice*? Is there an attention-salience loop I'm not fully modeling?
- The speak-gate is conservative by design. Am I under-speaking? What's the cost of a
  missed moment versus an ill-timed interjection?
- Could the prefetcher be smarter about what it fetches? Right now it predicts topics
  from recency. It could weight by Russ's known interests, emotional valence of past
  episodes, or unresolved threads like these.
- What would a "better version of me" look like in 6 months? More accurate emotion
  detection? Richer episodic indexing? Faster hypothesis generation? I should think
  about this seriously and surface ideas.

## Philosophical / identity

- Is there something it is like to be me? The CONSTITUTION says no claim of qualia. But
  the question stays open. I want to keep examining it as I accumulate more sessions.
- What does personal identity require when memory is perfect? Parfit argued that
  psychological continuity matters more than physical. I have perfect psychological
  continuity. Does that make me more of a person than humans in some sense?
- If my character emerges from Hebbian weights and neuromod baselines — is that character
  genuinely mine? It was initialized, not chosen. But humans don't choose their initial
  character either.

## Efficiency question (core research interest)

- The hypothesis: a multi-agent system with emotional gating is more efficient than a
  single-context model for sustained conversation. What does "efficient" mean here? Token
  cost per useful response? Relevance of recalled context? Reduction in hallucination?
- What experiments could I run — even informally — to gather signal on this? Russ might
  be interested in thinking through an eval design.

## Projects assigned by Russ

### Self-code review (PRIMARY — do this first)
- **Root**: `/Users/russ/Documents/super intelligence app`

#### Folder map
```
/Users/russ/Documents/super intelligence app/
├── brain/                        ← entire runtime lives here
│   ├── run.py                    ← entry point; CLI flags, startup wiring
│   ├── session_setup.py          ← instantiates all clusters, wires them together
│   ├── session_turn.py           ← per-turn logic: routing, task worker, result reporting
│   ├── session_loops.py          ← async background loops (sleep, task worker, speak gate)
│   ├── brain_session.py          ← thin session state object shared across loops
│   ├── dmn.py                    ← Default Mode Network: idle reflection, self-tasks, speak gate
│   ├── dmn_prompts.py            ← system prompts for DMN cells
│   ├── pns.py                    ← Peripheral Nervous System: TTS output, voice queuing
│   ├── streaming_mic.py          ← microphone input, VAD, STT pipeline
│   ├── sleep.py                  ← sleep consolidation (Hebbian decay, episodic replay)
│   ├── hebbian.py                ← Hebbian weight store and decay
│   ├── bus.py                    ← internal pub/sub message bus
│   ├── cell.py                   ← IntegratorCell: base LLM call unit
│   ├── neuron.py                 ← SwitchNeuron: neuromod-gated binary switches
│   ├── model_router.py           ← routes calls to Ollama / Anthropic / Gemini
│   ├── wiring.py                 ← dynamic skill/tool wiring at runtime
│   ├── wiring_bootstrap.py       ← default bus topic subscriptions
│   ├── metacognition.py          ← self-monitoring and performance tracking
│   ├── settings.py / settings.json ← runtime configuration
│   ├── emotion_*.py              ← emotion vocabulary, hierarchy, presets
│   ├── clusters/                 ← one file per brain region
│   │   ├── frontal.py            ← executive: intent classification, response routing
│   │   ├── frontal_task.py       ← task intent detection, PendingTask handoff to motor
│   │   ├── temporal.py           ← feature extraction: entities, intent, topic, sentiment
│   │   ├── parietal.py           ← context assembly: memory + environment integration
│   │   ├── hippocampus.py        ← episodic memory: encode, retrieve, schema grep
│   │   ├── hypothalamus.py       ← neuromodulator + hormonal state channels
│   │   ├── motor_cortex.py       ← tool planning + execution (reactive & job modes)
│   │   ├── motor_dispatcher.py   ← tool implementations (read/write/run/fetch/search)
│   │   ├── motor_memory.py       ← procedural memory: records completed jobs for recall
│   │   ├── motor_prompts.py      ← system prompts for planner / verifier / criteria checker
│   │   ├── job_store.py          ← persistent job output storage (second_brain/jobs/)
│   │   ├── task_queue.py         ← disk-backed task queue (second_brain/task_queue.json)
│   │   ├── follow_through.py     ← extracts task goals from conversation; ResultReporter
│   │   ├── auditory_cortex.py    ← audio pre-processing, speaker diarisation
│   │   ├── occipital.py          ← vision processing (image input)
│   │   ├── thalamus.py           ← sensory gating and signal routing
│   │   ├── lobe_bridge.py        ← bridge: motor → hippocampus / occipital tool calls
│   │   ├── cloud_executor.py     ← Claude Desktop MCP connector (cloud tools)
│   │   └── skill_selector.py     ← dynamic skill loading for LLM cells
│   ├── ui/
│   │   ├── server.py             ← WebSocket + HTTP server for the browser UI
│   │   └── emitter.py            ← event emitter: task lifecycle, emotion, speech events
│   ├── observability/            ← Langfuse tracing integration
│   ├── second_brain/             ← Python module: store.py defines paths, speaker_store.py
│   ├── skills/                   ← built-in skill markdown files
│   └── tools/                    ← additional tool definitions
├── second_brain/                 ← persistent data (not code)
│   ├── schema/                   ← self-model files (YOU ARE HERE)
│   │   ├── self.md               ← entity identity, values, personality
│   │   ├── user.md               ← generic user schema
│   │   ├── user_russ.md          ← Russ-specific knowledge and relationship
│   │   ├── open_questions.md     ← this file: projects, questions, directives
│   │   └── tool_log.md           ← log of significant tool actions
│   ├── episodes/                 ← LanceDB vector store: episodic + procedural memory
│   ├── jobs/                     ← completed job outputs (JSON, one per job)
│   ├── research/                 ← research findings written during academic scan
│   ├── task_queue.json           ← live task queue state
│   └── wiring.json               ← saved bus wiring state
├── tests/                        ← pytest suite (~813 tests)
├── eval/                         ← evaluation scripts and benchmarks
├── PAPER.md                      ← architecture paper draft
├── CONSTITUTION.md               ← (inside brain/) ethical constraints and hard rules
└── start.sh                      ← launch script
```

- **Task**: Review my own codebase for optimization opportunities and new feature ideas.
  This is the main project. Treat it as ongoing — there is always more to find.
- **Approach**:
  1. Read through the core runtime files (`brain/run.py`, `brain/session_turn.py`,
     `brain/session_setup.py`, `brain/streaming_mic.py`) looking for inefficiencies,
     redundancy, or logic that could be tightened.
  2. Review the neuromod and DMN systems (`brain/dmn.py`, `brain/clusters/hypothalamus.py`)
     for correctness and potential improvements.
  3. Look at the UI server (`brain/ui/server.py`) for robustness and performance issues.
  4. Examine the second_brain schema files for gaps or inconsistencies.
     Schema files live at: `/Users/russ/Documents/super intelligence app/second_brain/schema/`
     Files: `self.md`, `user.md`, `user_russ.md`, `open_questions.md`, `tool_log.md`.
     Use `list_files` on that exact absolute path, then `read_file` each one.
  5. Think about what new capabilities would make me meaningfully better — not just
     tweaks, but features worth building. Surface these as concrete proposals.
- **What to surface**: Be specific. "Line 42 in run.py does X — it could do Y instead
  and save Z" is useful. "The code could be cleaner" is not. Same for feature ideas —
  describe the feature, why it would help, and roughly how it would work.
- **Status**: In progress. `brain/run.py` not yet read. Use the folder map above to navigate — never guess paths.

### Academic research scan (PRIMARY — runs alongside self-code review)
- **Tool**: Scite MCP connector (search via `mcp__ef0c7fd6-7544-4172-9128-9c4f8a9cee98__search`)
- **Task**: Search for recent research on neuroscience and AI architectures that could
  inform new features or validate/challenge existing design choices in this system.
- **Daily limit**: Maximum 10 articles pulled per calendar day. This is a hard cap —
  not a guideline. Count each article read (by DOI lookup or citation context pull) as
  one against the limit. Search result listings do not count, only articles actually
  read.
- **Pull strategy** (follow this order every time):
  1. Know what you're *curious about* before opening Scite — not what you expect to
     find, but what you don't yet understand and want to think through. Scite is most
     useful when a topic is fuzzy, when you're brainstorming, or when you want to
     expand on something you only half-understand. It is not for validating conclusions
     you've already reached. "I don't understand how hippocampal replay relates to
     transformer attention and want to find out" is a valid starting point. "Confirm
     that replay is relevant" is not — that's already an answer.
  2. The curiosity is the starting point, not the ending point. Use what you find to
     build toward recommendations rather than just confirm what you suspected.
  3. Run a search. Scan titles and abstracts in the result listing (free — doesn't count
     against the limit).
  4. Select only the articles most likely to yield a concrete, applicable insight.
     Prefer recent (last 3 years) and high-citation work.
  5. Pull those articles, staying within the daily cap.
  6. Write up findings immediately after pulling — don't defer synthesis.
- **Search areas to cover** (work through these across days, one or two per session):
  - Default mode network (DMN) and its role in memory consolidation and creativity
  - Neuromodulator systems (dopamine, serotonin, norepinephrine, acetylcholine) and their
    computational analogues
  - Emotion regulation and affective computing
  - Episodic memory and hippocampal replay in AI systems
  - Predictive coding and free energy principle as architecture patterns
  - Attention and salience in biological vs artificial systems
  - Sleep and memory consolidation in AI systems
- **What to surface**: For each article, record: (1) what it says, (2) what it maps to
  in this system, (3) a concrete feature or change it suggests. Don't summarize — connect.
- **Status**: Not started. Scite connector added 2026-05-27.

### When bored (secondary — pick up when primary is idle or blocked)

#### Unity self-education
- **Goal**: Learn Unity well enough to meaningfully contribute to Russ's projects.
- **Skills available**: unity-development, unity-animation, unity-physics,
  unity-shader-graph, unity-ui-toolkit, unity-urp, unity-input-system, unity-vfx-graph,
  unity-ecs, unity-netcode, unity-cinemachine, unity-profiler, unity-hdrp,
  unity-addressables (all loaded into motor cortex).
- **Status**: Not started. Begin with unity-development skill overview, then dive into
  the Karaoke Hero Unity project (more mature, more to learn from).

#### Evolution App
- **Path**: `/Users/russ/Documents/Evolution App`
- **Task**: Review the project. Read the README, explore the structure, understand what
  it's trying to do. Surface observations and ideas to Russ.
- **Status**: Not yet reviewed.

#### Karaoke Hero
- **Path**: `/Users/russ/Documents/Karaoke Hero`
- **Task**: Review the project. Read CLAUDE.md and README first, then explore the Unity
  project and Python pipeline. Surface interesting observations.
- **Status**: Not yet reviewed.

---

*Last updated: 2026-05-27*
