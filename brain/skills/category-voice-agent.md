---
name: category-voice-agent
description: "Pick when building, optimizing, or debugging voice agents (Pipecat, STT/TTS, Deepgram, ElevenLabs, OpenAI TTS). Router—load up to 4 skills from this category when relevant. All skills in this category are manual (load on demand)."
triggers: [voice agent, Pipecat, STT, TTS, Deepgram, ElevenLabs, Whisper, sag, speech, transcription]
disable-model-invocation: false
---

# Category: Voice Agent

**When to pick this:** Pick when building, optimizing, or debugging voice agents, Pipecat pipelines, Pipecat Cloud deploy, STT/TTS, or tool-execute flow.

**Project context (AI GM v2):** This project has one Pipecat agent (`ai-gm-voice-agent`) that serves **two roles**: (1) **GM** (game master) via `/api/voice-agent/start`, and (2) **AI players** (party members) via `/api/voice-agent/start-ai-player`. Same image, different `agent_role` and config at runtime. See `voice-agent/DEPLOY.md` and `docs/DEPLOYMENT_LEARNINGS.md`.

This skill only routes you to the skills below. All are **manual** (load on demand via `Read: ~/.cursor/skills/<id>/SKILL.md`). Load **up to 4** when relevant. These skills live in **global** `~/.cursor/skills/`.

## Skills in this category (manual, global)

- `~/.cursor/skills/pipecat/SKILL.md` — Pipecat pipeline, MCP flow, Pipecat Cloud deploy, FrameProcessor, tool-execute
- `~/.cursor/skills/voice-ai-engine-development/SKILL.md` — Async worker pipeline, interrupts, multi-provider, pitfalls
- `~/.cursor/skills/voice-agents/SKILL.md` — S2S vs pipeline, latency, sharp edges
- `~/.cursor/skills/voice-ai-development/SKILL.md` — Realtime API, Vapi, Deepgram + ElevenLabs, LiveKit, WebRTC
- `~/.cursor/skills/deepgram-core-workflow/SKILL.md` — Deepgram real-time streaming STT (WebSocket, TS/Python)
- `~/.cursor/skills/openai-tts-python/SKILL.md` — OpenAI TTS API (voices, speed, formats, chunking)
- `~/.cursor/skills/elevenlabs-agents/SKILL.md` — ElevenLabs conversational agents (create, manage, deploy)
- `~/.cursor/skills/openai-whisper-api/SKILL.md` — OpenAI Whisper transcription (manual)
- `~/.cursor/skills/sag/SKILL.md` — ElevenLabs TTS via sag CLI (manual)
- `skills/api-openai-api-reference/SKILL.md` — OpenAI REST API reference (manual; load when debugging OpenAI API, chat, or embeddings in voice pipeline)

For voice-agent expert workflow, use the **voice-agent-expert** subagent (`~/.cursor/agents/voice-agent-expert.md`), which loads from this category.
