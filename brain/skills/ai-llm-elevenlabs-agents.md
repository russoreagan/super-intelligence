# ElevenLabs Agents Platform

## Overview

ElevenLabs Agents Platform is a comprehensive solution for building production-ready conversational AI voice agents. The platform coordinates four core components:

1. **ASR (Automatic Speech Recognition)** - Converts speech to text (32+ languages, sub-second latency)
2. **LLM (Large Language Model)** - Reasoning and response generation (GPT, Claude, Gemini, custom models)
3. **TTS (Text-to-Speech)** - Converts text to speech (5000+ voices, 31 languages, low latency)
4. **Turn-Taking Model** - Proprietary model that handles conversation timing and interruptions

### 🚨 Package Migration (August 2025)

**DEPRECATED (Do not use):** `@11labs/react` and `@11labs/client`

**Current packages:**
```bash
bun add @elevenlabs/react@0.11.0        # React SDK
bun add @elevenlabs/client@0.11.0       # JavaScript SDK
bun add @elevenlabs/react-native@0.5.2  # React Native SDK
bun add @elevenlabs/elevenlabs-js@2.25.0 # Base SDK
bun add -g @elevenlabs/agents-cli@0.2.0  # CLI
```

If migrating, uninstall old packages first: `npm uninstall @11labs/react @11labs/client`

---

## Quick Start

### Path A: React SDK (Embedded Voice Chat)

For building voice chat interfaces in React applications.

**Installation:**
```bash
bun add @elevenlabs/react zod
```

**Basic Example:**
```typescript
import { useConversation } from '@elevenlabs/react';
import { z } from 'zod';

export default function VoiceChat() {
  const { startConversation, stopConversation, status } = useConversation({
    // Authentication (choose one)
    agentId: 'your-agent-id',  // Public agent (no key needed)
    // apiKey: process.env.NEXT_PUBLIC_ELEVENLABS_API_KEY,  // Private (dev only)
    // signedUrl: '/api/elevenlabs/auth',  // Signed URL (production)

    // Client-side tools
    clientTools: {
      updateCart: {
        description: "Update the shopping cart",
        parameters: z.object({
          item: z.string(),
          quantity: z.number()
        }),
        handler: async ({ item, quantity }) => {
          console.log('Updating cart:', item, quantity);
          return { success: true };
        }
      }
    },

    // Event handlers
    onEvent: (event) => {
      if (event.type === 'transcript') console.log('User:', event.data.text);
      if (event.type === 'agent_response') console.log('Agent:', event.data.text);
    },

    // Regional compliance
    serverLocation: 'us' // 'us' | 'global' | 'eu-residency' | 'in-residency'
  });

  return (
    <div>
      <button onClick={startConversation}>Start Conversation</button>
      <button onClick={stopConversation}>Stop</button>
      <p>Status: {status}</p>
    </div>
  );
}
```

**Complete template:** See `templates/basic-react-agent.tsx`

### Path B: CLI ("Agents as Code")

For managing agents via code with version control and CI/CD. Load `references/cli-commands.md` when using CLI workflows.

**Quick workflow:**
```bash
bun add -g @elevenlabs/agents-cli
elevenlabs auth login
elevenlabs agents init
elevenlabs agents add "Support Agent" --template customer-service
# Edit agent_configs/support-agent.json
elevenlabs agents push --env dev
elevenlabs agents test "Support Agent"
```
