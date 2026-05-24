---
name: rag-implementation
description: Unified RAG implementation skill covering the full pipeline from chunking and embedding to hybrid search, reranking, grounded generation with citations, and evaluation/monitoring.
summary: RAG architecture: chunking, embedding model selection, hybrid search (vector + keyword), reranking, grounded generation with citations, and retrieval quality evaluation.
triggers: [RAG, retrieval, embedding, vector, knowledge base, citation, grounding, hybrid search, reranking]
disable-model-invocation: true

---
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

splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=100,
    separators=["\n\n", "\n", ". ", " ", ""]
)
chunks = splitter.split_documents(documents)
```

### Parent-Document Retriever
Retrieve small chunks but pass larger context to LLM:
- **Small chunks** (256 tokens) for embedding/retrieval
- **Parent documents** (1024+ tokens) for generation context

## Retrieval Strategies

### Dense (Vector) Search
Semantic similarity via embeddings—good for conceptual matching.

### Sparse (BM25) Keyword Search
Exact keyword matching—good for names, codes, specific terms.

### Hybrid Search (Recommended)
Combine both for best recall:

```
Query → ┬─► Vector Search ──► Candidates ─┐
        │                                  │
        └─► Keyword Search ─► Candidates ─┴─► Fusion ─► Results
```

### Reciprocal Rank Fusion (RRF)
```python
from collections import defaultdict

def reciprocal_rank_fusion(
    vector_results: list,
    keyword_results: list,
    k: int = 60
) -> list:
    """Combine multiple ranked lists using RRF."""
    scores = defaultdict(float)
    
    for rank, (doc_id, _) in enumerate(vector_results):
        scores[doc_id] += 1.0 / (k + rank + 1)
    
    for rank, (doc_id, _) in enumerate(keyword_results):
        scores[doc_id] += 1.0 / (k + rank + 1)
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

### Multi-Query Retrieval
Generate multiple query variants to improve recall:
```python
query_variants = [
    original_query,
    rephrase_query(original_query),
    hypothetical_document_embedding(original_query)  # HyDE
]
```

## Vector Database Options

| Database      | Type        | Best For                    |
| ------------- | ----------- | --------------------------- |
| **pgvector**  | PostgreSQL  | Existing Postgres, hybrid   |
| **Chroma**    | Embedded    | Local dev, simple apps      |
| **Pinecone**  | Managed     | Production scale, no ops    |
| **Qdrant**    | Self-hosted | Full control, filtering     |
| **Weaviate**  | Managed/OSS | Hybrid search built-in      |

### pgvector Hybrid Search
```sql
-- Create table with vector and full-text search
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    embedding vector(1536),
    ts_content tsvector GENERATED ALWAYS AS (
        to_tsvector('english', content)
    ) STORED
);

-- Vector index (HNSW)
CREATE INDEX documents_embedding_idx 
ON documents USING hnsw (embedding vector_cosine_ops);

-- Full-text index (GIN)
CREATE INDEX documents_fts_idx 
ON documents USING gin (ts_content);
```

## Reranking

After initial retrieval, rerank top candidates for precision:

### Cross-Encoder Reranking
```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

def rerank(query: str, candidates: list, top_k: int = 5) -> list:
    pairs = [(query, doc.content) for doc in candidates]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, score in ranked[:top_k]]
```

### Cohere Rerank API
```python
import cohere

co = cohere.Client(api_key="...")
results = co.rerank(
    query=query,
    documents=[doc.content for doc in candidates],
    top_n=5,
    model="rerank-english-v3.0"
)
```

## Grounded Generation

### Grounding Rules
1. If the answer is not supported by retrieved context, say so
2. Prefer quoting/snippets + citations over vague claims
3. Include source identifiers (doc IDs, URLs, page/section)
4. Never fabricate facts not present in context

### Prompt Template for Citations
```
You are a helpful assistant. Answer the user's question based ONLY on the provided context.
If the context doesn't contain enough information, say "I don't have enough information to answer this."

IMPORTANT:
- Cite your sources using [Source N] format
- Only state facts that appear in the context
- Quote directly when appropriate

Context:
[Source 1]: {chunk_1}
[Source 2]: {chunk_2}
...

Question: {query}

Answer with citations:
```

## Evaluation Metrics

### Retrieval Quality
- **Precision@k**: Relevant docs in top-k / k
- **Recall@k**: Relevant docs in top-k / total relevant
- **MRR**: Mean Reciprocal Rank of first relevant result
- **NDCG**: Normalized Discounted Cumulative Gain

### Generation Quality
- **Faithfulness**: Does answer match retrieved context?
- **Answer Relevance**: Does answer address the question?
- **Context Relevance**: Is retrieved context useful?

### Evaluation Approach
```python
eval_dataset = [
    {"query": "What is X?", "relevant_docs": ["doc_1", "doc_3"], "expected_answer": "..."},
    ...
]

# Run retrieval evaluation
for item in eval_dataset:
    retrieved = retrieve(item["query"])
    precision = len(set(retrieved) & set(item["relevant_docs"])) / len(retrieved)
    recall = len(set(retrieved) & set(item["relevant_docs"])) / len(item["relevant_docs"])
```

### LLM-as-Judge Evaluation
Use LLM to evaluate faithfulness:
```python
def evaluate_faithfulness(context: str, answer: str) -> float:
    prompt = f"""
    Context: {context}
    Answer: {answer}
    
    Is the answer fully supported by the context? 
    Score from 0 (unsupported) to 1 (fully supported).
    """
    # Call LLM and parse score
```

## Implementation Checklist
- [ ] Embedding model selected (consider domain-specific)
- [ ] Chunking strategy defined (size, overlap)
- [ ] Vector database set up with appropriate indexes
- [ ] Hybrid search implemented (vector + keyword)
- [ ] Reranking enabled for precision
- [ ] Grounding prompt enforces citations
- [ ] Evaluation dataset created
- [ ] Retrieval metrics tracked
- [ ] Faithfulness evaluation in place
- [ ] Latency and cost monitoring enabled
