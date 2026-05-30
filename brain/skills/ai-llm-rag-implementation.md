# RAG Implementation (Unified)

## Intent
Use when building retrieval-augmented LLM apps: doc Q&A, knowledge-grounded chat, research assistants, or "answer with citations".

## When to Use
- Building document Q&A systems
- Implementing knowledge-grounded chatbots
- Creating research assistants
- Adding citations to LLM responses
- Reducing hallucinations with context grounding

## Canonical RAG Architecture

```
Document → Chunking → Embedding → Vector DB
                                      ↓
Query → Embed Query → Retrieve → Rerank → Generate → Response
                         ↓
                   [+ Keyword Search (Hybrid)]
```

### Pipeline Steps
1. **Ingest**: Load documents + metadata
2. **Chunk**: Split into retrievable units
3. **Embed**: Compute embeddings for chunks
4. **Index**: Store in vector DB (optionally hybrid with BM25)
5. **Retrieve**: Top-k candidates + optional filters
6. **Rerank/compress**: Improve precision and reduce irrelevant context
7. **Generate**: Answer strictly from context; include citations
8. **Evaluate/monitor**: Measure faithfulness + retrieval quality; track latency

## Embedding Model Selection (2026)

| Model                      | Dimensions | Max Tokens | Best For                            |
| -------------------------- | ---------- | ---------- | ----------------------------------- |
| **voyage-3-large**         | 1024       | 32000      | Claude apps (Anthropic recommended) |
| **voyage-3**               | 1024       | 32000      | Claude apps, cost-effective         |
| **voyage-code-3**          | 1024       | 32000      | Code search                         |
| **voyage-finance-2**       | 1024       | 32000      | Financial documents                 |
| **voyage-law-2**           | 1024       | 32000      | Legal documents                     |
| **text-embedding-3-large** | 3072       | 8191       | OpenAI apps, high accuracy          |
| **text-embedding-3-small** | 1536       | 8191       | OpenAI apps, cost-effective         |
| **bge-large-en-v1.5**      | 1024       | 512        | Open source, local deployment       |

### Embedding with Voyage AI (Recommended for Claude)
```python
from langchain_voyageai import VoyageAIEmbeddings

embeddings = VoyageAIEmbeddings(
    model="voyage-3-large",
    voyage_api_key=os.environ.get("VOYAGE_API_KEY")
)

# Domain-specific models
code_embeddings = VoyageAIEmbeddings(model="voyage-code-3")
finance_embeddings = VoyageAIEmbeddings(model="voyage-finance-2")
```

### Embedding with OpenAI
```python
from openai import OpenAI

client = OpenAI()

def get_embedding(text: str, model: str = "text-embedding-3-small") -> list:
    response = client.embeddings.create(input=text, model=model)
    return response.data[0].embedding
```

## Chunking Strategies

### Chunk Size Guidelines
| Content Type    | Chunk Size    | Overlap  |
| --------------- | ------------- | -------- |
| Dense technical | 256-512 tokens| 50-100   |
| Narrative/prose | 512-1024 tokens| 100-200 |
| Code            | Function/class level | Context-aware |

### Recursive Character Splitter
```python
from langchain.text_splitter import RecursiveCharacterTextSplitter
