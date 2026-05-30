# Voice AI Engine Development

## Overview

This skill guides you through building production-ready voice AI engines with real-time conversation capabilities. Voice AI engines enable natural, bidirectional conversations between users and AI agents through streaming audio processing, speech-to-text transcription, LLM-powered responses, and text-to-speech synthesis.

The core architecture uses an async queue-based worker pipeline where each component runs independently and communicates via `asyncio.Queue` objects, enabling concurrent processing, interrupt handling, and real-time streaming at every stage.

## When to Use This Skill

Use this skill when:
- Building real-time voice conversation systems
- Implementing voice assistants or chatbots
- Creating voice-enabled customer service agents
- Developing voice AI applications with interrupt capabilities
- Integrating multiple transcription, LLM, or TTS providers
- Working with streaming audio processing pipelines
- The user mentions Vocode, voice engines, or conversational AI

## Core Architecture Principles

### The Worker Pipeline Pattern

Every voice AI engine follows this pipeline:

```
Audio In → Transcriber → Agent → Synthesizer → Audio Out
 (Worker 1) (Worker 2) (Worker 3)
```

**Key Benefits:**
- **Decoupling**: Workers only know about their input/output queues
- **Concurrency**: All workers run simultaneously via asyncio
- **Backpressure**: Queues automatically handle rate differences
- **Interruptibility**: Everything can be stopped mid-stream

### Base Worker Pattern

Every worker follows this pattern:

```python
class BaseWorker:
 def __init__(self, input_queue, output_queue):
 self.input_queue = input_queue # asyncio.Queue to consume from
 self.output_queue = output_queue # asyncio.Queue to produce to
 self.active = False

 def start(self):
 """Start the worker's processing loop"""
 self.active = True
 asyncio.create_task(self._run_loop())

 async def _run_loop(self):
 """Main processing loop - runs forever until terminated"""
 while self.active:
 item = await self.input_queue.get() # Block until item arrives
 await self.process(item) # Process the item

 async def process(self, item):
 """Override this - does the actual work"""
 raise NotImplementedError

 def terminate(self):
 """Stop the worker"""
 self.active = False
```

## Component Implementation Guide

### 1. Transcriber (Audio → Text)

**Purpose**: Converts incoming audio chunks to text transcriptions

**Interface Requirements**:
```python
class BaseTranscriber:
 def __init__(self, transcriber_config):
 self.input_queue = asyncio.Queue() # Audio chunks (bytes)
 self.output_queue = asyncio.Queue() # Transcriptions
 self.is_muted = False

 def send_audio(self, chunk: bytes):
 """Client calls this to send audio"""
 if not self.is_muted:
 self.input_queue.put_nowait(chunk)
 else:
 # Send silence instead (prevents echo during bot speech)
 self.input_queue.put_nowait(self.create_silent_chunk(len(chunk)))

 def mute(self):
 """Called when bot starts speaking (prevents echo)"""
 self.is_muted = True

 def unmute(self):
 """Called when bot stops speaking"""
 self.is_muted = False
```
