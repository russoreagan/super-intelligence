---
name: api-openai-api-reference
description: "Reference for OpenAI REST API: auth, endpoints, Responses API, Chat Completions, debugging headers, rate limits. Use when integrating OpenAI API, calling chat/completions or responses, debugging API errors, or when the user mentions OpenAI API, chat completions, embeddings, or OpenAI endpoints."
triggers: [OpenAI API, chat completions, responses API, embeddings, OpenAI endpoint]
disable-model-invocation: true
---

# OpenAI API Reference

Quick reference for the OpenAI REST API. Load when integrating OpenAI, calling chat/responses endpoints, or debugging API issues.

## TL;DR

- **Base URL:** `https://api.openai.com/v1`
- **Auth:** `Authorization: Bearer OPENAI_API_KEY`
- **REST version:** `2020-10-01`

## Authentication

```
Authorization: Bearer OPENAI_API_KEY
```

Multi-org/project headers: `OpenAI-Organization`, `OpenAI-Project`.

## Key Endpoints

| Use Case | Endpoint | Method |
|----------|----------|--------|
| Create response (recommended) | `/v1/responses` | POST |
| Create chat completion | `/v1/chat/completions` | POST |
| List models | `/v1/models` | GET |
| Embeddings | `/v1/embeddings` | POST |
| Images | `/v1/images/generations` | POST |
| Audio transcription | `/v1/audio/transcriptions` | POST |

## Responses API vs Chat Completions

- **Responses API** (`/v1/responses`): Recommended. Stateful, tools, function calling, web/file search.
- **Chat Completions** (`/v1/chat/completions`): Legacy. Message-based, widely used.

## Debugging Headers

- `x-request-id` — Log for support
- `X-Client-Request-Id` — Optional request header (your trace ID)
- `x-ratelimit-*` — Rate limit info
- `openai-processing-ms` — Latency

## Full Reference

For complete details (auth, params, response objects, backward compatibility), see:

- **Project:** `docs/reference/OPENAI_API_REFERENCE.md`
- **Official:** https://platform.openai.com/docs/api-reference/introduction
