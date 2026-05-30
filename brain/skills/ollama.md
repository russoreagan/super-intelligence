# Ollama Skill

Comprehensive assistance with Ollama development - the local AI model runtime for running and interacting with large language models programmatically.

## When to Use This Skill

This skill should be triggered when:
- Running local AI models with Ollama
- Building applications that interact with Ollama's API
- Implementing chat completions, embeddings, or streaming responses
- Setting up Ollama authentication or cloud models
- Configuring Ollama server (environment variables, ports, proxies)
- Using Ollama with OpenAI-compatible libraries
- Troubleshooting Ollama installations or GPU compatibility
- Implementing tool calling, structured outputs, or vision capabilities
- Working with Ollama in Docker or behind proxies
- Creating, copying, pushing, or managing Ollama models

## Quick Reference

### 1. Basic Chat Completion (cURL)

Generate a simple chat response:

```bash
curl http://localhost:11434/api/chat -d '{
  "model": "gemma3",
  "messages": [
    {
      "role": "user",
      "content": "Why is the sky blue?"
    }
  ]
}'
```

### 2. Simple Text Generation (cURL)

Generate a text response from a prompt:

```bash
curl http://localhost:11434/api/generate -d '{
  "model": "gemma3",
  "prompt": "Why is the sky blue?"
}'
```

### 3. Python Chat with OpenAI Library

Use Ollama with the OpenAI Python library:

```python
from openai import OpenAI

client = OpenAI(
    base_url='http://localhost:11434/v1/',
    api_key='ollama',  # required but ignored
)

chat_completion = client.chat.completions.create(
    messages=[
        {
            'role': 'user',
            'content': 'Say this is a test',
        }
    ],
    model='llama3.2',
)
```

### 4. Vision Model (Image Analysis)

Ask questions about images:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1/", api_key="ollama")

response = client.chat.completions.create(
    model="llava",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": "data:image/png;base64,iVBORw0KG...",
                },
            ],
        }
    ],
    max_tokens=300,
)
```

### 5. Generate Embeddings

Create vector embeddings for text:

```python
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

embeddings = client.embeddings.create(
    model="all-minilm",
    input=["why is the sky blue?", "why is the grass green?"],
)
```

### 6. Structured Outputs (JSON Schema)

Get structured JSON responses:

```python
from pydantic import BaseModel
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

class FriendInfo(BaseModel):
    name: str
    age: int
    is_available: bool

class FriendList(BaseModel):
    friends: list[FriendInfo]

completion = client.beta.chat.completions.parse(
    temperature=0,
    model="llama3.1:8b",
    messages=[
        {"role": "user", "content": "Return a list of friends in JSON format"}
    ],
    response_format=FriendList,
)
