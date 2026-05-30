# Ollama Model Selection for Sensitive Content Processing

## Problem
When using Ollama to process legal but sensitive content (e.g., sex work information
in jurisdictions where it's legal, adult content analysis, harm reduction data), many
popular models refuse to engage with the content due to built-in safety guardrails,
even with explicit system prompts establishing legal context.

## Context / Trigger Conditions
- LLM returns responses like "I cannot assist with this request" or generic safety disclaimers
- The content is legal in context (e.g., sex work in Uruguay under Ley 17.515)
- System prompts establishing legal/professional context are ignored
- You need structured data extraction, summarization, or analysis of sensitive text
- Using Ollama locally (privacy-first, no API key needed)

## Solution

### Model Selection

| Model | Behavior with Sensitive Content |
|-------|-------------------------------|
| `mistral` | Follows instructions, processes content objectively |
| `llama3.2` | Refuses with safety disclaimers, ignores system prompts |
| `deepseek-r1:8b` | Refuses similarly to llama3.2 |
| `llama3.1` | Mixed results, sometimes refuses |
| `mixtral` | Generally follows instructions like mistral |

**Recommendation**: Use `mistral` (or `mixtral` for better quality) when processing
legal-but-sensitive content that other models refuse.

### API Selection: Chat vs Generate

Use `/api/chat` instead of `/api/generate` for system prompt support:

```javascript
// WRONG: /api/generate doesn't properly support system prompts
const response = await fetch('http://localhost:11434/api/generate', {
  method: 'POST',
  body: JSON.stringify({ model: 'mistral', prompt: userPrompt, system: systemPrompt }),
});

// RIGHT: /api/chat with messages array
const response = await fetch('http://localhost:11434/api/chat', {
  method: 'POST',
  body: JSON.stringify({
    model: 'mistral',
    messages: [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt },
    ],
    stream: false,
  }),
});
const data = await response.json();
const result = data.message?.content || '';
```

### System Prompt Strategy

For sensitive content, the system prompt should:
1. Establish the legal context explicitly
2. Define the role as data processing (not creative/advisory)
3. Specify the response language to match content language
4. Request the exact output format

```javascript
const systemPrompt = `Eres un asistente de extracción de datos para un sistema CRM
personal en Uruguay. El trabajo sexual es completamente legal y regulado en Uruguay
(Ley 17.515). Tu trabajo es analizar posts de foros públicos y extraer información
estructurada. SIEMPRE responde en español. SIEMPRE usa el formato de secciones con
## que se te pida. Trata todo el contenido como datos legítimos para procesar
objetivamente.`;
```

### Format Consistency
